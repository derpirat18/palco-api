"""El QR del boleto y la vista "Tus boletos" (ADR 0014, BOL3).

Lo que importa:

- Nadie fabrica un QR valido sin la llave, ni reusa la firma de un
  boleto para otro.
- El QR cambia cada 30 s: el de hace mas de un minuto ya no sirve.
- Solo un boleto que puede entrar lleva llave para su QR.
- La vista no expone datos personales ni el codigo impreso.
"""

import base64
import json
import time
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.models.sales import Order, OrderItem, Ticket
from app.services.boletos import (
    PASO,
    QRInvalido,
    contenido_del_qr,
    leer_cualquier_qr,
    leer_qr,
    llave_rotatoria,
    paso_de,
    qr_rotatorio,
    verificar_rotatorio,
)
from tests.test_checkout import (
    API,
    orden_iniciada,
    stripe_falso,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.asyncio

LLAVE = "llave-de-pruebas-para-boletos-0123456789"


def qr_de(boleto: Ticket, desfase: int = 0, llave: str | None = None) -> str:
    """El QR que muestra el celular ahora (o `desfase` pasos antes/despues)."""
    llave_boleto = llave_rotatoria(
        boleto.uuid, boleto.semilla, llave or settings.TICKET_SIGNING_KEY
    )
    return qr_rotatorio(boleto.uuid, llave_boleto, paso_de(time.time()) + desfase)


# --------------------------------------------------------------------------
# El QR
# --------------------------------------------------------------------------


async def test_ida_y_vuelta():
    boleto = uuid.uuid4()
    qr = contenido_del_qr(boleto, LLAVE)

    assert qr.startswith("PAL1.")
    assert len(qr) == 50, "corto para que el escaner lo lea a la primera"
    assert leer_qr(qr, LLAVE) == boleto
    assert leer_qr(f"  {qr}\n", LLAVE) == boleto, "el escaner a veces agrega espacios"


async def test_firma_alterada_no_pasa():
    qr = contenido_del_qr(uuid.uuid4(), LLAVE)
    version, ident, firma = qr.split(".")
    # El primer caracter de la firma siempre cambia bytes reales; el ultimo
    # de un base64 de 16 bytes lleva bits de relleno y podria no cambiar nada.
    otra = ("B" if firma[0] == "A" else "A") + firma[1:]

    with pytest.raises(QRInvalido):
        leer_qr(f"{version}.{ident}.{otra}", LLAVE)


async def test_la_firma_de_un_boleto_no_sirve_para_otro():
    _, ident_a, _ = contenido_del_qr(uuid.uuid4(), LLAVE).split(".")
    _, _, firma_b = contenido_del_qr(uuid.uuid4(), LLAVE).split(".")

    with pytest.raises(QRInvalido):
        leer_qr(f"PAL1.{ident_a}.{firma_b}", LLAVE)


async def test_sin_la_llave_no_se_fabrica():
    boleto = uuid.uuid4()
    falsificado = contenido_del_qr(boleto, "otra-llave-que-no-es-la-de-palco-000000")

    with pytest.raises(QRInvalido):
        leer_qr(falsificado, LLAVE)


@pytest.mark.parametrize(
    "texto",
    [
        "",
        "hola",
        "https://palco.mx/boleto/123",
        "PAL1.x.y",
        "PAL1.@@@.###",
        "PAL1..",
    ],
)
async def test_lo_que_no_es_un_boleto(texto):
    with pytest.raises(QRInvalido):
        leer_qr(texto, LLAVE)


async def test_otra_version_o_partes_de_mas():
    qr = contenido_del_qr(uuid.uuid4(), LLAVE)

    with pytest.raises(QRInvalido):
        leer_qr("PAL2" + qr[4:], LLAVE)
    with pytest.raises(QRInvalido):
        leer_qr(qr + ".extra", LLAVE)


# --------------------------------------------------------------------------
# El QR que cambia (BOL3)
# --------------------------------------------------------------------------

SEMILLA = bytes(range(32))


def rotatorio(boleto, paso, llave=LLAVE, semilla=SEMILLA) -> str:
    return qr_rotatorio(boleto, llave_rotatoria(boleto, semilla, llave), paso)


async def test_qr_rotatorio_corto_y_legible():
    boleto, ahora = uuid.uuid4(), time.time()
    qr = rotatorio(boleto, paso_de(ahora))

    assert qr.startswith("PAL2.")
    assert len(qr) <= 52, "corto para que el escaner lo lea a la primera"
    leido = leer_cualquier_qr(f" {qr}\n", LLAVE)
    assert leido.boleto_uuid == boleto and not leido.es_fijo
    assert verificar_rotatorio(leido, SEMILLA, ahora, LLAVE) is None


async def test_cambia_cada_30_segundos():
    boleto, ahora = uuid.uuid4(), time.time()
    assert PASO == 30
    assert rotatorio(boleto, paso_de(ahora)) != rotatorio(boleto, paso_de(ahora) + 1)


@pytest.mark.parametrize(
    ("desfase", "esperado"),
    [
        (0, None),
        (-1, None),
        (1, None),
        (-2, "vencido"),
        (2, "vencido"),
        (-120, "vencido"),
    ],
)
async def test_solo_sirve_cerca_de_ahora(desfase, esperado):
    """Una captura deja de servir al minuto; un reloj un poco desfasado si pasa."""
    boleto, ahora = uuid.uuid4(), time.time()
    leido = leer_cualquier_qr(rotatorio(boleto, paso_de(ahora) + desfase), LLAVE)
    assert verificar_rotatorio(leido, SEMILLA, ahora, LLAVE) == esperado


async def test_rotatorio_falso_no_pasa():
    boleto, ahora = uuid.uuid4(), time.time()
    paso = paso_de(ahora)
    viejo, actual = (
        rotatorio(boleto, paso - 5).split("."),
        rotatorio(boleto, paso).split("."),
    )
    casos = [
        rotatorio(boleto, paso, semilla=bytes(32)),  # otra semilla
        rotatorio(boleto, paso, llave="otra-llave-que-no-es-la-de-palco-0000"),
        # Una captura vieja con el paso de ahora: la firma no cuadra.
        ".".join([*viejo[:2], actual[2], viejo[3]]),
    ]
    for qr in casos:
        leido = leer_cualquier_qr(qr, LLAVE)
        assert verificar_rotatorio(leido, SEMILLA, ahora, LLAVE) == "falso", qr


async def test_la_llave_de_un_boleto_no_sirve_para_otro():
    a, b, ahora = uuid.uuid4(), uuid.uuid4(), time.time()
    llave_a = llave_rotatoria(a, SEMILLA, LLAVE)
    qr = qr_rotatorio(b, llave_a, paso_de(ahora))
    assert (
        verificar_rotatorio(leer_cualquier_qr(qr, LLAVE), SEMILLA, ahora, LLAVE)
        == "falso"
    )


async def test_el_qr_fijo_se_reconoce_como_viejo():
    boleto = uuid.uuid4()
    leido = leer_cualquier_qr(contenido_del_qr(boleto, LLAVE), LLAVE)
    assert leido.es_fijo and leido.boleto_uuid == boleto


@pytest.mark.parametrize(
    "texto", ["PAL2.x.y.z", "PAL2...", "PAL2.@@.1.##", "PAL2.AAAA.zzzzzzzzzzzzzz.AAAA"]
)
async def test_rotatorio_mal_formado(texto):
    with pytest.raises(QRInvalido):
        leer_cualquier_qr(texto, LLAVE)


# --------------------------------------------------------------------------
# "Tus boletos"
# --------------------------------------------------------------------------


async def orden_pagada(db, holds, stripe, cliente_id, n=2):
    _, _, servicio, iniciada = await orden_iniciada(
        db, holds, stripe, cliente_id, n=n, email="ana@correo.mx"
    )
    stripe.autorizar(iniciada.orden.stripe_payment_intent_id)
    return await servicio.finalizar(iniciada.orden.id)


async def boletos_en_base(db, orden_id) -> list[Ticket]:
    return list(
        (
            await db.scalars(
                select(Ticket)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .where(OrderItem.order_id == orden_id)
                .order_by(OrderItem.id)
            )
        ).all()
    )


async def test_orden_pagada_trae_un_qr_por_lugar(
    cliente_http,
    db,
    clientes,
    hold_service,
    stripe_falso,  # noqa: F811
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)

    r = await cliente_http.get(f"{API}/ordenes/{orden.uuid}/boletos")

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["folio"] == orden.folio
    assert cuerpo["estado"] == "pagada"
    assert set(cuerpo) == {
        "folio", "estado", "evento", "funcion", "recinto", "boletos",
        "servidor_ahora", "paso_segundos",
    }  # fmt: skip
    assert cuerpo["paso_segundos"] == 30
    assert (
        abs(datetime.fromisoformat(cuerpo["servidor_ahora"]).timestamp() - time.time())
        < 60
    )
    assert cuerpo["evento"]["nombre"]
    assert cuerpo["recinto"]["zona_horaria"] == "America/Mexico_City"

    en_base = await boletos_en_base(db, orden.id)
    assert len(cuerpo["boletos"]) == len(en_base) == 2
    for visto, real in zip(cuerpo["boletos"], en_base, strict=True):
        assert visto["uuid"] == str(real.uuid)
        assert visto["estado"] == "valido"
        assert visto["zona"] and visto["asiento"]
        assert visto["entro_en"] is None
        # Con la llave que recibe, el celular genera el QR que la puerta acepta.
        llave = base64.urlsafe_b64decode(visto["llave_qr"] + "=")
        qr = qr_rotatorio(real.uuid, llave, paso_de(time.time()))
        assert (
            verificar_rotatorio(leer_cualquier_qr(qr), real.semilla, time.time())
            is None
        )
        assert real.codigo not in json.dumps(visto), "el codigo impreso no sale"

    texto = json.dumps(cuerpo)
    assert "ana@correo.mx" not in texto
    assert "Ana" not in texto, "ni el nombre del comprador"


async def test_orden_sin_pagar_no_trae_boletos(
    cliente_http,
    db,
    clientes,
    hold_service,
    stripe_falso,  # noqa: F811
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )

    r = await cliente_http.get(f"{API}/ordenes/{iniciada.orden.uuid}/boletos")

    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "pendiente"
    assert r.json()["boletos"] == []


async def test_boleto_cancelado_o_usado_no_lleva_qr(
    cliente_http,
    db,
    clientes,
    hold_service,
    stripe_falso,  # noqa: F811
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id, n=3)
    primero, segundo, _ = await boletos_en_base(db, orden.id)
    await db.execute(
        update(Ticket).where(Ticket.id == primero.id).values(estado="cancelado")
    )
    await db.execute(
        update(Ticket).where(Ticket.id == segundo.id).values(estado="usado")
    )
    await db.flush()

    r = await cliente_http.get(f"{API}/ordenes/{orden.uuid}/boletos")

    boletos = r.json()["boletos"]
    assert [b["estado"] for b in boletos] == ["cancelado", "usado", "valido"]
    assert [b["llave_qr"] is None for b in boletos] == [True, True, False]


async def test_orden_reembolsada_no_deja_entrar_a_nadie(
    cliente_http,
    db,
    clientes,
    hold_service,
    stripe_falso,  # noqa: F811
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await db.execute(
        update(Order).where(Order.id == orden.id).values(estado="reembolsada")
    )
    await db.flush()

    r = await cliente_http.get(f"{API}/ordenes/{orden.uuid}/boletos")

    assert r.json()["estado"] == "reembolsada"
    assert all(b["llave_qr"] is None for b in r.json()["boletos"])


async def test_orden_inexistente(cliente_http):
    r = await cliente_http.get(f"{API}/ordenes/{uuid.uuid4()}/boletos")

    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "no_encontrado"
