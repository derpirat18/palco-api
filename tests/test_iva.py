"""IVA por evento (ADR 0013).

El organizador escribe la tasa y dice si sus precios ya la incluyen. El
comprador ve y paga siempre el precio con IVA. Lo que se prueba:

- La conversion de precio capturado a precio al publico, al centavo.
- Que es la misma en el inventario, en el precio por butaca y en el
  "desde" de la pagina publica.
- Que cada orden desglosa su IVA y congela la tasa.
- Que lo que cambia el precio al publico no se puede mover con inventario.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.base import EstadoAsiento, EstadoEvento
from app.models.catalog import Event
from app.models.inventory import PerformanceSeat
from app.services.checkout import CheckoutService
from app.services.inventory import InventoryService
from app.services.venta import Iva
from tests.test_catalog import auth, crear_evento, crear_recinto, token_de
from tests.test_checkout import PasarelaFalsa, apartar, comprador
from tests.test_inventory import armar_escenario

pytestmark = pytest.mark.asyncio

API = "/api/v1"
D = Decimal


# --------------------------------------------------------------------------
# La regla
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,incluido,capturado,al_publico,iva",
    [
        ("16", True, "850.00", "850.00", "117.24"),
        ("16", False, "850.00", "986.00", "136.00"),
        ("8", False, "850.00", "918.00", "68.00"),
        ("0", True, "850.00", "850.00", "0.00"),
        ("0", False, "850.00", "850.00", "0.00"),
        # Redondeo comercial: 333.33 x 1.16 = 386.6628 -> 386.66.
        ("16", False, "333.33", "386.66", "53.33"),
    ],
)
async def test_precio_al_publico_y_su_iva(pct, incluido, capturado, al_publico, iva):
    regla = Iva(pct=D(pct), incluido=incluido)
    precio = regla.precio_al_publico(D(capturado))
    assert precio == D(al_publico)
    assert regla.iva_contenido(precio) == D(iva)


async def test_con_mas_iva_la_base_es_lo_que_capturo_el_organizador():
    """Lo que el organizador dijo que valia su boleto no se pierde."""
    regla = Iva(pct=D("16"), incluido=False)
    for capturado in ("100.00", "333.33", "850.00", "1234.56", "0.99"):
        precio = regla.precio_al_publico(D(capturado))
        assert precio - regla.iva_contenido(precio) == D(capturado)


# --------------------------------------------------------------------------
# Inventario y pagina publica
# --------------------------------------------------------------------------


async def evento_con_iva(db, cliente_id, pct="16.00", incluido=False) -> dict:
    """Escenario de prueba con el IVA fijado ANTES de generar inventario."""
    esc = await armar_escenario(db, cliente_id)
    evento = await db.get(Event, esc["event_id"])
    evento.iva_pct = D(pct)
    evento.precios_incluyen_iva = incluido
    evento.estado = EstadoEvento.PUBLICADO.value
    await db.flush()
    return esc


async def test_mas_iva_se_suma_al_generar_el_inventario(db, clientes):
    esc = await evento_con_iva(db, clientes[0].id)
    await InventoryService(db).materializar(esc["performance_id"])

    precios = set(
        (
            await db.scalars(
                select(PerformanceSeat.precio_final).where(
                    PerformanceSeat.performance_id == esc["performance_id"]
                )
            )
        ).all()
    )
    # Luneta capturada en 800 y balcon en 400.
    assert precios == {D("928.00"), D("464.00")}


async def test_iva_incluido_deja_el_precio_como_se_capturo(db, clientes):
    esc = await evento_con_iva(db, clientes[0].id, incluido=True)
    await InventoryService(db).materializar(esc["performance_id"])

    precios = set(
        (
            await db.scalars(
                select(PerformanceSeat.precio_final).where(
                    PerformanceSeat.performance_id == esc["performance_id"]
                )
            )
        ).all()
    )
    assert precios == {D("800.00"), D("400.00")}


async def test_precio_por_butaca_tambien_suma_el_iva(db, clientes):
    esc = await evento_con_iva(db, clientes[0].id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])
    una = await db.scalar(
        select(PerformanceSeat.id)
        .where(PerformanceSeat.performance_id == esc["performance_id"])
        .limit(1)
    )

    await servicio.fijar_precio_butacas(esc["performance_id"], [una], D("1000.00"))

    precio = await db.scalar(
        select(PerformanceSeat.precio_final).where(PerformanceSeat.id == una)
    )
    assert precio == D("1160.00")


async def test_desde_de_la_pagina_publica_es_con_iva(cliente_http, db, clientes):
    """Con y sin inventario, "desde" es lo que pagaria el comprador."""
    esc = await evento_con_iva(db, clientes[0].id)
    slug = (await db.get(Event, esc["event_id"])).slug

    sin_inventario = (await cliente_http.get(f"{API}/publico/eventos/{slug}")).json()
    await InventoryService(db).materializar(esc["performance_id"])
    await InventoryService(db).abrir_venta(esc["performance_id"])
    con_inventario = (await cliente_http.get(f"{API}/publico/eventos/{slug}")).json()

    assert sin_inventario["funciones"][0]["precio_desde"] == "464.00"
    assert con_inventario["funciones"][0]["precio_desde"] == "464.00"


# --------------------------------------------------------------------------
# La orden desglosa
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,incluido,subtotal,iva,total",
    [
        # 2 butacas de luneta capturadas en $800.
        ("16.00", True, "1379.32", "220.68", "1600.00"),
        # Mas IVA: el subtotal es justo lo que capturo el organizador.
        ("16.00", False, "1600.00", "256.00", "1856.00"),
        ("0.00", True, "1600.00", "0.00", "1600.00"),
    ],
)
async def test_la_orden_desglosa_y_congela_el_iva(
    db, clientes, hold_service, pct, incluido, subtotal, iva, total
):
    esc = await evento_con_iva(db, clientes[0].id, pct=pct, incluido=incluido)
    pid = esc["performance_id"]
    await InventoryService(db).materializar(pid)
    await InventoryService(db).abrir_venta(pid)
    luneta = list(
        (
            await db.scalars(
                select(PerformanceSeat.seat_id)
                .where(
                    PerformanceSeat.performance_id == pid,
                    PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value,
                )
                .order_by(PerformanceSeat.seat_id)
                .limit(2)
            )
        ).all()
    )
    esc["luneta"] = luneta
    hold = await apartar(db, hold_service, esc, luneta)

    iniciada = await CheckoutService(db, hold_service, PasarelaFalsa()).iniciar(
        pid, hold, comprador()
    )

    orden = iniciada.orden
    assert (orden.subtotal, orden.iva, orden.total) == (D(subtotal), D(iva), D(total))
    assert orden.subtotal + orden.iva - orden.descuento == orden.total
    assert orden.iva_pct_snap == D(pct)
    assert sum(p.iva for p in iniciada.partidas) == orden.iva

    # Cambiar la tasa despues no mueve lo vendido.
    evento = await db.get(Event, esc["event_id"])
    evento.iva_pct = D("8.00")
    await db.flush()
    await db.refresh(orden)
    assert orden.iva_pct_snap == D(pct)


async def test_la_base_no_deja_un_iva_mayor_que_el_importe(db, clientes):
    evento = await db.get(
        Event, (await armar_escenario(db, clientes[0].id))["event_id"]
    )
    with pytest.raises(IntegrityError, match="ck_events_iva_rango"):
        async with db.begin_nested():
            evento.iva_pct = D("160.00")
            await db.flush()


# --------------------------------------------------------------------------
# API: el organizador lo decide, y a tiempo
# --------------------------------------------------------------------------


async def test_alta_de_evento_con_su_iva(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue = await crear_recinto(cliente_http, tk)
    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(tk),
        json={
            "venue_id": venue,
            "nombre": "Teatro en la frontera",
            "client_id": usuarios["org_a"].client_id,
            "iva_pct": "8",
            "precios_incluyen_iva": False,
        },
    )
    assert r.status_code == 201, r.text
    assert (r.json()["iva_pct"], r.json()["precios_incluyen_iva"]) == ("8.00", False)


async def test_por_omision_16_por_ciento_incluido(cliente_http, usuarios):
    venue = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue)
    assert (evento["iva_pct"], evento["precios_incluyen_iva"]) == ("16.00", True)


@pytest.mark.parametrize("pct", ["-1", "100.01", "16.555"])
async def test_tasa_invalida_se_rechaza(cliente_http, usuarios, pct):
    venue = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue)
    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}", headers=auth(tk), json={"iva_pct": pct}
    )
    assert r.status_code == 422


async def patch_evento(cliente_http, usuarios, event_id, cuerpo):
    return await cliente_http.patch(
        f"{API}/events/{event_id}",
        headers=auth(token_de(usuarios["admin"])),
        json=cuerpo,
    )


async def test_antes_del_inventario_se_cambia_libremente(
    cliente_http, db, clientes, usuarios
):
    esc = await armar_escenario(db, clientes[0].id)
    r = await patch_evento(
        cliente_http,
        usuarios,
        esc["event_id"],
        {"precios_incluyen_iva": False, "iva_pct": "8"},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["iva_pct"], r.json()["precios_incluyen_iva"]) == ("8.00", False)


@pytest.mark.parametrize(
    "inicial_incluido,cuerpo",
    [
        (True, {"precios_incluyen_iva": False}),
        (False, {"precios_incluyen_iva": True}),
        (False, {"iva_pct": "8"}),
    ],
)
async def test_con_inventario_no_se_mueve_lo_que_paga_el_comprador(
    cliente_http, db, clientes, usuarios, inicial_incluido, cuerpo
):
    esc = await evento_con_iva(db, clientes[0].id, incluido=inicial_incluido)
    await InventoryService(db).materializar(esc["performance_id"])

    r = await patch_evento(cliente_http, usuarios, esc["event_id"], cuerpo)

    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "conflicto_de_catalogo"
    evento = await db.get(Event, esc["event_id"])
    await db.refresh(evento)
    assert evento.precios_incluyen_iva is inicial_incluido
    assert evento.iva_pct == D("16.00")


async def test_con_iva_incluido_la_tasa_si_se_corrige(
    cliente_http, db, clientes, usuarios
):
    """El precio al publico no cambia: solo el desglose de lo que venga."""
    esc = await evento_con_iva(db, clientes[0].id, incluido=True)
    await InventoryService(db).materializar(esc["performance_id"])

    r = await patch_evento(cliente_http, usuarios, esc["event_id"], {"iva_pct": "0"})

    assert r.status_code == 200, r.text
    assert r.json()["iva_pct"] == "0.00"


async def test_null_no_borra_el_tratamiento_de_iva(
    cliente_http, db, clientes, usuarios
):
    esc = await armar_escenario(db, clientes[0].id)
    r = await patch_evento(
        cliente_http,
        usuarios,
        esc["event_id"],
        {"iva_pct": None, "precios_incluyen_iva": None, "descripcion": "x"},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["iva_pct"], r.json()["precios_incluyen_iva"]) == ("16.00", True)
