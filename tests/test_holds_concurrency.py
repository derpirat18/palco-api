"""Pruebas de concurrencia del apartado de butacas.

Estas pruebas son la red de seguridad del sistema. Si alguna falla, hay
un camino por el que se puede vender dos veces la misma butaca.

NO se marcan como skip, NO se relajan y NO se borran. Si una empieza a
fallar, el bug esta en el codigo, no en la prueba.
"""

import asyncio
import uuid

import pytest

from app.core.errors import AsientoNoDisponible, LimiteDeAsientos
from app.services.holds import HoldService, clave_butaca

pytestmark = pytest.mark.asyncio

FUNCION = 9001


async def test_mil_peticiones_misma_butaca_solo_una_gana(hold_service: HoldService):
    """El escenario que define el sistema.

    Mil compradores simultaneos sobre la butaca K-14. Exactamente uno
    debe ganar. Ni cero (todos fallan), ni dos (doble venta).
    """
    seat_id = 1
    intentos = 1000

    async def intentar(i: int):
        try:
            return await hold_service.apartar(FUNCION, [seat_id], hold_id=f"hold-{i}")
        except AsientoNoDisponible:
            return None

    resultados = await asyncio.gather(*(intentar(i) for i in range(intentos)))
    ganadores = [r for r in resultados if r is not None]

    assert len(ganadores) == 1, (
        f"Ganaron {len(ganadores)} de {intentos}. "
        "Cualquier numero distinto de 1 es doble venta o bloqueo total."
    )


async def test_apartado_multiple_es_todo_o_nada(hold_service: HoldService):
    """Si una butaca del grupo esta ocupada, no se aparta ninguna.

    Un apartado parcial dejaria al comprador con 3 de 4 lugares y
    butacas bloqueadas que nadie pidio.
    """
    await hold_service.apartar(FUNCION, [20], hold_id="ocupante")

    with pytest.raises(AsientoNoDisponible) as exc:
        await hold_service.apartar(FUNCION, [18, 19, 20, 21], hold_id="segundo")

    assert exc.value.detalle["seat_id"] == 20

    # Las otras tres deben haber quedado libres.
    libre = await hold_service.apartar(FUNCION, [18, 19, 21], hold_id="tercero")
    assert libre.hold_id == "tercero"


async def test_liberar_no_destruye_apartado_ajeno(hold_service: HoldService):
    """El bug clasico: liberar a ciegas despues de que expiro el TTL.

    Si A pierde la butaca por expiracion y B la aparta, el DEL tardio de
    A no debe borrar el apartado de B.
    """
    seat_id = 30
    await hold_service.apartar(FUNCION, [seat_id], hold_id="hold-A", ttl=1)
    await asyncio.sleep(1.2)  # expira el de A

    await hold_service.apartar(FUNCION, [seat_id], hold_id="hold-B")

    liberadas = await hold_service.liberar(FUNCION, [seat_id], "hold-A")
    assert liberadas == 0, "A borro un apartado que ya no era suyo."

    assert await hold_service.es_titular(FUNCION, [seat_id], "hold-B")


async def test_extender_falla_si_perdio_una_butaca(hold_service: HoldService):
    """No se debe cobrar si el apartado ya no esta completo."""
    await hold_service.apartar(FUNCION, [40, 41], hold_id="hold-C", ttl=1)
    await asyncio.sleep(1.2)
    await hold_service.apartar(FUNCION, [41], hold_id="intruso")

    assert await hold_service.extender(FUNCION, [40, 41], "hold-C") is False


async def test_extender_renueva_el_ttl(hold_service: HoldService):
    await hold_service.apartar(FUNCION, [50], hold_id="hold-D", ttl=5)
    assert await hold_service.extender(FUNCION, [50], "hold-D", ttl=600)
    assert await hold_service.ttl_restante(FUNCION, 50) > 500


