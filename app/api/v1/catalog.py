"""Endpoints del catalogo.

Los routers son delgados: validan, delegan en el servicio y aplican
autorizacion. Ninguna regla de negocio vive aqui.
"""

import base64
import binascii
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.deps import (
    DB,
    UsuarioActual,
    requiere_rol,
    verificar_acceso_cliente,
    verificar_acceso_evento,
)
from app.core.errors import EVENTO_NO_EXISTE, FUNCION_NO_EXISTE, RecursoNoEncontrado
from app.core.security import PermisoInsuficiente
from app.models.acceso import AsignacionDeAcceso
from app.models.base import EstadoEvento, EstadoFuncion, RolUsuario
from app.models.catalog import Event, Performance, SeatMap, Venue, Zone
from app.schemas.catalog import (
    EventoActualizar,
    EventoCrear,
    EventoResponse,
    FuncionCrear,
    FuncionResponse,
    ImportarMapaRequest,
    LecturaDeMapaResponse,
    LeerArchivoDeMapaRequest,
    MapaDibujoResponse,
    PrecioResponse,
    PreciosRequest,
    SeatMapResponse,
    VenueCrear,
    VenueResponse,
    VenueUbicacion,
    ZonaResponse,
)
from app.services.catalog import CatalogService, ConflictoDeCatalogo, slugify
from app.services.catalogos import categoria_del_catalogo, ciudad_del_catalogo
from app.services.mapas_archivo import leer_archivo
from app.services.medios import (
    MENSAJE_DE_TAMANO,
    TAMANO_MAXIMO,
    ImagenInvalida,
    guardar_imagen_de_evento,
)
from app.services.seat_maps import (
    EspecificacionDeMapa,
    MapaGeneralSpec,
    MapaInvalido,
    calcular_viewport,
    generar_mapa,
    importar_mapa_csv,
    mapa_general,
)

router = APIRouter()

TIPOS_DE_IMAGEN = ("image/jpeg", "image/png", "image/webp")

solo_psd = Depends(requiere_rol(RolUsuario.ADMIN_PSD))
psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))


# --------------------------------------------------------------------------
# Recintos
# --------------------------------------------------------------------------
# Son infraestructura compartida: los administra PSD y cualquier
# organizador puede programar funciones en ellos.


@router.get("/venues", response_model=list[VenueResponse], tags=["catalogo"])
async def listar_recintos(
    db: DB,
    _: UsuarioActual,
    ciudad: str | None = Query(default=None),
) -> list[Venue]:
    consulta = select(Venue).order_by(Venue.ciudad, Venue.nombre)
    if ciudad:
        consulta = consulta.where(Venue.ciudad.ilike(f"%{ciudad}%"))
    return list((await db.scalars(consulta)).all())


