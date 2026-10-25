"""Toda respuesta JSON declara charset=utf-8 (#44).

Sin el charset, PowerShell 5.1 y otros clientes leen el cuerpo como
Latin-1: "Fila A · 1" llega como "Fila A Â· 1" y los acentos se rompen.
Se prueba cada camino por el que sale una respuesta: exito, error de
dominio, error HTTP de Starlette y error de validacion.
"""

import pytest

from app.core.respuestas import TIPO_JSON
from tests.test_catalog import API, auth, token_de

pytestmark = pytest.mark.asyncio


def declara_utf8(r) -> None:
    assert r.headers["content-type"] == TIPO_JSON, r.headers


async def test_respuesta_de_exito(cliente_http):
    r = await cliente_http.get(f"{API}/health")
    assert r.status_code == 200
    declara_utf8(r)


async def test_acentos_viajan_en_utf8(cliente_http, usuarios):
    nombre = "Plaza México · Peña"
    r = await cliente_http.post(
        f"{API}/venues",
        headers=auth(token_de(usuarios["admin"])),
        json={
            "nombre": nombre,
            "ciudad": "Ciudad de México",
            "coordenadas": "19.4326, -99.1332",
        },
    )
    assert r.status_code == 201, r.text
    declara_utf8(r)
    # Los bytes son UTF-8 tal cual, sin escapes \u: es lo que el charset
    # promete y lo que un cliente que lo respeta va a leer bien.
    assert nombre.encode("utf-8") in r.content
    assert r.json()["nombre"] == nombre


async def test_error_de_dominio(cliente_http):
    r = await cliente_http.get(f"{API}/venues")
    assert r.status_code == 401
    declara_utf8(r)
    assert r.json()["error"]["codigo"]


async def test_ruta_inexistente(cliente_http):
    r = await cliente_http.get(f"{API}/no-existe")
    assert r.status_code == 404
    declara_utf8(r)
    assert r.json()["error"]["codigo"] == "no_encontrado"


async def test_metodo_no_permitido_conserva_sus_encabezados(cliente_http):
    r = await cliente_http.delete(f"{API}/health")
    assert r.status_code == 405
    declara_utf8(r)
    assert r.headers["allow"] == "GET"


async def test_error_de_validacion(cliente_http):
    r = await cliente_http.post(f"{API}/auth/login", json={"email": "no-es-correo"})
    assert r.status_code == 422
    declara_utf8(r)
    assert r.json()["error"]["codigo"] == "datos_invalidos"
