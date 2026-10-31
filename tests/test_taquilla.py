"""Taquilla (TQ): vender en el lugar con caja, folios y corte a ciegas.

Lo que importa:

- Solo vende quien tiene puesto en la taquilla del evento, y solo en su caja.
- Vende del mismo inventario que la venta en linea, con el mismo candado:
  lo vendido o bloqueado no se vende otra vez.
- Cada venta lleva correo y emite boletos con QR (el mismo correo que en
  linea); efectivo con cambio, terminal con autorizacion, cortesia con motivo.
- Con medio fisico (pulsera, gafete) el cliente entra en ese momento: sus
  boletos quedan como usados y los folios son de la caja y no se repiten.
- Una correccion por error del cajero la hace el jefe, el mismo dia y antes
  del corte; los lugares regresan a la venta.
- El cajero cuenta a ciegas; el jefe cierra y, si hay diferencia, explica.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.models.base import EstadoAsiento
from app.models.catalog import Performance
from app.models.inventory import PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Consent, Cortesia, Order, OrderItem, Scan, Ticket
from tests.test_apartados import fila, funcion_a_la_venta, poner_estado
from tests.test_catalog import API, auth, token_de
from tests.test_correo import CarteroFalso, bandeja

pytestmark = pytest.mark.asyncio
TQ = f"{API}/taquilla"


def de(usuarios, quien) -> dict:
    return auth(token_de(usuarios[quien]))


@pytest.fixture
async def esc(db, clientes):
    """Funcion del organizador A: Luneta ($800) y Balcon ($400), 5 y 5."""
    return await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)


@pytest.fixture
async def caja(cliente_http, usuarios, esc):
    """La cuenta de acceso de A es cajero y tiene caja con folios 1001-1010."""
    org = de(usuarios, "org_a")
    ev = esc["event_id"]
    r = await cliente_http.put(
        f"{TQ}/eventos/{ev}/personal/{usuarios['acceso'].id}",
        json={"puesto": "cajero"},
        headers=org,
    )
    assert r.status_code == 204, r.text
    r = await cliente_http.post(
        f"{TQ}/eventos/{ev}/cajas",
        json={
            "cajero_id": usuarios["acceso"].id,
            "fondo": "500.00",
            "medio": "Pulsera amarilla",
            "folio_desde": 1001,
            "folio_hasta": 1010,
        },
        headers=org,
    )
    assert r.status_code == 201, r.text
    return r.json()["uuid"]


def venta(esc, seats, forma="efectivo", **kw) -> dict:
    cuerpo = {
        "performance_id": esc["performance_id"],
        "seat_ids": seats,
        "email": "Cliente@Correo.MX",
        "nombre": "Ana",
        "apellidos": "López",
        "forma_pago": forma,
        "acepta_aviso": True,
        "aviso_version": "2026-10",
        "clave": uuid4().hex,
    }
    if forma == "efectivo":
        cuerpo["recibido"] = "5000.00"
    return {**cuerpo, **kw}


async def vender(cliente_http, usuarios, caja, cuerpo, quien="acceso"):
    return await cliente_http.post(
        f"{TQ}/cajas/{caja}/ventas", json=cuerpo, headers=de(usuarios, quien)
    )


async def empieza_en(db, pid, minutos: int) -> None:
    await db.execute(
        update(Performance)
        .where(Performance.id == pid)
        .values(inicia_en=func.now() + timedelta(minutes=minutos))
    )


# --------------------------------------------------------------------------
# Quien
# --------------------------------------------------------------------------


async def test_solo_quien_tiene_puesto(cliente_http, usuarios, esc, caja):
    ev = esc["event_id"]
    # El cajero ve el evento y su caja; el organizador de otro, nada.
    r = await cliente_http.get(f"{TQ}/eventos", headers=de(usuarios, "acceso"))
    assert [(e["event_id"], e["puesto"]) for e in r.json()] == [(ev, "cajero")]
    r = await cliente_http.get(f"{TQ}/eventos/{ev}", headers=de(usuarios, "acceso"))
    assert r.json()["mi_caja"] == caja and r.json()["personal"] == []
    r = await cliente_http.get(f"{TQ}/eventos/{ev}", headers=de(usuarios, "org_b"))
    assert r.status_code == 404
    r = await cliente_http.get(f"{TQ}/cajas/{caja}", headers=de(usuarios, "org_b"))
    assert r.status_code == 404
    # El cajero no abre cajas ni configura.
    r = await cliente_http.post(
        f"{TQ}/eventos/{ev}/cajas",
        json={"cajero_id": usuarios["acceso"].id, "fondo": "0"},
        headers=de(usuarios, "acceso"),
    )
    assert r.status_code == 403
    r = await cliente_http.patch(
        f"{TQ}/eventos/{ev}/config",
        json={"texto_legal_medio": "x"},
        headers=de(usuarios, "acceso"),
    )
    assert r.status_code == 403
    # No se le abre una segunda caja, ni se cruzan los folios.
    r = await cliente_http.post(
        f"{TQ}/eventos/{ev}/cajas",
        json={"cajero_id": usuarios["acceso"].id, "fondo": "0"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 409
    r = await cliente_http.post(
        f"{TQ}/eventos/{ev}/cajas",
        json={
            "cajero_id": usuarios["org_a"].id,
            "fondo": "0",
            "folio_desde": 1010,
            "folio_hasta": 1020,
        },
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 409
    assert "otra caja" in r.json()["error"]["mensaje"]


async def test_configuracion_del_organizador(cliente_http, usuarios, esc):
    ev = esc["event_id"]
    r = await cliente_http.patch(
        f"{TQ}/eventos/{ev}/config",
        json={
            "taquilla_minutos_despues": 30,
            "texto_legal_medio": "Intransferible. Válido solo para esta función.",
            "medio_lo_paga": "organizador",
        },
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["taquilla_minutos_despues"], d["medio_lo_paga"]) == (30, "organizador")
    assert d["texto_legal_medio"].startswith("Intransferible")


# --------------------------------------------------------------------------
# Vender
# --------------------------------------------------------------------------


async def test_venta_en_efectivo_emite_boletos_y_correo(
    cliente_http, db, usuarios, esc, caja
):
    a, b = esc["seat_ids"][:2]
    cuerpo = venta(esc, [a, b], recibido="2000.00")
    r = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r.status_code == 201, r.text
    rec = r.json()
    assert (rec["total"], rec["recibido"], rec["cambio"]) == (
        "1600.00",
        "2000.00",
        "400.00",
    )
    assert rec["correo_tapado"] == "cl•••@correo.mx" and rec["cajero"]
    assert len(rec["lugares"]) == 2 and rec["folios"] == []

    orden = await db.scalar(select(Order).where(Order.folio == rec["folio"]))
    assert (orden.canal, orden.estado, orden.tarjeta_snap) == (
        "taquilla",
        "pagada",
        "Efectivo",
    )
    assert orden.capturada_en is not None
    assert (
        await fila(db, esc["performance_id"], a)
    ).estado == EstadoAsiento.VENDIDO.value
    boletos = (
        await db.scalars(
            select(Ticket.estado)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
        )
    ).all()
    assert boletos == ["valido", "valido"]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(Consent)
            .where(Consent.order_id == orden.id)
        )
        == 1
    )

    # Reintento del equipo con la misma clave: no se cobra dos veces.
    r = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r.json()["folio"] == rec["folio"]
    assert (
        await db.scalar(
            select(func.count()).select_from(Order).where(Order.canal == "taquilla")
        )
        == 1
    )

    # Le llega el mismo correo con QR que en linea, con "Efectivo".
    cartero = CarteroFalso()
    await bandeja(db, cartero).una_vuelta()
    (m,) = [x for x in cartero.enviados if x.para == "cliente@correo.mx"]
    assert "Ver mis boletos" in m.html and "Efectivo" in m.html

    # Ya no se pueden volver a vender.
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a]))
    assert r.status_code == 409
    assert r.json()["error"]["detalle"]["motivo"] == "lugar_ocupado"


async def test_reglas_de_cobro(cliente_http, db, usuarios, esc, caja):
    a, b, c = esc["seat_ids"][:3]
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a], recibido="10.00"))
    assert r.status_code == 409 and r.json()["error"]["detalle"]["campo"] == "recibido"
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a], forma="terminal"))
    assert (
        r.status_code == 409 and r.json()["error"]["detalle"]["campo"] == "autorizacion"
    )
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a], acepta_aviso=False))
    assert r.status_code == 409
    r = await vender(
        cliente_http,
        usuarios,
        caja,
        venta(esc, [a], forma="terminal", autorizacion="A1B2C3"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["autorizacion"] == "A1B2C3"
    orden = await db.scalar(select(Order).where(Order.folio == r.json()["folio"]))
    assert orden.tarjeta_snap == "Tarjeta en terminal · aut. A1B2C3"

    # Cortesia: con motivo, sin cobro, como cualquier cortesia.
    r = await vender(cliente_http, usuarios, caja, venta(esc, [b], forma="cortesia"))
    assert r.status_code == 409
    r = await vender(
        cliente_http,
        usuarios,
        caja,
        venta(esc, [b], forma="cortesia", motivo_cortesia="Familia del elenco"),
    )
    assert r.status_code == 201 and r.json()["total"] == "0.00"
    orden = await db.scalar(select(Order).where(Order.folio == r.json()["folio"]))
    assert orden.canal == "cortesia"
    assert (
        await db.scalar(select(Cortesia).where(Cortesia.order_id == orden.id))
    ).motivo == ("Taquilla: Familia del elenco")

    # Lo bloqueado no se vende en taquilla (ni sale libre en su mapa).
    await poner_estado(db, esc["performance_id"], c, EstadoAsiento.BLOQUEADO.value)
    r = await vender(cliente_http, usuarios, caja, venta(esc, [c]))
    assert r.status_code == 409
    r = await cliente_http.get(
        f"{TQ}/funciones/{esc['performance_id']}/mapa", headers=de(usuarios, "acceso")
    )
    estados = {x["id"]: x["estado"] for x in r.json()["butacas"]}
    assert (estados[a], estados[c]) == ("ocupada", "ocupada")


async def test_solo_el_cajero_vende_en_su_caja(cliente_http, usuarios, esc, caja):
    r = await vender(
        cliente_http, usuarios, caja, venta(esc, esc["seat_ids"][:1]), quien="org_a"
    )
    assert r.status_code == 403


async def test_fuera_de_horario(cliente_http, db, usuarios, esc, caja):
    await empieza_en(db, esc["performance_id"], -61)
    r = await vender(cliente_http, usuarios, caja, venta(esc, esc["seat_ids"][:1]))
    assert r.status_code == 409
    assert r.json()["error"]["detalle"]["motivo"] == "fuera_de_horario"


# --------------------------------------------------------------------------
# Medio fisico
# --------------------------------------------------------------------------


async def test_con_pulsera_entra_en_ese_momento(cliente_http, db, usuarios, esc, caja):
    a, b, c = esc["seat_ids"][:3]
    # Lejos de la funcion no se entrega: todavia no abre el acceso.
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a], folios=[1001]))
    assert r.status_code == 409 and r.json()["error"]["detalle"]["campo"] == "folios"

    await empieza_en(db, esc["performance_id"], 30)
    for malos in ([1001], [999, 1000], [1001, 1001]):
        r = await vender(cliente_http, usuarios, caja, venta(esc, [a, b], folios=malos))
        assert r.status_code == 409, malos
    r = await vender(
        cliente_http, usuarios, caja, venta(esc, [a, b], folios=[1001, 1002])
    )
    assert r.status_code == 201, r.text
    assert (
        r.json()["folios"] == [1001, 1002] and r.json()["medio"] == "Pulsera amarilla"
    )
    orden = await db.scalar(select(Order).where(Order.folio == r.json()["folio"]))
    estados = (
        await db.scalars(
            select(Ticket.estado)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
        )
    ).all()
    assert estados == ["usado", "usado"]
    notas = (
        await db.scalars(
            select(Scan.nota).where(Scan.performance_id == esc["performance_id"])
        )
    ).all()
    assert sorted(notas) == [
        "Entró por taquilla: Pulsera amarilla 1001",
        "Entró por taquilla: Pulsera amarilla 1002",
    ]
    # Un folio no se entrega dos veces.
    r = await vender(cliente_http, usuarios, caja, venta(esc, [c], folios=[1002]))
    assert r.status_code == 409 and "ya se entregaron" in r.json()["error"]["mensaje"]


# --------------------------------------------------------------------------
# Correcciones
# --------------------------------------------------------------------------


async def test_correccion_por_error_del_cajero(cliente_http, db, usuarios, esc, caja):
    a = esc["seat_ids"][0]
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a]))
    v = r.json()["uuid"]
    r = await cliente_http.post(
        f"{TQ}/ventas/{v}/anular",
        json={"motivo": "Cobró Luneta"},
        headers=de(usuarios, "acceso"),
    )
    assert r.status_code == 403
    r = await cliente_http.post(
        f"{TQ}/ventas/{v}/anular",
        json={"motivo": "Error de cajero: era Balcón"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["anulada"] is True
    assert (await fila(db, esc["performance_id"], a)).estado == "disponible"
    orden = await db.scalar(
        select(Order)
        .where(Order.folio == r.json()["folio"])
        .execution_options(populate_existing=True)
    )
    assert (orden.estado, orden.cerrada_motivo) == ("cancelada", "error_de_cajero")
    assert set(
        (
            await db.scalars(
                select(Ticket.estado)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .where(OrderItem.order_id == orden.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    ) == {"cancelado"}
    r = await cliente_http.post(
        f"{TQ}/ventas/{v}/anular",
        json={"motivo": "Otra vez"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 409
    # El lugar se vuelve a vender.
    r = await vender(cliente_http, usuarios, caja, venta(esc, [a]))
    assert r.status_code == 201


async def test_corregir_el_correo(cliente_http, db, usuarios, esc, caja):
    r = await vender(cliente_http, usuarios, caja, venta(esc, esc["seat_ids"][:1]))
    v, folio = r.json()["uuid"], r.json()["folio"]
    r = await cliente_http.post(
        f"{TQ}/ventas/{v}/correo",
        json={"email": "ana.bien@correo.mx"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["enviado_a"] == "an•••@correo.mx"
    orden = await db.scalar(
        select(Order)
        .where(Order.folio == folio)
        .execution_options(populate_existing=True)
    )
    assert orden.email_snap == "ana.bien@correo.mx"
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.tipo == "orden.reenvio")
        )
        == 1
    )


# --------------------------------------------------------------------------
# Corte a ciegas
# --------------------------------------------------------------------------


async def test_corte_a_ciegas(cliente_http, db, usuarios, esc, caja):
    a, b = esc["seat_ids"][:2]
    await vender(cliente_http, usuarios, caja, venta(esc, [a], recibido="1000.00"))
    await vender(
        cliente_http,
        usuarios,
        caja,
        venta(esc, [b], forma="terminal", autorizacion="999111"),
    )
    # El cajero no ve lo que espera el sistema; el jefe si.
    r = await cliente_http.get(f"{TQ}/cajas/{caja}", headers=de(usuarios, "acceso"))
    assert r.json()["caja"]["totales"] is None and len(r.json()["ventas"]) == 2
    r = await cliente_http.get(f"{TQ}/cajas/{caja}", headers=de(usuarios, "org_a"))
    t = r.json()["caja"]["totales"]
    assert (t["efectivo"], t["efectivo_esperado"], t["terminal"]) == (
        "800.00",
        "1300.00",
        "800.00",
    )
    assert t["folios_esperados"] == 10

    # Cuenta: $50 de menos en efectivo.
    r = await cliente_http.post(
        f"{TQ}/cajas/{caja}/contar",
        json={"efectivo": "1250.00", "terminal": "800.00", "folios_sobrantes": 10},
        headers=de(usuarios, "acceso"),
    )
    assert r.status_code == 200, r.text
    assert (
        r.json()["caja"]["estado"] == "contada" and r.json()["caja"]["totales"] is None
    )
    # Ya contada: ni vende ni se corrige.
    r = await vender(cliente_http, usuarios, caja, venta(esc, esc["seat_ids"][2:3]))
    assert r.status_code == 409
    # El cajero no cierra; el jefe necesita explicar la diferencia.
    r = await cliente_http.post(
        f"{TQ}/cajas/{caja}/cerrar", json={}, headers=de(usuarios, "acceso")
    )
    assert r.status_code == 403
    r = await cliente_http.post(
        f"{TQ}/cajas/{caja}/cerrar", json={}, headers=de(usuarios, "org_a")
    )
    assert r.status_code == 409 and r.json()["error"]["detalle"]["campo"] == "nota"
    r = await cliente_http.post(
        f"{TQ}/cajas/{caja}/cerrar",
        json={"nota": "Faltan $50: se dio cambio de más"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["caja"]["estado"] == "cerrada"
    # Cerrada, el cajero ya ve su corte y lo descarga.
    r = await cliente_http.get(f"{TQ}/cajas/{caja}", headers=de(usuarios, "acceso"))
    assert r.json()["caja"]["totales"]["efectivo_esperado"] == "1300.00"
    r = await cliente_http.get(
        f"{TQ}/cajas/{caja}/corte.xlsx", headers=de(usuarios, "acceso")
    )
    assert r.status_code == 200 and r.content[:2] == b"PK"
    # Y se le puede abrir otra caja.
    r = await cliente_http.post(
        f"{TQ}/eventos/{esc['event_id']}/cajas",
        json={"cajero_id": usuarios["acceso"].id, "fondo": "0"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 201


async def test_inventario_no_se_pierde(cliente_http, db, usuarios, esc, caja):
    """Todo lugar vendido por taquilla tiene su partida viva (indice unico)."""
    seats = esc["seat_ids"][:3]
    await vender(cliente_http, usuarios, caja, venta(esc, seats))
    vivos = await db.scalar(
        select(func.count())
        .select_from(OrderItem)
        .join(PerformanceSeat, PerformanceSeat.id == OrderItem.performance_seat_id)
        .where(PerformanceSeat.seat_id.in_(seats), OrderItem.anulado.is_(False))
    )
    assert vivos == 3


# --------------------------------------------------------------------------
# Sin internet (fase 3): reserva de la caja y ventas que se suben despues
# --------------------------------------------------------------------------


async def reservar(cliente_http, usuarios, caja, esc, quien="acceso", **kw):
    return await cliente_http.post(
        f"{TQ}/cajas/{caja}/reserva",
        json={"performance_id": esc["performance_id"], **kw},
        headers=de(usuarios, quien),
    )


async def test_reserva_aparta_lo_que_menos_se_vende(
    cliente_http, db, usuarios, esc, caja, hold_service
):
    from app.services.apartados import ApartadoService
    from tests.test_apartados import nuevo_hold

    # Luneta se vende mas: la reserva sale de Balcon, de las ultimas filas.
    luneta, balcon = esc["seat_ids"][:5], esc["seat_ids"][5:]
    for s in luneta[:3]:
        await poner_estado(db, esc["performance_id"], s, EstadoAsiento.VENDIDO.value)
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=3)
    assert r.status_code == 200, r.text
    lugares = r.json()["lugares"]
    assert len(lugares) == 3 and {x["zona"] for x in lugares} == {"Balcon"}
    assert {x["seat_id"] for x in lugares} <= set(balcon)
    assert r.json()["folios_usados"] == []
    # Pedir otra vez la misma cantidad no aparta mas.
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=3)
    assert len(r.json()["lugares"]) == 3

    # En linea esos lugares estan ocupados.
    reservado = lugares[0]["seat_id"]
    with pytest.raises(Exception) as e:
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], [reservado], nuevo_hold()
        )
    assert "Alguien más" in str(e.value)
    # Una zona escogida.
    r = await reservar(
        cliente_http, usuarios, caja, esc, cantidad=4, zone_id=esc["zonas"][0]
    )
    assert len(r.json()["lugares"]) == 4

    # Contar la caja la suelta.
    r = await cliente_http.post(
        f"{TQ}/cajas/{caja}/contar",
        json={"efectivo": "500.00", "terminal": "0", "folios_sobrantes": 10},
        headers=de(usuarios, "acceso"),
    )
    assert r.status_code == 200, r.text
    ps = await fila(db, esc["performance_id"], reservado)
    assert ps.estado == EstadoAsiento.DISPONIBLE.value and ps.hold_id is None


async def test_reserva_solo_con_folios_y_venta_abierta(
    cliente_http, db, usuarios, esc, caja
):
    org = de(usuarios, "org_a")
    ev = esc["event_id"]
    # Una caja sin folios no tiene reserva: sin red se entra con medio fisico.
    r = await cliente_http.post(
        f"{TQ}/eventos/{ev}/cajas",
        json={"cajero_id": usuarios["org_a"].id, "fondo": "0"},
        headers=org,
    )
    sin_folios = r.json()["uuid"]
    r = await reservar(cliente_http, usuarios, sin_folios, esc, quien="org_a")
    assert r.status_code == 409
    # Otro cajero no toca la reserva ajena (el jefe si).
    r = await reservar(cliente_http, usuarios, caja, esc, quien="org_a", cantidad=1)
    assert r.status_code == 200
    # Con la venta en linea cerrada no hace falta reserva.
    await db.execute(
        update(Performance)
        .where(Performance.id == esc["performance_id"])
        .values(venta_cierra_en=func.now() - timedelta(minutes=1))
    )
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=2)
    assert r.status_code == 409 and r.json()["error"]["detalle"]["motivo"] == (
        "venta_cerrada"
    )


async def test_venta_sin_red_se_sube_despues(cliente_http, db, usuarios, esc, caja):
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=2)
    a, b = (x["seat_id"] for x in r.json()["lugares"])

    # Sin folios no: sin red, el cliente entra con el medio fisico.
    cuerpo = venta(esc, [a], sin_red=True, vendida_en="2020-01-01T00:00:00Z")
    r = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r.status_code == 409 and r.json()["error"]["detalle"]["campo"] == "folios"

    cuerpo = venta(
        esc,
        [a, b],
        sin_red=True,
        folios=[1001, 1002],
        vendida_en="2020-01-01T00:00:00Z",
    )
    r = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r.status_code == 201, r.text
    # Reintentar la subida no la duplica.
    r2 = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r2.json()["folio"] == r.json()["folio"]

    orden = await db.scalar(select(Order).where(Order.folio == r.json()["folio"]))
    boletos = (
        await db.scalars(
            select(Ticket)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
        )
    ).all()
    assert {t.estado for t in boletos} == {"usado"}
    lecturas = (
        await db.scalars(
            select(Scan).where(Scan.ticket_id.in_([t.id for t in boletos]))
        )
    ).all()
    assert all(x.offline for x in lecturas)
    # La hora del equipo, pero nunca antes de abrir la caja.
    from app.models.taquilla import CajaDeTaquilla, VentaDeTaquilla

    la_caja = await db.scalar(select(CajaDeTaquilla).where(CajaDeTaquilla.uuid == caja))
    vta = await db.scalar(
        select(VentaDeTaquilla).where(VentaDeTaquilla.order_id == orden.id)
    )
    assert vta.sin_red and vta.vendida_en == la_caja.abierta_en == orden.pagada_en
    r = await cliente_http.get(
        f"{TQ}/cajas/{caja}/reserva", headers=de(usuarios, "acceso")
    )
    assert r.json()["lugares"] == [] and r.json()["folios_usados"] == [1001, 1002]


async def test_otra_caja_no_vende_la_reserva(cliente_http, db, usuarios, esc, caja):
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=1)
    reservado = r.json()["lugares"][0]["seat_id"]
    org = de(usuarios, "org_a")
    r = await cliente_http.post(
        f"{TQ}/eventos/{esc['event_id']}/cajas",
        json={"cajero_id": usuarios["org_a"].id, "fondo": "0"},
        headers=org,
    )
    otra = r.json()["uuid"]
    r = await vender(cliente_http, usuarios, otra, venta(esc, [reservado]), "org_a")
    assert r.status_code == 409
    assert r.json()["error"]["detalle"]["motivo"] == "lugar_ocupado"
    # Su caja si la vende, con red y desde el mapa.
    r = await cliente_http.get(
        f"{TQ}/funciones/{esc['performance_id']}/mapa",
        params={"caja": caja},
        headers=de(usuarios, "acceso"),
    )
    libres = {b["id"] for b in r.json()["butacas"] if b["estado"] == "libre"}
    assert reservado in libres
    r = await vender(cliente_http, usuarios, caja, venta(esc, [reservado]))
    assert r.status_code == 201, r.text
