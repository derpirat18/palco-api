"""Reglas de venta que comparten la pagina publica, el apartado y el cobro.

Viven aqui para que "esta funcion se puede comprar" signifique lo mismo
en todas partes. Si la pagina publica dijera "en venta" con una regla y
el apartado rechazara con otra, el comprador veria un boton que no
funciona.

Lo que cuesta es el precio del boleto y nada mas: al comprador no se le
cobran cargos encima. PSD cobra su comision al organizador (ADR 0012).
Ese precio siempre incluye el IVA; como se calcula desde lo que captura
el organizador tambien vive aqui (`Iva`, ADR 0013).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.models.base import EstadoEvento, EstadoFuncion
from app.models.catalog import Client, Event, Performance

CENTAVO = Decimal("0.01")


AUN_NO_ABRE = "La venta de esta función todavía no abre."
# Lo que lee el comprador segun el estado de la funcion. Son reglas de
# negocio, no fallas: se dicen claro para que nadie piense que la
# plataforma fallo.
_POR_ESTADO = {
    EstadoFuncion.CANCELADA.value: (
        "Esta función fue cancelada. Si compraste boletos, te escribiremos "
        "con los pasos para tu reembolso."
    ),
    EstadoFuncion.AGOTADA.value: "Boletos agotados para esta función.",
    EstadoFuncion.REALIZADA.value: "Esta función ya se realizó.",
    EstadoFuncion.PROGRAMADA.value: AUN_NO_ABRE,
}


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
            _POR_ESTADO.get(funcion.estado, AUN_NO_ABRE),
            {"motivo": "estado", "estado": funcion.estado},
        )
    if not funcion.inventario_generado:
        return AUN_NO_ABRE, {"motivo": "sin_inventario"}
    if funcion.venta_abre_en is not None and ahora < funcion.venta_abre_en:
        # La fecha va en `abre_en`: el front la muestra a la hora del recinto.
        return AUN_NO_ABRE, {
            "motivo": "aun_no_abre",
            "abre_en": funcion.venta_abre_en.isoformat(),
        }
    cierra = funcion.venta_cierra_en or funcion.inicia_en
    if ahora >= cierra:
        return "La venta en línea para esta función ya cerró.", {
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


def al_centavo(monto: Decimal) -> Decimal:
    """Redondeo comercial (mitad hacia arriba), el que espera una factura."""
    return monto.quantize(CENTAVO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Iva:
    """Como trata el IVA un evento. Lo decide el organizador con su contador.

    `pct` es la tasa que el organizador escribe (16, 8, 0 si esta exento,
    o la que le indique su contador). `incluido` dice como capturo sus
    precios: con el IVA adentro, o sin el para que el sistema lo sume.

    El comprador siempre ve y paga el precio con IVA: la ley pide exhibir
    el total a pagar. Lo unico que cambia es de donde sale ese total.
    """

    pct: Decimal
    incluido: bool

    def precio_al_publico(self, capturado: Decimal) -> Decimal:
        """Lo que paga el comprador por un precio capturado."""
        if self.incluido:
            return capturado
        return al_centavo(capturado * (1 + self.pct / 100))

    def iva_contenido(self, precio_al_publico: Decimal) -> Decimal:
        """Cuanto de un precio al publico es IVA. Para el desglose.

        Se calcula igual en los dos modos: con "mas IVA" sale exacto lo que
        se sumo (850 a 16 % -> 986.00, de los que 136.00 son IVA).
        """
        if self.pct == 0:
            return al_centavo(Decimal("0"))
        return al_centavo(precio_al_publico - precio_al_publico / (1 + self.pct / 100))


def iva_de(evento: Event) -> Iva:
    return Iva(pct=evento.iva_pct, incluido=evento.precios_incluyen_iva)
