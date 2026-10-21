"""La cartelera publica: que sale, con que datos y como se busca.

Igual que la pagina del evento, cada campo sale a internet: la prueba de
campos obliga a decidir a proposito cualquiera que se agregue.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.models.base import EstadoEvento, EstadoFuncion
from app.models.catalog import Client, Event, Performance, Venue
from app.models.inventory import PerformanceSeat
from app.services.cartelera import normalizar
from tests.test_apartados import funcion_a_la_venta

pytestmark = pytest.mark.asyncio

API = "/api/v1"


@pytest.fixture
def api(cliente_http, redis_test):
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    app.dependency_overrides[get_redis_publico] = lambda: redis_test
    return cliente_http


async def un_evento(
    db,
    client_id,
    *,
    nombre,
    ciudad="CDMX",
    pais="MX",
    recinto=None,
    categoria=None,
    destacado=False,
    dias=20,
) -> dict:
    """Evento publicado con una funcion en venta, en el recinto y fecha dados."""
    esc = await funcion_a_la_venta(db, client_id)
    evento = await db.get(Event, esc["event_id"])
    evento.nombre, evento.categoria, evento.destacado = nombre, categoria, destacado
    lugar = await db.get(Venue, evento.venue_id)
    lugar.ciudad, lugar.pais = ciudad, pais
    if recinto:
        lugar.nombre = recinto
    funcion = await db.get(Performance, esc["performance_id"])
    funcion.inicia_en = datetime.now(UTC) + timedelta(days=dias)
    await db.flush()
    esc["slug"] = evento.slug
    return esc


async def cartelera(api, **params):
    r = await api.get(f"{API}/publico/cartelera", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def slugs(cuerpo) -> list[str]:
    return [e["slug"] for e in cuerpo["eventos"]]


# --------------------------------------------------------------------------
# Lo que sale y lo que no
# --------------------------------------------------------------------------


async def test_campos_exactos_de_la_tarjeta(api, db, clientes):
    """Nada del organizador ni conteos de butacas."""
    await un_evento(
        db, clientes[0].id, nombre="Circo en Teatro", categoria="Familiares"
    )

    cuerpo = await cartelera(api)

    assert set(cuerpo) == {"eventos", "total", "filtros"}
    [tarjeta] = cuerpo["eventos"]
    assert set(tarjeta) == {
        "slug",
        "nombre",
        "categoria",
        "imagen_url",
        "destacado",
        "recinto",
        "proxima_funcion",
        "funciones",
        "estado_venta",
        "disponibilidad",
        "precio_desde",
    }
    assert set(tarjeta["recinto"]) == {
        "nombre",
        "ciudad",
        "estado_geo",
        "pais",
        "zona_horaria",
    }
    assert set(cuerpo["filtros"]) == {"paises", "ciudades", "categorias", "meses"}
    assert set(cuerpo["filtros"]["ciudades"][0]) == {"nombre", "pais", "eventos"}
    # Precio final sin cargos y disponibilidad en niveles, no en numeros.
    assert tarjeta["precio_desde"] == "400.00"
    assert tarjeta["disponibilidad"] == "disponible"
    assert tarjeta["estado_venta"] == "en_venta"
    assert tarjeta["funciones"] == 1


async def test_solo_lo_que_se_puede_ver(api, db, clientes):
    visible = await un_evento(db, clientes[0].id, nombre="Visible")
    borrador = await un_evento(db, clientes[0].id, nombre="Borrador")
    await db.execute(
        update(Event)
        .where(Event.id == borrador["event_id"])
        .values(estado=EstadoEvento.BORRADOR.value)
    )
    de_baja = await un_evento(db, clientes[1].id, nombre="De baja")
    await db.execute(
        update(Client).where(Client.id == clientes[1].id).values(activo=False)
    )
    pasado = await un_evento(db, clientes[0].id, nombre="Pasado", dias=-1)
    cancelado = await un_evento(db, clientes[0].id, nombre="Cancelado")
    await db.execute(
        update(Performance)
        .where(Performance.id == cancelado["performance_id"])
        .values(estado=EstadoFuncion.CANCELADA.value)
    )
    await db.flush()

    cuerpo = await cartelera(api)

    assert slugs(cuerpo) == [visible["slug"]]
    for oculto in (borrador, de_baja, pasado, cancelado):
        assert oculto["slug"] not in slugs(cuerpo)


async def test_agotado_sale_sin_precio(api, db, clientes):
    esc = await un_evento(db, clientes[0].id, nombre="Lleno")
    await db.execute(
        update(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == esc["performance_id"],
            PerformanceSeat.estado != "kill",
        )
        .values(estado="vendido")
    )
    await db.flush()

    [tarjeta] = (await cartelera(api))["eventos"]

    assert tarjeta["estado_venta"] == "agotada"
    assert tarjeta["disponibilidad"] == "agotado"
    assert tarjeta["precio_desde"] is None


# --------------------------------------------------------------------------
# Busqueda y filtros
# --------------------------------------------------------------------------


async def test_ordena_por_la_funcion_mas_proxima(api, db, clientes):
    lejos = await un_evento(db, clientes[0].id, nombre="Lejos", dias=40)
    cerca = await un_evento(db, clientes[0].id, nombre="Cerca", dias=5)

    assert slugs(await cartelera(api)) == [cerca["slug"], lejos["slug"]]


async def test_el_texto_ignora_acentos_y_mayusculas(api, db, clientes):
    circo = await un_evento(
        db,
        clientes[0].id,
        nombre="Circo Atayde en Teatro",
        ciudad="San Cristóbal de las Casas",
        recinto="Teatro Hermanos Domínguez",
    )
    await un_evento(db, clientes[0].id, nombre="Concierto", ciudad="Monterrey")

    for texto in ("cristobal", "CIRCO", "hermanos dominguez", "atayde cristóbal"):
        assert slugs(await cartelera(api, q=texto)) == [circo["slug"]], texto
    assert slugs(await cartelera(api, q="circo monterrey")) == []


async def test_filtros_por_pais_ciudad_categoria_y_destacados(api, db, clientes):
    mty = await un_evento(
        db,
        clientes[0].id,
        nombre="A",
        ciudad="Monterrey",
        categoria="Familiares",
        destacado=True,
    )
    await un_evento(db, clientes[0].id, nombre="B", ciudad="Xalapa", categoria="Teatro")
    bog = await un_evento(db, clientes[0].id, nombre="C", ciudad="Bogotá", pais="CO")

    assert slugs(await cartelera(api, ciudad="monterrey")) == [mty["slug"]]
    assert slugs(await cartelera(api, pais="CO")) == [bog["slug"]]
    assert slugs(await cartelera(api, categoria="familiares")) == [mty["slug"]]
    assert slugs(await cartelera(api, destacados=True)) == [mty["slug"]]


async def test_filtro_por_mes_en_la_hora_del_recinto(api, db, clientes):
    """En UTC ya es diciembre, pero en el recinto todavia es 30 de noviembre:
    la funcion se anuncia (y se filtra) en la hora del recinto."""
    esc = await un_evento(db, clientes[0].id, nombre="Fin de mes")
    funcion = await db.get(Performance, esc["performance_id"])
    # 30 nov 2099 a las 23:00 en CDMX = 1 dic 2099 05:00 UTC.
    funcion.inicia_en = datetime(2099, 12, 1, 5, 0, tzinfo=UTC)
    await db.flush()

    assert slugs(await cartelera(api, mes="2099-11")) == [esc["slug"]]
    assert slugs(await cartelera(api, mes="2099-12")) == []


async def test_los_filtros_salen_de_toda_la_cartelera(api, db, clientes):
    """Filtrar por una ciudad no borra las demas del selector."""
    await un_evento(db, clientes[0].id, nombre="A", ciudad="Monterrey")
    await un_evento(db, clientes[0].id, nombre="B", ciudad="Monterrey")
    await un_evento(db, clientes[0].id, nombre="C", ciudad="Xalapa")

    cuerpo = await cartelera(api, ciudad="Xalapa")

    assert cuerpo["total"] == 1
    assert {c["nombre"]: c["eventos"] for c in cuerpo["filtros"]["ciudades"]} == {
        "Monterrey": 2,
        "Xalapa": 1,
    }


async def test_limite_y_total(api, db, clientes):
    for i in range(3):
        await un_evento(db, clientes[0].id, nombre=f"E{i}", dias=10 + i)

    cuerpo = await cartelera(api, limite=2)

    assert len(cuerpo["eventos"]) == 2
    assert cuerpo["total"] == 3


async def test_parametros_invalidos_son_422(api):
    for params in (
        {"mes": "2026-13"},
        {"pais": "MEX"},
        {"limite": 0},
        {"orden": "precio"},
    ):
        r = await api.get(f"{API}/publico/cartelera", params=params)
        assert r.status_code == 422, params
        assert r.json()["error"]["codigo"] == "datos_invalidos"


# --------------------------------------------------------------------------
# Lo mas buscado
# --------------------------------------------------------------------------


async def test_lo_mas_buscado_por_visitas_a_la_pagina(api, db, clientes):
    poco = await un_evento(db, clientes[0].id, nombre="Poco", ciudad="Xalapa", dias=5)
    mucho = await un_evento(
        db, clientes[0].id, nombre="Mucho", ciudad="Monterrey", dias=30
    )
    for _ in range(3):
        assert (
            await api.get(f"{API}/publico/eventos/{mucho['slug']}")
        ).status_code == 200
    await api.get(f"{API}/publico/eventos/{poco['slug']}")

    populares = await cartelera(api, orden="populares")

    assert slugs(populares) == [mucho["slug"], poco["slug"]]
    # La ciudad mas buscada va primero en el selector.
    assert populares["filtros"]["ciudades"][0]["nombre"] == "Monterrey"
    # Por fecha no cambia.
    assert slugs(await cartelera(api)) == [poco["slug"], mucho["slug"]]


async def test_sin_redis_lo_mas_buscado_cae_a_fecha(api, db, clientes):
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    class RedisCaido:
        async def mget(self, *a, **k):
            from redis.exceptions import ConnectionError

            raise ConnectionError("sin redis")

    app.dependency_overrides[get_redis_publico] = lambda: RedisCaido()
    a = await un_evento(db, clientes[0].id, nombre="A", dias=5)
    b = await un_evento(db, clientes[0].id, nombre="B", dias=9)

    assert slugs(await cartelera(api, orden="populares")) == [a["slug"], b["slug"]]


async def test_normalizar():
    assert normalizar("  San  Cristóbal ") == "san cristobal"
    assert normalizar("ÑANDÚ") == "nandu"
    assert normalizar(None) == ""
