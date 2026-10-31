"""Tipos de boleto (TB): adulto, nino y adulto mayor.

Lo que importa:

- El organizador pone, por zona y funcion, precio de nino y de adulto mayor
  (o ninguno: entonces ese tipo no se vende ahi) y las edades del evento.
- En linea y en taquilla se cobra lo mismo por cada tipo, con el mismo IVA
  que el precio de adulto.
- Un tipo sin precio, o una butaca con precio puesto a mano, no se vende
  como nino ni adulto mayor: se rechaza, nunca se cobra el de adulto a
  escondidas.
- El tipo queda en la partida y en el asiento congelado ("Fila A · 1 ·
  Niño"): asi sale en el boleto y en la puerta, donde se revisa la edad.
- Cambiar los tipos en el checkout es otra orden, como cambiar lugares.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models.catalog import Event, PerformancePrice
from app.models.inventory import PerformanceSeat
from app.models.sales import Order, OrderItem
from app.services.checkout import CheckoutService
from app.services.tipos import TipoNoDisponible
from tests.test_apartados import funcion_a_la_venta
from tests.test_catalog import API, auth, token_de
from tests.test_checkout import PasarelaFalsa, apartar, comprador, recargar

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def esc(db, clientes):
    """Luneta $800 (nino $500, adulto mayor $600) y Balcon $400 sin tipos."""
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)
    luneta, _ = esc["zonas"]
    await db.execute(
        update(PerformancePrice)
        .where(
            PerformancePrice.performance_id == esc["performance_id"],
            PerformancePrice.zone_id == luneta,
        )
        .values(precio_nino=Decimal("500.00"), precio_mayor=Decimal("600.00"))
    )
    evento = await db.get(Event, esc["event_id"])
    evento.edad_nino_hasta = 12
    evento.edad_mayor_desde = 60
    await db.flush()
    esc["luneta"] = esc["seat_ids"][:5]
    esc["balcon"] = esc["seat_ids"][5:]
    return esc


# --------------------------------------------------------------------------
# El organizador los define
# --------------------------------------------------------------------------


async def test_precios_por_tipo_y_edades(cliente_http, usuarios, clientes, db):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    org = auth(token_de(usuarios["org_a"]))
    pid = esc["performance_id"]
    luneta, balcon = esc["zonas"]
    url = f"{API}/performances/{pid}/precios"

    r = await cliente_http.put(
        url,
        json={
            "precios": {str(luneta): "800.00", str(balcon): "400.00"},
            "tipos": {str(luneta): {"nino": "500.00", "mayor": "600.00"}},
        },
        headers=org,
    )
    assert r.status_code == 200, r.text
    por_zona = {p["zone_id"]: p for p in r.json()}
    assert por_zona[luneta]["precio_nino"] == "500.00"
    assert por_zona[luneta]["precio_mayor"] == "600.00"
    assert por_zona[balcon]["precio_nino"] is None

    # Sin "tipos" no se tocan (p. ej. al copiar solo el de adulto)...
    r = await cliente_http.put(
        url,
        json={"precios": {str(luneta): "850.00", str(balcon): "400.00"}},
        headers=org,
    )
    assert {p["zone_id"]: p["precio_nino"] for p in r.json()}[luneta] == "500.00"
    # ...y con "tipos" la zona que no aparece se queda sin ellos.
    r = await cliente_http.put(
        url,
        json={"precios": {str(luneta): "850.00", str(balcon): "400.00"}, "tipos": {}},
        headers=org,
    )
    assert all(p["precio_nino"] is None for p in r.json())

    # Precio negativo o zona ajena: no.
    r = await cliente_http.put(
        url,
        json={"precios": {str(luneta): "800"}, "tipos": {str(luneta): {"nino": "-1"}}},
        headers=org,
    )
    assert r.status_code == 422
    r = await cliente_http.put(
        url,
        json={"precios": {str(luneta): "800"}, "tipos": {"999999": {"nino": "1"}}},
        headers=org,
    )
    assert r.status_code == 409

    # Las edades van con el evento.
    r = await cliente_http.patch(
        f"{API}/events/{esc['event_id']}",
        json={"edad_nino_hasta": 12, "edad_mayor_desde": 60},
        headers=org,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["edad_nino_hasta"], r.json()["edad_mayor_desde"]) == (12, 60)
    r = await cliente_http.patch(
        f"{API}/events/{esc['event_id']}", json={"edad_nino_hasta": 30}, headers=org
    )
    assert r.status_code == 422


async def test_el_mapa_publico_dice_que_tipos_hay(cliente_http, hold_service, esc):
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    class SinCache:
        async def get(self, _):
            return None

        async def set(self, *a, **k):
            return None

    app.dependency_overrides[get_redis_publico] = lambda: SinCache()
    try:
        r = await cliente_http.get(
            f"{API}/publico/funciones/{esc['performance_id']}/mapa"
        )
    finally:
        app.dependency_overrides.pop(get_redis_publico, None)
    assert r.status_code == 200, r.text
    zonas = {z["nombre"]: z["tarifas"] for z in r.json()["zonas"]}
    assert zonas["Luneta"] == [
        {"tipo": "nino", "nombre": "Niño (hasta 12 años)", "precio": "500.00"},
        {"tipo": "mayor", "nombre": "Adulto mayor (60 años o más)", "precio": "600.00"},
    ]
    assert zonas["Balcon"] == []
    assert not any(b["solo_adulto"] for b in r.json()["butacas"])


# --------------------------------------------------------------------------
# En linea
# --------------------------------------------------------------------------


async def test_en_linea_cobra_cada_tipo(db, esc, hold_service, stripe_falso):
    a, b, c = esc["luneta"][:3]
    hold = await apartar(db, hold_service, esc, [a, b, c])
    iniciada = await CheckoutService(db, hold_service, stripe_falso).iniciar(
        esc["performance_id"], hold, comprador(), tipos={b: "nino", c: "mayor"}
    )
    partidas = {p.asiento_snap: (p.tipo, p.importe) for p in iniciada.partidas}
    assert partidas == {
        "Fila A · 1": ("adulto", Decimal("800.00")),
        "Fila A · 2 · Niño": ("nino", Decimal("500.00")),
        "Fila A · 3 · Adulto mayor": ("mayor", Decimal("600.00")),
    }
    assert iniciada.orden.total == Decimal("1900.00")
    intento = stripe_falso.intentos[iniciada.orden.stripe_payment_intent_id]
    assert intento.monto_centavos == 190000


async def test_mas_iva_tambien_en_los_tipos(db, esc, hold_service, stripe_falso):
    evento = await db.get(Event, esc["event_id"])
    evento.precios_incluyen_iva = False
    await db.flush()
    a = esc["luneta"][0]
    hold = await apartar(db, hold_service, esc, [a])
    iniciada = await CheckoutService(db, hold_service, stripe_falso).iniciar(
        esc["performance_id"], hold, comprador(), tipos={a: "nino"}
    )
    # 500 + 16 %.
    assert iniciada.partidas[0].importe == Decimal("580.00")


async def test_sin_precio_de_ese_tipo_no_se_vende(db, esc, hold_service, stripe_falso):
    servicio = CheckoutService(db, hold_service, stripe_falso)
    balcon = esc["balcon"][0]
    hold = await apartar(db, hold_service, esc, [balcon])
    with pytest.raises(TipoNoDisponible) as e:
        await servicio.iniciar(
            esc["performance_id"], hold, comprador(), tipos={balcon: "nino"}
        )
    assert e.value.detalle["motivo"] == "sin_precio"
    assert await db.scalar(select(Order.id).where(Order.hold_id == hold)) is None

    # Butaca con precio puesto a mano: solo adulto.
    luneta = esc["luneta"][0]
    await db.execute(
        update(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == esc["performance_id"],
            PerformanceSeat.seat_id == luneta,
        )
        .values(precio_final=Decimal("999.00"), precio_personalizado=True)
    )
    hold = await apartar(db, hold_service, esc, [luneta])
    with pytest.raises(TipoNoDisponible):
        await servicio.iniciar(
            esc["performance_id"], hold, comprador(), tipos={luneta: "mayor"}
        )
    # Un lugar que no es del apartado tampoco.
    with pytest.raises(TipoNoDisponible):
        await servicio.iniciar(
            esc["performance_id"], hold, comprador(), tipos={esc["luneta"][4]: "nino"}
        )


async def test_cambiar_el_tipo_es_otra_orden(db, esc, hold_service, stripe_falso):
    servicio = CheckoutService(db, hold_service, stripe_falso)
    a, b = esc["luneta"][:2]
    hold = await apartar(db, hold_service, esc, [a, b])
    primera = await servicio.iniciar(esc["performance_id"], hold, comprador())
    misma = await servicio.iniciar(
        esc["performance_id"], hold, comprador(), tipos={a: "adulto"}
    )
    assert misma.orden.id == primera.orden.id

    otra = await servicio.iniciar(
        esc["performance_id"], hold, comprador(), tipos={b: "nino"}
    )
    vieja = await recargar(db, Order, primera.orden.id)
    assert otra.orden.id != vieja.id and vieja.estado == "cancelada"
    assert otra.orden.total == Decimal("1300.00")
    assert len(stripe_falso.intentos) == 2


# --------------------------------------------------------------------------
# En taquilla
# --------------------------------------------------------------------------


async def test_taquilla_cobra_lo_mismo(cliente_http, usuarios, esc, db):
    org = auth(token_de(usuarios["org_a"]))
    cajero = auth(token_de(usuarios["acceso"]))
    ev = esc["event_id"]
    r = await cliente_http.put(
        f"{API}/taquilla/eventos/{ev}/personal/{usuarios['acceso'].id}",
        json={"puesto": "cajero"},
        headers=org,
    )
    assert r.status_code == 204
    r = await cliente_http.post(
        f"{API}/taquilla/eventos/{ev}/cajas",
        json={"cajero_id": usuarios["acceso"].id, "fondo": "0"},
        headers=org,
    )
    caja = r.json()["uuid"]
    a, b = esc["luneta"][:2]
    cuerpo = {
        "performance_id": esc["performance_id"],
        "seat_ids": [a, b],
        "email": "familia@correo.mx",
        "nombre": "Ana",
        "forma_pago": "efectivo",
        "recibido": "1300.00",
        "acepta_aviso": True,
        "aviso_version": "2026-10",
        "clave": uuid4().hex,
        "tipos": {str(b): "nino"},
    }
    r = await cliente_http.post(
        f"{API}/taquilla/cajas/{caja}/ventas", json=cuerpo, headers=cajero
    )
    assert r.status_code == 201, r.text
    recibo = r.json()
    assert recibo["total"] == "1300.00" and recibo["cambio"] == "0.00"
    assert [x["asiento"] for x in recibo["lugares"]] == [
        "Fila A · 1",
        "Fila A · 2 · Niño",
    ]
    tipos = (
        await db.scalars(
            select(OrderItem.tipo)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.folio == recibo["folio"])
            .order_by(OrderItem.id)
        )
    ).all()
    assert tipos == ["adulto", "nino"]

    # Balcon no tiene precio de adulto mayor.
    cuerpo = {
        **cuerpo,
        "seat_ids": [esc["balcon"][0]],
        "tipos": {str(esc["balcon"][0]): "mayor"},
        "clave": uuid4().hex,
    }
    r = await cliente_http.post(
        f"{API}/taquilla/cajas/{caja}/ventas", json=cuerpo, headers=cajero
    )
    assert r.status_code == 409 and r.json()["error"]["codigo"] == "tipo_no_disponible"