async def test_liberar_permite_revender(hold_service: HoldService):
    """Abandonar el carrito devuelve la butaca al inventario."""
    seat_id = 60
    ap = await hold_service.apartar(FUNCION, [seat_id])
    await hold_service.liberar(FUNCION, [seat_id], ap.hold_id)

    nuevo = await hold_service.apartar(FUNCION, [seat_id], hold_id="otro")
    assert nuevo.hold_id == "otro"


async def test_respeta_el_tope_por_orden(hold_service: HoldService):
    with pytest.raises(LimiteDeAsientos):
        await hold_service.apartar(FUNCION, list(range(100, 120)))


async def test_rechaza_butacas_repetidas(hold_service: HoldService):
    with pytest.raises(LimiteDeAsientos):
        await hold_service.apartar(FUNCION, [70, 70, 71])


async def test_funciones_distintas_no_se_estorban(hold_service: HoldService):
    """La butaca 80 de la funcion del sabado y la del domingo son
    inventarios independientes."""
    await hold_service.apartar(FUNCION, [80], hold_id="sabado")
    otra = await hold_service.apartar(FUNCION + 1, [80], hold_id="domingo")
    assert otra.hold_id == "domingo"


async def test_hash_tag_agrupa_por_funcion():
    """Sin el hash tag, los scripts Lua multi-llave fallan en Cluster."""
    a = clave_butaca(123, 1)
    b = clave_butaca(123, 2)
    assert "{func:123}" in a and "{func:123}" in b
    assert a != b


async def test_grupos_concurrentes_no_se_solapan(hold_service: HoldService):
    """Cien compradores pidiendo 4 butacas de un pool de 8.

    Como mucho dos pueden ganar, y ninguna butaca puede quedar en dos
    apartados distintos.
    """
    pool = [200, 201, 202, 203, 204, 205, 206, 207]

    async def intentar(i: int):
        try:
            grupo = pool[:4] if i % 2 == 0 else pool[4:]
            return await hold_service.apartar(
                FUNCION, grupo, hold_id=f"grupo-{uuid.uuid4().hex[:8]}"
            )
        except AsientoNoDisponible:
            return None

    resultados = await asyncio.gather(*(intentar(i) for i in range(100)))
    ganadores = [r for r in resultados if r is not None]

    assert len(ganadores) == 2
    tomadas = [s for g in ganadores for s in g.seat_ids]
    assert len(tomadas) == len(set(tomadas)), "Una butaca quedo en dos apartados."


async def test_reintento_del_mismo_comprador_no_falla(hold_service: HoldService):
    """Doble clic o reintento de red: el comprador ya tiene esas butacas."""
    await hold_service.apartar(FUNCION, [90, 91], hold_id="carrito-1")
    otra_vez = await hold_service.apartar(FUNCION, [90, 91], hold_id="carrito-1")

    assert otra_vez.hold_id == "carrito-1"
    assert await hold_service.es_titular(FUNCION, [90, 91], "carrito-1")


async def test_reintento_no_alarga_el_apartado(hold_service: HoldService):
    """Reintentar no puede servir para acaparar butacas sin limite."""
    await hold_service.apartar(FUNCION, [92], hold_id="carrito-2", ttl=5)
    await hold_service.apartar(FUNCION, [92], hold_id="carrito-2", ttl=600)

    assert await hold_service.ttl_restante(FUNCION, 92) <= 5


async def test_reintento_no_toca_butacas_ajenas(hold_service: HoldService):
    """Ser dueño de una butaca del grupo no da derecho a las demas."""
    await hold_service.apartar(FUNCION, [93], hold_id="ajeno")
    await hold_service.apartar(FUNCION, [94], hold_id="carrito-3")

    with pytest.raises(AsientoNoDisponible) as exc:
        await hold_service.apartar(FUNCION, [94, 93], hold_id="carrito-3")

    assert exc.value.detalle["seat_id"] == 93
    assert await hold_service.es_titular(FUNCION, [93], "ajeno")
    assert await hold_service.es_titular(FUNCION, [94], "carrito-3")
