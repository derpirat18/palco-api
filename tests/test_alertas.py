"""Alertas por correo para PSD (A1, A4).

Lo que importa:

- Salen a todos los destinatarios de la lista, que PSD administra en el panel.
- La misma falla no llena el correo: una vez cada 30 min; un cobro sin
  boletos, una sola vez.
- Se avisa de: cobro sin boletos, correos que la bandeja ya no reintenta,
  procesos detenidos y fallas inesperadas (500).
- Solo PSD ve y cambia la lista; no se puede quedar vacia.
- Nada de datos personales en la alerta.
"""

import time

import pytest
from sqlalchemy import func, select, update

from app.core import fallas
from app.models.alertas import Alerta, DestinatarioDeAlertas
from app.models.outbox import OutboxEvent
from app.services import alertas
from app.services.bandeja import MAX_INTENTOS
from app.services.publico import PublicoService
from tests.test_catalog import API, auth, token_de
from tests.test_correo import CarteroFalso

pytestmark = pytest.mark.asyncio


class RedisFalso:
    def __init__(self, **latidos) -> None:
        ahora = int(time.time())
        self.d = {f"latido:{k}": ahora - hace for k, hace in latidos.items()}

    async def get(self, clave):
        return self.d.get(clave)


AL_DIA = {"barrido": 10, "bandeja": 5}


@pytest.fixture
async def lista(db):
    db.add_all(
        [
            DestinatarioDeAlertas(email="juanmanuel@psdigital.mx", nombre="JuanMa"),
            DestinatarioDeAlertas(email="nadia@psdigital.mx", nombre="Nadia"),
        ]
    )
    await db.flush()


