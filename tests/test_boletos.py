"""El QR del boleto y la vista "Tus boletos" (ADR 0014).

Lo que importa:

- Nadie fabrica un QR valido sin la llave, ni reusa la firma de un
  boleto para otro.
- Solo un boleto que puede entrar lleva QR.
- La vista no expone datos personales: el QR y el uuid son opacos.
"""

import json
import uuid

import pytest
from sqlalchemy import select, update

from app.models.sales import Order, OrderItem, Ticket
from app.services.boletos import QRInvalido, contenido_del_qr, leer_qr
from tests.test_checkout import (
    API,
    orden_iniciada,
    stripe_falso,  # noqa: F401  (fixture)
)

pytestmark = pytest.mark.asyncio

LLAVE = "llave-de-pruebas-para-boletos-0123456789"


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
    assert set(cuerpo) == {"folio", "estado", "evento", "funcion", "recinto", "boletos"}
    assert cuerpo["evento"]["nombre"]
    assert cuerpo["recinto"]["zona_horaria"] == "America/Mexico_City"

    en_base = await boletos_en_base(db, orden.id)
    assert len(cuerpo["boletos"]) == len(en_base) == 2
    for visto, real in zip(cuerpo["boletos"], en_base, strict=True):
        assert visto["uuid"] == str(real.uuid)
        assert visto["codigo"] == real.codigo
        assert visto["estado"] == "valido"
        assert visto["zona"] and visto["asiento"]
        # El QR que ve el comprador es el que la puerta va a aceptar.
        assert leer_qr(visto["qr"]) == real.uuid

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
    assert [b["qr"] is None for b in boletos] == [True, True, False]


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
    assert all(b["qr"] is None for b in r.json()["boletos"])


async def test_orden_inexistente(cliente_http):
    r = await cliente_http.get(f"{API}/ordenes/{uuid.uuid4()}/boletos")

    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "no_encontrado"
