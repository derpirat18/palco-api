"""Correo recordatorio 24 h antes de la funcion (BOL10).

Lo que importa:

- Sale una sola vez, cuando faltan 24 h o menos, a quien compro y a cada
  acompanante con su propia liga; cada uno con solo sus lugares.
- No sale si se compro con menos de 24 h, si la funcion se cancelo o si ya
  no tiene boletos que puedan entrar.
- Lleva "Ver mis boletos" y "Como llegar".
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.core.config import settings
from app.models.catalog import Performance, Venue
from app.models.sales import Order, Transfer
from app.services.recordatorios import como_llegar
from tests.test_boletos import boletos_en_base, orden_pagada
from tests.test_checkout import PasarelaFalsa
from tests.test_correo import CarteroFalso, bandeja

pytestmark = pytest.mark.asyncio

PUB = "/api/v1/publico"


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def compra(db, clientes, hold_service, stripe_falso, monkeypatch):
    monkeypatch.setattr(settings, "FRONT_URL", "https://palco.mx")
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await bandeja(db, CarteroFalso()).una_vuelta()  # el correo de la compra
    # La compra se hizo hace dias; la funcion es en 20 h.
    await db.execute(
        update(Order)
        .where(Order.id == orden.id)
        .values(pagada_en=datetime.now(UTC) - timedelta(days=3))
    )
    await mover_funcion(db, orden.performance_id, horas=20)
    return orden


async def mover_funcion(db, pid, horas):
    funcion = await db.get(Performance, pid)
    funcion.inicia_en = datetime.now(UTC) + timedelta(hours=horas)
    await db.flush()


async def vuelta(db) -> list:
    cartero = CarteroFalso()
    await bandeja(db, cartero).una_vuelta()
    return cartero.enviados


async def test_sale_una_vez_con_su_liga_y_como_llegar(db, compra):
    (m,) = await vuelta(db)

    assert m.para == "ana@correo.mx"
    assert m.asunto.startswith("Ya casi: ")
    for parte in (m.html, m.texto):
        assert f"https://palco.mx/orden/{compra.uuid}" in parte
        assert "https://www.google.com/maps/search/" in parte
        assert "$" not in parte
    assert "¡Ya casi es tu función!" in m.html
    assert await vuelta(db) == [], "una sola vez"


async def test_comprado_con_menos_de_24_h_no_sale(db, compra):
    await db.execute(
        update(Order)
        .where(Order.id == compra.id)
        .values(pagada_en=datetime.now(UTC) - timedelta(hours=2))
    )
    assert await vuelta(db) == []


async def test_todavia_falta_mas_de_un_dia(db, compra):
    await mover_funcion(db, compra.performance_id, horas=30)
    assert await vuelta(db) == []
    await mover_funcion(db, compra.performance_id, horas=23)
    assert len(await vuelta(db)) == 1


async def test_funcion_cancelada_o_compra_reembolsada(db, compra):
    funcion = await db.get(Performance, compra.performance_id)
    funcion.estado = "cancelada"
    await db.flush()
    assert await vuelta(db) == []

    funcion.estado = "en_venta"
    await db.execute(
        update(Order).where(Order.id == compra.id).values(estado="reembolsada")
    )
    assert await vuelta(db) == []


async def test_el_acompanante_recibe_el_suyo(cliente_http, db, compra):
    b0, _ = await boletos_en_base(db, compra.id)
    r = await cliente_http.post(
        f"{PUB}/ordenes/{compra.uuid}/boletos/{b0.uuid}/pasar",
        json={"nombre": "Beto", "apellidos": "Ruiz", "email": "beto@correo.mx"},
    )
    assert r.status_code == 200, r.text
    # El pase y el recordatorio de quien compro (el pase es de ahorita: el
    # acompanante todavia no).
    primera = await vuelta(db)
    assert [m.asunto.split(" ")[0] for m in primera if m.para == "beto@correo.mx"] == [
        "Ana"
    ]
    await db.execute(
        update(Transfer).values(aceptada_en=datetime.now(UTC) - timedelta(days=2))
    )

    enviados = {
        m.para: m for m in primera + await vuelta(db) if m.asunto.startswith("Ya casi")
    }

    assert set(enviados) == {"ana@correo.mx", "beto@correo.mx"}
    pase = (await db.execute(Transfer.__table__.select())).first()
    beto, ana = enviados["beto@correo.mx"], enviados["ana@correo.mx"]
    assert f"https://palco.mx/pase/{pase.token}" in beto.html
    assert f"/orden/{compra.uuid}" not in beto.html
    assert "Ver mi boleto" in beto.html
    assert beto.html.count("Boleto general") == 1
    assert ana.html.count("Boleto general") == 1, "solo el que le queda"


async def test_como_llegar_por_coordenadas_o_por_direccion():
    v = Venue(nombre="Teatro de la Ciudad", direccion="Hidalgo 1", ciudad="Saltillo")
    assert como_llegar(v).endswith("query=Teatro+de+la+Ciudad%2C+Hidalgo+1%2C+Saltillo")
    v.lat, v.lng = 25.42, -101.0
    assert como_llegar(v).endswith("query=25.42%2C-101.0")


async def test_dos_boletos_al_mismo_acompanante_un_solo_correo(
    cliente_http, db, compra
):
    for b in await boletos_en_base(db, compra.id):
        r = await cliente_http.post(
            f"{PUB}/ordenes/{compra.uuid}/boletos/{b.uuid}/pasar",
            json={"nombre": "Beto", "apellidos": "Ruiz", "email": "beto@correo.mx"},
        )
        assert r.status_code == 200, r.text
    await db.execute(
        update(Transfer).values(aceptada_en=datetime.now(UTC) - timedelta(days=2))
    )

    recordatorios = [m for m in await vuelta(db) if m.asunto.startswith("Ya casi")]

    # A quien compro ya no le queda ninguno: solo le llega a Beto, con los 2.
    (m,) = recordatorios
    assert m.para == "beto@correo.mx"
    assert m.html.count("Boleto general") == 2
    assert "Ver mis boletos" in m.html
