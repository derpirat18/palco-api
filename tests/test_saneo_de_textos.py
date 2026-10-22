"""Texto libre limpio al entrar (app/core/textos.py).

SQL y HTML ya estan cubiertos por otras capas (parametros de SQLAlchemy,
escape de React y del correo); aqui se prueba lo demas: invisibles,
espacios, nombres que no son nombres, formulas de Excel, direcciones que no
son https y topes que antes terminaban en error 500 de la base.
"""

import uuid

import pytest
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.core.textos import (
    Linea,
    NombreDePersona,
    limpiar_linea,
    limpiar_parrafo,
    validar_url_https,
)
from app.models.sales import Order
from app.services.seat_maps import BloqueDeButacas

# api y stripe_falso son fixtures: pytest los toma de aqui.
from tests.test_checkout import (  # noqa: F401
    CUERPO,
    apartar,
    api,
    escenario,
    stripe_falso,
)

API = "/api/v1"


class _Persona(BaseModel):
    nombre: NombreDePersona = Field(min_length=1, max_length=120)
    nota: Linea | None = Field(default=None, max_length=10)


# --- limpieza -------------------------------------------------------------------


def test_linea_quita_invisibles_y_junta_espacios():
    assert limpiar_linea("  Juan \t  Pérez\n ") == "Juan Pérez"
    # Ancho cero, marca de direccion y un caracter de control.
    assert limpiar_linea("Ju​an‮ \x00Pérez") == "Juan Pérez"
    # Espacio que no se rompe: es un espacio.
    assert limpiar_linea("Ana\u00a0María") == "Ana María"


def test_linea_une_acentos_escritos_por_separado():
    """e + acento combinado y é son el mismo texto (NFC)."""
    assert limpiar_linea("Pérez") == "Pérez"


def test_parrafo_conserva_saltos_pero_no_lineas_en_blanco_de_mas():
    texto = "Primera  línea\r\n\r\n\r\n\r\nSegunda​\n  tercera "
    assert limpiar_parrafo(texto) == "Primera línea\n\nSegunda\ntercera"


def test_lo_que_no_es_texto_pasa_sin_tocar():
    assert limpiar_linea(None) is None
    assert limpiar_linea(5) == 5


# --- nombres de persona -----------------------------------------------------------


@pytest.mark.parametrize(
    "nombre",
    [
        "Ana",
        "María José",
        "José Ángel Núñez",
        "O'Neill",
        "D\u2019Angelo",
        "Ma. Guadalupe",
        "Pérez-Gómez",
        "Zoë",
        "Björk",
    ],
)
def test_nombres_reales_pasan(nombre):
    assert _Persona(nombre=nombre).nombre == nombre


@pytest.mark.parametrize(
    "nombre",
    [
        "<script>alert(1)</script>",
        "Robert'); DROP TABLE orders;--",
        '=HYPERLINK("http://x")',
        "+5512345678",
        "-Ana",
        "@ana",
        "Juan 2",
        "55 1234 5678",
        "Ana<b>",
    ],
)
def test_lo_que_no_es_un_nombre_se_rechaza(nombre):
    with pytest.raises(ValidationError):
        _Persona(nombre=nombre)


def test_solo_espacios_cuenta_como_vacio():
    with pytest.raises(ValidationError) as exc:
        _Persona(nombre="   ​ ")
    assert exc.value.errors()[0]["type"] == "string_too_short"


def test_el_tope_se_mide_sobre_el_texto_limpio():
    assert _Persona(nombre="Ana", nota="  a    b   ").nota == "a b"


# --- direcciones y mapas ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "http://sitio.mx/cartel.jpg",
        "https://sitio.mx/a b.jpg",
        'https://sitio.mx/"onerror="x',
    ],
)
def test_imagen_solo_https(url):
    with pytest.raises(ValueError):
        validar_url_https(url)


def test_imagen_https_pasa():
    assert validar_url_https("https://cdn.sitio.mx/cartel.jpg?v=2") == (
        "https://cdn.sitio.mx/cartel.jpg?v=2"
    )


def test_fila_larga_se_rechaza_antes_de_llegar_a_la_base():
    """seats.fila es de 8: antes, una fila de 9 terminaba en error 500."""
    with pytest.raises(ValidationError):
        BloqueDeButacas(zona="Luneta", filas=["ABCDEFGHI"], butacas_por_fila=5)


def test_color_debe_ser_hexadecimal():
    with pytest.raises(ValidationError):
        BloqueDeButacas(zona="Luneta", color="rojo", filas=["A"], butacas_por_fila=5)
    bloque = BloqueDeButacas(
        zona="  Luneta ", color="#7C3AED", filas=[" A "], butacas_por_fila=5
    )
    assert (bloque.zona, bloque.etiquetas_de_fila()) == ("Luneta", ["A"])


# --- por la API ---------------------------------------------------------------------


async def test_checkout_rechaza_codigo_en_el_nombre_campo_por_campo(
    api,  # noqa: F811
    db,
    clientes,
    hold_service,
):
    esc = await escenario(db, clientes[0].id)
    hold = uuid.uuid4()
    await apartar(db, hold_service, esc, esc["luneta"][:1], hold=hold.hex)

    r = await api.post(
        f"{API}/performances/{esc['performance_id']}/apartados/{hold}/checkout",
        json={**CUERPO, "nombre": "<script>alert(1)</script>", "apellidos": "=1+1"},
    )

    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["codigo"] == "datos_invalidos"
    campos = {c["campo"]: c["mensaje"] for c in error["detalle"]["campos"]}
    assert set(campos) == {"nombre", "apellidos"}
    assert campos["nombre"].startswith("Empieza con una letra")
    assert await db.scalar(select(Order).where(Order.hold_id == hold.hex)) is None


async def test_checkout_guarda_el_nombre_limpio(
    api,  # noqa: F811
    db,
    clientes,
    hold_service,
    monkeypatch,
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    esc = await escenario(db, clientes[0].id)
    hold = uuid.uuid4()
    await apartar(db, hold_service, esc, esc["luneta"][:1], hold=hold.hex)

    r = await api.post(
        f"{API}/performances/{esc['performance_id']}/apartados/{hold}/checkout",
        json={**CUERPO, "nombre": "  María​   José ", "apellidos": " Pérez "},
    )

    assert r.status_code == 201, r.text
    orden = await db.scalar(select(Order).where(Order.hold_id == hold.hex))
    assert (orden.nombre_snap, orden.apellidos_snap) == ("María José", "Pérez")
