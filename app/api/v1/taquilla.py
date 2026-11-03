"""Taquilla (TQ). Ver app/services/taquilla.py para las reglas."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual
from app.core.errors import (
    FUNCION_NO_EXISTE,
    ErrorDeDominio,
    RecursoNoEncontrado,
)
from app.core.textos import Linea, LineaSegura, NombreDePersona, Parrafo
from app.models.base import RolUsuario
from app.models.catalog import Client, Event, Performance, SeatMap, Venue, Zone
from app.models.sales import Order
from app.models.taquilla import CERRADA, JEFE
from app.models.users import User
from app.schemas.cortesias import MapaDelPanelResponse
from app.services import promociones
from app.services.mapa_panel import MapaPanelService
from app.services.reenvio import pedir_reenvio, tapar_correo
from app.services.taquilla import (
    SinPermisoDeTaquilla,
    TaquillaService,
    evento_de_taquilla,
    formas_de_pago,
    pie_del_recibo,
    pie_por_omision,
    reserva_de,
)

router = APIRouter(prefix="/taquilla", tags=["taquilla"])


class ConfigInvalida(ErrorDeDominio):
    codigo = "configuracion_invalida"
    http_status = 422


# --------------------------------------------------------------------------
# Esquemas
# --------------------------------------------------------------------------


class FuncionTaquilla(BaseModel):
    id: int
    inicia_en: datetime


class EventoTaquilla(BaseModel):
    event_id: int
    nombre: str
    puesto: Literal["jefe", "cajero"]
    recinto: str
    ciudad: str
    zona_horaria: str


class PersonaTaquilla(BaseModel):
    user_id: int
    nombre: str
    email: str
    puesto: str


class TotalesResponse(BaseModel):
    ventas: int
    boletos: int
    efectivo: Decimal
    terminal: Decimal
    cortesias: int
    folios_entregados: int
    anuladas: int
    efectivo_esperado: Decimal
    folios_esperados: int | None
    devuelto_efectivo: Decimal = Decimal("0")
    devuelto_terminal: Decimal = Decimal("0")


class CajaResponse(BaseModel):
    uuid: UUID
    cajero_id: int
    cajero: str
    abierta_en: datetime
    fondo: Decimal
    medio: str | None
    folio_desde: int | None
    folio_hasta: int | None
    estado: str
    contada_en: datetime | None
    efectivo_contado: Decimal | None
    terminal_contado: Decimal | None
    folios_sobrantes: int | None
    cerrada_en: datetime | None
    nota_de_cierre: str | None
    # Solo para el jefe, o para el cajero cuando ya se cerro (corte a ciegas).
    totales: TotalesResponse | None = None


class VentaResumen(BaseModel):
    uuid: UUID
    folio: str
    vendida_en: datetime
    forma_pago: str
    total: Decimal
    lugares: list[str]
    correo: str
    folios: list[int]
    anulada: bool
    motivo_anulacion: str | None


class ZonaTaquilla(BaseModel):
    id: int
    nombre: str
    mapa: str


FormaDePago = Literal["efectivo", "terminal", "cortesia"]
Quien = Literal["psd", "organizador"]


class EventoDetalle(BaseModel):
    evento: EventoTaquilla
    funciones: list[FuncionTaquilla]
    # Configuracion. La comision solo la ven PSD y el organizador.
    taquilla_minutos_despues: int
    texto_legal_medio: str | None
    medio_lo_paga: str | None
    comision_pct: Decimal | None
    comision_en_linea: Decimal | None
    efectivo_lo_tiene: Quien
    terminal_de: Quien
    pie_recibo: str | None
    pie_por_omision: str
    formas_pago: list[FormaDePago]
    max_lugares: int
    reserva_cantidad: int
    reserva_zona_id: int | None
    tolerancia_corte: Decimal
    zonas: list[ZonaTaquilla]
    # Quien pregunta puede cambiar comision, efectivo y terminal (solo PSD).
    es_psd: bool
    personal: list[PersonaTaquilla]
    cajas: list[CajaResponse]
    # La caja sin cerrar de quien pregunta, si tiene.
    mi_caja: UUID | None


class CajaDetalle(BaseModel):
    caja: CajaResponse
    evento: EventoTaquilla
    funciones: list[FuncionTaquilla]
    ventas: list[VentaResumen]
    es_mia: bool
    # Lo que la caja necesita de la configuracion, tambien sin internet.
    formas_pago: list[FormaDePago]
    max_lugares: int
    pie: str
    reserva_cantidad: int
    reserva_zona_id: int | None
    zonas: list[ZonaTaquilla]


class ConfigRequest(BaseModel):
    taquilla_minutos_despues: int | None = Field(default=None, ge=0, le=600)
    texto_legal_medio: Parrafo | None = Field(default=None, max_length=600)
    medio_lo_paga: Quien | None = None
    # Solo PSD. comision_pct en null: la del organizador en linea.
    comision_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    efectivo_lo_tiene: Quien | None = None
    terminal_de: Quien | None = None
    # Organizador o PSD. pie_recibo en null: "Organiza: <organizador>".
    pie_recibo: Linea | None = Field(default=None, max_length=120)
    formas_pago: list[FormaDePago] | None = Field(
        default=None, min_length=1, max_length=3
    )
    max_lugares: int | None = Field(default=None, ge=1, le=50)
    reserva_cantidad: int | None = Field(default=None, ge=1, le=50)
    # null: la zona que menos se vende.
    reserva_zona_id: int | None = None
    tolerancia_corte: Decimal | None = Field(
        default=None, ge=0, le=100_000, decimal_places=2
    )


class PersonalRequest(BaseModel):
    puesto: Literal["cajero", "jefe"]


class AbrirCajaRequest(BaseModel):
    cajero_id: int
    fondo: Decimal = Field(ge=0, le=1_000_000, decimal_places=2)
    medio: Linea | None = Field(default=None, max_length=60)
    folio_desde: int | None = Field(default=None, ge=1, le=99_999_999)
    folio_hasta: int | None = Field(default=None, ge=1, le=99_999_999)


class ContarRequest(BaseModel):
    efectivo: Decimal = Field(ge=0, le=10_000_000, decimal_places=2)
    terminal: Decimal = Field(ge=0, le=10_000_000, decimal_places=2)
    folios_sobrantes: int | None = Field(default=None, ge=0, le=100_000)


class CerrarRequest(BaseModel):
    nota: Linea | None = Field(default=None, max_length=300)


class VenderRequest(BaseModel):
    performance_id: int
    seat_ids: list[int] = Field(min_length=1, max_length=50)
    email: EmailStr
    nombre: NombreDePersona = Field(min_length=1, max_length=120)
    apellidos: NombreDePersona | None = Field(default=None, max_length=120)
    telefono: Linea | None = Field(default=None, max_length=30)
    forma_pago: Literal["efectivo", "terminal", "cortesia"]
    recibido: Decimal | None = Field(
        default=None, ge=0, le=10_000_000, decimal_places=2
    )
    autorizacion: Linea | None = Field(default=None, max_length=40)
    motivo_cortesia: LineaSegura | None = Field(default=None, max_length=100)
    folios: list[int] = Field(default_factory=list, max_length=50)
    acepta_aviso: bool
    acepta_marketing: bool = False
    aviso_version: str = Field(min_length=1, max_length=20)
    # Tipos de boleto (TB) por lugar (seat_id); el que no viene es adulto.
    tipos: dict[int, Literal["adulto", "nino", "mayor"]] = Field(
        default_factory=dict, max_length=50
    )
    # Fase 3: la venta se hizo sin internet y se sube ahora; `vendida_en`
    # es la hora del equipo.
    sin_red: bool = False
    vendida_en: datetime | None = None
    # Sin red: la promocion que aplico el equipo (PR1). Con red la decide el
    # servidor y esto no se usa.
    promocion_id: int | None = None
    # La pone el equipo una vez por venta: si reintenta, no se cobra dos veces.
    clave: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LugarRecibo(BaseModel):
    zona: str
    asiento: str
    precio: Decimal


class ReciboResponse(BaseModel):
    folio: str
    uuid: UUID
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    lugares: list[LugarRecibo]
    # Promociones (PR1): cuanto se desconto y con cual.
    descuento: Decimal = Decimal("0")
    promocion: str | None = None
    total: Decimal
    iva: Decimal
    iva_pct: Decimal
    forma_pago: str
    recibido: Decimal | None
    cambio: Decimal | None
    autorizacion: str | None
    medio: str | None
    folios: list[int]
    correo: str
    correo_tapado: str
    cajero: str | None
    vendida_en: datetime
    anulada: bool
    # Para el comprobante de devolucion (3 nov).
    anulada_en: datetime | None = None
    motivo_anulacion: str | None = None
    devolucion: str | None = None
    devuelto: Decimal | None = None
    texto_legal: str | None
    pie: str


class MotivoRequest(BaseModel):
    motivo: LineaSegura = Field(min_length=5, max_length=200)
    # Con el cliente enfrente: "le devolvi su dinero" (no aplica a cortesias).
    devolucion_confirmada: bool = False


class CorreoRequest(BaseModel):
    email: EmailStr


class ReenvioResponse(BaseModel):
    enviado_a: str


# --------------------------------------------------------------------------
# Ayudantes
# --------------------------------------------------------------------------


def _evento(evento, puesto: str, recinto: Venue) -> EventoTaquilla:
    return EventoTaquilla(
        event_id=evento.id,
        nombre=evento.nombre,
        puesto=puesto,
        recinto=recinto.nombre,
        ciudad=recinto.ciudad,
        zona_horaria=recinto.zona_horaria,
    )


async def _caja(
    svc: TaquillaService, caja, cajero: User, ver_totales: bool
) -> CajaResponse:
    totales = None
    if ver_totales or caja.estado == CERRADA:
        totales = TotalesResponse(**(await svc.totales(caja)).__dict__)
    return CajaResponse(
        uuid=caja.uuid,
        cajero_id=caja.cajero_id,
        cajero=cajero.nombre,
        abierta_en=caja.abierta_en,
        fondo=caja.fondo,
        medio=caja.medio,
        folio_desde=caja.folio_desde,
        folio_hasta=caja.folio_hasta,
        estado=caja.estado,
        contada_en=caja.contada_en,
        efectivo_contado=caja.efectivo_contado,
        terminal_contado=caja.terminal_contado,
        folios_sobrantes=caja.folios_sobrantes,
        cerrada_en=caja.cerrada_en,
        nota_de_cierre=caja.nota_de_cierre,
        totales=totales,
    )


async def _zonas_del_evento(db, event_id: int) -> list[ZonaTaquilla]:
    """Las zonas de los mapas de sus funciones (para escoger la reserva)."""
    filas = (
        await db.execute(
            select(Zone.id, Zone.nombre, SeatMap.nombre)
            .join(SeatMap, SeatMap.id == Zone.seat_map_id)
            .where(
                Zone.seat_map_id.in_(
                    select(Performance.seat_map_id).where(
                        Performance.event_id == event_id
                    )
                )
            )
            .order_by(SeatMap.nombre, Zone.orden, Zone.id)
        )
    ).all()
    return [ZonaTaquilla(id=i, nombre=n, mapa=m) for i, n, m in filas]


def _solo_organizador(usuario: User) -> None:
    if usuario.rol not in (RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR):
        raise SinPermisoDeTaquilla("Esto lo configura el organizador o PSD.")


async def _recibo(svc, venta, caja, evento) -> ReciboResponse:
    datos = await svc.recibo(venta, caja, evento)
    return ReciboResponse(**datos, correo_tapado=tapar_correo(datos["correo"]))


# --------------------------------------------------------------------------
# Eventos, personal y configuracion
# --------------------------------------------------------------------------


@router.get("/eventos", response_model=list[EventoTaquilla])
async def eventos(db: DB, usuario: UsuarioActual) -> list[EventoTaquilla]:
    """Los eventos donde esta persona vende o supervisa taquilla."""
    return [
        _evento(e, puesto, recinto)
        for e, puesto, recinto in await TaquillaService(db).eventos_para(usuario)
    ]


@router.get("/eventos/{event_id}", response_model=EventoDetalle)
async def evento(event_id: int, db: DB, usuario: UsuarioActual) -> EventoDetalle:
    ev, puesto = await evento_de_taquilla(db, event_id, usuario)
    svc = TaquillaService(db)
    recinto = await db.get(Venue, ev.venue_id)
    cajas = await svc.cajas(ev, usuario, puesto)
    cliente = await db.get(Client, ev.client_id)
    ve_comision = usuario.rol in (RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR)
    mi_caja = next(
        (c.uuid for c, _ in cajas if c.cajero_id == usuario.id and c.estado != CERRADA),
        None,
    )
    return EventoDetalle(
        evento=_evento(ev, puesto, recinto),
        funciones=[
            FuncionTaquilla(id=f.id, inicia_en=f.inicia_en)
            for f in await svc.funciones_de(ev)
        ],
        taquilla_minutos_despues=ev.taquilla_minutos_despues,
        texto_legal_medio=ev.texto_legal_medio,
        medio_lo_paga=ev.medio_lo_paga,
        comision_pct=ev.taquilla_comision_pct if ve_comision else None,
        comision_en_linea=cliente.comision_pct if ve_comision else None,
        efectivo_lo_tiene=ev.taquilla_efectivo_lo_tiene,
        terminal_de=ev.taquilla_terminal_de,
        pie_recibo=ev.taquilla_pie_recibo,
        pie_por_omision=pie_por_omision(cliente),
        formas_pago=formas_de_pago(ev),
        max_lugares=ev.taquilla_max_lugares,
        reserva_cantidad=ev.taquilla_reserva_cantidad,
        reserva_zona_id=ev.taquilla_reserva_zona_id,
        tolerancia_corte=ev.taquilla_tolerancia_corte,
        zonas=await _zonas_del_evento(db, ev.id),
        es_psd=usuario.rol == RolUsuario.ADMIN_PSD,
        personal=[
            PersonaTaquilla(
                user_id=u.id, nombre=u.nombre, email=u.email, puesto=p.puesto
            )
            for p, u in await svc.personal(ev)
        ]
        if puesto == JEFE
        else [],
        cajas=[await _caja(svc, c, u, puesto == JEFE) for c, u in cajas],
        mi_caja=mi_caja,
    )


@router.patch("/eventos/{event_id}/config", response_model=EventoDetalle)
async def configurar(
    event_id: int, datos: ConfigRequest, db: DB, usuario: UsuarioActual
) -> EventoDetalle:
    _solo_organizador(usuario)
    ev, _ = await evento_de_taquilla(db, event_id, usuario, jefe=True)
    cambios = datos.model_dump(exclude_unset=True)
    if cambios.get("taquilla_minutos_despues") is not None:
        ev.taquilla_minutos_despues = cambios["taquilla_minutos_despues"]
    if "texto_legal_medio" in cambios:
        ev.texto_legal_medio = (cambios["texto_legal_medio"] or "").strip() or None
    if "medio_lo_paga" in cambios:
        ev.medio_lo_paga = cambios["medio_lo_paga"]
    solo_psd = {"comision_pct", "efectivo_lo_tiene", "terminal_de"} & set(cambios)
    if solo_psd and usuario.rol != RolUsuario.ADMIN_PSD:
        raise SinPermisoDeTaquilla(
            "La comisión, el efectivo y la terminal de la taquilla solo los cambia PSD."
        )
    if "comision_pct" in cambios:
        ev.taquilla_comision_pct = cambios["comision_pct"]
    for campo in ("efectivo_lo_tiene", "terminal_de"):
        if cambios.get(campo) is not None:
            setattr(ev, f"taquilla_{campo}", cambios[campo])
    if "pie_recibo" in cambios:
        ev.taquilla_pie_recibo = (cambios["pie_recibo"] or "").strip() or None
    if cambios.get("formas_pago") is not None:
        ev.taquilla_formas_pago = sorted(set(cambios["formas_pago"]))
    for campo in ("max_lugares", "reserva_cantidad", "tolerancia_corte"):
        if cambios.get(campo) is not None:
            setattr(ev, f"taquilla_{campo}", cambios[campo])
    if "reserva_zona_id" in cambios:
        zona = cambios["reserva_zona_id"]
        if zona is not None and zona not in {
            z.id for z in await _zonas_del_evento(db, ev.id)
        }:
            raise ConfigInvalida(
                "Esa zona no es de los mapas de este evento.",
                {"campo": "reserva_zona_id"},
            )
        ev.taquilla_reserva_zona_id = zona
    await db.commit()
    return await evento(event_id, db, usuario)


@router.get("/eventos/{event_id}/cuentas", response_model=list[PersonaTaquilla])
async def cuentas_disponibles(
    event_id: int, db: DB, usuario: UsuarioActual
) -> list[PersonaTaquilla]:
    """Las cuentas de personal del organizador que se pueden asignar."""
    _solo_organizador(usuario)
    ev, _ = await evento_de_taquilla(db, event_id, usuario, jefe=True)
    filas = (
        await db.scalars(
            select(User)
            .where(
                User.client_id == ev.client_id,
                User.rol == RolUsuario.ACCESO,
                User.activo.is_(True),
            )
            .order_by(User.nombre)
        )
    ).all()
    return [
        PersonaTaquilla(user_id=u.id, nombre=u.nombre, email=u.email, puesto="")
        for u in filas
    ]


@router.put(
    "/eventos/{event_id}/personal/{cuenta_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def asignar(
    event_id: int,
    cuenta_id: int,
    datos: PersonalRequest,
    db: DB,
    usuario: UsuarioActual,
) -> Response:
    _solo_organizador(usuario)
    ev, _ = await evento_de_taquilla(db, event_id, usuario, jefe=True)
    await TaquillaService(db).asignar(ev, cuenta_id, datos.puesto, usuario)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/eventos/{event_id}/personal/{cuenta_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def quitar(
    event_id: int, cuenta_id: int, db: DB, usuario: UsuarioActual
) -> Response:
    _solo_organizador(usuario)
    ev, _ = await evento_de_taquilla(db, event_id, usuario, jefe=True)
    await TaquillaService(db).quitar(ev, cuenta_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Cajas
# --------------------------------------------------------------------------


@router.post("/eventos/{event_id}/cajas", response_model=CajaResponse, status_code=201)
async def abrir_caja(
    event_id: int, datos: AbrirCajaRequest, db: DB, usuario: UsuarioActual
) -> CajaResponse:
    ev, _ = await evento_de_taquilla(db, event_id, usuario, jefe=True)
    svc = TaquillaService(db)
    caja = await svc.abrir(
        ev,
        datos.cajero_id,
        datos.fondo,
        usuario,
        medio=datos.medio,
        folio_desde=datos.folio_desde,
        folio_hasta=datos.folio_hasta,
    )
    await db.commit()
    return await _caja(svc, caja, await db.get(User, caja.cajero_id), True)


async def _detalle(svc, caja, ev, puesto, usuario, db) -> CajaDetalle:
    recinto = await db.get(Venue, ev.venue_id)
    ventas = [
        VentaResumen(
            uuid=v.uuid,
            folio=o.folio,
            vendida_en=v.vendida_en,
            forma_pago=v.forma_pago,
            total=v.total,
            lugares=[f"{p.zona_snap} {p.asiento_snap}" for p in partidas],
            correo=o.email_snap or "",
            folios=v.folios,
            anulada=v.anulada_en is not None,
            motivo_anulacion=v.motivo_anulacion,
        )
        for v, o, partidas in await svc.ventas_de(caja)
    ]
    return CajaDetalle(
        caja=await _caja(svc, caja, await db.get(User, caja.cajero_id), puesto == JEFE),
        evento=_evento(ev, puesto, recinto),
        funciones=[
            FuncionTaquilla(id=f.id, inicia_en=f.inicia_en)
            for f in await svc.funciones_de(ev)
        ],
        ventas=ventas,
        es_mia=caja.cajero_id == usuario.id,
        formas_pago=formas_de_pago(ev),
        max_lugares=ev.taquilla_max_lugares,
        pie=pie_del_recibo(ev, await db.get(Client, ev.client_id)),
        reserva_cantidad=ev.taquilla_reserva_cantidad,
        reserva_zona_id=ev.taquilla_reserva_zona_id,
        zonas=await _zonas_del_evento(db, ev.id),
    )


@router.get("/cajas/{caja_uuid}", response_model=CajaDetalle)
async def ver_caja(caja_uuid: UUID, db: DB, usuario: UsuarioActual) -> CajaDetalle:
    svc = TaquillaService(db)
    caja, ev, puesto = await svc.caja(caja_uuid, usuario)
    return await _detalle(svc, caja, ev, puesto, usuario, db)


@router.post("/cajas/{caja_uuid}/contar", response_model=CajaDetalle)
async def contar(
    caja_uuid: UUID, datos: ContarRequest, db: DB, usuario: UsuarioActual
) -> CajaDetalle:
    svc = TaquillaService(db)
    caja, ev, puesto = await svc.caja(caja_uuid, usuario)
    await svc.contar(
        caja, usuario, puesto, datos.efectivo, datos.terminal, datos.folios_sobrantes
    )
    await db.commit()
    return await _detalle(svc, caja, ev, puesto, usuario, db)


@router.post("/cajas/{caja_uuid}/cerrar", response_model=CajaDetalle)
async def cerrar(
    caja_uuid: UUID, datos: CerrarRequest, db: DB, usuario: UsuarioActual
) -> CajaDetalle:
    svc = TaquillaService(db)
    caja, ev, puesto = await svc.caja(caja_uuid, usuario)
    if puesto != JEFE:
        raise SinPermisoDeTaquilla("La caja la cierra el jefe de taquilla.")
    await svc.cerrar(caja, usuario, datos.nota)
    await db.commit()
    return await _detalle(svc, caja, ev, puesto, usuario, db)


@router.get("/cajas/{caja_uuid}/corte.xlsx")
async def corte_excel(caja_uuid: UUID, db: DB, usuario: UsuarioActual) -> Response:
    svc = TaquillaService(db)
    caja, ev, puesto = await svc.caja(caja_uuid, usuario)
    if puesto != JEFE and caja.estado != CERRADA:
        raise SinPermisoDeTaquilla(
            "El corte se descarga cuando el jefe cierra la caja."
        )
    nombre = f"corte-caja-{str(caja.uuid)[:8]}.xlsx"
    return Response(
        content=await svc.corte_excel(caja, ev),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# --------------------------------------------------------------------------
# Vender
# --------------------------------------------------------------------------


class PromocionDeTaquilla(BaseModel):
    """Lo que el equipo necesita para calcular la promocion (PR1)."""

    id: int
    nombre: str
    tipo: Literal["2x1", "porcentaje"]
    porcentaje: Decimal | None
    todos_los_tipos: bool
    # Zonas de la funcion donde aplica; None: todas.
    zonas: list[int] | None


class PromocionesDeFuncion(BaseModel):
    performance_id: int
    promociones: list[PromocionDeTaquilla]


class MapaDeTaquillaResponse(MapaDelPanelResponse):
    promociones: list[PromocionDeTaquilla] = Field(default_factory=list)


@router.get("/funciones/{performance_id}/mapa", response_model=MapaDeTaquillaResponse)
async def mapa(
    performance_id: int, db: DB, usuario: UsuarioActual, caja: UUID | None = None
) -> MapaDeTaquillaResponse:
    """El mapa con lo que la taquilla puede vender ahora (sin bloqueados)."""
    funcion = await db.get(Performance, performance_id)
    if funcion is None:
        raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
    try:
        await evento_de_taquilla(db, funcion.event_id, usuario)
    except RecursoNoEncontrado as exc:
        raise RecursoNoEncontrado(FUNCION_NO_EXISTE) from exc
    reserva = None
    if caja is not None:
        # Los lugares de la reserva de esa caja salen libres para ella.
        la_caja, _, _ = await TaquillaService(db).caja(caja, usuario)
        reserva = reserva_de(la_caja)
    base = MapaDelPanelResponse.model_validate(
        await MapaPanelService(db).de_funcion(
            funcion, con_bloqueadas=False, reserva=reserva
        )
    )
    evento = await db.get(Event, funcion.event_id)
    reglas = await promociones.reglas_de(db, funcion, evento, promociones.TAQUILLA)
    return MapaDeTaquillaResponse(
        **base.model_dump(),
        promociones=[PromocionDeTaquilla(**r.publica()) for r in reglas],
    )


# --------------------------------------------------------------------------
# Reserva para vender sin internet (fase 3)
# --------------------------------------------------------------------------


class ReservarRequest(BaseModel):
    performance_id: int
    # Sin cantidad ni zona: las de la configuracion. Solo el jefe escoge otras.
    cantidad: int | None = Field(default=None, ge=1, le=50)
    zone_id: int | None = None


class LugarReservado(BaseModel):
    performance_id: int
    inicia_en: datetime
    seat_id: int
    zona_id: int
    zona: str
    fila: str
    numero: str
    precio: Decimal
    precio_nino: Decimal | None
    precio_mayor: Decimal | None
    vence_en: datetime


class ReservaResponse(BaseModel):
    lugares: list[LugarReservado]
    # Para vender sin red el equipo necesita saber que folios ya salieron.
    folios_usados: list[int]
    # Las promociones de cada funcion de la reserva: sin red el equipo las
    # aplica con la misma regla.
    promociones: list[PromocionesDeFuncion] = Field(default_factory=list)


async def _reserva(svc: TaquillaService, caja) -> ReservaResponse:
    ventas = await svc.ventas_de(caja)
    usados = sorted({f for v, *_ in ventas if v.anulada_en is None for f in v.folios})
    lugares = [LugarReservado(**x) for x in await svc.reserva(caja)]
    return ReservaResponse(
        lugares=lugares,
        folios_usados=usados,
        promociones=[
            PromocionesDeFuncion(
                performance_id=pid,
                promociones=[PromocionDeTaquilla(**r.publica()) for r in reglas],
            )
            for pid, reglas in (
                await svc.promociones_de(caja, {x.performance_id for x in lugares})
            ).items()
        ],
    )


def _solo_su_caja(caja, usuario: User, puesto: str) -> None:
    if puesto != JEFE and caja.cajero_id != usuario.id:
        raise SinPermisoDeTaquilla("Solo el cajero de esta caja o el jefe.")


@router.get("/cajas/{caja_uuid}/reserva", response_model=ReservaResponse)
async def ver_reserva(caja_uuid: UUID, db: DB, usuario: UsuarioActual):
    svc = TaquillaService(db)
    caja, _, puesto = await svc.caja(caja_uuid, usuario)
    _solo_su_caja(caja, usuario, puesto)
    return await _reserva(svc, caja)


@router.post("/cajas/{caja_uuid}/reserva", response_model=ReservaResponse)
async def reservar(
    caja_uuid: UUID, datos: ReservarRequest, db: DB, usuario: UsuarioActual
):
    svc = TaquillaService(db)
    caja, _, puesto = await svc.caja(caja_uuid, usuario)
    _solo_su_caja(caja, usuario, puesto)
    escoge = puesto == JEFE
    await svc.reservar(
        caja,
        usuario,
        datos.performance_id,
        datos.cantidad if escoge else None,
        datos.zone_id if escoge else None,
    )
    await db.commit()
    return await _reserva(svc, caja)


@router.delete("/cajas/{caja_uuid}/reserva", response_model=ReservaResponse)
async def soltar_reserva(
    caja_uuid: UUID, db: DB, usuario: UsuarioActual, performance_id: int | None = None
):
    svc = TaquillaService(db)
    caja, _, puesto = await svc.caja(caja_uuid, usuario)
    _solo_su_caja(caja, usuario, puesto)
    await svc.soltar_reserva(caja, usuario, performance_id)
    await db.commit()
    return await _reserva(svc, caja)


@router.post(
    "/cajas/{caja_uuid}/ventas", response_model=ReciboResponse, status_code=201
)
async def vender(
    caja_uuid: UUID, datos: VenderRequest, db: DB, usuario: UsuarioActual
) -> ReciboResponse:
    svc = TaquillaService(db)
    caja, ev, _ = await svc.caja(caja_uuid, usuario)
    venta = await svc.vender(
        caja,
        usuario,
        datos.performance_id,
        datos.seat_ids,
        str(datos.email),
        datos.nombre,
        datos.forma_pago,
        datos.clave,
        datos.acepta_aviso,
        datos.aviso_version,
        apellidos=datos.apellidos,
        telefono=datos.telefono,
        recibido=datos.recibido,
        autorizacion=datos.autorizacion,
        motivo_cortesia=datos.motivo_cortesia,
        folios=datos.folios,
        acepta_marketing=datos.acepta_marketing,
        tipos=dict(datos.tipos),
        sin_red=datos.sin_red,
        vendida_en=datos.vendida_en,
        promocion_id=datos.promocion_id,
    )
    await db.commit()
    return await _recibo(svc, venta, caja, ev)


@router.get("/ventas/{venta_uuid}/recibo", response_model=ReciboResponse)
async def recibo(venta_uuid: UUID, db: DB, usuario: UsuarioActual) -> ReciboResponse:
    svc = TaquillaService(db)
    venta, caja, ev, _ = await svc.venta(venta_uuid, usuario)
    return await _recibo(svc, venta, caja, ev)


@router.post("/ventas/{venta_uuid}/anular", response_model=ReciboResponse)
async def anular(
    venta_uuid: UUID, datos: MotivoRequest, db: DB, usuario: UsuarioActual
) -> ReciboResponse:
    """Correccion por error del cajero (la hace el jefe)."""
    svc = TaquillaService(db)
    venta, caja, ev, puesto = await svc.venta(venta_uuid, usuario)
    if puesto != JEFE:
        raise SinPermisoDeTaquilla("Las correcciones las autoriza el jefe de taquilla.")
    await svc.anular(
        venta, caja, ev, usuario, datos.motivo, datos.devolucion_confirmada
    )
    await db.commit()
    return await _recibo(svc, venta, caja, ev)


@router.post("/ventas/{venta_uuid}/correo", response_model=ReenvioResponse)
async def corregir_correo(
    venta_uuid: UUID, datos: CorreoRequest, db: DB, usuario: UsuarioActual
) -> ReenvioResponse:
    """El cliente dicto mal su correo: el jefe lo corrige y se le reenvian."""
    svc = TaquillaService(db)
    venta, caja, ev, puesto = await svc.venta(venta_uuid, usuario)
    if puesto != JEFE:
        raise SinPermisoDeTaquilla("El correo lo corrige el jefe de taquilla.")
    orden = await svc.cambiar_correo(venta, caja, ev, str(datos.email))
    enviado = await pedir_reenvio(db, orden.uuid, actor_id=usuario.id)
    await db.commit()
    return ReenvioResponse(enviado_a=enviado.enviado_a)


@router.post("/ventas/{venta_uuid}/reenviar", response_model=ReenvioResponse)
async def reenviar(venta_uuid: UUID, db: DB, usuario: UsuarioActual) -> ReenvioResponse:
    svc = TaquillaService(db)
    venta, _, _, _ = await svc.venta(venta_uuid, usuario)
    orden = await db.get(Order, venta.order_id)
    enviado = await pedir_reenvio(db, orden.uuid, actor_id=usuario.id)
    await db.commit()
    return ReenvioResponse(enviado_a=enviado.enviado_a)
