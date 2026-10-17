"""Encabezados de seguridad (B7), tamano maximo de peticion (B12) y
latidos de los procesos de fondo (A3)."""

import time

import pytest

from app.core.config import settings
from app.core.proteccion import EncabezadosDeSeguridad
from app.services.latidos import clave, latir
from tests.test_catalog import API

pytestmark = pytest.mark.asyncio

SEGURIDAD = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


def lleva_seguridad(r) -> None:
    for nombre, valor in SEGURIDAD.items():
        assert r.headers.get(nombre) == valor, (nombre, r.headers.get(nombre))


# --------------------------------------------------------------------------
# B7
# --------------------------------------------------------------------------


async def test_toda_respuesta_lleva_encabezados_de_seguridad(cliente_http):
    ok = await cliente_http.get(f"{API}/health")
    no_existe = await cliente_http.get(f"{API}/no-existe")
    invalido = await cliente_http.post(f"{API}/auth/login", json={})

    for r in (ok, no_existe, invalido):
        lleva_seguridad(r)
    # En local no hay HTTPS: obligarlo romperia el desarrollo.
    assert "strict-transport-security" not in ok.headers


async def test_la_documentacion_local_se_sigue_viendo(cliente_http):
    r = await cliente_http.get("/docs")

    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_fuera_de_local_se_obliga_https(monkeypatch):
    monkeypatch.setattr(settings, "ENTORNO", "prod")
    enviados = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(mensaje):
        enviados.append(mensaje)

    await EncabezadosDeSeguridad(app)({"type": "http", "path": "/x"}, None, send)

    encabezados = dict(enviados[0]["headers"])
    assert encabezados[b"strict-transport-security"] == (
        b"max-age=31536000; includeSubDomains"
    )


async def test_no_pisa_lo_que_la_respuesta_ya_decidio(monkeypatch):
    enviados = []

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"cache-control", b"public, max-age=60")],
            }
        )

    async def send(mensaje):
        enviados.append(mensaje)

    await EncabezadosDeSeguridad(app)({"type": "http", "path": "/x"}, None, send)

    cache = [v for k, v in enviados[0]["headers"] if k == b"cache-control"]
    assert cache == [b"public, max-age=60"]


# --------------------------------------------------------------------------
# B12
# --------------------------------------------------------------------------


def es_413(r) -> None:
    assert r.status_code == 413, r.text
    assert r.json()["error"]["codigo"] == "solicitud_muy_grande"
    assert "demasiado grandes" in r.json()["error"]["mensaje"]


async def test_cuerpo_demasiado_grande_con_content_length(cliente_http, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CUERPO_BYTES", 200)

    r = await cliente_http.post(
        f"{API}/auth/login",
        content=b'{"email": "' + b"a" * 500 + b'@x.mx"}',
        headers={"Content-Type": "application/json"},
    )

    es_413(r)
    lleva_seguridad(r)


async def test_cuerpo_por_partes_tambien_se_corta(cliente_http, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CUERPO_BYTES", 200)

    async def partes():
        for _ in range(10):
            yield b"x" * 50

    r = await cliente_http.post(
        f"{API}/auth/login",
        content=partes(),
        headers={"Content-Type": "application/json"},
    )

    es_413(r)


async def test_por_partes_dentro_del_limite_llega_completo(cliente_http):
    async def partes():
        yield b'{"email": "nadie@correo.mx",'
        yield b' "password": "incorrecta-123"}'

    r = await cliente_http.post(
        f"{API}/auth/login",
        content=partes(),
        headers={"Content-Type": "application/json"},
    )

    # Llego entero y se proceso: credenciales malas, no un cuerpo cortado.
    assert r.status_code == 401, r.text


async def test_el_limite_alcanza_para_un_mapa_grande():
    # 50,000 butacas en CSV, a ~40 bytes por linea.
    assert settings.MAX_CUERPO_BYTES >= 50_000 * 40


# --------------------------------------------------------------------------
# A3
# --------------------------------------------------------------------------


@pytest.fixture
def salud(cliente_http, redis_test):
    from app.api.v1.health import get_redis_salud
    from app.main import app

    app.dependency_overrides[get_redis_salud] = lambda: redis_test
    return cliente_http


async def test_sin_latidos_se_avisa(salud):
    r = await salud.get(f"{API}/health/trabajadores")

    assert r.status_code == 503
    assert r.json()["estado"] == "detenido"
    assert r.json()["procesos"]["barrido"] == {"al_dia": False, "hace_segundos": None}


async def test_con_latidos_al_dia(salud, redis_test):
    await latir(redis_test, "barrido")
    await latir(redis_test, "bandeja")

    r = await salud.get(f"{API}/health/trabajadores")

    assert r.status_code == 200
    assert r.json()["estado"] == "ok"
    assert all(p["al_dia"] for p in r.json()["procesos"].values())


async def test_un_proceso_parado_se_nota(salud, redis_test):
    await latir(redis_test, "bandeja")
    await redis_test.set(clave("barrido"), int(time.time()) - 600)

    r = await salud.get(f"{API}/health/trabajadores")

    assert r.status_code == 503
    barrido = r.json()["procesos"]["barrido"]
    assert barrido["al_dia"] is False
    assert 590 <= barrido["hace_segundos"] <= 700
    assert r.json()["procesos"]["bandeja"]["al_dia"] is True


async def test_la_api_sigue_lista_aunque_un_proceso_este_parado(salud):
    """El balanceador mira /health/ready: no debe sacar a la API por eso."""
    r = await salud.get(f"{API}/health")

    assert r.status_code == 200
