"""Correo con los boletos y la bandeja que lo manda (ADR 0015).

Lo que importa:

- El correo es el diseno de Nadia y no lleva QR ni codigos: lleva a
  "Ver mis boletos", donde el QR cambia cada 30 s (BOL3).
- Las cifras son las de la orden: Subtotal + IVA - Descuento = Total.
- Un correo que no sale se reintenta, con espera creciente, y al final
  se avisa. Nunca se manda dos veces en vueltas normales.
- Si la orden ya no esta pagada cuando toca mandarlo, no se manda.
"""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email import message_from_bytes

import pytest
from sqlalchemy import func, select, update

from app.core.config import settings
from app.models.catalog import Event, Performance
from app.models.outbox import OutboxEvent
from app.models.sales import Order
from app.services.bandeja import MAX_INTENTOS, BandejaService, espera_tras
from app.services.confirmacion import (
    armar_confirmacion,
    fecha_corta,
    hora_corta,
    pesos,
)
from app.services.correo import CorreoNoEnviado, Mensaje, armar_mime
from tests.test_boletos import boletos_en_base, orden_pagada
from tests.test_checkout import PasarelaFalsa, orden_iniciada

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


class CarteroFalso:
    def __init__(self) -> None:
        self.enviados: list[Mensaje] = []
        self.fallas_pendientes = 0

    async def enviar(self, mensaje: Mensaje) -> None:
        if self.fallas_pendientes:
            self.fallas_pendientes -= 1
            raise CorreoNoEnviado("421 servicio no disponible")
        self.enviados.append(mensaje)


def bandeja(db, cartero) -> BandejaService:
    @asynccontextmanager
    async def sesiones():
        yield db

    return BandejaService(sesiones, cartero)


async def hecho_de(db, orden_uuid, tipo="orden.pagada") -> OutboxEvent:
    return await db.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.agregado_uuid == orden_uuid, OutboxEvent.tipo == tipo)
        .execution_options(populate_existing=True)
    )


# --------------------------------------------------------------------------
# Formatos
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "fecha", "hora"),
    [
        (datetime(2026, 11, 21, 1, 0, tzinfo=UTC), "vie, 20 nov 2026", "19:00 h"),
        (datetime(2026, 10, 23, 22, 0, tzinfo=UTC), "vie, 23 oct 2026", "16:00 h"),
        (datetime(2027, 1, 3, 2, 5, tzinfo=UTC), "sáb, 2 ene 2027", "20:05 h"),
    ],
)
async def test_fecha_y_hora_del_recinto(utc, fecha, hora):
    assert fecha_corta(utc, "America/Mexico_City") == fecha
    assert hora_corta(utc, "America/Mexico_City") == hora


async def test_pesos():
    assert pesos(Decimal("1972.00")) == "$1,972.00"
    assert pesos(Decimal("986.5")) == "$986.50"


async def test_espera_creciente_con_tope():
    assert [espera_tras(n).total_seconds() for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert espera_tras(20) == timedelta(hours=1)


# --------------------------------------------------------------------------
# El correo
# --------------------------------------------------------------------------


async def test_el_correo_lleva_a_mis_boletos_y_no_trae_qr(
    db,
    clientes,
    hold_service,
    stripe_falso,
    monkeypatch,
):
    monkeypatch.setattr(settings, "FRONT_URL", "https://palco.mx/")
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)

    m = await armar_confirmacion(db, orden.uuid)

    assert m.para == "ana@correo.mx"
    assert m.asunto.startswith("¡Listo! Tus boletos para ")
    assert m.imagenes == [], "sin QR: el del correo serviria para siempre"
    boletos = await boletos_en_base(db, orden.id)
    for parte in (m.html, m.texto):
        assert f"https://palco.mx/orden/{orden.uuid}" in parte
        assert "Este correo no sirve para entrar al evento" in parte
        assert "las capturas de pantalla no sirven" in parte
        assert orden.folio in parte
        for b in boletos:
            assert b.codigo not in parte, "el codigo impreso no sale"
            assert str(b.uuid) not in parte
    assert "PAL1" not in m.html and "PAL2" not in m.html
    assert f"https://palco.mx/orden/{orden.uuid}/correo" in m.html
    assert "https://palco.mx/img/correo/logo-psd-blanco.png" in m.html

    # $800 con 16 % incluido, dos lugares (mismas cifras que el checkout).
    for parte in (m.html, m.texto):
        assert "2 \u00d7 " in parte and "$800.00 por boleto" in m.html
        assert "$1,379.32" in parte
        assert "IVA incluido (16%)" in parte
        assert "$220.68" in parte
        assert "$1,600.00" in parte
        assert "Descuento" not in parte
        assert "Pagado el " in parte
        # La tarjeta espera autorizacion (zona critica).
        assert "Pagado con" not in parte
        # Sin cuentas de comprador todavia (R1): ni invitacion ni "Mi cuenta".
        assert "Crear mi cuenta" not in parte and "Mi cuenta" not in parte


