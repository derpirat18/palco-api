"""Tipos de boleto (TB): adulto, nino y adulto mayor.

El precio de adulto es el de siempre: `performance_seats.precio_final`. El de
nino y el de adulto mayor salen del precio de la zona en esa funcion
(`performance_prices.precio_nino` / `precio_mayor`), convertidos a precio al
publico igual que el de adulto. Una zona sin ese precio no vende ese tipo, y
una butaca con precio personalizado solo se vende como adulto: su precio es
una excepcion y no hay de donde sacar el de nino.

Lo usan la compra en linea y la taquilla, para que los dos cobren lo mismo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio
from app.models.catalog import Event, PerformancePrice
from app.services.venta import Iva

ADULTO = "adulto"
NINO = "nino"
MAYOR = "mayor"
TIPOS = (ADULTO, NINO, MAYOR)
NOMBRES = {ADULTO: "Adulto", NINO: "Niño", MAYOR: "Adulto mayor"}


class TipoNoDisponible(ErrorDeDominio):
    codigo = "tipo_no_disponible"
    http_status = 409


def nombre_con_edad(tipo: str, evento: Event) -> str:
    """ "Niño (hasta 12 años)": lo que lee el comprador al escoger."""
    if tipo == NINO and evento.edad_nino_hasta:
        return f"Niño (hasta {evento.edad_nino_hasta} años)"
    if tipo == MAYOR and evento.edad_mayor_desde:
        return f"Adulto mayor ({evento.edad_mayor_desde} años o más)"
    return NOMBRES[tipo]


ACCESO_GENERAL = "Acceso general"


def asiento_de(fila: str, numero: str, general: bool = False) -> str:
    """Lo que dice el boleto del lugar. Entrada general (EG): no hay lugar
    numerado; el cupo interno no se muestra."""
    return ACCESO_GENERAL if general else f"Fila {fila} · {numero}"


def asiento_con_tipo(asiento: str, tipo: str) -> str:
    """El tipo viaja dentro del asiento congelado: asi sale en el boleto, el
    correo, la lista de la puerta y el escaner sin tocar cada pantalla.
    Adulto no se escribe: es lo normal.
    """
    return asiento if tipo == ADULTO else f"{asiento} · {NOMBRES[tipo]}"


@dataclass(frozen=True)
class Tarifas:
    """Precio al publico de nino y adulto mayor por zona de una funcion."""

    por_zona: dict[int, dict[str, Decimal]]

    def de_zona(self, zone_id: int) -> dict[str, Decimal]:
        return self.por_zona.get(zone_id, {})

    def precio(
        self,
        tipo: str,
        zone_id: int,
        precio_final: Decimal,
        personalizado: bool,
        lugar: str,
    ) -> Decimal:
        """Lo que se cobra por una butaca segun su tipo."""
        if tipo == ADULTO:
            return precio_final
        if tipo not in TIPOS:
            raise TipoNoDisponible(
                "Ese tipo de boleto no existe.", {"tipo": tipo, "lugar": lugar}
            )
        precio = None if personalizado else self.de_zona(zone_id).get(tipo)
        if precio is None:
            raise TipoNoDisponible(
                f"{lugar} no tiene precio de {NOMBRES[tipo].lower()}. Escoge otro "
                "tipo de boleto para ese lugar.",
                {"tipo": tipo, "lugar": lugar, "motivo": "sin_precio"},
            )
        return precio


async def tarifas_de(db: AsyncSession, performance_id: int, iva: Iva) -> Tarifas:
    filas = (
        await db.execute(
            select(
                PerformancePrice.zone_id,
                PerformancePrice.precio_nino,
                PerformancePrice.precio_mayor,
            ).where(
                PerformancePrice.performance_id == performance_id,
                PerformancePrice.activo.is_(True),
            )
        )
    ).all()
    por_zona: dict[int, dict[str, Decimal]] = {}
    for zone_id, nino, mayor in filas:
        t: dict[str, Decimal] = {}
        if nino is not None:
            t[NINO] = iva.precio_al_publico(nino)
        if mayor is not None:
            t[MAYOR] = iva.precio_al_publico(mayor)
        if t:
            por_zona[zone_id] = t
    return Tarifas(por_zona)
