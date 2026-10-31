"""Entrada general (EG): zonas sin lugar numerado, en linea y en taquilla.

Lo que importa:

- PSD da de alta un mapa de entrada general: zonas con cupo. Sus lugares
  son internos (no se dibujan ni se muestran).
- El comprador escoge cuantos; el sistema aparta cupos con el mismo candado
  que las butacas (Redis, Postgres y el tope por compra) y nunca vende mas
  del cupo.
- Sin lugares numerados no aplican butacas sueltas ni lugares juntos.
- El boleto dice "Acceso general" (con su tipo si no es adulto).
- La taquilla vende cupos del mismo inventario.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import AsientoNoDisponible, LimiteDeAsientos
from app.models.base import EstadoEvento, EstadoFuncion
from app.models.catalog import Event, Performance, PerformancePrice, Venue, Zone
from app.models.inventory import PerformanceSeat
from app.services.apartados import ApartadoService
from app.services.checkout import CheckoutService
from app.services.inventory import InventoryService
from tests.test_apartados import nuevo_hold
from tests.test_catalog import API, auth, token_de
from tests.test_checkout import PasarelaFalsa, comprador

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def ga(cliente_http, db, usuarios, clientes):
    """Un mapa General (20) y VIP (3), con su funcion a la venta."""
    recinto = Venue(nombre=f"Explanada {uuid4().hex[:6]}", ciudad="Saltillo")
    db.add(recinto)
    await db.flush()
    psd = auth(token_de(usuarios["admin"]))
    r = await cliente_http.post(
        f"{API}/venues/{recinto.id}/seat-maps/general",
        json={
            "nombre": "Explanada",
            "zonas": [
                {"nombre": "General", "capacidad": 20},
                {"nombre": "VIP", "capacidad": 3, "color": "#C2334A"},
            ],
        },
        headers=psd,
    )
    assert r.status_code == 201, r.text
    mapa = r.json()
    assert (mapa["total_butacas"], mapa["total_vendibles"]) == (23, 23)
    zonas = (
        await db.scalars(
            select(Zone).where(Zone.seat_map_id == mapa["id"]).order_by(Zone.orden)
        )
    ).all()
    assert all(z.general for z in zonas)
    general, vip = zonas

    evento = Event(
        client_id=clientes[0].id,
        venue_id=recinto.id,
        nombre="Circo en explanada",
        slug=f"circo-explanada-{uuid4().hex[:6]}",
        estado=EstadoEvento.PUBLICADO.value,
    )
    db.add(evento)
    await db.flush()
    funcion = Performance(
        event_id=evento.id,
        seat_map_id=mapa["id"],
        inicia_en=datetime.now(UTC) + timedelta(days=10),
        estado=EstadoFuncion.PROGRAMADA.value,
    )
    db.add(funcion)
    await db.flush()
    db.add_all(
        [
            PerformancePrice(
                performance_id=funcion.id,
                zone_id=general.id,
                precio=Decimal("300.00"),
                precio_nino=Decimal("150.00"),
            ),
            PerformancePrice(
                performance_id=funcion.id, zone_id=vip.id, precio=Decimal("900.00")
            ),
        ]
    )
    await db.flush()
    await InventoryService(db).materializar(funcion.id)
    await InventoryService(db).abrir_venta(funcion.id)
    await db.flush()
    return {
        "performance_id": funcion.id,
        "event_id": evento.id,
        "general": general.id,
        "vip": vip.id,
    }


async def test_el_mapa_no_trae_cupos_sino_cuantos_quedan(
    cliente_http, db, ga, hold_service
):
    from app.api.v1.publico import get_redis_publico
    from app.main import app

    class SinCache:
        async def get(self, _):
            return None

        async def set(self, *a, **k):
            return None

    hold = nuevo_hold()
    await ApartadoService(db, hold_service).apartar_general(
        ga["performance_id"], ga["general"], 3, hold
    )
    app.dependency_overrides[get_redis_publico] = lambda: SinCache()
    try:
        r = await cliente_http.get(
            f"{API}/publico/funciones/{ga['performance_id']}/mapa",
            params={"apartado": str(uuid4())},
        )
        cuerpo = r.json()
        assert cuerpo["butacas"] == []
        zonas = {z["nombre"]: z for z in cuerpo["zonas"]}
        assert zonas["General"]["general"] is True
        assert zonas["General"]["disponibles"] == 17
        assert zonas["General"]["precio_desde"] == "300.00"
        assert zonas["VIP"]["disponibles"] == 3
        # Con su apartado, el comprador ve sus cupos.
        from uuid import UUID

        r = await cliente_http.get(
            f"{API}/publico/funciones/{ga['performance_id']}/mapa",
            params={"apartado": str(UUID(hex=hold))},
        )
        mias = [b for b in r.json()["butacas"] if b["estado"] == "tuya"]
        assert len(mias) == 3 and {b["zona_id"] for b in mias} == {ga["general"]}
    finally:
        app.dependency_overrides.pop(get_redis_publico, None)


async def test_nunca_vende_mas_del_cupo(db, ga, hold_service):
    servicio = ApartadoService(db, hold_service)
    pid, vip = ga["performance_id"], ga["vip"]
    uno = nuevo_hold()
    estado = await servicio.apartar_general(pid, vip, 2, uno)
    assert len(estado.butacas) == 2
    with pytest.raises(AsientoNoDisponible) as e:
        await servicio.apartar_general(pid, vip, 2, nuevo_hold())
    assert e.value.detalle["quedan"] == 1
    await servicio.apartar_general(pid, vip, 1, nuevo_hold())
    with pytest.raises(AsientoNoDisponible) as e:
        await servicio.apartar_general(pid, vip, 1, nuevo_hold())
    assert "agotó" in str(e.value)
    # El tope por compra tambien aplica, sumando lo que ya tenia.
    with pytest.raises(LimiteDeAsientos):
        await servicio.apartar_general(pid, ga["general"], 9, nuevo_hold())
    otro = nuevo_hold()
    await servicio.apartar_general(pid, ga["general"], 6, otro)
    with pytest.raises(LimiteDeAsientos):
        await servicio.apartar_general(pid, ga["general"], 3, otro)


async def test_zona_numerada_no_es_general(db, ga, hold_service, clientes):
    from tests.test_apartados import funcion_a_la_venta

    esc = await funcion_a_la_venta(db, clientes[0].id)
    with pytest.raises(Exception) as e:
        await ApartadoService(db, hold_service).apartar_general(
            esc["performance_id"], esc["zonas"][0], 1, nuevo_hold()
        )
    assert "entrada general" in str(e.value)


async def test_el_boleto_dice_acceso_general(db, ga, hold_service, stripe_falso):
    hold = nuevo_hold()
    estado = await ApartadoService(db, hold_service).apartar_general(
        ga["performance_id"], ga["general"], 2, hold
    )
    _, b = (x.seat_id for x in estado.butacas)
    iniciada = await CheckoutService(db, hold_service, stripe_falso).iniciar(
        ga["performance_id"], hold, comprador(), tipos={b: "nino"}
    )
    assert sorted(p.asiento_snap for p in iniciada.partidas) == [
        "Acceso general",
        "Acceso general · Niño",
    ]
    assert iniciada.orden.total == Decimal("450.00")


async def test_por_api_con_las_mismas_guardas(cliente_http, ga, hold_service):
    from app.api.v1.apartados import get_hold_service
    from app.main import app

    app.dependency_overrides[get_hold_service] = lambda: hold_service
    try:
        r = await cliente_http.post(
            f"{API}/performances/{ga['performance_id']}/apartados/general",
            json={"hold_id": str(uuid4()), "zone_id": ga["vip"], "cantidad": 2},
        )
    finally:
        app.dependency_overrides.pop(get_hold_service, None)
    assert r.status_code == 201, r.text
    assert len(r.json()["butacas"]) == 2


async def test_taquilla_vende_cupos(cliente_http, db, usuarios, ga):
    org = auth(token_de(usuarios["org_a"]))
    r = await cliente_http.post(
        f"{API}/taquilla/eventos/{ga['event_id']}/cajas",
        json={"cajero_id": usuarios["org_a"].id, "fondo": "0"},
        headers=org,
    )
    caja = r.json()["uuid"]
    r = await cliente_http.get(
        f"{API}/taquilla/funciones/{ga['performance_id']}/mapa", headers=org
    )
    zonas = {z["id"]: z for z in r.json()["zonas"]}
    assert zonas[ga["vip"]]["general"] is True
    libres = [
        b["id"]
        for b in r.json()["butacas"]
        if b["zona_id"] == ga["vip"] and b["estado"] == "libre"
    ]
    r = await cliente_http.post(
        f"{API}/taquilla/cajas/{caja}/ventas",
        json={
            "performance_id": ga["performance_id"],
            "seat_ids": libres[:2],
            "email": "puerta@correo.mx",
            "nombre": "Ana",
            "forma_pago": "efectivo",
            "recibido": "1800.00",
            "acepta_aviso": True,
            "aviso_version": "2026-10",
            "clave": uuid4().hex,
        },
        headers=org,
    )
    assert r.status_code == 201, r.text
    assert [x["asiento"] for x in r.json()["lugares"]] == ["Acceso general"] * 2
    vendidos = await db.scalar(
        select(func.count(PerformanceSeat.id)).where(
            PerformanceSeat.performance_id == ga["performance_id"],
            PerformanceSeat.estado == "vendido",
        )
    )
    assert vendidos == 2
