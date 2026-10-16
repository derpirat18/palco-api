"""Nadie ve un error tecnico (M2, M3).

Lo que importa:

- Una falla nuestra da un 500 con "El sistema no está disponible" y un
  folio; el registro guarda la traza con ese mismo folio. El navegador
  puede leer la respuesta (lleva CORS).
- Un dato mal escrito dice que campo corregir, en espanol, y nunca
  repite lo que el usuario escribio.
- Todo error, venga de donde venga, tiene la misma forma:
  {"error": {"codigo", "mensaje", "detalle"}}.
"""

import logging
import re

import pytest

from app.core.fallas import SISTEMA_NO_DISPONIBLE
from app.services.publico import PublicoService
from tests.test_catalog import API

pytestmark = pytest.mark.asyncio


def forma_unica(cuerpo: dict) -> None:
    assert set(cuerpo) == {"error"}
    assert set(cuerpo["error"]) == {"codigo", "mensaje", "detalle"}


async def test_una_falla_nuestra_no_muestra_nada_tecnico(
    cliente_http, monkeypatch, caplog
):
    async def explota(self, slug):
        raise RuntimeError("psycopg: connection reset by peer en 10.0.3.7")

    monkeypatch.setattr(PublicoService, "evento_por_slug", explota)

    with caplog.at_level(logging.ERROR, logger="palco.fallas"):
        r = await cliente_http.get(
            f"{API}/publico/eventos/cualquiera",
            headers={"Origin": "http://localhost:5173"},
        )

    assert r.status_code == 500
    forma_unica(r.json())
    error = r.json()["error"]
    assert error["codigo"] == "error_interno"
    assert error["mensaje"] == SISTEMA_NO_DISPONIBLE
    folio = error["detalle"]["folio"]
    assert re.fullmatch(r"INC-[0-9A-F]{8}", folio)
    assert r.headers["x-folio"] == folio
    # El navegador puede leerla: lleva los encabezados de CORS.
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    # Nada de la causa sale hacia afuera...
    assert "psycopg" not in r.text and "10.0.3.7" not in r.text
    # ...pero queda completa en el registro, con el mismo folio.
    assert folio in caplog.text
    assert "connection reset by peer" in caplog.text


async def test_datos_invalidos_dicen_que_corregir(cliente_http):
    r = await cliente_http.post(
        f"{API}/auth/login", json={"email": "ana@@correo..mx<script>"}
    )

    assert r.status_code == 422
    forma_unica(r.json())
    error = r.json()["error"]
    assert error["codigo"] == "datos_invalidos"
    assert error["mensaje"] == "Revisa los datos marcados e intenta de nuevo."
    campos = {c["campo"]: c["mensaje"] for c in error["detalle"]["campos"]}
    assert campos == {
        "email": "Escribe un correo válido, por ejemplo nombre@correo.com.",
        "password": "Este dato es obligatorio.",
    }
    # Nunca se repite lo que el usuario escribio.
    assert "<script>" not in r.text and "ana@@" not in r.text
    assert "Field required" not in r.text, "nada en ingles"


async def test_largo_y_tipo_en_palabras(cliente_http):
    r = await cliente_http.post(
        f"{API}/auth/login", json={"email": "ana@correo.mx", "password": "corta"}
    )

    campos = {c["campo"]: c["mensaje"] for c in r.json()["error"]["detalle"]["campos"]}
    assert campos == {"password": "Debe tener al menos 8 caracteres."}


async def test_ruta_inexistente_y_metodo_no_permitido(cliente_http):
    no_existe = await cliente_http.get(f"{API}/no-existe")
    metodo = await cliente_http.delete(f"{API}/health")

    forma_unica(no_existe.json())
    forma_unica(metodo.json())
    assert no_existe.json()["error"]["codigo"] == "no_encontrado"
    assert metodo.json()["error"]["codigo"] == "metodo_no_permitido"
    assert metodo.headers["allow"] == "GET"
    assert "Not Found" not in no_existe.text
    assert "Method Not Allowed" not in metodo.text


async def test_stripe_sin_configurar_no_lo_cuenta(cliente_http, monkeypatch, caplog):
    """Al comprador no se le dice que falta una llave en el .env."""
    from app.services.pasarela import (
        SISTEMA_DE_PAGOS_NO_DISPONIBLE,
        PasarelaSinConfigurar,
        StripePasarela,
    )

    pasarela = StripePasarela(secret_key="", webhook_secret="")
    with (
        caplog.at_level(logging.ERROR, logger="palco.pasarela"),
        pytest.raises(PasarelaSinConfigurar) as exc,
    ):
        pasarela._stripe()

    assert exc.value.mensaje == SISTEMA_DE_PAGOS_NO_DISPONIBLE
    assert ".env" not in exc.value.mensaje
    assert "STRIPE_SECRET_KEY" in caplog.text
