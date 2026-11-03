"""Metricas del panel (bloque 14): solo lectura.

Lo que importa:

- Cada quien ve lo suyo: PSD todo, el organizador sus eventos y funciones;
  lo ajeno responde igual que lo que no existe; el personal de acceso no
  tiene estas pantallas.
- Los numeros: vendidos (sin cortesias) de cuantos lugares, venta (lo
  cobrado, sin cortesias), por zona, por canal, por tipo y por dia.
- La puerta: cuantos entraron; al terminar, como llegaron cada 15 minutos.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.catalog import Event, Performance
from app.models.sales import Scan, Ticket
from app.services import metricas
from tests.test_catalog import API
from tests.test_reportes import caja_con_venta, de, stripe_falso, venta  # noqa: F401

pytestmark = pytest.mark.asyncio
M = f"{API}/panel/metricas"


async def test_eventos_cada_quien_lo_suyo(cliente_http, usuarios, venta):  # noqa: F811
    r = await cliente_http.get(f"{M}/eventos", headers=de(usuarios, "org_a"))
    assert r.status_code == 200, r.text
    ev = next(e for e in r.json()["eventos"] if e["event_id"] > 0)
    assert ev["vendidos"] == 2 and ev["capacidad"] >= 2
    assert Decimal(ev["venta"]) == venta.total
    assert ev["proxima"]["performance_id"] == venta.performance_id
    assert ev["tono"] in {"good", "warn", "bad", "mute"} and ev["estado"]

    # PSD ve el evento del organizador A; el organizador B, no.
    r = await cliente_http.get(f"{M}/eventos", headers=de(usuarios, "admin"))
    assert ev["event_id"] in {e["event_id"] for e in r.json()["eventos"]}
    r = await cliente_http.get(f"{M}/eventos", headers=de(usuarios, "org_b"))
    assert ev["event_id"] not in {e["event_id"] for e in r.json()["eventos"]}
    # El personal de acceso no tiene esta pantalla.
    r = await cliente_http.get(f"{M}/eventos", headers=de(usuarios, "acceso"))
    assert r.status_code == 403


async def test_lo_ajeno_es_igual_que_lo_que_no_existe(cliente_http, usuarios, venta):  # noqa: F811
    pid = venta.performance_id
    r = await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "org_b"))
    no_existe = await cliente_http.get(
        f"{M}/funciones/999999", headers=de(usuarios, "org_b")
    )
    assert r.status_code == no_existe.status_code == 404
    assert r.json()["error"]["mensaje"] == no_existe.json()["error"]["mensaje"]
    ev = (
        await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "org_a"))
    ).json()["event_id"]
    r = await cliente_http.get(
        f"{M}/eventos/{ev}/funciones", headers=de(usuarios, "org_b")
    )
    no_existe = await cliente_http.get(
        f"{M}/eventos/999999/funciones", headers=de(usuarios, "org_b")
    )
    assert r.status_code == no_existe.status_code == 404
    assert r.json()["error"]["mensaje"] == no_existe.json()["error"]["mensaje"]
    r = await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "acceso"))
    assert r.status_code == 403


async def test_funcion_por_zona_canal_tipo_y_dia(cliente_http, usuarios, venta):  # noqa: F811
    await caja_con_venta(cliente_http, usuarios, venta)
    pid = venta.performance_id
    r = await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "org_a"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["vendidos"] == 3
    assert d["disponibles"] == d["capacidad"] - 3 - d["cortesias"]
    assert sum(z["vendidos"] for z in d["zonas"]) == 3
    assert d["canales"]["linea"]["boletos"] == 2
    assert d["canales"]["taquilla"]["boletos"] == 1
    assert Decimal(d["canales"]["linea"]["monto"]) == venta.total
    assert d["tipos"]["adulto"] == 3
    assert len(d["venta_por_dia"]) == 7
    assert Decimal(d["venta"]) == sum(Decimal(x["monto"]) for x in d["venta_por_dia"])
    # Lista de funciones del evento: vendidos y dias que faltan.
    r = await cliente_http.get(
        f"{M}/eventos/{d['event_id']}/funciones", headers=de(usuarios, "org_a")
    )
    f = next(x for x in r.json()["funciones"] if x["performance_id"] == pid)
    assert f["vendidos"] == 3 and f["dias_texto"]
    assert Decimal(f["venta"]) == Decimal(d["venta"])


async def test_puerta_y_llegadas(db, cliente_http, usuarios, venta):  # noqa: F811
    pid = venta.performance_id
    boletos = (
        await db.scalars(
            select(Ticket)
            .join(Ticket.partida)
            .where(Ticket.partida.has(order_id=venta.id))
        )
    ).all()
    r = await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "org_a"))
    p = r.json()["puerta"]
    assert p["boletos"] == 2 and p["entraron"] == 0 and not p["termino"]
    assert p["no_llegaron"] is None and p["llegadas"] == []

    # La funcion ya paso: entro uno a las 16:05 y hubo un rechazo.
    inicio = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    await db.execute(
        update(Performance).where(Performance.id == pid).values(inicia_en=inicio)
    )
    boletos[0].estado = "usado"
    db.add_all(
        [
            Scan(
                ticket_id=boletos[0].id,
                performance_id=pid,
                resultado="valido",
                escaneado_en=inicio + timedelta(minutes=5),
                client_scan_id="m1",
            ),
            Scan(
                ticket_id=boletos[0].id,
                performance_id=pid,
                resultado="duplicado",
                escaneado_en=inicio + timedelta(minutes=9),
                client_scan_id="m2",
            ),
        ]
    )
    await db.commit()
    r = await cliente_http.get(f"{M}/funciones/{pid}", headers=de(usuarios, "org_a"))
    p = r.json()["puerta"]
    assert p["termino"] and p["entraron"] == 1 and p["no_llegaron"] == 1
    assert p["rechazos"] == 1
    assert sum(x["entraron"] for x in p["llegadas"]) == 1 and p["pico"]


async def test_como_va():
    f = Performance(estado="en_venta", inventario_generado=True)
    assert metricas._como_va("publicado", f, 100, 10, 2) == ("warn", "Venta lenta")
    assert metricas._como_va("publicado", f, 100, 10, 10) == ("good", "En venta")
    assert metricas._como_va("publicado", f, 100, 95, 10) == ("bad", "Casi agotada")
    assert metricas._como_va("borrador", f, 100, 0, 1) == ("mute", "Borrador")
    assert metricas._como_va("publicado", None, 0, 0, None)[1] == (
        "Sin funciones próximas"
    )
    assert metricas.dias_en_texto(1) == "Mañana"
    assert metricas.dias_en_texto(3) == "Faltan 3 días"


# --------------------------------------------------------------------------
# Administracion (bloque 15)
# --------------------------------------------------------------------------


async def test_recintos_cada_quien_sus_numeros(db, cliente_http, usuarios, venta):  # noqa: F811
    f = await db.get(Performance, venta.performance_id)
    ev = await db.get(Event, f.event_id)
    vid = ev.venue_id
    r = await cliente_http.get(f"{M}/recintos", headers=de(usuarios, "org_a"))
    assert r.status_code == 200, r.text
    mio = next(x for x in r.json()["recintos"] if x["venue_id"] == vid)
    assert mio["eventos"] >= 1 and mio["funciones"] >= 1 and mio["mapas"] >= 1
    assert mio["proxima"] is not None and mio["ocupacion"] is None

    # El recinto es de todos, pero el organizador B no ve los numeros de A.
    r = await cliente_http.get(f"{M}/recintos", headers=de(usuarios, "org_b"))
    ajeno = next(x for x in r.json()["recintos"] if x["venue_id"] == vid)
    assert ajeno["eventos"] == 0 and ajeno["funciones"] == 0
    r = await cliente_http.get(f"{M}/recintos/{vid}", headers=de(usuarios, "org_b"))
    assert r.json()["eventos"] == 0
    assert all(m["funciones"] == 0 for m in r.json()["mapas_uso"])
    # ...pero sabe que el mapa ya se usa: un mapa con funciones no se cambia.
    assert any(m["en_uso"] for m in r.json()["mapas_uso"])

    # La funcion ya paso: cuenta en la ocupacion promedio.
    await db.execute(
        update(Performance)
        .where(Performance.id == f.id)
        .values(inicia_en=datetime(2026, 1, 10, 22, 0, tzinfo=UTC))
    )
    await db.commit()
    r = await cliente_http.get(f"{M}/recintos/{vid}", headers=de(usuarios, "admin"))
    d = r.json()
    assert d["funciones_pasadas"] >= 1 and Decimal(d["ocupacion"]) > 0
    no_existe = await cliente_http.get(
        f"{M}/recintos/999999", headers=de(usuarios, "admin")
    )
    assert no_existe.status_code == 404
    r = await cliente_http.get(f"{M}/recintos", headers=de(usuarios, "acceso"))
    assert r.status_code == 403


async def test_organizadores_venta_y_comision_del_mes(
    db,
    cliente_http,
    usuarios,
    venta,  # noqa: F811
):
    r = await cliente_http.get(f"{M}/organizadores", headers=de(usuarios, "admin"))
    assert r.status_code == 200, r.text
    d = r.json()
    a = next(
        o for o in d["organizadores"] if o["client_id"] == usuarios["org_a"].client_id
    )
    assert Decimal(a["venta_mes"]) == venta.total
    pct = venta.comision_pct_snap
    assert Decimal(a["comision_mes"]) > 0
    assert abs(Decimal(a["comision_mes"]) - venta.total * pct / 100) <= Decimal("0.02")
    assert Decimal(d["venta_mes"]) >= venta.total and d["con_venta"] >= 1
    assert a["cuentas"] >= 2 and a["eventos"] >= 1
    # Solo PSD: es el acuerdo comercial de todos.
    for quien in ("org_a", "acceso"):
        r = await cliente_http.get(f"{M}/organizadores", headers=de(usuarios, quien))
        assert r.status_code == 403


async def test_cuentas_escaneos_de_su_ultima_funcion(
    db,
    cliente_http,
    usuarios,
    venta,  # noqa: F811
):
    pid = venta.performance_id
    puerta = usuarios["acceso"]
    momento = datetime.now(UTC)
    db.add_all(
        [
            Scan(
                performance_id=pid,
                user_id=puerta.id,
                resultado=r,
                escaneado_en=momento + timedelta(seconds=i),
                client_scan_id=f"c{i}",
            )
            for i, r in enumerate(["valido", "valido", "duplicado", "deshecho"])
        ]
    )
    await db.commit()
    r = await cliente_http.get(f"{M}/cuentas", headers=de(usuarios, "org_a"))
    assert r.status_code == 200, r.text
    filas = {c["user_id"]: c for c in r.json()["cuentas"]}
    # El organizador solo ve su personal de acceso.
    assert set(filas) <= {u.id for u in usuarios.values() if u.rol == "acceso"}
    u = filas[puerta.id]["ultima_funcion"]
    assert u["performance_id"] == pid
    assert u["escaneos"] == 2 and u["rechazos"] == 1 and u["evento"]
    r = await cliente_http.get(f"{M}/cuentas", headers=de(usuarios, "org_b"))
    assert puerta.id not in {c["user_id"] for c in r.json()["cuentas"]}
    r = await cliente_http.get(f"{M}/cuentas", headers=de(usuarios, "admin"))
    assert puerta.id in {c["user_id"] for c in r.json()["cuentas"]}
    r = await cliente_http.get(f"{M}/cuentas", headers=de(usuarios, "acceso"))
    assert r.status_code == 403
