"""Limites contra bots y fuerza bruta (ADR 0016).

Lo que importa:

- Rebasar el limite da 429 con `Retry-After` y un mensaje para personas.
- Un `X-Forwarded-For` inventado no sirve para saltarse el limite.
- Tras 5 fallos la cuenta no entra ni con la contrasena correcta, y un
  correo que no existe se comporta igual que uno que si.
- Un bot no acapara el mapa con hold_id nuevos, ni un correo abre mas
  compras de la cuenta.
- Sin Redis, se deja pasar: los limites no tumban la venta.
"""

import uuid

import pytest
import redis.asyncio as aioredis

from app.core.config import settings
from app.services import limites
from tests.test_catalog import API
from tests.test_checkout import CUERPO, PasarelaFalsa, escenario

pytestmark = pytest.mark.asyncio

PASSWORD = "Secreta-12345"


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
def api(cliente_http, hold_service, stripe_falso, redis_test, monkeypatch):
    """Cliente con los limites encendidos, contando en el Redis de pruebas."""
    from app.api.v1.apartados import get_hold_service
    from app.api.v1.checkout import get_pasarela
    from app.main import app

    monkeypatch.setattr(settings, "LIMITES_ACTIVOS", True)
    monkeypatch.setattr(settings, "PROXIES_DE_CONFIANZA", 0)
    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[get_pasarela] = lambda: stripe_falso
    app.dependency_overrides[limites.get_redis_limites] = lambda: redis_test
    return cliente_http


async def login(api, email, password="incorrecta-123", **kw):
    return await api.post(
        f"{API}/auth/login", json={"email": email, "password": password}, **kw
    )


# --------------------------------------------------------------------------
# Limite de velocidad
# --------------------------------------------------------------------------


async def test_rebasar_el_limite_da_429_con_retry_after(api):
    tope = limites.POR_MINUTO["login"]
    for i in range(tope):
        r = await login(api, f"nadie{i}@correo.mx")
        assert r.status_code == 401, r.text

    r = await login(api, "otro@correo.mx")

    assert r.status_code == 429
    error = r.json()["error"]
    assert error["codigo"] == "demasiadas_solicitudes"
    assert "Espera un minuto" in error["mensaje"]
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert error["detalle"]["reintentar_en_segundos"] == int(r.headers["retry-after"])


async def test_un_x_forwarded_for_inventado_no_salta_el_limite(api):
    tope = limites.POR_MINUTO["login"]
    for i in range(tope):
        await login(
            api, f"nadie{i}@correo.mx", headers={"X-Forwarded-For": f"9.9.9.{i}"}
        )

    r = await login(api, "otro@correo.mx", headers={"X-Forwarded-For": "8.8.8.8"})

    assert r.status_code == 429, "sin proxies de confianza, el encabezado no cuenta"


async def test_detras_del_balanceador_cada_ip_tiene_su_limite(api, monkeypatch):
    monkeypatch.setattr(settings, "PROXIES_DE_CONFIANZA", 1)
    tope = limites.POR_MINUTO["login"]
    # El cliente puede anteponer lo que quiera; cuenta lo que agrego el
    # balanceador, la ultima entrada.
    for i in range(tope):
        await login(
            api, f"nadie{i}@correo.mx", headers={"X-Forwarded-For": "1.1.1.1, 7.7.7.7"}
        )

    misma = await login(api, "x@correo.mx", headers={"X-Forwarded-For": "7.7.7.7"})
    otra = await login(api, "x@correo.mx", headers={"X-Forwarded-For": "1.1.1.1"})

    assert misma.status_code == 429
    assert otra.status_code == 401


async def test_apagados_no_limitan(api, monkeypatch):
    monkeypatch.setattr(settings, "LIMITES_ACTIVOS", False)
    for i in range(limites.POR_MINUTO["login"] + 5):
        assert (await login(api, f"nadie{i}@correo.mx")).status_code == 401


async def test_sin_redis_se_deja_pasar(api, usuarios):
    from app.main import app

    caido = aioredis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    app.dependency_overrides[limites.get_redis_limites] = lambda: caido

    malo = await login(api, "admin@psdigital.mx")
    bueno = await login(api, "admin@psdigital.mx", PASSWORD)

    assert (malo.status_code, bueno.status_code) == (401, 200)
    await caido.aclose()


