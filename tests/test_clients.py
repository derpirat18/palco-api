"""Pruebas de organizadores.

Lo delicado aqui no es el CRUD: es que un organizador no pueda editarse
su propia comision. Esa cifra es el acuerdo comercial con PSD y define
cuanto cobra PSD por cada boleto vendido.
"""

from decimal import Decimal

import pytest

from app.core.security import crear_access_token

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def token_de(usuario) -> str:
    return crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )


ALTA_MINIMA = {"nombre": "Circo Tres"}


# --------------------------------------------------------------------------
# Alta
# --------------------------------------------------------------------------


async def test_alta_aplica_condiciones_por_omision(cliente_http, usuarios):
    r = await cliente_http.post(
        f"{API}/clients", headers=auth(token_de(usuarios["admin"])), json=ALTA_MINIMA
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert Decimal(c["comision_pct"]) == Decimal("10.00")
    assert Decimal(c["cargo_servicio_pct"]) == Decimal("10.00")
    assert Decimal(c["cargo_orden"]) == Decimal("50.00")
    assert c["activo"] is True


async def test_alta_con_condiciones_propias(cliente_http, usuarios):
    r = await cliente_http.post(
        f"{API}/clients",
        headers=auth(token_de(usuarios["admin"])),
        json={
            "nombre": "Teatro Cuatro",
            "razon_social": "Teatro Cuatro S.A. de C.V.",
            "rfc": "tcu-123456-ab7",
            "email": "  Contacto@Teatro4.MX ",
            "clabe": "0021 8004 5678 9012 34",
            "comision_pct": "7.50",
            "cargo_orden": "35",
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert Decimal(c["comision_pct"]) == Decimal("7.50")
    assert Decimal(c["cargo_orden"]) == Decimal("35.00")
    # Los identificadores se normalizan al guardarlos.
    assert c["rfc"] == "TCU123456AB7"
    assert c["email"] == "contacto@teatro4.mx"
    assert c["clabe"] == "002180045678901234"


async def test_organizador_no_da_de_alta_organizadores(cliente_http, usuarios):
    r = await cliente_http.post(
        f"{API}/clients", headers=auth(token_de(usuarios["org_a"])), json=ALTA_MINIMA
    )
    assert r.status_code == 403


async def test_nombre_repetido_avisa(cliente_http, usuarios):
    tk = auth(token_de(usuarios["admin"]))
    await cliente_http.post(f"{API}/clients", headers=tk, json=ALTA_MINIMA)
    r = await cliente_http.post(
        f"{API}/clients", headers=tk, json={"nombre": "  circo tres  "}
    )
    assert r.status_code == 409
    assert "client_id" in r.json()["error"]["detalle"]


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("rfc", "CORTO"),
        ("clabe", "123"),
        ("clabe", "00218004567890123X"),
        ("comision_pct", "150"),
        ("comision_pct", "-1"),
        ("comision_pct", "10.555"),
        ("cargo_orden", "-5"),
        ("nombre", "X"),
    ],
)
async def test_datos_invalidos_se_rechazan(cliente_http, usuarios, campo, valor):
    r = await cliente_http.post(
        f"{API}/clients",
        headers=auth(token_de(usuarios["admin"])),
        json={**ALTA_MINIMA, campo: valor},
    )
    assert r.status_code == 422, f"{campo}={valor} paso la validacion"


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------


async def test_psd_ve_todos_el_organizador_solo_a_si_mismo(
    cliente_http, usuarios, clientes
):
    cliente_a, _ = clientes

    todos = await cliente_http.get(
        f"{API}/clients", headers=auth(token_de(usuarios["admin"]))
    )
    assert {c["nombre"] for c in todos.json()} == {"Circo Uno", "Teatro Dos"}

    propio = await cliente_http.get(
        f"{API}/clients", headers=auth(token_de(usuarios["org_a"]))
    )
    assert [c["id"] for c in propio.json()] == [cliente_a.id]


async def test_organizador_no_lee_a_otro(cliente_http, usuarios, clientes):
    _, cliente_b = clientes
    r = await cliente_http.get(
        f"{API}/clients/{cliente_b.id}", headers=auth(token_de(usuarios["org_a"]))
    )
    assert r.status_code == 403


async def test_inactivos_fuera_de_la_lista_por_omision(cliente_http, usuarios, db):
    from app.models.catalog import Client

    db.add(Client(nombre="Circo Retirado", activo=False))
    await db.flush()

    tk = auth(token_de(usuarios["admin"]))
    normal = await cliente_http.get(f"{API}/clients", headers=tk)
    assert "Circo Retirado" not in {c["nombre"] for c in normal.json()}

    con_bajas = await cliente_http.get(
        f"{API}/clients?incluir_inactivos=true", headers=tk
    )
    assert "Circo Retirado" in {c["nombre"] for c in con_bajas.json()}


# --------------------------------------------------------------------------
# Edicion: la parte que importa
# --------------------------------------------------------------------------


async def test_organizador_no_edita_su_comision(cliente_http, usuarios, clientes):
    """La garantia de este modulo.

    Si pudiera, PSD dejaria de cobrar lo acordado sin enterarse.
    """
    cliente_a, _ = clientes
    r = await cliente_http.patch(
        f"{API}/clients/{cliente_a.id}",
        headers=auth(token_de(usuarios["org_a"])),
        json={"comision_pct": "0"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["detalle"]["campos"] == ["comision_pct"]


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("cargo_servicio_pct", "0"),
        ("cargo_orden", "0"),
        ("activo", False),
        ("nombre", "Nombre Secuestrado"),
    ],
)
async def test_el_organizador_no_toca_lo_de_psd(
    cliente_http, usuarios, clientes, campo, valor
):
    cliente_a, _ = clientes
    r = await cliente_http.patch(
        f"{API}/clients/{cliente_a.id}",
        headers=auth(token_de(usuarios["org_a"])),
        json={campo: valor},
    )
    assert r.status_code == 403
    assert r.json()["error"]["detalle"]["campos"] == [campo]


async def test_organizador_si_mantiene_sus_datos_de_contacto(
    cliente_http, usuarios, clientes
):
    cliente_a, _ = clientes
    r = await cliente_http.patch(
        f"{API}/clients/{cliente_a.id}",
        headers=auth(token_de(usuarios["org_a"])),
        json={"telefono": "55-1234-5678", "clabe": "002180045678901234"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["telefono"] == "55-1234-5678"
    assert r.json()["clabe"] == "002180045678901234"


async def test_psd_si_edita_condiciones(cliente_http, usuarios, clientes):
    cliente_a, _ = clientes
    r = await cliente_http.patch(
        f"{API}/clients/{cliente_a.id}",
        headers=auth(token_de(usuarios["admin"])),
        json={"comision_pct": "12.50"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["comision_pct"]) == Decimal("12.50")


async def test_organizador_no_edita_a_otro(cliente_http, usuarios, clientes):
    _, cliente_b = clientes
    r = await cliente_http.patch(
        f"{API}/clients/{cliente_b.id}",
        headers=auth(token_de(usuarios["org_a"])),
        json={"telefono": "55-0000-0000"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Baja
# --------------------------------------------------------------------------


async def test_baja_es_logica(cliente_http, usuarios, clientes):
    cliente_a, _ = clientes
    r = await cliente_http.delete(
        f"{API}/clients/{cliente_a.id}", headers=auth(token_de(usuarios["admin"]))
    )
    assert r.status_code == 200
    assert r.json()["activo"] is False


async def test_no_se_da_de_baja_con_eventos_publicados(
    cliente_http, usuarios, clientes, db
):
    """Dejaria funciones a la venta de un organizador dado de baja."""
    from app.models.catalog import Event, Venue

    cliente_a, _ = clientes
    recinto = Venue(nombre="Teatro X", ciudad="CDMX")
    db.add(recinto)
    await db.flush()
    db.add(
        Event(
            client_id=cliente_a.id,
            venue_id=recinto.id,
            nombre="En venta",
            slug="en-venta",
            estado="publicado",
        )
    )
    await db.flush()

    r = await cliente_http.delete(
        f"{API}/clients/{cliente_a.id}", headers=auth(token_de(usuarios["admin"]))
    )
    assert r.status_code == 409
    assert r.json()["error"]["detalle"]["eventos_publicados"] == 1


async def test_organizador_no_se_da_de_baja(cliente_http, usuarios, clientes):
    cliente_a, _ = clientes
    r = await cliente_http.delete(
        f"{API}/clients/{cliente_a.id}", headers=auth(token_de(usuarios["org_a"]))
    )
    assert r.status_code == 403
