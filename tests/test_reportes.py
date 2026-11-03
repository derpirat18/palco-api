"""Reportes (RP3), Inicio y buscador del panel.

Lo que importa:

- Cada quien ve lo suyo: PSD todo, el organizador sus eventos (aunque pida
  otro organizador), el jefe de taquilla solo Taquilla y cortes; cajeros y
  puerta, nada.
- Los numeros salen de lo congelado en cada compra: venta bruta, IVA y la
  comision de PSD de esa compra.
- Compradores solo trae correo y telefono de quien acepto promociones en una
  compra de ese organizador.
- Excel, CSV y PDF se descargan, y cada descarga queda en la bitacora con
  sus filtros; ver Compradores y buscar un correo, tambien.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.acceso import ConsultaDeDatos
from tests.test_boletos import orden_pagada
from tests.test_catalog import API, auth, token_de
from tests.test_checkout import PasarelaFalsa

pytestmark = pytest.mark.asyncio
RP = f"{API}/reportes"


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


def de(usuarios, quien) -> dict:
    return auth(token_de(usuarios[quien]))


@pytest.fixture
async def venta(db, clientes, hold_service, stripe_falso):
    """Una compra en linea de 2 lugares del organizador A."""
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await db.commit()
    return orden


async def caja_con_venta(cliente_http, usuarios, orden, marketing=False) -> dict:
    """El organizador A abre su caja y vende 1 lugar en efectivo."""
    org = de(usuarios, "org_a")
    pid = orden.performance_id
    r = await cliente_http.get(f"{API}/taquilla/funciones/{pid}/mapa", headers=org)
    libre = next(b["id"] for b in r.json()["butacas"] if b["estado"] == "libre")
    eventos = (await cliente_http.get(f"{API}/taquilla/eventos", headers=org)).json()
    ev = eventos[0]["event_id"]
    r = await cliente_http.post(
        f"{API}/taquilla/eventos/{ev}/cajas",
        json={"cajero_id": usuarios["org_a"].id, "fondo": "100"},
        headers=org,
    )
    caja = r.json()["uuid"]
    r = await cliente_http.post(
        f"{API}/taquilla/cajas/{caja}/ventas",
        json={
            "performance_id": pid,
            "seat_ids": [libre],
            "email": "Luz@Correo.mx",
            "nombre": "Luz",
            "forma_pago": "efectivo",
            "recibido": "5000.00",
            "acepta_aviso": True,
            "acepta_marketing": marketing,
            "aviso_version": "2026-10",
            "clave": uuid4().hex,
        },
        headers=org,
    )
    assert r.status_code == 201, r.text
    return {"caja": caja, "event_id": ev, "recibo": r.json()}


async def test_catalogo_segun_quien(cliente_http, usuarios):
    r = await cliente_http.get(RP, headers=de(usuarios, "admin"))
    tipos = [x["tipo"] for x in r.json()["reportes"]]
    assert tipos == [
        "ventas", "taquilla", "asistencia", "cortesias", "compradores", "ocupacion"
    ]  # fmt: skip
    assert [x["tipo"] for x in r.json()["proximamente"]] == [
        "liquidacion",
        "promociones",
    ]
    r = await cliente_http.get(RP, headers=de(usuarios, "org_a"))
    assert len(r.json()["reportes"]) == 6
    # La puerta no tiene reportes.
    r = await cliente_http.get(RP, headers=de(usuarios, "acceso"))
    assert r.json()["reportes"] == []
    r = await cliente_http.get(f"{RP}/ventas", headers=de(usuarios, "acceso"))
    assert r.status_code == 403


async def test_ventas_con_lo_congelado(cliente_http, usuarios, venta):
    r = await cliente_http.get(f"{RP}/ventas", headers=de(usuarios, "org_a"))
    assert r.status_code == 200, r.text
    d = r.json()
    ind = {i["etiqueta"]: i["valor"] for i in d["indicadores"]}
    assert Decimal(ind["Venta bruta"]) == venta.total
    assert ind["Boletos vendidos"] == 2 and ind["Órdenes"] == 1
    # Comision del organizador A: 10 % sobre lo que pago el comprador.
    assert Decimal(ind["Comisión de PSD"]) == (venta.total / 10).quantize(
        Decimal("0.01")
    )
    assert Decimal(d["totales"]["venta_bruta"]) == venta.total
    assert d["filas"][0]["canal"] == "En línea"
    assert d["por"] == "compra" and d["grafica"]["titulo"] == "Venta por día"

    # El organizador B no ve nada, ni pidiendo al organizador A.
    r = await cliente_http.get(
        f"{RP}/ventas",
        params={"client_id": usuarios["org_a"].client_id},
        headers=de(usuarios, "org_b"),
    )
    assert r.json()["filas"] == []
    # Filtros: por canal y por periodo.
    r = await cliente_http.get(
        f"{RP}/ventas", params={"canal": "taquilla"}, headers=de(usuarios, "admin")
    )
    assert r.json()["filas"] == []
    r = await cliente_http.get(
        f"{RP}/ventas",
        params={"desde": "2020-01-01", "hasta": "2020-01-31"},
        headers=de(usuarios, "admin"),
    )
    assert r.json()["filas"] == []
    r = await cliente_http.get(
        f"{RP}/ventas",
        params={"desde": "2026-12-01", "hasta": "2026-01-01"},
        headers=de(usuarios, "admin"),
    )
    assert r.status_code == 422


async def test_descargas_quedan_en_la_bitacora(cliente_http, db, usuarios, venta):
    org = de(usuarios, "org_a")
    r = await cliente_http.get(
        f"{RP}/ventas/descargar", params={"formato": "xlsx"}, headers=org
    )
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert "reporte-ventas-" in r.headers["content-disposition"]
    r = await cliente_http.get(
        f"{RP}/ventas/descargar", params={"formato": "csv"}, headers=org
    )
    texto = r.content.decode("utf-8-sig")
    assert texto.splitlines()[0].startswith("Evento,Función,Zona")
    r = await cliente_http.get(
        f"{RP}/ocupacion/descargar",
        params={"formato": "pdf", "desde": date.today().isoformat()},
        headers=org,
    )
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    registros = (
        await db.scalars(
            select(ConsultaDeDatos).where(ConsultaDeDatos.recurso.like("reporte_%"))
        )
    ).all()
    assert [(c.recurso, c.accion, c.filtros["formato"]) for c in registros] == [
        ("reporte_ventas", "descargar", "xlsx"),
        ("reporte_ventas", "descargar", "csv"),
        ("reporte_ocupacion", "descargar", "pdf"),
    ]
    assert registros[0].user_id == usuarios["org_a"].id
    assert registros[0].filtros["client_id"] == str(usuarios["org_a"].client_id)


async def test_taquilla_y_jefe(cliente_http, db, usuarios, venta):
    datos = await caja_con_venta(cliente_http, usuarios, venta)
    r = await cliente_http.get(f"{RP}/taquilla", headers=de(usuarios, "org_a"))
    d = r.json()
    ind = {i["etiqueta"]: i["valor"] for i in d["indicadores"]}
    total = Decimal(datos["recibo"]["total"])
    assert ind["Cajas"] == 1 and Decimal(ind["Efectivo"]) == total
    assert Decimal(ind["Efectivo con el organizador"]) == total
    assert d["filas"][0]["estado"] == "Abierta"
    # Ventas ya cuenta la taquilla.
    r = await cliente_http.get(
        f"{RP}/ventas", params={"canal": "taquilla"}, headers=de(usuarios, "org_a")
    )
    assert r.json()["filas"][0]["canal"] == "Taquilla"

    # El personal hecho jefe ve solo Taquilla y cortes de su evento.
    r = await cliente_http.put(
        f"{API}/taquilla/eventos/{datos['event_id']}/personal/{usuarios['acceso'].id}",
        json={"puesto": "jefe"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 204
    jefe = de(usuarios, "acceso")
    r = await cliente_http.get(RP, headers=jefe)
    assert [x["tipo"] for x in r.json()["reportes"]] == ["taquilla"]
    r = await cliente_http.get(f"{RP}/taquilla", headers=jefe)
    assert r.status_code == 200 and len(r.json()["filas"]) == 1
    r = await cliente_http.get(f"{RP}/ventas", headers=jefe)
    assert r.status_code == 403
    r = await cliente_http.get(
        f"{RP}/taquilla", params={"event_id": 999_999}, headers=jefe
    )
    assert r.status_code == 403


async def test_compradores_cuida_los_datos(cliente_http, db, usuarios, venta):
    await caja_con_venta(cliente_http, usuarios, venta, marketing=True)
    r = await cliente_http.get(f"{RP}/compradores", headers=de(usuarios, "org_a"))
    filas = {f["nombre"].split()[0]: f for f in r.json()["filas"]}
    # Luz acepto promociones en una compra de A: sale su correo.
    assert filas["Luz"]["acepta"] == "Sí" and filas["Luz"]["correo"] == "luz@correo.mx"
    # La compradora en linea no las acepto: sin correo ni telefono.
    otra = next(f for n, f in filas.items() if n != "Luz")
    assert otra["acepta"] == "No" and otra["correo"] == "" and otra["telefono"] == ""
    ver = await db.scalar(
        select(ConsultaDeDatos).where(ConsultaDeDatos.recurso == "reporte_compradores")
    )
    assert ver.accion == "ver"


async def test_asistencia_cortesias_y_ocupacion(cliente_http, usuarios, venta):
    org = de(usuarios, "org_a")
    for tipo in ("asistencia", "cortesias", "ocupacion"):
        r = await cliente_http.get(f"{RP}/{tipo}", headers=org)
        assert r.status_code == 200, (tipo, r.text)
    r = await cliente_http.get(f"{RP}/asistencia", headers=org)
    ind = {i["etiqueta"]: i["valor"] for i in r.json()["indicadores"]}
    assert ind["Boletos"] == 2 and ind["Entraron"] == 0
    r = await cliente_http.get(f"{RP}/ocupacion", headers=org)
    ind = {i["etiqueta"]: i["valor"] for i in r.json()["indicadores"]}
    assert ind["Vendidos"] == 2 and ind["Capacidad"] > 2


async def test_opciones_de_los_filtros(cliente_http, usuarios, venta):
    r = await cliente_http.get(f"{RP}/opciones", headers=de(usuarios, "admin"))
    assert r.json()["organizadores"] and r.json()["eventos"]
    ev = r.json()["eventos"][0]["id"]
    r = await cliente_http.get(
        f"{RP}/opciones", params={"event_id": ev}, headers=de(usuarios, "org_a")
    )
    d = r.json()
    assert d["organizadores"] == [] and d["funciones"] and d["zonas"]
    r = await cliente_http.get(
        f"{RP}/opciones", params={"event_id": ev}, headers=de(usuarios, "org_b")
    )
    assert r.json()["funciones"] == []


async def test_inicio_y_buscador(cliente_http, db, usuarios, venta):
    r = await cliente_http.get(f"{API}/panel/inicio", headers=de(usuarios, "org_a"))
    d = r.json()
    assert Decimal(d["venta_hoy"]) == venta.total and d["boletos_hoy"] == 2
    assert d["proximas"][0]["vendidos"] == 2
    assert len(d["venta_7_dias"]) == 7
    r = await cliente_http.get(f"{API}/panel/inicio", headers=de(usuarios, "org_b"))
    assert r.json()["venta_hoy"] == "0.00"
    r = await cliente_http.get(f"{API}/panel/inicio", headers=de(usuarios, "acceso"))
    assert r.status_code == 403

    r = await cliente_http.get(
        f"{API}/panel/buscar",
        params={"q": venta.folio[-6:]},
        headers=de(usuarios, "org_a"),
    )
    assert any(x["titulo"] == venta.folio for x in r.json()["resultados"])
    r = await cliente_http.get(
        f"{API}/panel/buscar",
        params={"q": "ANA@correo.mx"},
        headers=de(usuarios, "org_a"),
    )
    compras = [x for x in r.json()["resultados"] if x["tipo"] == "compra"]
    assert compras and compras[0]["a"].endswith(f"?folio={venta.folio}")
    log = await db.scalar(
        select(ConsultaDeDatos).where(ConsultaDeDatos.recurso == "busqueda")
    )
    assert log.filtros["q"] == "ANA@correo.mx"
    # El organizador B no encuentra compras de A.
    r = await cliente_http.get(
        f"{API}/panel/buscar", params={"q": venta.folio}, headers=de(usuarios, "org_b")
    )
    assert r.json()["resultados"] == []