async def test_el_pie_solo_lleva_lo_que_esta_configurado(
    db, clientes, hold_service, stripe_falso, monkeypatch
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    m = await armar_confirmacion(db, orden.uuid)
    assert "[Razón social]" not in m.html and "TU-DOMINIO" not in m.html
    assert "Escríbenos" not in m.html, "sin correo de soporte, sin renglon"

    monkeypatch.setattr(settings, "SOPORTE_EMAIL", "ayuda@psdevents.com")
    monkeypatch.setattr(settings, "EMPRESA_RAZON_SOCIAL", "PSD Digital SA de CV")
    monkeypatch.setattr(settings, "EMPRESA_DOMICILIO", "Puebla, Pue.")
    monkeypatch.setattr(settings, "REDES_SOCIALES", [])
    m = await armar_confirmacion(db, orden.uuid)
    assert "mailto:ayuda@psdevents.com" in m.html
    assert "PSD Digital SA de CV, Puebla, Pue." in m.html
    assert "PSD Digital SA de CV, Puebla, Pue." in m.texto
    assert "Síguenos" not in m.html


async def test_el_nombre_del_evento_no_inyecta_html(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    del_evento = select(Performance.event_id).where(
        Performance.id == orden.performance_id
    )
    await db.execute(
        update(Event)
        .where(Event.id == del_evento.scalar_subquery())
        .values(nombre='Circo <script>alert("x")</script>')
    )

    m = await armar_confirmacion(db, orden.uuid)

    assert "<script>" not in m.html
    assert "&lt;script&gt;" in m.html


async def test_sin_pagar_o_reembolsada_no_hay_correo(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    assert await armar_confirmacion(db, iniciada.orden.uuid) is None

    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[1].id)
    await db.execute(
        update(Order).where(Order.id == orden.id).values(estado="reembolsada")
    )
    assert await armar_confirmacion(db, orden.uuid) is None


async def test_mime_texto_y_html(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    m = await armar_confirmacion(db, orden.uuid)

    crudo = armar_mime(m, "Palco <boletos@palco.mx>").as_bytes()
    correo = message_from_bytes(crudo)

    assert correo["To"] == "ana@correo.mx"
    assert correo["Message-ID"].endswith("@palco.mx>")
    assert correo.get_content_type() == "multipart/alternative"
    texto, html = correo.get_payload()
    assert texto.get_content_type() == "text/plain"
    assert html.get_content_type() in ("text/html", "multipart/related")


# --------------------------------------------------------------------------
# La bandeja
# --------------------------------------------------------------------------


async def test_se_manda_una_vez(db, clientes, hold_service, stripe_falso):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    cartero = CarteroFalso()

    r = await bandeja(db, cartero).una_vuelta()
    otra = await bandeja(db, cartero).una_vuelta()

    assert (r.entregados, otra.entregados) == (1, 0)
    assert [m.para for m in cartero.enviados] == ["ana@correo.mx"]
    hecho = await hecho_de(db, orden.uuid)
    assert hecho.publicado_en is not None
    assert hecho.intentos == 0


async def test_falla_espera_y_reintenta(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    cartero = CarteroFalso()
    cartero.fallas_pendientes = 1

    r = await bandeja(db, cartero).una_vuelta()

    assert (r.entregados, r.reintentos) == (0, 1)
    hecho = await hecho_de(db, orden.uuid)
    assert hecho.publicado_en is None
    assert hecho.intentos == 1
    assert "421" in hecho.ultimo_error
    assert hecho.reintentar_en > await db.scalar(select(func.now()))

    # Antes de la hora no se vuelve a intentar.
    assert (await bandeja(db, cartero).una_vuelta()).hubo_algo() is False
    assert cartero.enviados == []

    await db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == hecho.id)
        .values(reintentar_en=func.now() - timedelta(seconds=1))
    )
    r = await bandeja(db, cartero).una_vuelta()

    assert r.entregados == 1
    assert len(cartero.enviados) == 1
    hecho = await hecho_de(db, orden.uuid)
    assert hecho.publicado_en is not None
    assert hecho.ultimo_error is None


async def test_tras_el_ultimo_intento_avisa_y_se_detiene(
    db,
    clientes,
    hold_service,
    stripe_falso,
    caplog,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    hecho = await hecho_de(db, orden.uuid)
    await db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == hecho.id)
        .values(intentos=MAX_INTENTOS - 1)
    )
    cartero = CarteroFalso()
    cartero.fallas_pendientes = 99

    with caplog.at_level(logging.ERROR, logger="palco.bandeja"):
        r = await bandeja(db, cartero).una_vuelta()

    assert r.abandonados == 1
    assert any("sin entregar" in m for m in caplog.messages)
    assert "ana@correo.mx" not in caplog.text, "el registro no guarda correos"
    hecho = await hecho_de(db, orden.uuid)
    assert hecho.intentos == MAX_INTENTOS

    assert (await bandeja(db, cartero).una_vuelta()).hubo_algo() is False


async def test_orden_reembolsada_antes_del_correo(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await db.execute(
        update(Order).where(Order.id == orden.id).values(estado="reembolsada")
    )
    cartero = CarteroFalso()

    r = await bandeja(db, cartero).una_vuelta()

    assert r.entregados == 1, "el hecho queda atendido"
    assert cartero.enviados == [], "pero no se manda nada"


async def test_hechos_sin_quien_los_atienda_no_se_tocan(
    db,
    clientes,
    hold_service,
    stripe_falso,
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    db.add(OutboxEvent(tipo="orden.cancelada", agregado_uuid=orden.uuid, payload={}))
    await db.flush()

    await bandeja(db, CarteroFalso()).una_vuelta()

    otro = await hecho_de(db, orden.uuid, tipo="orden.cancelada")
    assert (otro.publicado_en, otro.intentos) == (None, 0)


async def test_ver_en_el_navegador(
    cliente_http, db, clientes, hold_service, stripe_falso
):
    import uuid as _uuid

    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    r = await cliente_http.get(f"/api/v1/publico/ordenes/{orden.uuid}/correo")
    assert r.status_code == 200, r.text
    assert r.json()["html"] == (await armar_confirmacion(db, orden.uuid)).html
    assert r.json()["asunto"].startswith("¡Listo!")

    r = await cliente_http.get(f"/api/v1/publico/ordenes/{_uuid.uuid4()}/correo")
    assert r.status_code == 404
