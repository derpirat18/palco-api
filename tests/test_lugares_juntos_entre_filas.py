"""Lugares juntos cuando lo ya apartado esta en otra zona o fila (ADR 0020).

Caso real del sandbox (21 oct): A en Zona A y luego B en Zona B, uno por
uno. Al apartar la segunda, la revision solo miraba la fila de la nueva y
no veia la primera: las dos quedaban apartadas y el cobro las rechazaba
despues ("Tus lugares ya no estan juntos..."). Se tiene que rechazar al
apartar, con un mensaje que diga por que.
"""

import uuid

import pytest

from app.core.errors import LugaresSeparados
from app.services.apartados import ApartadoService
from app.services.holds import clave_butaca
from tests.test_apartados import fila, funcion_a_la_venta

pytestmark = pytest.mark.asyncio


@pytest.fixture
def api(cliente_http, hold_service):
    """Cliente HTTP con el Redis de pruebas en el endpoint."""
    from app.api.v1.apartados import get_hold_service
    from app.main import app

    app.dependency_overrides[get_hold_service] = lambda: hold_service
    return cliente_http


API = "/api/v1"
OTRA_FILA = (
    "En una compra, todos los lugares van en la misma zona y fila. Para "
    "lugares en otra zona o fila, termina esta compra y haz otra."
)


async def test_no_aparta_un_lugar_de_otra_zona(db, clientes, hold_service, redis_test):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)
    pid = esc["performance_id"]
    luneta, balcon = esc["seat_ids"][0], esc["seat_ids"][5]
    hold = uuid.uuid4().hex
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [luneta], hold)

    with pytest.raises(LugaresSeparados) as exc:
        await servicio.apartar(pid, [balcon], hold)

    assert exc.value.mensaje == OTRA_FILA
    assert (await fila(db, pid, balcon)).estado == "disponible"
    assert await redis_test.get(clave_butaca(pid, balcon)) is None
    # Lo que ya tenia sigue siendo suyo.
    assert (await fila(db, pid, luneta)).hold_id == hold


async def test_soltando_la_primera_si_puede_cambiar_de_zona(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)
    pid = esc["performance_id"]
    luneta, balcon = esc["seat_ids"][0], esc["seat_ids"][5]
    hold = uuid.uuid4().hex
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [luneta], hold)
    await servicio.liberar(pid, hold, [luneta])

    await servicio.apartar(pid, [balcon], hold)

    assert (await fila(db, pid, balcon)).hold_id == hold


async def test_otro_comprador_en_otra_zona_no_cuenta(db, clientes, hold_service):
    """Solo cuenta lo de este apartado, no lo de otras personas."""
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)
    pid = esc["performance_id"]
    luneta, balcon = esc["seat_ids"][0], esc["seat_ids"][5]
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [luneta], uuid.uuid4().hex)

    await servicio.apartar(pid, [balcon], uuid.uuid4().hex)


async def test_api_explica_que_es_otra_zona(api, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)
    pid = esc["performance_id"]
    luneta, balcon = esc["seat_ids"][0], esc["seat_ids"][5]
    hold = str(uuid.uuid4())

    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": hold, "seat_ids": [luneta]},
    )
    assert r.status_code < 300, r.text
    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": hold, "seat_ids": [balcon]},
    )

    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "lugares_separados"
    assert r.json()["error"]["mensaje"] == OTRA_FILA
