"""La puerta sin internet (E3b).

Si se cae la red, el celular del personal sigue revisando boletos solo:

1. Con internet, el escaner baja el **paquete** de la funcion: cada boleto
   con su estado y, si puede entrar, la llave de su QR que cambia (BOL3).
2. Sin internet, el celular revisa el QR con esa llave y su reloj (corregido
   con la hora del servidor), marca el boleto como usado en el celular y
   guarda la lectura.
3. Al volver la red, sube las lecturas. El servidor las juzga otra vez, a la
   hora en que se hicieron (`EscanerService.escanear(momento=...)`): lo que
   cuenta es lo que diga el servidor.

Lo que no se puede evitar sin red: dos puertas sin internet dejando pasar el
mismo boleto. Al subir, la segunda sale como "duplicado" y el escaner lo
avisa ("entro dos veces"), para que el organizador sepa.

Cuidados del paquete, porque con las llaves se pueden fabricar QR de esa
funcion:
- Solo de una funcion, y solo para quien puede escanearla (asignacion).
- Solo dentro del horario de escaneo (E3c): fuera de el no se entrega.
- El celular lo borra al cerrar el horario y al salir de la sesion.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio
from app.models.base import EstadoBoleto, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Customer, Order, OrderItem, Scan, Ticket
from app.services.boletos import PASO, TOLERANCIA, llave_rotatoria
from app.services.escaner import horario_de_escaneo
from app.services.lista_de_acceso import titular_de

# El paquete se puede bajar desde un poco antes de abrir: el personal llega
# antes y prepara su celular con internet.
BAJAR_ANTES = timedelta(hours=2)
# Lecturas sin internet que se aceptan al subir: de un dia para atras.
VIGENCIA_DE_LECTURAS = timedelta(hours=36)


class PaqueteFueraDeHorario(ErrorDeDominio):
    codigo = "paquete_fuera_de_horario"
    http_status = 409


@dataclass(frozen=True)
class BoletoDelPaquete:
    # uuid del boleto en base64url (como va en el QR) para buscarlo rapido.
    u: str
    # Llave del QR que cambia; nula si el boleto no puede entrar.
    k: str | None
    estado: str  # valido, usado, cancelado
    motivo: str | None
    zona: str
    asiento: str
    titular: str
    folio: str
    cortesia: bool
    entro_en: datetime | None


@dataclass(frozen=True)
class Paquete:
    performance_id: int
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    escaneo_abre_en: datetime
    escaneo_cierra_en: datetime
    servidor_ahora: datetime
    paso_segundos: int
    tolerancia_pasos: int
    boletos: list[BoletoDelPaquete]


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _estado_en_la_puerta(
    boleto: Ticket, partida: OrderItem, orden: Order
) -> tuple[str, str | None]:
    """Lo mismo que decide la puerta en linea (EscanerService._decidir)."""
    if orden.cerrada_motivo == "cortesia_revocada":
        return "cancelado", "revocada"
    if orden.estado != EstadoOrden.PAGADA.value:
        return "cancelado", orden.estado
    if partida.anulado or boleto.estado == EstadoBoleto.CANCELADO.value:
        return "cancelado", "cancelada"
    if boleto.estado == EstadoBoleto.TRANSFERIDO.value:
        return "cancelado", "transferido"
    if boleto.estado == EstadoBoleto.USADO.value:
        return "usado", None
    return "valido", None


async def paquete_de_la_funcion(
    db: AsyncSession, funcion: Performance, evento: Event
) -> Paquete:
    ahora: datetime = await db.scalar(select(func.now()))
    abre, cierra = horario_de_escaneo(funcion)
    if not abre - BAJAR_ANTES <= ahora <= cierra:
        raise PaqueteFueraDeHorario(
            "La lista para escanear sin internet se baja el día de la función, "
            "desde dos horas antes de que abra el escaneo.",
            {"abre_en": abre.isoformat(), "cierra_en": cierra.isoformat()},
        )
    recinto = await db.get(Venue, evento.venue_id)
    filas = (
        await db.execute(
            select(Ticket, OrderItem, Order, Customer)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Ticket.holder_customer_id)
            .where(Order.performance_id == funcion.id)
            .order_by(OrderItem.id)
        )
    ).all()
    entradas = dict(
        (
            await db.execute(
                select(Scan.ticket_id, func.max(Scan.escaneado_en))
                .where(Scan.performance_id == funcion.id, Scan.resultado == "valido")
                .group_by(Scan.ticket_id)
            )
        ).all()
    )
    boletos = []
    for boleto, partida, orden, cliente in filas:
        estado, motivo = _estado_en_la_puerta(boleto, partida, orden)
        boletos.append(
            BoletoDelPaquete(
                u=_b64(boleto.uuid.bytes),
                k=(
                    _b64(llave_rotatoria(boleto.uuid, boleto.semilla))
                    if estado == "valido"
                    else None
                ),
                estado=estado,
                motivo=motivo,
                zona=partida.zona_snap,
                asiento=partida.asiento_snap,
                titular=titular_de(orden, boleto, cliente),
                folio=orden.folio,
                cortesia=orden.canal == "cortesia",
                entro_en=entradas.get(boleto.id) if estado == "usado" else None,
            )
        )
    return Paquete(
        performance_id=funcion.id,
        evento=evento.nombre,
        recinto=recinto.nombre,
        ciudad=recinto.ciudad,
        zona_horaria=recinto.zona_horaria,
        inicia_en=funcion.inicia_en,
        escaneo_abre_en=abre,
        escaneo_cierra_en=cierra,
        servidor_ahora=ahora,
        paso_segundos=PASO,
        tolerancia_pasos=TOLERANCIA,
        boletos=boletos,
    )


def momento_de_la_lectura(escaneado_en: datetime, ahora: datetime) -> datetime | None:
    """La hora con que se juzga una lectura sin internet, o None si no se acepta.

    El celular manda la hora corregida con la del servidor. Un poco en el
    futuro (relojes) se toma como ahora; de hace mas de un dia, no se acepta.
    """
    if escaneado_en.tzinfo is None:
        escaneado_en = escaneado_en.replace(tzinfo=UTC)
    if escaneado_en > ahora:
        return ahora
    if ahora - escaneado_en > VIGENCIA_DE_LECTURAS:
        return None
    return escaneado_en
