"""Servicio de apartado de butacas.

Es la pieza critica del sistema. Toda la correccion de "nunca vender dos
veces la misma butaca" pasa por aqui, en tres capas:

  1. Redis apartado atomico   -> rapido, efimero, resuelve la carrera
  2. UPDATE condicional       -> estructural, una fila por butaca/funcion
  3. Indice unico parcial     -> ultima linea, en order_items

Si Redis se cae se pierden los apartados en vuelo, pero las capas 2 y 3
siguen impidiendo la doble venta. Redis es velocidad, Postgres es verdad.

Regla para quien toque este archivo: cualquier cambio exige correr
tests/test_holds_concurrency.py y que siga pasando.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import AsientoNoDisponible, LimiteDeAsientos
from app.core.redis import LuaScripts, redis_client


def clave_butaca(performance_id: int, seat_id: int) -> str:
    """Llave de Redis para una butaca en una funcion.

    El hash tag {func:<id>} agrupa todas las butacas de una funcion en el
    mismo slot de Redis Cluster. Sin esto los scripts Lua multi-llave
    fallan en cuanto se pase de una sola instancia.
    """
    return f"hold:{{func:{performance_id}}}:{seat_id}"


@dataclass(frozen=True)
class Apartado:
    hold_id: str
    performance_id: int
    seat_ids: tuple[int, ...]
    ttl_segundos: int


class HoldService:
    """Apartados de butacas sobre Redis."""

    def __init__(self, redis=redis_client) -> None:
        self._redis = redis

    async def apartar(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str | None = None,
        ttl: int | None = None,
    ) -> Apartado:
        """Aparta todas las butacas o ninguna.

        Levanta AsientoNoDisponible indicando cual fallo, para que el
        front pueda refrescar solo esa butaca en el mapa.
        """
        if not seat_ids:
            raise LimiteDeAsientos("Elige al menos un lugar.")

        if len(seat_ids) > settings.MAX_ASIENTOS_POR_ORDEN:
            raise LimiteDeAsientos(
                f"Puedes comprar hasta {settings.MAX_ASIENTOS_POR_ORDEN} boletos "
                "por compra.",
                {"solicitadas": len(seat_ids)},
            )

        if len(set(seat_ids)) != len(seat_ids):
            raise LimiteDeAsientos("Hay butacas repetidas en la solicitud.")

        hold_id = hold_id or uuid.uuid4().hex
        ttl = ttl or settings.HOLD_TTL_SEGUNDOS
        llaves = [clave_butaca(performance_id, s) for s in seat_ids]

        script = LuaScripts.get("hold_seats")
        resultado = int(await script(keys=llaves, args=[hold_id, ttl]))

        if resultado != 0:
            # El script devuelve el indice base 1 de la primera ocupada.
            seat_id = seat_ids[resultado - 1]
            raise AsientoNoDisponible(
                "Alguien más acaba de elegir ese lugar. Elige otro, por favor.",
                {"seat_id": seat_id, "performance_id": performance_id},
            )

        return Apartado(
            hold_id=hold_id,
            performance_id=performance_id,
            seat_ids=tuple(seat_ids),
            ttl_segundos=ttl,
        )

    async def liberar(
        self, performance_id: int, seat_ids: list[int], hold_id: str
    ) -> int:
        """Libera solo las butacas que siguen siendo de este hold."""
        if not seat_ids:
            return 0
        llaves = [clave_butaca(performance_id, s) for s in seat_ids]
        script = LuaScripts.get("release_seats")
        return int(await script(keys=llaves, args=[hold_id]))

    async def extender(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str,
        ttl: int | None = None,
    ) -> bool:
        """Extiende el apartado al entrar al checkout.

        Devuelve False si perdio alguna butaca: en ese caso hay que
        abortar el pago antes de cobrar.
        """
        if not seat_ids:
            return False
        ttl = ttl or settings.HOLD_TTL_CHECKOUT_SEGUNDOS
        llaves = [clave_butaca(performance_id, s) for s in seat_ids]
        script = LuaScripts.get("extend_hold")
        return int(await script(keys=llaves, args=[hold_id, ttl])) == 1

    async def es_titular(
        self, performance_id: int, seat_ids: list[int], hold_id: str
    ) -> bool:
        """Verifica propiedad sin modificar nada.

        Se llama justo antes de capturar el pago en Stripe.
        """
        if not seat_ids:
            return False
        llaves = [clave_butaca(performance_id, s) for s in seat_ids]
        valores = await self._redis.mget(llaves)
        return all(v == hold_id for v in valores)

    async def ttl_restante(self, performance_id: int, seat_id: int) -> int:
        """Segundos que le quedan al apartado. -2 si ya no existe."""
        return int(await self._redis.ttl(clave_butaca(performance_id, seat_id)))


hold_service = HoldService()
