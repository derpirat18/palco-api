"""Plantillas de correo con la sintaxis de Handlebars (lo poco que usan).

Las plantillas las disena Nadia en Handlebars (`app/plantillas/correo/`) y
se guardan tal cual: si cambia el diseno, se reemplaza el archivo sin tocar
codigo. Aqui solo vive el subconjunto que usan, sin agregar dependencias:

- `{{a.b}}`: el valor, escapado para HTML (salvo con `escapar=False`, para
  la version de texto). `{{../a}}` sube un nivel dentro de `#each`.
- `{{#if x}}...{{else}}...{{/if}}` y `{{#unless x}}...{{/unless}}`.
- `{{#if (eq a b)}}`: el unico ayudante.
- `{{#each lista}}...{{/each}}`, con `this` y los campos de cada elemento.

Lo que no conoce lo rechaza con un error: una plantilla nueva con algo
que no se entiende falla en las pruebas, no en el correo de un cliente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CARPETA = Path(__file__).resolve().parent.parent / "plantillas"

# El mismo escape que Handlebars, para que el resultado sea identico al
# del ejemplo que genera Nadia.
_ESCAPES = str.maketrans(
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;",
     "`": "&#x60;", "=": "&#x3D;"}
)  # fmt: skip


def escape(texto: str) -> str:
    return texto.translate(_ESCAPES)


_ETIQUETA = re.compile(r"\{\{(.*?)\}\}", re.S)


class PlantillaInvalida(ValueError):
    pass


@dataclass
class _Nodo:
    tipo: str  # texto, valor, if, unless, each
    texto: str = ""
    expr: str = ""
    hijos: list[_Nodo] = field(default_factory=list)
    otro: list[_Nodo] = field(default_factory=list)


def _analizar(fuente: str) -> list[_Nodo]:
    raiz: list[_Nodo] = []
    pila: list[tuple[_Nodo, list[_Nodo]]] = []
    actual = raiz
    pos = 0
    for m in _ETIQUETA.finditer(fuente):
        dentro = m.group(1).strip()
        corte, siguiente = m.start(), m.end()
        if dentro[:1] in "#/!" or dentro == "else":
            # Como Handlebars: un bloque solo en su renglon no deja el
            # renglon vacio en el resultado.
            inicio = fuente.rfind("\n", 0, m.start()) + 1
            fin = fuente.find("\n", m.end())
            fin = len(fuente) if fin == -1 else fin
            if (
                inicio >= pos
                and not fuente[inicio : m.start()].strip()
                and not fuente[m.end() : fin].strip()
            ):
                corte, siguiente = inicio, min(fin + 1, len(fuente))
        if corte > pos:
            actual.append(_Nodo("texto", texto=fuente[pos:corte]))
        pos = siguiente
        if dentro.startswith("!"):
            continue
        if dentro.startswith("#"):
            nombre, _, expr = dentro[1:].partition(" ")
            if nombre not in {"if", "unless", "each"}:
                raise PlantillaInvalida(f"Bloque desconocido: #{nombre}")
            nodo = _Nodo(nombre, expr=expr.strip())
            actual.append(nodo)
            pila.append((nodo, actual))
            actual = nodo.hijos
        elif dentro == "else":
            if not pila or pila[-1][0].tipo == "each":
                raise PlantillaInvalida("{{else}} fuera de un #if")
            actual = pila[-1][0].otro
        elif dentro.startswith("/"):
            if not pila or pila[-1][0].tipo != dentro[1:].strip():
                raise PlantillaInvalida(f"Cierre sin abrir: {dentro}")
            _, actual = pila.pop()
        else:
            if not re.fullmatch(r"(\.\./)*[A-Za-z_][\w.]*|this", dentro):
                raise PlantillaInvalida(f"Expresion desconocida: {{{{{dentro}}}}}")
            actual.append(_Nodo("valor", expr=dentro))
    if pila:
        raise PlantillaInvalida(f"Bloque sin cerrar: #{pila[-1][0].tipo}")
    actual.append(_Nodo("texto", texto=fuente[pos:]))
    return raiz


def _buscar(expr: str, contextos: list[Any]) -> Any:
    nivel = len(contextos) - 1
    while expr.startswith("../"):
        expr, nivel = expr[3:], max(0, nivel - 1)
    valor = contextos[nivel]
    if expr == "this":
        return valor
    for parte in expr.split("."):
        valor = valor.get(parte) if isinstance(valor, dict) else None
        if valor is None:
            return None
    return valor


def _condicion(expr: str, contextos: list[Any]) -> bool:
    m = re.fullmatch(r"\(eq\s+(\S+)\s+(\S+)\)", expr)
    if m:
        a, b = (
            _buscar(x, contextos) if not re.fullmatch(r"-?\d+", x) else int(x)
            for x in m.groups()
        )
        return a == b
    if expr.startswith("("):
        raise PlantillaInvalida(f"Ayudante desconocido: {expr}")
    valor = _buscar(expr, contextos)
    return bool(valor) and valor != []


def _pintar(nodos: list[_Nodo], contextos: list[Any], escapar: bool) -> str:
    salida: list[str] = []
    for n in nodos:
        if n.tipo == "texto":
            salida.append(n.texto)
        elif n.tipo == "valor":
            valor = _buscar(n.expr, contextos)
            texto = "" if valor is None else str(valor)
            salida.append(escape(texto) if escapar else texto)
        elif n.tipo in ("if", "unless"):
            si = _condicion(n.expr, contextos)
            if n.tipo == "unless":
                si = not si
            salida.append(_pintar(n.hijos if si else n.otro, contextos, escapar))
        else:
            for elemento in _buscar(n.expr, contextos) or []:
                salida.append(_pintar(n.hijos, [*contextos, elemento], escapar))
    return "".join(salida)


def pintar(fuente: str, datos: dict, escapar: bool = True) -> str:
    return _pintar(_analizar(fuente), [datos], escapar)


def pintar_archivo(nombre: str, datos: dict, escapar: bool = True) -> str:
    return pintar((CARPETA / nombre).read_text(encoding="utf-8"), datos, escapar)