# --------------------------------------------------------------------------
# Candado de login
# --------------------------------------------------------------------------


async def test_tras_cinco_fallos_ni_la_correcta_entra(api, usuarios):
    for _ in range(limites.FALLOS_DE_LOGIN):
        assert (await login(api, "admin@psdigital.mx")).status_code == 401

    r = await login(api, "admin@psdigital.mx", PASSWORD)

    assert r.status_code == 429
    assert r.json()["error"]["codigo"] == "acceso_bloqueado"
    assert "15 minutos" in r.json()["error"]["mensaje"]
    assert 800 < int(r.headers["retry-after"]) <= 900
    # Otra cuenta, desde la misma IP, no se ve afectada.
    assert (await login(api, "org.a@circo.mx", PASSWORD)).status_code == 200


async def test_entrar_bien_borra_los_fallos(api, usuarios):
    for _ in range(2):
        for _ in range(limites.FALLOS_DE_LOGIN - 1):
            await login(api, "admin@psdigital.mx")
        assert (await login(api, "admin@psdigital.mx", PASSWORD)).status_code == 200


async def test_un_correo_inexistente_se_bloquea_igual(api, usuarios):
    """Si solo se bloquearan los que existen, el bloqueo los delataria."""
    for correo in ("admin@psdigital.mx", "nadie@psdigital.mx"):
        for _ in range(limites.FALLOS_DE_LOGIN):
            await login(api, correo)

    real = await login(api, "admin@psdigital.mx")
    falso = await login(api, "nadie@psdigital.mx")

    assert real.status_code == falso.status_code == 429
    assert real.json()["error"]["codigo"] == falso.json()["error"]["codigo"]
    assert real.json()["error"]["mensaje"] == falso.json()["error"]["mensaje"]


# --------------------------------------------------------------------------
# Acaparamiento
# --------------------------------------------------------------------------


async def apartar(api, pid, seat, hold=None):
    hold = hold or uuid.uuid4()
    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": str(hold), "seat_ids": [seat]},
    )
    return hold, r


async def test_tope_de_apartados_vigentes_por_ip(api, db, clientes):
    esc = await escenario(db, clientes[0].id, por_zona=5)
    pid, seats = esc["performance_id"], esc["seat_ids"]
    tope = limites.APARTADOS_VIGENTES_POR_IP

    holds = []
    for s in seats[:tope]:
        hold, r = await apartar(api, pid, s)
        assert r.status_code == 201, r.text
        holds.append(hold)

    _, r = await apartar(api, pid, seats[tope])
    assert r.status_code == 429
    assert r.json()["error"]["codigo"] == "demasiados_apartados"
    assert "compras en proceso" in r.json()["error"]["mensaje"]

    # El mismo comprador puede seguir sumando butacas a su apartado.
    _, r = await apartar(api, pid, seats[tope], hold=holds[0])
    assert r.status_code == 201, r.text

    # Soltar uno completo deja lugar para otro.
    r = await api.delete(f"{API}/performances/{pid}/apartados/{holds[1]}")
    assert r.status_code == 204
    _, r = await apartar(api, pid, seats[1])
    assert r.status_code == 201, r.text


async def test_un_correo_no_abre_compras_sin_fin(api, db, clientes):
    esc = await escenario(db, clientes[0].id, por_zona=5)
    pid, seats = esc["performance_id"], esc["luneta"]
    tope = limites.ORDENES_PENDIENTES_POR_CORREO

    holds = []
    for s in seats[: tope + 1]:
        hold, r = await apartar(api, pid, s)
        assert r.status_code == 201, r.text
        holds.append(hold)

    for hold in holds[:tope]:
        r = await api.post(
            f"{API}/performances/{pid}/apartados/{hold}/checkout", json=CUERPO
        )
        assert r.status_code == 201, r.text

    r = await api.post(
        f"{API}/performances/{pid}/apartados/{holds[tope]}/checkout", json=CUERPO
    )
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "demasiadas_ordenes"

    # Repetir el pago de una compra que ya existe no cuenta como otra.
    r = await api.post(
        f"{API}/performances/{pid}/apartados/{holds[0]}/checkout", json=CUERPO
    )
    assert r.status_code == 201, r.text
