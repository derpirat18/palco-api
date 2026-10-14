"""Normalizacion de telefonos a E.164, sin base de datos."""

import pytest

from app.core.telefonos import TelefonoInvalido, normalizar_telefono


@pytest.mark.parametrize(
    "escrito",
    [
        "55 1234 5678",
        "5512345678",
        "55-1234-5678",
        "(55) 1234 5678",
        "+52 55 1234 5678",
        "+52 (55) 1234-5678",
        # Formatos que Mexico elimino en 2019 y que la gente sigue usando.
        "+52 1 55 1234 5678",
        "521 55 1234 5678",
        "044 55 1234 5678",
        "045 55 1234 5678",
    ],
)
def test_todas_las_formas_de_escribir_el_mismo_numero(escrito):
    """Un mismo numero, una sola forma de guardarlo: si no, el CRM ve
    tres contactos y WhatsApp le escribe tres veces a la misma persona."""
    assert normalizar_telefono(escrito) == "+525512345678"


@pytest.mark.parametrize(
    "escrito,esperado",
    [
        ("+57 300 123 4567", "+573001234567"),  # Colombia
        ("+34 612 34 56 78", "+34612345678"),  # España
        ("+1 (213) 555-0142", "+12135550142"),  # Estados Unidos
    ],
)
def test_numeros_de_otros_paises_con_su_codigo(escrito, esperado):
    assert normalizar_telefono(escrito) == esperado


@pytest.mark.parametrize("vacio", [None, "", "   "])
def test_vacio_es_sin_telefono(vacio):
    assert normalizar_telefono(vacio) is None


@pytest.mark.parametrize("malo", ["1234", "abc", "55 1234", "+52 00 0000 0000"])
def test_un_numero_que_no_existe_se_rechaza(malo):
    """Guardado, no serviria ni para avisar de un cambio de horario."""
    with pytest.raises(TelefonoInvalido):
        normalizar_telefono(malo)