@router.post(
    "/venues",
    response_model=VenueResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def crear_recinto(datos: VenueCrear, db: DB) -> Venue:
    campos = datos.model_dump(exclude={"coordenadas"})
    # GEO1: la ciudad y el estado como están en el catálogo.
    ciudad = await ciudad_del_catalogo(db, datos.pais, datos.estado_geo, datos.ciudad)
    campos.update(estado_geo=ciudad.estado, ciudad=ciudad.nombre)
    lat, lng = datos.coordenadas.split(",")
    recinto = Venue(**campos, lat=Decimal(lat), lng=Decimal(lng))
    db.add(recinto)
    await db.commit()
    await db.refresh(recinto)
    return recinto


@router.patch(
    "/venues/{venue_id}/ubicacion",
    response_model=VenueResponse,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def cambiar_ubicacion(venue_id: int, datos: VenueUbicacion, db: DB) -> Venue:
    """BOL9: direccion y punto en el mapa, para "Como llegar".

    Sin coordenadas, Google Maps busca por nombre, direccion y ciudad.
    """
    recinto = await db.get(Venue, venue_id)
    if recinto is None:
        raise RecursoNoEncontrado("No encontramos ese recinto.")
    recinto.direccion = datos.direccion or None
    if datos.coordenadas:
        lat, lng = datos.coordenadas.split(",")
        recinto.lat, recinto.lng = Decimal(lat), Decimal(lng)
    else:
        recinto.lat = recinto.lng = None
    await db.commit()
    await db.refresh(recinto)
    return recinto


# --------------------------------------------------------------------------
# Mapas de butacas
# --------------------------------------------------------------------------


@router.post(
    "/venues/{venue_id}/seat-maps/generar",
    response_model=SeatMapResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def generar_mapa_de_butacas(
    venue_id: int, spec: EspecificacionDeMapa, db: DB
) -> SeatMapResponse:
    """Genera el mapa a partir de bloques de filas y butacas.

    Cubre la mayoria de los recintos en minutos. Para formas que no
    encajan en rectangulos, usar la importacion por archivo.
    """
    servicio = CatalogService(db)
    mapa = generar_mapa(spec)
    seat_map = await servicio.guardar_mapa(venue_id, mapa)
    await db.commit()
    await db.refresh(seat_map)

    total, vendibles = await servicio.contar_butacas(seat_map.id)
    return SeatMapResponse(
        **{
            c: getattr(seat_map, c)
            for c in ("id", "venue_id", "nombre", "viewport", "activo")
        },
        total_butacas=total,
        total_vendibles=vendibles,
    )


@router.post(
    "/venues/{venue_id}/seat-maps/general",
    response_model=SeatMapResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def crear_mapa_general(
    venue_id: int, spec: MapaGeneralSpec, db: DB
) -> SeatMapResponse:
    """Entrada general (EG): zonas con cupo y sin lugares numerados."""
    servicio = CatalogService(db)
    seat_map = await servicio.guardar_mapa(venue_id, mapa_general(spec))
    await db.commit()
    await db.refresh(seat_map)
    total, vendibles = await servicio.contar_butacas(seat_map.id)
    return SeatMapResponse(
        **{
            c: getattr(seat_map, c)
            for c in ("id", "venue_id", "nombre", "viewport", "activo")
        },
        total_butacas=total,
        total_vendibles=vendibles,
    )


@router.post(
    "/venues/{venue_id}/seat-maps/importar",
    response_model=SeatMapResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def importar_mapa_de_butacas(
    venue_id: int, datos: ImportarMapaRequest, db: DB
) -> SeatMapResponse:
    """Carga un mapa desde CSV: zona, fila, numero, x, y, kill."""
    servicio = CatalogService(db)
    mapa = importar_mapa_csv(datos.nombre, datos.csv)
    if datos.ajustar_viewport:
        mapa.viewport = calcular_viewport(mapa)

    seat_map = await servicio.guardar_mapa(venue_id, mapa)
    await db.commit()
    await db.refresh(seat_map)

    total, vendibles = await servicio.contar_butacas(seat_map.id)
    return SeatMapResponse(
        **{
            c: getattr(seat_map, c)
            for c in ("id", "venue_id", "nombre", "viewport", "activo")
        },
        total_butacas=total,
        total_vendibles=vendibles,
    )


@router.get(
    "/venues/{venue_id}/seat-maps",
    response_model=list[SeatMapResponse],
    tags=["catalogo"],
)
async def listar_mapas(
    venue_id: int, db: DB, _: UsuarioActual
) -> list[SeatMapResponse]:
    servicio = CatalogService(db)
    mapas = list(
        (
            await db.scalars(
                select(SeatMap)
                .where(SeatMap.venue_id == venue_id)
                .order_by(SeatMap.nombre)
            )
        ).all()
    )
    salida = []
    for m in mapas:
        total, vendibles = await servicio.contar_butacas(m.id)
        salida.append(
            SeatMapResponse(
                **{
                    c: getattr(m, c)
                    for c in ("id", "venue_id", "nombre", "viewport", "activo")
                },
                total_butacas=total,
                total_vendibles=vendibles,
            )
        )
    return salida


@router.post(
    "/seat-maps/vista-previa",
    response_model=MapaDibujoResponse,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def vista_previa_de_mapa(spec: EspecificacionDeMapa) -> MapaDibujoResponse:
    """Lo que generaria `seat-maps/generar`, sin guardar nada.

    Usa el mismo generador: lo que se ve en el panel es exactamente lo que
    se guarda (curvas, nones y pares, butacas omitidas o en kill).
    """
    return MapaDibujoResponse.de(generar_mapa(spec))


@router.post(
    "/seat-maps/vista-previa-csv",
    response_model=MapaDibujoResponse,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def vista_previa_de_csv(datos: ImportarMapaRequest) -> MapaDibujoResponse:
    """Lo que cargaria `seat-maps/importar`, sin guardar nada."""
    mapa = importar_mapa_csv(datos.nombre, datos.csv)
    if datos.ajustar_viewport:
        mapa.viewport = calcular_viewport(mapa)
    return MapaDibujoResponse.de(mapa)


@router.post(
    "/seat-maps/leer-archivo",
    response_model=LecturaDeMapaResponse,
    dependencies=[solo_psd],
    tags=["catalogo"],
)
async def leer_archivo_de_mapa(
    datos: LeerArchivoDeMapaRequest,
) -> LecturaDeMapaResponse:
    """Butacas de un CSV, Excel o PDF, para revisarlas antes de importar.

    No guarda nada. Leer un PDF toma un par de segundos: corre en un hilo
    aparte para no frenar a los compradores mientras tanto.
    """
    try:
        contenido = base64.b64decode(datos.contenido_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MapaInvalido("No pudimos recibir el archivo. Vuelve a elegirlo.") from exc
    lectura = await run_in_threadpool(leer_archivo, datos.archivo, contenido)
    return LecturaDeMapaResponse(
        origen=lectura.origen,
        total=len(lectura.butacas),
        zonas=[{"nombre": n, "color": c, "butacas": k} for n, c, k in lectura.zonas()],
        butacas=[vars(b) for b in lectura.butacas],
        avisos=lectura.avisos,
    )


@router.get(
    "/seat-maps/{seat_map_id}/dibujo",
    response_model=MapaDibujoResponse,
    tags=["catalogo"],
)
async def dibujo_de_mapa(
    seat_map_id: int, db: DB, _: UsuarioActual
) -> MapaDibujoResponse:
    """Un mapa guardado, para revisarlo en el panel. Los recintos son
    compartidos: cualquier persona del personal los puede ver."""
    return MapaDibujoResponse.de(await CatalogService(db).leer_mapa(seat_map_id))


@router.get(
    "/seat-maps/{seat_map_id}/zonas",
    response_model=list[ZonaResponse],
    tags=["catalogo"],
)
async def listar_zonas(seat_map_id: int, db: DB, _: UsuarioActual) -> list[Zone]:
    return list(
        (
            await db.scalars(
                select(Zone).where(Zone.seat_map_id == seat_map_id).order_by(Zone.orden)
            )
        ).all()
    )


# --------------------------------------------------------------------------
# Eventos
# --------------------------------------------------------------------------


@router.get("/events", response_model=list[EventoResponse], tags=["catalogo"])
async def listar_eventos(db: DB, usuario: UsuarioActual) -> list[Event]:
    """Un organizador solo ve sus propios eventos; su personal de acceso,
    solo aquellos a los que lo asignaron."""
    consulta = select(Event).order_by(Event.id.desc())
    if usuario.rol != RolUsuario.ADMIN_PSD:
        consulta = consulta.where(Event.client_id == usuario.client_id)
    if usuario.rol == RolUsuario.ACCESO:
        consulta = consulta.where(
            Event.id.in_(
                select(AsignacionDeAcceso.event_id).where(
                    AsignacionDeAcceso.user_id == usuario.id
                )
            )
        )
    return list((await db.scalars(consulta)).all())


@router.post(
    "/events",
    response_model=EventoResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
)
async def crear_evento(datos: EventoCrear, db: DB, usuario: UsuarioActual) -> Event:
    # Un organizador no puede crear eventos a nombre de otro: su
    # client_id lo fija el servidor, no la peticion.
    if usuario.rol == RolUsuario.ADMIN_PSD:
        if datos.client_id is None:
            raise ConflictoDeCatalogo(
                "Como admin de PSD hay que indicar a qué organizador pertenece."
            )
        client_id = datos.client_id
    else:
        client_id = usuario.client_id

    if await db.get(Venue, datos.venue_id) is None:
        raise RecursoNoEncontrado("El recinto no existe.")

    servicio = CatalogService(db)
    slug = await servicio.slug_disponible(slugify(datos.nombre))

    evento = Event(
        client_id=client_id,
        venue_id=datos.venue_id,
        nombre=datos.nombre,
        slug=slug,
        # CAT1: solo una categoria aprobada del catalogo.
        categoria=await categoria_del_catalogo(db, datos.categoria),
        descripcion=datos.descripcion,
        imagen_url=datos.imagen_url,
        destacado=datos.destacado,
        iva_pct=datos.iva_pct,
        precios_incluyen_iva=datos.precios_incluyen_iva,
        edad_nino_hasta=datos.edad_nino_hasta,
        edad_mayor_desde=datos.edad_mayor_desde,
        estado=EstadoEvento.BORRADOR,
    )
    db.add(evento)
    await db.commit()
    await db.refresh(evento)
    return evento


@router.get("/events/{event_id}", response_model=EventoResponse, tags=["catalogo"])
async def obtener_evento(event_id: int, db: DB, usuario: UsuarioActual) -> Event:
    evento = await CatalogService(db).obtener_evento(event_id)
    # Se valida con el client_id real del recurso, nunca con uno que
    # venga en la peticion.
    await verificar_acceso_evento(
        db, usuario, evento.id, evento.client_id, no_encontrado=EVENTO_NO_EXISTE
    )
    return evento


@router.patch(
    "/events/{event_id}",
    response_model=EventoResponse,
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
)
async def actualizar_evento(
    event_id: int, datos: EventoActualizar, db: DB, usuario: UsuarioActual
) -> Event:
    servicio = CatalogService(db)
    evento = await servicio.obtener_evento(event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)

    cambios = datos.model_dump(exclude_unset=True)
    if "categoria" in cambios:
        # CAT1: solo una categoria aprobada del catalogo (o ninguna).
        cambios["categoria"] = await categoria_del_catalogo(db, cambios["categoria"])

    # El organizador sube y edita su evento; publicarlo o retirarlo lo hace
    # PSD despues de revisarlo (ADR 0021). Un precio o una fecha mal puestos
    # no llegan a la venta.
    if "estado" in cambios and usuario.rol != RolUsuario.ADMIN_PSD:
        raise PermisoInsuficiente(
            "La publicación del evento la hace PSD después de revisarlo. "
            "Avísanos cuando esté listo.",
            {"campos": ["estado"]},
        )

    if "estado" in cambios and cambios["estado"] not in {e.value for e in EstadoEvento}:
        raise ConflictoDeCatalogo(f"Estado inválido: {cambios['estado']}.")

    await servicio.verificar_cambio_de_iva(evento, cambios)

    # La direccion (URL1). Publicado, sus ligas ya circulan en anuncios y
    # mensajes: cambiarla la decide PSD, y la vieja sigue llevando al evento.
    direccion = cambios.pop("slug", None)
    if direccion is not None and slugify(direccion) != evento.slug:
        if (
            usuario.rol != RolUsuario.ADMIN_PSD
            and evento.estado != EstadoEvento.BORRADOR.value
        ):
            raise PermisoInsuficiente(
                "El evento ya está publicado y su dirección ya circula. "
                "Si necesitas cambiarla, pídeselo a PSD.",
                {"campos": ["slug"]},
            )
        await servicio.cambiar_direccion(evento, direccion, usuario.id)
    elif (
        direccion is None
        and "nombre" in cambios
        and cambios["nombre"] != evento.nombre
        and evento.estado == EstadoEvento.BORRADOR.value
        and servicio.es_slug_automatico(evento.slug, evento.nombre)
    ):
        # Mientras es borrador y nadie eligio la direccion, sigue al nombre.
        # Ya publicado, nunca: romperia las ligas que se compartieron.
        evento.slug = await servicio.slug_disponible(
            slugify(cambios["nombre"]), event_id=evento.id
        )

    for campo, valor in cambios.items():
        setattr(evento, campo, valor)

    await db.commit()
    await db.refresh(evento)
    return evento


@router.put(
    "/events/{event_id}/imagen",
    response_model=EventoResponse,
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                t: {"schema": {"type": "string", "format": "binary"}}
                for t in TIPOS_DE_IMAGEN
            },
        }
    },
)
async def subir_imagen_del_evento(
    event_id: int, request: Request, db: DB, usuario: UsuarioActual
) -> Event:
    """Sube la imagen del evento (F7). El cuerpo es el archivo tal cual
    (Content-Type image/jpeg, image/png o image/webp), hasta 4 MB.

    El servidor la revisa, la limpia y la reduce (app/services/medios.py).
    """
    servicio = CatalogService(db)
    evento = await servicio.obtener_evento(event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)

    tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if tipo not in TIPOS_DE_IMAGEN:
        raise ImagenInvalida("Sube una imagen JPG, PNG o WebP.")
    largo = request.headers.get("content-length", "")
    if largo.isdigit() and int(largo) > TAMANO_MAXIMO:
        raise ImagenInvalida(MENSAJE_DE_TAMANO)
    datos = bytearray()
    async for parte in request.stream():
        datos += parte
        if len(datos) > TAMANO_MAXIMO:
            raise ImagenInvalida(MENSAJE_DE_TAMANO)

    evento.imagen_url = await guardar_imagen_de_evento(
        evento.uuid, bytes(datos), str(request.base_url)
    )
    await db.commit()
    await db.refresh(evento)
    return evento


# --------------------------------------------------------------------------
# Funciones
# --------------------------------------------------------------------------


@router.get(
    "/events/{event_id}/performances",
    response_model=list[FuncionResponse],
    tags=["catalogo"],
)
async def listar_funciones(
    event_id: int, db: DB, usuario: UsuarioActual
) -> list[Performance]:
    evento = await CatalogService(db).obtener_evento(event_id)
    await verificar_acceso_evento(
        db, usuario, evento.id, evento.client_id, no_encontrado=EVENTO_NO_EXISTE
    )
    return list(
        (
            await db.scalars(
                select(Performance)
                .where(Performance.event_id == event_id)
                .order_by(Performance.inicia_en)
            )
        ).all()
    )


@router.post(
    "/events/{event_id}/performances",
    response_model=FuncionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
)
async def crear_funcion(
    event_id: int, datos: FuncionCrear, db: DB, usuario: UsuarioActual
) -> Performance:
    servicio = CatalogService(db)
    evento = await servicio.obtener_evento(event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)

    seat_map = await db.get(SeatMap, datos.seat_map_id)
    if seat_map is None:
        raise RecursoNoEncontrado("El mapa de butacas no existe.")
    if seat_map.venue_id != evento.venue_id:
        raise ConflictoDeCatalogo("El mapa pertenece a otro recinto, no al del evento.")

    funcion = Performance(
        event_id=event_id,
        seat_map_id=datos.seat_map_id,
        inicia_en=datos.inicia_en,
        apertura_puertas=datos.apertura_puertas,
        venta_abre_en=datos.venta_abre_en,
        venta_cierra_en=datos.venta_cierra_en,
        estado=EstadoFuncion.PROGRAMADA,
    )
    db.add(funcion)
    await db.commit()
    await db.refresh(funcion)
    return funcion


@router.get(
    "/performances/{performance_id}/precios",
    response_model=list[PrecioResponse],
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
)
async def consultar_precios(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> list[PrecioResponse]:
    """Precio base vigente de cada zona (sin IVA agregado: como se capturo)."""
    servicio = CatalogService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await servicio.obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)
    return [
        PrecioResponse.model_validate(p)
        for p in await servicio.precios_vigentes(performance_id)
    ]


@router.put(
    "/performances/{performance_id}/precios",
    response_model=list[PrecioResponse],
    dependencies=[psd_u_organizador],
    tags=["catalogo"],
)
async def fijar_precios(
    performance_id: int, datos: PreciosRequest, db: DB, usuario: UsuarioActual
) -> list[PrecioResponse]:
    servicio = CatalogService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await servicio.obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)

    # Los importes viajan como Decimal de extremo a extremo: convertirlos
    # a float aqui introduciria error de redondeo en el precio base.
    tipos = (
        {z: (t.nino, t.mayor) for z, t in datos.tipos.items()}
        if datos.tipos is not None
        else None
    )
    precios = await servicio.fijar_precios(performance_id, datos.precios, tipos)
    await db.commit()
    return [PrecioResponse.model_validate(p) for p in precios]
