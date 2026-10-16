"""Los mensajes al usuario se leen bien y no cuentan detalles tecnicos (M4, M5).

Guardas para la regla de AGENTS.md: un mensaje nuevo sin acentos o con
la causa tecnica adentro rompe esta prueba, no le llega a un comprador.
"""

import ast
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.models.base import EstadoFuncion
from app.models.catalog import Performance
from app.services.venta import motivo_no_se_vende

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

SIN_ACENTO = re.compile(
    r"\b(funcion|vencio|seleccion|telefono|valido|valida|invalido|todavia|"
    r"cortesia|cortesias|numero|digitos|codigo|operacion|sesion|pagina|"
    r"version|ningun|cerro|aparto|recibio|genero)\b"
)
TECNICO = re.compile(r"\.env\b|STRIPE_|SECRET|token de acceso|Traceback|SQL")


def mensajes_de_error():
    """Cada texto que un `raise` entrega como mensaje, con su ubicacion."""
    for archivo in sorted(APP.rglob("*.py")):
        if archivo.name == "config.py":  # mensajes de arranque, para desarrollo
            continue
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if not (isinstance(nodo, ast.Raise) and isinstance(nodo.exc, ast.Call)):
                continue
            if not nodo.exc.args:
                continue
            arg = nodo.exc.args[0]
            partes = []
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                partes = [arg.value]
            elif isinstance(arg, ast.JoinedStr):
                partes = [
                    v.value
                    for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
            if partes:
                yield f"{archivo.relative_to(APP)}:{nodo.lineno}", "".join(partes)


def test_hay_mensajes_que_revisar():
    assert len(list(mensajes_de_error())) > 50


@pytest.mark.parametrize(("donde", "texto"), list(mensajes_de_error()))
def test_mensaje_con_acentos_y_sin_detalles_tecnicos(donde, texto):
    assert not SIN_ACENTO.search(texto), f"{donde}: falta un acento en {texto!r}"
    assert not TECNICO.search(texto), f"{donde}: detalle técnico en {texto!r}"


def funcion(**kw) -> Performance:
    ahora = datetime.now(UTC)
    datos = {
        "estado": EstadoFuncion.EN_VENTA.value,
        "inventario_generado": True,
        "inicia_en": ahora + timedelta(days=5),
        "venta_abre_en": None,
        "venta_cierra_en": None,
    }
    datos.update(kw)
    return Performance(**datos)


@pytest.mark.parametrize(
    ("kw", "mensaje", "motivo"),
    [
        (
            {"estado": "cancelada"},
            "Esta función fue cancelada. Si compraste boletos, te escribiremos "
            "con los pasos para tu reembolso.",
            "estado",
        ),
        ({"estado": "agotada"}, "Boletos agotados para esta función.", "estado"),
        ({"estado": "realizada"}, "Esta función ya se realizó.", "estado"),
        (
            {"estado": "programada"},
            "La venta de esta función todavía no abre.",
            "estado",
        ),
        (
            {"inventario_generado": False},
            "La venta de esta función todavía no abre.",
            "sin_inventario",
        ),
        (
            {"venta_abre_en": datetime.now(UTC) + timedelta(days=1)},
            "La venta de esta función todavía no abre.",
            "aun_no_abre",
        ),
        (
            {"venta_cierra_en": datetime.now(UTC) - timedelta(hours=1)},
            "La venta en línea para esta función ya cerró.",
            "venta_cerrada",
        ),
    ],
)
def test_cada_motivo_de_no_venta_se_explica(kw, mensaje, motivo):
    texto, detalle = motivo_no_se_vende(funcion(**kw), datetime.now(UTC))

    assert texto == mensaje
    assert detalle["motivo"] == motivo


def test_cuando_abre_viaja_la_fecha_para_el_front():
    abre = datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    _, detalle = motivo_no_se_vende(
        funcion(venta_abre_en=abre), datetime(2026, 10, 30, tzinfo=UTC)
    )
    assert detalle == {"motivo": "aun_no_abre", "abre_en": abre.isoformat()}