async def bitacora(db) -> list[Alerta]:
    return list(
        (
            await db.scalars(
                select(Alerta)
                .order_by(Alerta.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


# --------------------------------------------------------------------------
# Enviar
# --------------------------------------------------------------------------


async def test_sale_a_toda_la_lista_y_no_se_repite(db, lista):
    cartero = CarteroFalso()

    a = await alertas.alertar(db, cartero, "x", "Algo pasó", "Detalle")
    otra = await alertas.alertar(db, cartero, "x", "Algo pasó", "Detalle")

    assert a.enviada_a == 2 and otra is None
    assert sorted(m.para for m in cartero.enviados) == [
        "juanmanuel@psdigital.mx",
        "nadia@psdigital.mx",
    ]
    assert cartero.enviados[0].asunto == "[PSD Boletera (local)] Algo pasó"

    # Pasada la espera, vuelve a avisar.
    await db.execute(
        update(Alerta).values(creada_en=func.now() - alertas.ESPERA - alertas.ESPERA)
    )
    assert await alertas.alertar(db, cartero, "x", "Algo pasó", "Detalle")
    # Un hecho unico, nunca dos veces.
    assert await alertas.alertar(db, cartero, "u", "Uno", "D", una_vez=True)
    await db.execute(update(Alerta).values(creada_en=func.now() - alertas.ESPERA * 9))
    assert await alertas.alertar(db, cartero, "u", "Uno", "D", una_vez=True) is None


async def test_la_prueba_siempre_sale_y_se_nota(db, lista):
    cartero = CarteroFalso()
    await alertas.alertar(db, cartero, "prueba", "Alerta de prueba", "x", prueba=True)
    await alertas.alertar(db, cartero, "prueba", "Alerta de prueba", "x", prueba=True)
    assert len(cartero.enviados) == 4
    assert cartero.enviados[0].asunto.startswith("[PSD Boletera (local)] Prueba: ")
    assert "no pasó nada" in cartero.enviados[0].texto


async def test_sin_destinatarios_queda_en_la_bitacora(db, caplog):
    a = await alertas.alertar(db, CarteroFalso(), "y", "Algo", "D")
    assert a.enviada_a == 0
    assert "sin destinatarios" in caplog.text


# --------------------------------------------------------------------------
# Revision periodica
# --------------------------------------------------------------------------


async def test_cobro_sin_boletos_se_avisa_una_vez(db, lista):
    import uuid

    db.add(
        OutboxEvent(
            tipo="orden.requiere_reembolso",
            agregado_uuid=uuid.uuid4(),
            payload={"folio": "PSD-ABCD1234", "email": "no@sale.mx"},
        )
    )
    await db.flush()
    cartero = CarteroFalso()

    assert await alertas.revisar(db, cartero, RedisFalso(**AL_DIA)) == 1
    assert await alertas.revisar(db, cartero, RedisFalso(**AL_DIA)) == 0

    (a,) = await bitacora(db)
    assert a.titulo == "Cobro sin boletos: folio PSD-ABCD1234"
    assert "reembolsarle" in a.detalle
    assert "no@sale.mx" not in a.detalle, "nada de datos personales"


async def test_correos_que_ya_no_se_reintentan(db, lista):
    import uuid

    db.add(
        OutboxEvent(
            tipo="orden.pagada",
            agregado_uuid=uuid.uuid4(),
            payload={"folio": "PSD-ZZZZ9999"},
            intentos=MAX_INTENTOS,
            ultimo_error="CorreoNoEnviado: 554 rechazado",
        )
    )
    await db.flush()

    assert await alertas.revisar(db, CarteroFalso(), RedisFalso(**AL_DIA)) == 1

    (a,) = await bitacora(db)
    assert a.titulo == "1 correo(s) sin entregar"
    assert "PSD-ZZZZ9999" in a.detalle and "554 rechazado" in a.detalle


async def test_proceso_detenido(db, lista):
    cartero = CarteroFalso()

    n = await alertas.revisar(db, cartero, RedisFalso(barrido=10, bandeja=900))

    assert n == 1
    (a,) = await bitacora(db)
    assert a.titulo == "El proceso «bandeja» está detenido"
    assert "15 min" in a.detalle and "correos de boletos" in a.detalle
    # Sin registro de que corra, tambien.
    await alertas.revisar(db, cartero, RedisFalso(bandeja=5))
    assert (await bitacora(db))[-1].titulo == "El proceso «barrido» está detenido"


async def test_todo_en_orden_no_avisa_nada(db, lista):
    cartero = CarteroFalso()
    assert await alertas.revisar(db, cartero, RedisFalso(**AL_DIA)) == 0
    assert cartero.enviados == []


# --------------------------------------------------------------------------
# Fallas de la API
# --------------------------------------------------------------------------


async def test_una_falla_inesperada_avisa_con_su_folio(cliente_http, monkeypatch):
    avisos = []

    async def explota(self, slug):
        raise RuntimeError("psycopg: connection reset")

    monkeypatch.setattr(PublicoService, "evento_por_slug", explota)
    monkeypatch.setattr(fallas, "al_fallar", lambda *a: avisos.append(a))

    r = await cliente_http.get(f"{API}/publico/eventos/cualquiera")

    assert r.status_code == 500
    ((folio, metodo, ruta, exc),) = avisos
    assert folio == r.json()["error"]["detalle"]["folio"]
    assert (metodo, ruta) == ("GET", "/api/v1/publico/eventos/cualquiera")
    assert isinstance(exc, RuntimeError)


async def test_en_local_y_en_pruebas_estan_apagadas(monkeypatch):
    from app.core.config import settings

    assert alertas.activas() is False
    monkeypatch.setattr(settings, "ENTORNO", "staging")
    assert alertas.activas() is True
    monkeypatch.setattr(settings, "ALERTAS_ACTIVAS", False)
    assert alertas.activas() is False


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------


@pytest.fixture
def cartero_del_panel():
    from app.api.v1.alertas import get_cartero
    from app.main import app

    cartero = CarteroFalso()
    app.dependency_overrides[get_cartero] = lambda: cartero
    return cartero


def psd(usuarios):
    return auth(token_de(usuarios["admin"]))


async def test_solo_psd(cliente_http, usuarios, lista):
    for quien in ("org_a", "acceso"):
        h = auth(token_de(usuarios[quien]))
        assert (await cliente_http.get(f"{API}/alertas", headers=h)).status_code == 403
        r = await cliente_http.post(
            f"{API}/alertas/destinatarios", json={"email": "x@y.mx"}, headers=h
        )
        assert r.status_code == 403


async def test_agregar_quitar_y_probar(
    cliente_http, usuarios, lista, cartero_del_panel
):
    h = psd(usuarios)
    r = await cliente_http.post(
        f"{API}/alertas/destinatarios",
        json={"email": "Soporte@PSDigital.MX", "nombre": "Soporte"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    correos = [d["email"] for d in r.json()["destinatarios"]]
    assert correos[-1] == "soporte@psdigital.mx" and len(correos) == 3

    r = await cliente_http.post(
        f"{API}/alertas/destinatarios",
        json={"email": "soporte@psdigital.mx"},
        headers=h,
    )
    assert r.status_code == 409

    r = await cliente_http.post(f"{API}/alertas/prueba", headers=h)
    assert r.status_code == 200
    assert len(cartero_del_panel.enviados) == 3
    assert r.json()["recientes"][0]["prueba"] is True

    ids = [d["id"] for d in r.json()["destinatarios"]]
    for i in ids[:-1]:
        r = await cliente_http.delete(f"{API}/alertas/destinatarios/{i}", headers=h)
        assert r.status_code == 200, r.text
    r = await cliente_http.delete(f"{API}/alertas/destinatarios/{ids[-1]}", headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "ultimo_destinatario"
