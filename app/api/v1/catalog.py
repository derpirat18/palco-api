"""Endpoints del catalogo.

Los routers son delgados: validan, delegan en el servicio y aplican
autorizacion. Ninguna regla de negocio vive aqui.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_cliente
from app.core.errors import EVENTO_NO_EXISTE, FUNCION_NO_EXISTE, RecursoNoEncontrado
from app.models.base import EstadoEvento, EstadoFuncion, RolUsuario
from app.models.catalog import Event, Performance, SeatMap, Venue, Zone
from app.schemas.catalog import (
    EventoActualizar,
    EventoCrear,
    EventoResponse,
    FuncionCrear,
    FuncionResponse,
    ImportarMapaRequest,
    PrecioResponse,
    PreciosRequest,
    SeatMapResponse,
    VenueCrear,
    VenueResponse,
    ZonaResponse,
)
from app.services.catalog import CatalogService, ConflictoDeCatalogo, slugify
from app.services.seat_maps import (
    EspecificacionDeMapa,
    calcular_viewport,
    generar_mapa,
    importar_mapa_csv,
)

router = APIRouter()

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
    recinto = Venue(**datos.model_dump())
    db.add(recinto)
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
    """Un organizador solo ve sus propios eventos."""
    consulta = select(Event).order_by(Event.id.desc())
    if usuario.rol != RolUsuario.ADMIN_PSD:
        consulta = consulta.where(Event.client_id == usuario.client_id)
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
                "Como admin de PSD hay que indicar a que organizador pertenece."
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
        categoria=datos.categoria,
        descripcion=datos.descripcion,
        imagen_url=datos.imagen_url,
        destacado=datos.destacado,
        cargo_servicio_pct=datos.cargo_servicio_pct,
        cargo_orden=datos.cargo_orden,
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
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)
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

    if "estado" in cambios and cambios["estado"] not in {e.value for e in EstadoEvento}:
        raise ConflictoDeCatalogo(f"Estado invalido: {cambios['estado']}.")

    if "nombre" in cambios and cambios["nombre"] != evento.nombre:
        evento.slug = await servicio.slug_disponible(
            slugify(cambios["nombre"]), event_id=evento.id
        )

    for campo, valor in cambios.items():
        setattr(evento, campo, valor)

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
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)
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
    precios = await servicio.fijar_precios(performance_id, datos.precios)
    await db.commit()
    return [PrecioResponse.model_validate(p) for p in precios]
