"""Puerta del turno (ADR 0017).

Lo que importa:

- Apagada, no cambia nada.
- Con la sala activa, nadie empieza un apartado sin un pase valido de
  ese evento: ni sin pase, ni con uno alterado, vencido o de otro evento.
- Quien ya estaba comprando sigue sin pase.
- Un pase no sirve para abrir apartados sin fin.
"""

import time
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.catalog import Event, Performance
from app.services import limites
from app.services.turnos import (
    APARTADOS_POR_PASE,
    PaseInvalido,
    clave_sala_activa,
    emitir_pase,
    leer_pase,
)
from tests.test_catalog import API
from tests.test_checkout import escenario

pytestmark = pytest.mark.asyncio

LLAVE = "llave-de-pruebas-para-turnos-0123456789"


@pytest.fixture
def api(cliente_http, hold_service, redis_test, monkeypatch):
    from app.api.v1.apartados import get_hold_service
    from app.main import app

    monkeypatch.setattr(settings, "SALA_DE_ESPERA_HABILITADA", True)
    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[limites.get_redis_limites] = lambda: redis_test
    return cliente_http


async def evento_de(db, performance_id) -> uuid.UUID:
    return await db.scalar(
        select(Event.uuid)
        .join(Performance, Performance.event_id == Event.id)
        .where(Performance.id == performance_id)
    )


async def apartar(api, pid, seat, hold=None, pase=None):
    hold = hold or uuid.uuid4()
    headers = {"X-Palco-Turno": pase} if pase else {}
    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": str(hold), "seat_ids": [seat]},
        headers=headers,
    )
    return hold, r


@pytest.fixture
async def sala_activa(db, clientes, redis_test):
    esc = await escenario(db, clientes[0].id, por_zona=5)
    evento = await evento_de(db, esc["performance_id"])
    await redis_test.set(clave_sala_activa(evento), "1")
    return esc["performance_id"], esc["seat_ids"], evento


# --------------------------------------------------------------------------
# El pase
# --------------------------------------------------------------------------


async def test_pase_ida_y_vuelta():
    evento = uuid.uuid4()
    pase = leer_pase(emitir_pase(evento, 600, LLAVE), LLAVE)

    assert pase.evento_uuid == evento
    assert abs(pase.vence - (time.time() + 600)) < 5


async def test_pase_alterado_o_ajeno_no_se_lee():
    texto = emitir_pase(uuid.uuid4(), 600, LLAVE)
    version, evento, ident, vence, firma = texto.split(".")
    otro = emitir_pase(uuid.uuid4(), 600, LLAVE).split(".")[1]

    for malo in [
        f"{version}.{evento}.{ident}.{vence}9.{firma}",  # alargar la vigencia
        f"{version}.{otro}.{ident}.{vence}.{firma}",  # cambiar de evento
        emitir_pase(uuid.uuid4(), 600, "otra-llave-que-no-es-la-de-palco-0000"),
        "",
        "T1.a.b.c",
        texto + ".extra",
    ]:
        with pytest.raises(PaseInvalido):
            leer_pase(malo, LLAVE)


# --------------------------------------------------------------------------
# La puerta
# --------------------------------------------------------------------------


async def test_apagada_no_pide_nada(api, sala_activa, monkeypatch):
    monkeypatch.setattr(settings, "SALA_DE_ESPERA_HABILITADA", False)
    pid, seats, _ = sala_activa

    _, r = await apartar(api, pid, seats[0])

    assert r.status_code == 201, r.text


async def test_sin_sala_activa_no_pide_nada(api, db, clientes):
    esc = await escenario(db, clientes[0].id)

    _, r = await apartar(api, esc["performance_id"], esc["seat_ids"][0])

    assert r.status_code == 201, r.text


async def test_con_la_sala_activa_sin_pase_no_se_aparta(api, sala_activa):
    pid, seats, _ = sala_activa

    _, r = await apartar(api, pid, seats[0])

    assert r.status_code == 403
    error = r.json()["error"]
    assert error["codigo"] == "turno_requerido"
    assert "no garantiza boletos" in error["mensaje"]


@pytest.mark.parametrize("caso", ["alterado", "otro_evento"])
async def test_pase_que_no_sirve(api, sala_activa, caso):
    pid, seats, evento = sala_activa
    if caso == "alterado":
        pase = emitir_pase(evento, 600)[:-2] + "AA"
    else:
        pase = emitir_pase(uuid.uuid4(), 600)

    _, r = await apartar(api, pid, seats[0], pase=pase)

    assert r.status_code == 403
    assert r.json()["error"]["codigo"] == "turno_requerido"


async def test_pase_vencido(api, sala_activa):
    pid, seats, evento = sala_activa

    _, r = await apartar(api, pid, seats[0], pase=emitir_pase(evento, -1))

    assert r.status_code == 403
    assert r.json()["error"]["codigo"] == "turno_vencido"
    assert "sala de espera" in r.json()["error"]["mensaje"]


async def test_con_pase_valido_se_aparta(api, sala_activa):
    pid, seats, evento = sala_activa

    _, r = await apartar(api, pid, seats[0], pase=emitir_pase(evento, 600))

    assert r.status_code == 201, r.text


async def test_quien_ya_compraba_sigue_sin_pase(api, sala_activa, redis_test):
    pid, seats, evento = sala_activa
    await redis_test.delete(clave_sala_activa(evento))
    hold, r = await apartar(api, pid, seats[0])
    assert r.status_code == 201, r.text

    # Se activa la sala a media compra: puede seguir sumando lugares.
    await redis_test.set(clave_sala_activa(evento), "1")
    _, r = await apartar(api, pid, seats[1], hold=hold)

    assert r.status_code == 201, r.text


async def test_un_pase_no_abre_apartados_sin_fin(api, sala_activa):
    pid, seats, evento = sala_activa
    pase = emitir_pase(evento, 600)

    holds = []
    for s in seats[:APARTADOS_POR_PASE]:
        hold, r = await apartar(api, pid, s, pase=pase)
        assert r.status_code == 201, r.text
        holds.append(hold)

    _, r = await apartar(api, pid, seats[APARTADOS_POR_PASE], pase=pase)
    assert r.status_code == 403
    assert r.json()["error"]["codigo"] == "turno_usado"

    # Repetir con un apartado que el pase ya abrio no cuenta como otro (la
    # de junto: una compra va en lugares seguidos).
    _, r = await apartar(api, pid, seats[APARTADOS_POR_PASE], hold=holds[-1], pase=pase)
    assert r.status_code == 201, r.text
