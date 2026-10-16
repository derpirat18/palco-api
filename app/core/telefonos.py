"""Telefonos normalizados a formato internacional (E.164).

"55 1234 5678", "5512345678" y "+52 (55) 1234-5678" son el mismo numero.
Guardados tal como llegan son tres contactos distintos para cualquier CRM,
y una campaña de WhatsApp le llega tres veces a la misma persona o a
ninguna. Normalizar al escribir es trivial; limpiar cien mil registros
despues, no.

Se guarda siempre como +<pais><numero>, sin espacios: +525512345678.
"""

from __future__ import annotations

import re

import phonenumbers

from app.core.errors import ErrorDeDominio

# Lo que exige la base (CHECK en customers.telefono). E.164: "+", codigo de
# pais que no empieza en 0, y a lo mas 15 digitos en total.
PATRON_E164 = r"^\+[1-9][0-9]{7,14}$"

# Prefijos que Mexico elimino en 2019 y que la gente sigue escribiendo.
_LARGA_DISTANCIA_MX = re.compile(r"^(?:044|045|01)(\d{10})$")
_MOVIL_ANTIGUO_MX = re.compile(r"^\+?521(\d{10})$")


class TelefonoInvalido(ErrorDeDominio):
    codigo = "telefono_invalido"
    http_status = 422


def normalizar_telefono(texto: str | None, region: str = "MX") -> str | None:
    """Devuelve el telefono en E.164, o None si viene vacio.

    Sin codigo de pais se asume `region`, que por omision es Mexico. Un
    numero que no es valido para su pais se rechaza en lugar de guardarse:
    un telefono malo no sirve ni para avisarle al comprador de un cambio
    de horario.
    """
    if texto is None or not texto.strip():
        return None

    limpio = re.sub(r"[^\d+]", "", texto.strip())
    if m := _MOVIL_ANTIGUO_MX.match(limpio):
        limpio = "+52" + m.group(1)
    elif m := _LARGA_DISTANCIA_MX.match(limpio):
        limpio = m.group(1)

    try:
        numero = phonenumbers.parse(limpio, region)
    except phonenumbers.NumberParseException as exc:
        raise TelefonoInvalido(
            "Ese teléfono no tiene un formato válido.", {"telefono": texto}
        ) from exc

    if not phonenumbers.is_valid_number(numero):
        raise TelefonoInvalido(
            "Ese teléfono no es válido. Si es de otro país, escríbelo con su "
            "código, por ejemplo +57 300 123 4567.",
            {"telefono": texto},
        )
    return phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)
