"""Cuenta del comprador (R1): entrar con un codigo al correo.

Lo que importa:

- Se entra con un codigo de 6 digitos que llega al correo: vence, aguanta
  5 intentos y sirve una sola vez. Hay tope de codigos por correo.
- Quien entra ve todas las compras hechas con su correo, tambien las de
  invitado, y los boletos que le pasaron. Nada de otro comprador.
- El token del comprador no abre el panel, y el del panel no abre la cuenta.
- Cambiar de correo se confirma con un codigo al correo nuevo; si el nuevo
  ya era de alguien que entro, las cuentas se juntan.
- "Salir de todos" cierra las demas sesiones.
"""

import re

import pytest
from sqlalchemy import select

from app.api.v1.cuenta import get_cartero_cuenta, get_redis_cuenta
from app.main import app
from app.models.sales import Consent, Customer, Order
from app.services.confirmacion import armar_confirmacion
from tests.test_boletos import orden_pagada
from tests.test_catalog import auth, token_de
from tests.test_checkout import PasarelaFalsa
from tests.test_correo import CarteroFalso, bandeja
from tests.test_pases import pasar

pytestmark = pytest.mark.asyncio

CTA = "/api/v1/publico/cuenta"


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
def cartero(redis_test):
    falso = CarteroFalso()
    app.dependency_overrides[get_cartero_cuenta] = lambda: falso
    app.dependency_overrides[get_redis_cuenta] = lambda: redis_test
    return falso


def ultimo_codigo(cartero, para: str) -> str:
    (m,) = [x for x in cartero.enviados if x.para == para][-1:]
    codigo = re.match(r"(\d{6}) ", m.asunto).group(1)
    assert codigo in m.texto and codigo in m.html
    return codigo


async def entrar(cliente_http, cartero, correo: str) -> dict:
    r = await cliente_http.post(f"{CTA}/codigo", json={"correo": correo})
    assert r.status_code == 202, r.text
    r = await cliente_http.post(
        f"{CTA}/entrar",
        json={"correo": correo, "codigo": ultimo_codigo(cartero, correo.lower())},
    )
    assert r.status_code == 200, r.text
    return auth(r.json()["token"])


@pytest.fixture
async def compra(db, clientes, hold_service, stripe_falso):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await bandeja(db, CarteroFalso()).una_vuelta()
    return orden


# --------------------------------------------------------------------------
# Entrar
# --------------------------------------------------------------------------


