"""Texto libre que entra a Palco: se limpia antes de validarlo y guardarlo.

Contra inyeccion de SQL y de codigo ya protegen otras capas: SQLAlchemy
manda todo como parametro (nunca se arma SQL con texto del usuario), React
escapa lo que muestra y el correo escapa cada dato (`confirmacion.py`).
Esto cuida lo que esas capas no ven:

- Caracteres invisibles (control, ancho cero, cambio de direccion del
  texto): no se ven en el panel, pero rompen busquedas, la lista de la
  puerta y pueden disfrazar un nombre ("Juan" con un caracter oculto no es
  "Juan").
- Espacios de mas: "  Juan   Perez " es "Juan Perez".
- Formulas de Excel: un nombre que empieza con = + - @ se ejecuta al abrir
  la lista de la puerta en Excel. Los nombres de persona deben empezar con
  letra; el CSV ademas las neutraliza (palco-web).

Se aplica con `BeforeValidator`, asi que los topes de `Field` (min_length,
max_length) se miden sobre el texto ya limpio: "   " cuenta como vacio.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator

# Control (Cc), formato (Cf: ancho cero, marcas de direccion), uso privado
# (Co), sustitutos sueltos (Cs) y sin asignar (Cn).
_INVISIBLES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
_ESPACIOS = re.compile(r"\s+")
# Espacio, apostrofo recto y tipografico (\u2019), punto y guion.
_SIGNOS_DE_NOMBRE = frozenset(" '\u2019.-")


def _sin_invisibles(texto: str) -> str:
    """Quita lo invisible; tabuladores y saltos se quedan para luego juntarlos."""
    texto = unicodedata.normalize("NFC", texto)
    return "".join(
        c for c in texto if c in "\t\n\r" or unicodedata.category(c) not in _INVISIBLES
    )


def limpiar_linea(valor: Any) -> Any:
    """Una linea: sin invisibles, saltos ni espacios repetidos."""
    if not isinstance(valor, str):
        return valor
    return _ESPACIOS.sub(" ", _sin_invisibles(valor)).strip()


def limpiar_parrafo(valor: Any) -> Any:
    """Texto con saltos de linea (descripciones): a lo mas una linea en blanco."""
    if not isinstance(valor, str):
        return valor
    texto = _sin_invisibles(valor.replace("\r\n", "\n").replace("\r", "\n"))
    lineas = [_ESPACIOS.sub(" ", linea).strip() for linea in texto.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def validar_nombre_de_persona(valor: str | None) -> str | None:
    """Letras (con acentos y ñ), espacios, apostrofo, punto y guion.

    Empieza con letra: asi nunca es una formula de Excel. Los numeros no van
    en un nombre; si alguien escribe su telefono o un codigo aqui, mejor
    decirselo al momento que imprimirlo en la lista de la puerta.
    """
    if not valor:
        return valor
    if not valor[0].isalpha():
        raise ValueError("Empieza con una letra.")
    for c in valor:
        if not (
            c.isalpha()
            or unicodedata.category(c).startswith("M")
            or c in _SIGNOS_DE_NOMBRE
        ):
            raise ValueError(
                "Escribe solo letras; se permiten espacios, acentos, apóstrofo y guion."
            )
    return valor


def validar_url_https(valor: str | None) -> str | None:
    """Solo https://: nada de javascript:, data: ni direcciones sin cifrar."""
    if not valor:
        return None
    if not re.fullmatch(r"https://[^\s<>\"']+", valor):
        raise ValueError(
            "Escribe una dirección que empiece con https://, sin espacios."
        )
    return valor


def validar_color(valor: str | None) -> str | None:
    if not valor:
        return None
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", valor):
        raise ValueError("El color va en formato #RRGGBB, por ejemplo #7C3AED.")
    return valor


# Texto de una linea: nombres de evento, recinto, zona, motivo...
Linea = Annotated[str, BeforeValidator(limpiar_linea)]
# Texto largo con saltos de linea: la descripcion del evento.
Parrafo = Annotated[str, BeforeValidator(limpiar_parrafo)]
# El nombre de una persona: lo que sale en la puerta y en el correo.
NombreDePersona = Annotated[
    str, BeforeValidator(limpiar_linea), AfterValidator(validar_nombre_de_persona)
]
UrlHttps = Annotated[
    str, BeforeValidator(limpiar_linea), AfterValidator(validar_url_https)
]
Color = Annotated[str, BeforeValidator(limpiar_linea), AfterValidator(validar_color)]
