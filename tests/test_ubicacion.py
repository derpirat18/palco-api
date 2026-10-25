"""Ubicacion del recinto (BOL9): direccion y punto en el mapa para "Como llegar".

Lo que importa:

- Solo PSD la cambia (los recintos son de todos).
- Las coordenadas se pegan como las copia Google Maps, o con una liga larga
  de Maps; si no se entienden, se dice como escribirlas.
- "Tus boletos" las trae para que el celular abra el punto exacto.
"""

import pytest

from app.schemas.catalog import leer_coordenadas
from tests.test_boletos import orden_pagada
from tests.test_catalog import API, auth, crear_recinto, token_de
from tests.test_checkout import PasarelaFalsa

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.mark.parametrize(
    ("texto", "punto"),
    [
        ("25.4232, -101.0053", (25.4232, -101.0053)),
        ("25.4232,-101.0053", (25.4232, -101.0053)),
        (
            "https://www.google.com/maps/place/Teatro/@25.4232117,-101.0053,17z/data=x",
            (25.423212, -101.0053),
        ),
        (
            "https://www.google.com/maps/search/?api=1&query=25.1%2C-100.2",
            (25.1, -100.2),
        ),
    ],
)
async def test_leer_coordenadas(texto, punto):
    assert leer_coordenadas(texto) == punto


async def test_solo_psd_y_con_mensaje_claro(cliente_http, usuarios):
    psd = auth(token_de(usuarios["admin"]))
    vid = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    url = f"{API}/venues/{vid}/ubicacion"

    r = await cliente_http.patch(
        url,
        json={"direccion": "Hidalgo 1, Centro", "coordenadas": "25.4232, -101.0053"},
        headers=psd,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["direccion"], r.json()["lat"], r.json()["lng"]) == (
        "Hidalgo 1, Centro",
        25.4232,
        -101.0053,
    )

    r = await cliente_http.patch(
        url, json={"coordenadas": "por el kiosko"}, headers=psd
    )
    assert r.status_code == 422
    assert "25.4232, -101.0053" in r.text
    r = await cliente_http.patch(
        url, json={"coordenadas": "125.1, -101.0"}, headers=psd
    )
    assert r.status_code == 422

    # Vacias: se quitan.
    r = await cliente_http.patch(
        url, json={"direccion": "Hidalgo 1", "coordenadas": ""}, headers=psd
    )
    assert (r.json()["lat"], r.json()["lng"]) == (None, None)

    org = auth(token_de(usuarios["org_a"]))
    r = await cliente_http.patch(url, json={"direccion": "x"}, headers=org)
    assert r.status_code == 403


async def test_tus_boletos_traen_el_punto(
    cliente_http, db, clientes, hold_service, stripe_falso
):
    from app.models.catalog import Event, Performance, Venue

    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    funcion = await db.get(Performance, orden.performance_id)
    evento = await db.get(Event, funcion.event_id)
    recinto = await db.get(Venue, evento.venue_id)
    recinto.lat, recinto.lng = 25.4232, -101.0053
    await db.flush()

    r = await cliente_http.get(f"{API}/ordenes/{orden.uuid}/boletos")

    assert (r.json()["recinto"]["lat"], r.json()["recinto"]["lng"]) == (
        25.4232,
        -101.0053,
    )
