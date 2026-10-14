"""Reglas de venta que comparten la pagina publica, el apartado y el cobro.

Viven aqui para que "esta funcion se puede comprar" y "cuanto cuesta con
cargos" signifiquen lo mismo en todas partes. Si la pagina publica dijera
"en venta" con una regla y el apartado rechazara con otra, el comprador
veria un boton que no funciona.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.models.base import EstadoEvento, EstadoFuncion
from app.models.catalog import Client, Event, Performance

CENTAVO = Decimal("0.01")


def motivo_no_se_vende(
    funcion: Performance, ahora: datetime
) -> tuple[str, dict] | None:
    """La primera regla de venta que no se cumple, o None si se vende.

    El motivo viaja en la respuesta porque le sirve al comprador ("la
    venta abre el 1 de noviembre") y a quien opera la funcion. No revela
    nada: solo se evalua sobre eventos ya publicos.

    `ahora` debe venir de la base (`now()`), no del reloj del servidor.
    """
    if funcion.estado != EstadoFuncion.EN_VENTA.value:
        return (
            "Esta funcion no esta a la venta.",
            {"motivo": "estado", "estado": funcion.estado},
        )
    if not funcion.inventario_generado:
        return "Esta funcion todavia no tiene inventario.", {"motivo": "sin_inventario"}
    if funcion.venta_abre_en is not None and ahora < funcion.venta_abre_en:
        return "La venta de esta funcion todavia no abre.", {
            "motivo": "aun_no_abre",
            "abre_en": funcion.venta_abre_en.isoformat(),
        }
    cierra = funcion.venta_cierra_en or funcion.inicia_en
    if ahora >= cierra:
        return "La venta de esta funcion ya cerro.", {
            "motivo": "venta_cerrada",
            "cerro_en": cierra.isoformat(),
        }
    return None


def visible_al_publico(evento: Event, cliente: Client) -> bool:
    """Un evento en borrador o de un organizador dado de baja no existe
    para el comprador: ni se muestra ni se vende.

    Quien llama responde igual que si el evento no existiera, para no
    anunciar lo que no se ha lanzado ni a quien ya no opera.
    """
    return evento.estado == EstadoEvento.PUBLICADO.value and cliente.activo


@dataclass(frozen=True)
class Cargos:
    """Cargos al comprador vigentes para un evento.

    Son los de hoy: la orden congela los suyos al cobrar. Nunca se usan
    para recalcular algo ya vendido.
    """

    servicio_pct: Decimal
    por_orden: Decimal

    def con_cargo_de_servicio(self, precio: Decimal) -> Decimal:
        """Precio de una butaca mas el cargo por servicio, al centavo.

        Redondeo comercial (mitad hacia arriba), el que espera un
        comprador y el que usa una factura. El cargo por orden no entra:
        se cobra una vez por compra, no por butaca.
        """
        return (precio * (1 + self.servicio_pct / 100)).quantize(
            CENTAVO, rounding=ROUND_HALF_UP
        )


def cargos_de(evento: Event, cliente: Client) -> Cargos:
    """El evento puede tener cargos propios; si no, hereda los del cliente."""
    return Cargos(
        servicio_pct=(
            evento.cargo_servicio_pct
            if evento.cargo_servicio_pct is not None
            else cliente.cargo_servicio_pct
        ),
        por_orden=(
            evento.cargo_orden
            if evento.cargo_orden is not None
            else cliente.cargo_orden
        ),
    )