async def test_entrar_con_el_codigo_del_correo(cliente_http, cartero):
    r = await cliente_http.post(f"{CTA}/codigo", json={"correo": " Ana@Correo.MX "})
    assert r.status_code == 202
    assert r.json() == {"enviado_a": "a•••@correo.mx", "minutos": 10}
    codigo = ultimo_codigo(cartero, "ana@correo.mx")
    assert "No lo compartas" in cartero.enviados[-1].texto

    malo = "000000" if codigo != "000000" else "111111"
    r = await cliente_http.post(
        f"{CTA}/entrar", json={"correo": "ana@correo.mx", "codigo": malo}
    )
    assert r.status_code == 422
    assert r.json()["error"]["detalle"]["intentos"] == 4

    r = await cliente_http.post(
        f"{CTA}/entrar",
        json={"correo": "ANA@correo.mx", "codigo": f"{codigo[:3]} {codigo[3:]}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["expira_en_segundos"] == 30 * 24 * 3600
    h = auth(r.json()["token"])
    r = await cliente_http.get(CTA, headers=h)
    assert r.status_code == 200
    assert r.json()["correo"] == "ana@correo.mx"

    # Sirve una sola vez.
    r = await cliente_http.post(
        f"{CTA}/entrar", json={"correo": "ana@correo.mx", "codigo": codigo}
    )
    assert r.status_code == 422
    assert "venció" in r.json()["error"]["mensaje"]


async def test_cinco_intentos_y_hay_que_pedir_otro(cliente_http, cartero):
    await cliente_http.post(f"{CTA}/codigo", json={"correo": "ana@correo.mx"})
    codigo = ultimo_codigo(cartero, "ana@correo.mx")
    malo = "000000" if codigo != "000000" else "111111"
    for _ in range(5):
        r = await cliente_http.post(
            f"{CTA}/entrar", json={"correo": "ana@correo.mx", "codigo": malo}
        )
        assert r.status_code == 422
    r = await cliente_http.post(
        f"{CTA}/entrar", json={"correo": "ana@correo.mx", "codigo": codigo}
    )
    assert r.status_code == 422, "agotado, ni el bueno sirve"


async def test_tope_de_codigos_por_correo(cliente_http, cartero):
    for _ in range(3):
        r = await cliente_http.post(f"{CTA}/codigo", json={"correo": "ana@correo.mx"})
        assert r.status_code == 202
    r = await cliente_http.post(f"{CTA}/codigo", json={"correo": "ana@correo.mx"})
    assert r.status_code == 429
    assert len(cartero.enviados) == 3
    # Otro correo no se ve afectado.
    r = await cliente_http.post(f"{CTA}/codigo", json={"correo": "beto@correo.mx"})
    assert r.status_code == 202


async def test_sin_sesion_o_con_token_del_panel_no_entra(
    cliente_http, cartero, usuarios
):
    assert (await cliente_http.get(CTA)).status_code == 401
    panel = auth(token_de(usuarios["admin"]))
    assert (await cliente_http.get(CTA, headers=panel)).status_code == 401
    # Y al reves: el del comprador no abre el panel.
    h = await entrar(cliente_http, cartero, "ana@correo.mx")
    assert (await cliente_http.get("/api/v1/auth/me", headers=h)).status_code == 401


# --------------------------------------------------------------------------
# Compras y pases
# --------------------------------------------------------------------------


async def test_ve_sus_compras_de_invitado_y_nada_ajeno(cliente_http, cartero, compra):
    h = await entrar(cliente_http, cartero, "ana@correo.mx")
    r = await cliente_http.get(f"{CTA}/compras", headers=h)
    assert r.status_code == 200
    (c,) = r.json()["compras"]
    assert (c["folio"], c["uuid"], c["boletos"], c["estado"]) == (
        compra.folio,
        str(compra.uuid),
        2,
        "pagada",
    )
    assert c["evento"] and c["recinto"] and c["zona_horaria"]
    assert r.json()["pases"] == []

    otro = await entrar(cliente_http, cartero, "luis@correo.mx")
    r = await cliente_http.get(f"{CTA}/compras", headers=otro)
    assert r.json() == {"compras": [], "pases": []}


async def test_ve_los_boletos_que_le_pasaron(cliente_http, db, cartero, compra):
    r = await cliente_http.get(f"/api/v1/ordenes/{compra.uuid}/boletos")
    b0 = r.json()["boletos"][0]
    r = await pasar(cliente_http, compra.uuid, b0["uuid"])
    assert r.status_code == 200, r.text

    h = await entrar(cliente_http, cartero, "beto@correo.mx")
    r = await cliente_http.get(f"{CTA}/compras", headers=h)
    assert r.json()["compras"] == []
    (p,) = r.json()["pases"]
    assert p["boletos"] == 1 and p["de"] == "Ana"
    r = await cliente_http.get(f"/api/v1/publico/pases/{p['token']}")
    assert r.status_code == 200


async def test_el_correo_invita_a_entrar_hasta_que_entra(
    cliente_http, db, cartero, compra
):
    antes = await armar_confirmacion(db, compra.uuid)
    assert "Crear mi cuenta" in antes.html and "/entrar" in antes.html
    await entrar(cliente_http, cartero, "ana@correo.mx")
    despues = await armar_confirmacion(db, compra.uuid)
    assert "Crear mi cuenta" not in despues.html
    assert "/cuenta" in despues.html


# --------------------------------------------------------------------------
# Perfil
# --------------------------------------------------------------------------


async def test_mi_perfil(cliente_http, db, cartero):
    h = await entrar(cliente_http, cartero, "ana@correo.mx")
    r = await cliente_http.patch(
        CTA,
        json={
            "nombre": "Ana",
            "apellidos": "López Pérez",
            "telefono": "55 1234 5678",
            "acepta_marketing": True,
            "aviso_version": "2026-10",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "correo": "ana@correo.mx",
        "nombre": "Ana",
        "apellidos": "López Pérez",
        "telefono": "+525512345678",
        "acepta_marketing": True,
    }
    r = await cliente_http.patch(CTA, json={"acepta_marketing": False}, headers=h)
    assert r.json()["acepta_marketing"] is False
    c = await db.scalar(select(Customer).where(Customer.email == "ana@correo.mx"))
    origenes = (
        await db.scalars(
            select(Consent.origen)
            .where(Consent.customer_id == c.id)
            .order_by(Consent.id)
        )
    ).all()
    assert origenes == ["mi_cuenta", "mi_cuenta"]

    r = await cliente_http.patch(CTA, json={"telefono": "123"}, headers=h)
    assert r.status_code == 422
    r = await cliente_http.patch(CTA, json={"nombre": "<b>"}, headers=h)
    assert r.status_code == 422
    r = await cliente_http.patch(CTA, json={"telefono": ""}, headers=h)
    assert r.json()["telefono"] is None


async def test_salir_de_todos(cliente_http, cartero):
    vieja = await entrar(cliente_http, cartero, "ana@correo.mx")
    r = await cliente_http.post(f"{CTA}/salir-de-todos", headers=vieja)
    assert r.status_code == 200
    nueva = auth(r.json()["token"])
    assert (await cliente_http.get(CTA, headers=vieja)).status_code == 401
    assert (await cliente_http.get(CTA, headers=nueva)).status_code == 200


# --------------------------------------------------------------------------
# Cambiar correo
# --------------------------------------------------------------------------


async def test_cambiar_correo(cliente_http, db, cartero, compra):
    h = await entrar(cliente_http, cartero, "ana@correo.mx")
    r = await cliente_http.post(
        f"{CTA}/correo", json={"correo": "ana@correo.mx"}, headers=h
    )
    assert r.status_code == 409
    r = await cliente_http.post(
        f"{CTA}/correo", json={"correo": "Ana.Nueva@Correo.mx"}, headers=h
    )
    assert r.status_code == 202
    codigo = ultimo_codigo(cartero, "ana.nueva@correo.mx")
    assert "cambiar tu correo" in cartero.enviados[-1].asunto

    # El codigo es de esta sesion: no sirve para entrar.
    r = await cliente_http.post(
        f"{CTA}/entrar", json={"correo": "ana.nueva@correo.mx", "codigo": codigo}
    )
    assert r.status_code == 422

    r = await cliente_http.post(
        f"{CTA}/correo/confirmar",
        json={"correo": "ana.nueva@correo.mx", "codigo": codigo},
        headers=h,
    )
    assert r.status_code == 200, r.text
    nueva = auth(r.json()["token"])
    assert (await cliente_http.get(CTA, headers=h)).status_code == 401
    assert (await cliente_http.get(CTA, headers=nueva)).json()["correo"] == (
        "ana.nueva@correo.mx"
    )
    orden = await db.get(Order, compra.id, populate_existing=True)
    assert orden.email_snap == "ana.nueva@correo.mx"
    r = await cliente_http.get(f"{CTA}/compras", headers=nueva)
    assert len(r.json()["compras"]) == 1


async def test_cambiar_a_un_correo_con_cuenta_las_junta(
    cliente_http, db, cartero, compra
):
    luis = await entrar(cliente_http, cartero, "luis@correo.mx")
    await cliente_http.patch(CTA, json={"nombre": "Luis"}, headers=luis)
    ana = await entrar(cliente_http, cartero, "ana@correo.mx")
    ana_id = (
        await db.scalar(select(Customer).where(Customer.email == "ana@correo.mx"))
    ).id

    await cliente_http.post(
        f"{CTA}/correo", json={"correo": "luis@correo.mx"}, headers=ana
    )
    r = await cliente_http.post(
        f"{CTA}/correo/confirmar",
        json={
            "correo": "luis@correo.mx",
            "codigo": ultimo_codigo(cartero, "luis@correo.mx"),
        },
        headers=ana,
    )
    assert r.status_code == 200, r.text
    juntos = auth(r.json()["token"])
    perfil = (await cliente_http.get(CTA, headers=juntos)).json()
    assert (perfil["correo"], perfil["nombre"]) == ("luis@correo.mx", "Luis")
    r = await cliente_http.get(f"{CTA}/compras", headers=juntos)
    assert [c["folio"] for c in r.json()["compras"]] == [compra.folio]
    assert await db.get(Customer, ana_id, populate_existing=True) is None
    # Las dos sesiones de antes ya no sirven.
    assert (await cliente_http.get(CTA, headers=ana)).status_code == 401
    assert (await cliente_http.get(CTA, headers=luis)).status_code == 401


async def test_el_codigo_de_cambio_es_de_quien_lo_pidio(cliente_http, cartero):
    ana = await entrar(cliente_http, cartero, "ana@correo.mx")
    beto = await entrar(cliente_http, cartero, "beto@correo.mx")
    await cliente_http.post(
        f"{CTA}/correo", json={"correo": "nuevo@correo.mx"}, headers=ana
    )
    r = await cliente_http.post(
        f"{CTA}/correo/confirmar",
        json={
            "correo": "nuevo@correo.mx",
            "codigo": ultimo_codigo(cartero, "nuevo@correo.mx"),
        },
        headers=beto,
    )
    assert r.status_code == 422
