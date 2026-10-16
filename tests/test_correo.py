"""Correo con los boletos y la bandeja que lo manda (ADR 0015).

Lo que importa:

- El correo trae un QR por lugar, y cada QR es el que la puerta acepta.
- Las cifras son las de la orden: Subtotal + IVA = Total.
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
from app.services.boletos import leer_qr
from app.services.confirmacion import armar_confirmacion, fecha_larga, pesos, tasa
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


DIA = "viernes 20 de noviembre de 2026"


@pytest.mark.parametrize(
    ("utc", "esperado"),
    [
        (datetime(2026, 11, 21, 1, 0, tzinfo=UTC), f"{DIA}, 7:00 p. m."),
        (datetime(2026, 11, 20, 15, 30, tzinfo=UTC), f"{DIA}, 9:30 a. m."),
        (datetime(2026, 11, 20, 6, 0, tzinfo=UTC), f"{DIA}, 12:00 a. m."),
        (datetime(2026, 11, 20, 18, 5, tzinfo=UTC), f"{DIA}, 12:05 p. m."),
        (
            datetime(2027, 1, 3, 2, 0, tzinfo=UTC),
            "sábado 2 de enero de 2027, 8:00 p. m.",
        ),
    ],
)
async def test_fecha_a_la_hora_del_recinto(utc, esperado):
    assert fecha_larga(utc, "America/Mexico_City") == esperado


async def test_tasas_y_pesos():
    assert tasa(Decimal("16.00")) == "16 %"
    assert tasa(Decimal("10.00")) == "10 %"
    assert tasa(Decimal("8.50")) == "8.5 %"
    assert tasa(Decimal("0.00")) == "0 %"
    assert pesos(Decimal("1972.00")) == "$1,972.00"
    assert pesos(Decimal("986.5")) == "$986.50"


async def test_espera_creciente_con_tope():
    assert [espera_tras(n).total_seconds() for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert espera_tras(20) == timedelta(hours=1)


# --------------------------------------------------------------------------
# El correo
# --------------------------------------------------------------------------


async def test_un_qr_por_lugar_y_las_cifras_de_la_orden(
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
    assert orden.folio in m.asunto
    assert m.asunto.startswith("Tus boletos para ")

    boletos = await boletos_en_base(db, orden.id)
    assert len(m.imagenes) == len(boletos) == 2
    for img, boleto in zip(m.imagenes, boletos, strict=True):
        assert leer_qr(img.texto) == boleto.uuid, "el QR del correo entra por la puerta"
        assert img.png.startswith(b"\x89PNG\r\n\x1a\n")
        assert f'src="cid:{img.cid}"' in m.html
        assert boleto.codigo in m.html
        assert boleto.codigo in m.texto

    # $800 con 16 % incluido, dos lugares (mismas cifras que el checkout).
    for parte in (m.html, m.texto):
        assert "$1,379.32" in parte
        assert "IVA (16 %)" in parte
        assert "$220.68" in parte
        assert "$1,600.00" in parte
        assert "Descuento" not in parte
        assert "está confirmada" in parte
    assert f"https://palco.mx/boletos/{orden.uuid}" in m.html


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


async def test_mime_texto_html_e_imagenes_pegadas(
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
    assert html.get_content_type() == "multipart/related"
    cuerpo, *imagenes = html.get_payload()
    assert cuerpo.get_content_type() == "text/html"
    assert [i["Content-ID"] for i in imagenes] == [f"<{x.cid}>" for x in m.imagenes]
    assert all(i.get_content_type() == "image/png" for i in imagenes)


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
