"""Mapa publico de la funcion (F2).

Lo que importa:

- Lo que el mapa dice "libre" se puede apartar, y lo que dice "ocupada"
  no: misma regla que el apartado.
- "Ocupada" no dice por que (vendida, apartada, bloqueada, cortesia).
- El precio es el que se cobra.
- Solo de funciones publicas y vigentes; lo demas es 404.
- El cache no esconde lo del propio comprador ("tuya" siempre es fresco).
"""

import json
from datetime import timedelta

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, update

from app.models.catalog import Event, Performance
from app.models.inventory import PerformanceSeat
from app.services.apartados import ApartadoService
from tests.test_apartados import funcion_a_la_venta, nuevo_hold
from tests.test_catalog import API

pytestmark = pytest.mark.asyncio


@pytest.fixture
def api(cliente_http, hold_service, redis_test):
    from app.api.v1.apartados import get_hold_service
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[get_redis_publico] = lambda: redis_test
    return cliente_http


@pytest.fixture
async def esc(db, clientes):
    """Luneta A-1..A-5 ($800, la A-2 en kill) y Balcon A-1..A-5 ($400)."""
    return await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5, kill=(2,))


async def mapa(api, pid, **params):
    return await api.get(f"{API}/publico/funciones/{pid}/mapa", params=params)


async def poner(db, pid, seat_id, **valores):
    await db.execute(
        update(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == pid,
            PerformanceSeat.seat_id == seat_id,
        )
        .values(**valores)
    )
    await db.flush()


def estados(cuerpo) -> dict[int, str]:
    return {b["id"]: b["estado"] for b in cuerpo["butacas"]}


async def test_el_mapa_completo_con_precios(api, esc):
    r = await mapa(api, esc["performance_id"])

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"funcion", "viewport", "zonas", "butacas"}
    assert cuerpo["funcion"]["estado_venta"] == "en_venta"
    assert [(z["nombre"], z["precio_desde"]) for z in cuerpo["zonas"]] == [
        ("Luneta", "800.00"),
        ("Balcon", "400.00"),
    ]
    # 10 butacas en el mapa, una en kill: no existe para el comprador.
    assert len(cuerpo["butacas"]) == 9
    assert sorted(b["id"] for b in cuerpo["butacas"]) == sorted(esc["seat_ids"])
    assert set(cuerpo["butacas"][0]) == {
        "id",
        "zona_id",
        "fila",
        "numero",
        "x",
        "y",
        "estado",
        "precio",
    }
    assert all(b["estado"] == "libre" for b in cuerpo["butacas"])
    precios = {b["precio"] for b in cuerpo["butacas"]}
    assert precios == {"800.00", "400.00"}


async def test_ocupada_no_dice_por_que(api, db, esc, hold_service):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await poner(db, pid, s[0], estado="vendido")
    await poner(db, pid, s[1], estado="bloqueado")
    await poner(db, pid, s[2], estado="cortesia")
    await ApartadoService(db, hold_service).apartar(pid, [s[3]], nuevo_hold())
    # Un apartado vencido en Postgres vuelve a estar libre, igual que para
    # el apartado.
    await ApartadoService(db, hold_service).apartar(pid, [s[4]], nuevo_hold())
    await poner(db, pid, s[4], hold_expira_en=func.now() - timedelta(minutes=5))

    r = await mapa(api, pid)

    vistos = estados(r.json())
    assert [vistos[x] for x in s[:5]] == ["ocupada"] * 4 + ["libre"]
    texto = json.dumps(r.json())
    for interno in ("vendido", "bloqueado", "cortesia", "apartado", "hold"):
        assert interno not in texto


async def test_el_precio_es_el_que_se_cobra(api, db, esc):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await poner(db, pid, s[0], precio_final=650)

    cuerpo = (await mapa(api, pid)).json()

    precio = {b["id"]: b["precio"] for b in cuerpo["butacas"]}
    assert precio[s[0]] == "650.00"
    assert cuerpo["zonas"][0]["precio_desde"] == "650.00"


async def test_zona_sin_libres_no_tiene_desde(api, db, esc):
    pid = esc["performance_id"]
    luneta = esc["zonas"][0]
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid)
        .where(
            PerformanceSeat.seat_id.in_(
                [b for b in esc["seat_ids"][:4]]  # las 4 vendibles de luneta
            )
        )
        .values(estado="vendido")
    )

    cuerpo = (await mapa(api, pid)).json()

    zonas = {z["id"]: z["precio_desde"] for z in cuerpo["zonas"]}
    assert zonas[luneta] is None


async def test_lo_tuyo_se_marca_aunque_el_mapa_venga_del_cache(
    api, db, esc, hold_service
):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await mapa(api, pid)  # queda en cache con todo libre
    mio = nuevo_hold()
    await ApartadoService(db, hold_service).apartar(pid, [s[0], s[1]], mio)
    ajeno = nuevo_hold()
    await ApartadoService(db, hold_service).apartar(pid, [s[2]], ajeno)

    con_mio = estados((await mapa(api, pid, apartado=mio)).json())

    assert (con_mio[s[0]], con_mio[s[1]]) == ("tuya", "tuya")
    # El de otro sale del cache: hasta 2 s puede verse libre. Si alguien lo
    # elige en ese lapso, el apartado lo rechaza.
    assert con_mio[s[2]] in ("libre", "ocupada")


async def test_el_cache_dura_poco_y_luego_se_ve_lo_nuevo(api, db, esc, redis_test):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await mapa(api, pid)
    await poner(db, pid, s[0], estado="vendido")

    cacheado = estados((await mapa(api, pid)).json())
    await redis_test.delete(f"mapa:{pid}")  # lo que hacen los 2 s
    fresco = estados((await mapa(api, pid)).json())

    assert cacheado[s[0]] == "libre"
    assert fresco[s[0]] == "ocupada"
    assert 0 < await redis_test.pttl(f"mapa:{pid}") <= 2000


async def test_sin_redis_se_consulta_la_base(api, esc):
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    caido = aioredis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    app.dependency_overrides[get_redis_publico] = lambda: caido

    r = await mapa(api, esc["performance_id"])

    assert r.status_code == 200, r.text
    await caido.aclose()


@pytest.mark.parametrize("caso", ["borrador", "cancelada", "inexistente"])
async def test_lo_que_no_es_publico_no_existe(api, db, esc, caso):
    pid = esc["performance_id"]
    if caso == "borrador":
        await db.execute(
            update(Event).where(Event.id == esc["event_id"]).values(estado="borrador")
        )
    elif caso == "cancelada":
        await db.execute(
            update(Performance).where(Performance.id == pid).values(estado="cancelada")
        )
    else:
        pid = pid + 10_000

    r = await mapa(api, pid)

    assert r.status_code == 404
    assert r.json()["error"] == {
        "codigo": "no_encontrado",
        "mensaje": "No encontramos esta función.",
        "detalle": {},
    }


async def test_lo_libre_se_puede_apartar(api, db, esc):
    pid = esc["performance_id"]
    cuerpo = (await mapa(api, pid)).json()
    libre = next(b["id"] for b in cuerpo["butacas"] if b["estado"] == "libre")

    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": "4f1b2c3d-1111-4222-8333-944455556666", "seat_ids": [libre]},
    )

    assert r.status_code == 201, r.text
    assert r.json()["butacas"][0]["precio"] == next(
        b["precio"] for b in cuerpo["butacas"] if b["id"] == libre
    ), "el precio del mapa es el del apartado"
