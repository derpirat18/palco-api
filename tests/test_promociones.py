"""Promociones (PR1): 2x1 o porcentaje por dias de la funcion.

Lo que importa:

- La calculadora: en 2x1 se hacen pares por zona y el mas barato de cada par
  va sin costo; sin compras impares, salvo el ultimo lugar libre de la zona.
  El porcentaje se redondea al centavo. Si aplican dos, gana la de mayor
  descuento.
- Cuenta el dia de la funcion (a la hora del recinto), que este encendida,
  las fechas, el evento, la zona, el tipo de boleto y el canal. Las
  cortesias nunca llevan promocion.
- En linea y en taquilla se cobra lo mismo; sin red se respeta lo que cobro
  el equipo. Stripe cobra el total ya con descuento.
- Cada organizador ve y edita solo las suyas (lo ajeno, igual que lo
  inexistente); solo PSD pone una comision especial, que va por boleto.
- Una promocion que ya se uso no se borra: se apaga.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.catalog import Performance, Venue
from app.models.sales import Order, OrderItem
from app.services import promociones as p
from app.services.checkout import CheckoutService
from app.services.promociones import Lugar, Regla
from tests.test_apartados import funcion_a_la_venta
from tests.test_catalog import API, auth, igual_que_inexistente, token_de
from tests.test_checkout import PasarelaFalsa, apartar, comprador
from tests.test_taquilla import TQ, reservar, vender, venta

pytestmark = pytest.mark.asyncio
PR = f"{API}/promociones"
D = Decimal


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def tq(db, clientes):
    """Funcion del organizador A para taquilla: Luneta ($800) y Balcon ($400)."""
    return await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)


@pytest.fixture
async def caja_tq(cliente_http, usuarios, tq):
    """La cuenta de acceso de A es cajero, con caja y folios 1001-1010."""
    org = de(usuarios, "org_a")
    ev = tq["event_id"]
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


def de(usuarios, quien) -> dict:
    return auth(token_de(usuarios[quien]))


def promo(**kw) -> dict:
    cuerpo = {
        "nombre": "2x1 de la semana",
        "tipo": "2x1",
        "dias": [0, 1, 2, 3, 4, 5, 6],
        "en_linea": True,
        "en_taquilla": True,
        "todos_los_eventos": True,
    }
    return {**cuerpo, **kw}


async def crear(cliente_http, usuarios, quien="org_a", **kw) -> dict:
    r = await cliente_http.post(PR, json=promo(**kw), headers=de(usuarios, quien))
    assert r.status_code == 201, r.text
    return r.json()


async def dia_de(db, esc) -> int:
    """El dia de la semana de la funcion, a la hora del recinto."""
    from app.models.catalog import Event

    inicia, zh = (
        await db.execute(
            select(Performance.inicia_en, Venue.zona_horaria)
            .join(Event, Event.id == Performance.event_id)
            .join(Venue, Venue.id == Event.venue_id)
            .where(Performance.id == esc["performance_id"])
        )
    ).one()
    return p.fecha_local(inicia, zh).weekday()


async def iniciar(db, hold_service, stripe, esc, seats, **kw):
    hold = await apartar(db, hold_service, esc, seats)
    return await CheckoutService(db, hold_service, stripe).iniciar(
        esc["performance_id"], hold, comprador(), **kw
    )


# --------------------------------------------------------------------------
# La calculadora
# --------------------------------------------------------------------------


def regla(**kw) -> Regla:
    datos = {
        "id": 1,
        "nombre": "2x1",
        "tipo": "2x1",
        "porcentaje": None,
        "todos_los_tipos": False,
        "zonas": None,
    }
    return Regla(**{**datos, **kw})


async def test_2x1_pares_por_zona_y_el_mas_barato_gratis():
    lugares = [
        Lugar(1, 10, "adulto", D("800")),
        Lugar(2, 10, "adulto", D("800")),
        Lugar(3, 10, "nino", D("500")),
        Lugar(4, 10, "mayor", D("600")),
        Lugar(5, 20, "adulto", D("400")),
        Lugar(6, 20, "adulto", D("400")),
    ]
    # Solo adulto: Luneta (800, 800) y Balcon (400, 400).
    r = p.aplicar(regla(), lugares, set())
    assert r.descuentos == {2: D("800"), 6: D("400")} and not r.faltan
    assert r.alcanzados == {1, 2, 5, 6}
    # Con todos los tipos: 800, 800, 600, 500 -> pares (800,800) y (600,500).
    r = p.aplicar(regla(todos_los_tipos=True), lugares, set())
    assert r.descuentos == {2: D("800"), 3: D("500"), 6: D("400")}
    # Solo en Balcon.
    r = p.aplicar(regla(zonas=frozenset({20})), lugares, set())
    assert r.descuentos == {6: D("400")}


async def test_2x1_sin_impares_salvo_el_ultimo_lugar():
    tres = [Lugar(i, 10, "adulto", D("800")) for i in (1, 2, 3)]
    r = p.aplicar(regla(), tres, set())
    assert r.faltan == (10,) and r.descuento == D("800")
    # Ya no queda otro lugar libre en la zona: el impar paga completo.
    r = p.aplicar(regla(), tres, {10})
    assert r.faltan == () and r.descuento == D("800")
    assert "1 lugar más en Luneta" in p.mensaje_de_par(
        p.aplicar(regla(), tres, set()), {10: "Luneta"}
    )


async def test_porcentaje_al_centavo_y_gana_el_mayor():
    lugares = [Lugar(1, 10, "adulto", D("333.33")), Lugar(2, 10, "nino", D("200"))]
    r = p.aplicar(regla(tipo="porcentaje", porcentaje=D("15")), lugares, set())
    # Solo adulto; 333.33 x 15 % = 49.9995 -> 50.00.
    assert r.descuentos == {1: D("50.00")}

    dos = [Lugar(1, 10, "adulto", D("800")), Lugar(2, 10, "adulto", D("800"))]
    dos_x_uno = regla(id=1)
    treinta = regla(id=2, tipo="porcentaje", porcentaje=D("30"))
    sesenta = regla(id=3, tipo="porcentaje", porcentaje=D("60"))
    assert p.mejor([dos_x_uno, treinta], dos, set()).regla.id == 1  # 800 > 480
    assert p.mejor([dos_x_uno, sesenta], dos, set()).regla.id == 3  # 960 > 800
    # Sin nada que descontar, no hay promocion...
    assert p.mejor([treinta], [Lugar(1, 10, "nino", D("1"))], set()).regla is None
    # ...pero un 2x1 con un solo boleto pide el segundo.
    uno = [Lugar(1, 10, "adulto", D("800"))]
    assert p.mejor([dos_x_uno], uno, set()).faltan == (10,)


async def test_comision_especial_solo_en_los_boletos_de_la_promocion():
    lugares = [Lugar(1, 10, "adulto", D("800")), Lugar(2, 20, "adulto", D("400"))]
    r = p.aplicar(
        regla(tipo="porcentaje", porcentaje=D("10"), zonas=frozenset({10}),
              comision_pct=D("5")),
        lugares,
        set(),
    )  # fmt: skip
    assert r.comision_de(1) == D("5") and r.comision_de(2) is None
    assert p.SIN_PROMOCION.comision_de(1) is None


# --------------------------------------------------------------------------
# Panel: quien y que
# --------------------------------------------------------------------------


async def test_cada_organizador_lo_suyo(cliente_http, usuarios, clientes, db):
    esc_a = await funcion_a_la_venta(db, clientes[0].id)
    esc_b = await funcion_a_la_venta(db, clientes[1].id)
    await db.commit()
    a = await crear(cliente_http, usuarios)
    assert a["client_id"] == clientes[0].id and a["valor"] == "2x1"
    assert a["dias_texto"] == "Todos los días" and a["activa"] is True
    assert a["funciones_proximas"] == 1 and a["proxima"]["evento"]

    # El organizador nunca crea para otro, aunque lo pida.
    otra = await crear(
        cliente_http, usuarios, nombre="Martes", client_id=clientes[1].id, dias=[1]
    )
    assert otra["client_id"] == clientes[0].id and otra["dias_texto"] == "Martes"

    # B no ve ni toca las de A: igual que si no existieran.
    b = de(usuarios, "org_b")
    r = await cliente_http.get(PR, headers=b)
    assert r.json() == []
    for metodo, extra in (
        ("get", {}),
        ("put", {"json": promo()}),
        ("patch", {"json": {"activa": False}}),
        ("delete", {}),
    ):
        cola = "/activa" if metodo == "patch" else ""
        url, nada = f"{PR}/{a['id']}{cola}", f"{PR}/999999{cola}"
        igual_que_inexistente(
            await getattr(cliente_http, metodo)(url, headers=b, **extra),
            await getattr(cliente_http, metodo)(nada, headers=b, **extra),
        )
    # Ni con un evento ajeno.
    r = await cliente_http.post(
        PR,
        json=promo(todos_los_eventos=False, eventos=[{"event_id": esc_a["event_id"]}]),
        headers=b,
    )
    assert r.status_code == 404
    r = await cliente_http.post(
        PR,
        json=promo(todos_los_eventos=False, eventos=[{"event_id": esc_b["event_id"]}]),
        headers=b,
    )
    assert r.status_code == 201, r.text

    # La puerta no entra.
    r = await cliente_http.get(PR, headers=de(usuarios, "acceso"))
    assert r.status_code == 403

    # PSD ve todas y filtra por organizador.
    psd = de(usuarios, "admin")
    assert len((await cliente_http.get(PR, headers=psd)).json()) == 3
    r = await cliente_http.get(PR, params={"client_id": clientes[1].id}, headers=psd)
    assert [x["client_id"] for x in r.json()] == [clientes[1].id]
    # PSD tiene que decir de que organizador es.
    r = await cliente_http.post(PR, json=promo(), headers=psd)
    assert r.status_code == 422


async def test_solo_psd_pone_la_comision(cliente_http, usuarios, clientes, db):
    await funcion_a_la_venta(db, clientes[0].id)
    await db.commit()
    org = de(usuarios, "org_a")
    r = await cliente_http.post(PR, json=promo(comision_pct="5"), headers=org)
    assert r.status_code == 403
    a = await crear(cliente_http, usuarios)
    r = await cliente_http.put(
        f"{PR}/{a['id']}",
        json=promo(client_id=clientes[0].id, comision_pct="5"),
        headers=de(usuarios, "admin"),
    )
    assert r.status_code == 200 and r.json()["comision_pct"] == "5.00"
    # El organizador edita lo demas sin tocarla (y si la manda igual, pasa).
    r = await cliente_http.put(
        f"{PR}/{a['id']}", json=promo(nombre="Otro nombre"), headers=org
    )
    assert r.status_code == 200 and r.json()["comision_pct"] == "5.00"
    r = await cliente_http.put(
        f"{PR}/{a['id']}", json=promo(comision_pct="5.00"), headers=org
    )
    assert r.status_code == 200
    r = await cliente_http.put(
        f"{PR}/{a['id']}", json=promo(comision_pct=None), headers=org
    )
    assert r.status_code == 403


async def test_datos_que_se_corrigen(cliente_http, usuarios, clientes, db):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    await db.commit()
    org = de(usuarios, "org_a")
    malos = [
        (promo(dias=[]), None),
        (promo(tipo="porcentaje"), "porcentaje"),
        (promo(tipo="porcentaje", porcentaje="95"), None),
        (promo(en_linea=False, en_taquilla=False), "en_linea"),
        (promo(desde="2026-12-10", hasta="2026-12-01"), "hasta"),
        (promo(todos_los_eventos=False), "eventos"),
        (
            promo(
                todos_los_eventos=False,
                eventos=[{"event_id": esc["event_id"], "zonas": [999999]}],
            ),
            "eventos",
        ),
    ]
    for cuerpo, campo in malos:
        r = await cliente_http.post(PR, json=cuerpo, headers=org)
        assert r.status_code == 422, (cuerpo, r.text)
        if campo:
            assert r.json()["error"]["detalle"]["campo"] == campo


async def test_vista_previa_y_opciones(cliente_http, usuarios, clientes, db):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    await db.commit()
    org = de(usuarios, "org_a")
    r = await cliente_http.get(f"{PR}/opciones", headers=org)
    assert r.status_code == 200, r.text
    ev = r.json()["eventos"][0]
    assert ev["event_id"] == esc["event_id"] and ev["funciones"] == 1
    assert [z["nombre"] for z in ev["zonas"]] == ["Luneta", "Balcon"]
    assert r.json()["organizadores"] == []
    psd = await cliente_http.get(f"{PR}/opciones", headers=de(usuarios, "admin"))
    assert len(psd.json()["organizadores"]) == 2

    dia = await dia_de(db, esc)
    luneta = ev["zonas"][0]["ids"]
    cuerpo = promo(
        dias=[dia],
        todos_los_eventos=False,
        eventos=[{"event_id": esc["event_id"], "zonas": luneta}],
    )
    r = await cliente_http.post(f"{PR}/vista-previa", json=cuerpo, headers=org)
    assert r.status_code == 200, r.text
    assert r.json()["total_funciones"] == 1
    assert r.json()["funciones"][0]["zonas"] == "Luneta"
    otro = (dia + 1) % 7
    r = await cliente_http.post(
        f"{PR}/vista-previa", json={**cuerpo, "dias": [otro]}, headers=org
    )
    assert r.json()["total_funciones"] == 0

    # Otra promocion (aunque este apagada) que cae en la misma funcion se avisa.
    a = await crear(cliente_http, usuarios, nombre="Treinta", tipo="porcentaje",
                    porcentaje="30", activa=False)  # fmt: skip
    r = await cliente_http.post(f"{PR}/vista-previa", json=cuerpo, headers=org)
    assert r.json()["conflictos"] == [
        {"id": a["id"], "nombre": "Treinta", "activa": False, "valor": "30 %"}
    ]


# --------------------------------------------------------------------------
# En linea
# --------------------------------------------------------------------------


async def test_en_linea_2x1(cliente_http, usuarios, clientes, db, hold_service,
                            stripe_falso):  # fmt: skip
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=6)
    await db.commit()
    a = await crear(cliente_http, usuarios)
    luneta = esc["seat_ids"][:6]
    iniciada = await iniciar(db, hold_service, stripe_falso, esc, luneta[:4])
    orden = iniciada.orden
    assert orden.total == D("1600.00") and orden.descuento == D("1600.00")
    assert orden.promocion_id == a["id"] and orden.promocion_snap == "2x1 de la semana"
    # La factura cuadra: subtotal + IVA - descuento = total.
    assert orden.subtotal + orden.iva - orden.descuento == orden.total
    importes = sorted(x.importe for x in iniciada.partidas)
    assert importes == [D("0.00"), D("0.00"), D("800.00"), D("800.00")]
    assert all(x.iva == 0 for x in iniciada.partidas if x.importe == 0)
    intento = stripe_falso.intentos[orden.stripe_payment_intent_id]
    assert intento.monto_centavos == 160000


async def test_en_linea_impar_pide_otro(clientes, db, hold_service, stripe_falso):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=6)
    await p.guardar(db, _usuario(clientes[0].id), clientes[0].id, _datos())
    with pytest.raises(p.ParIncompleto) as e:
        await iniciar(db, hold_service, stripe_falso, esc, esc["seat_ids"][:3])
    assert "Luneta" in e.value.mensaje


async def test_el_ultimo_lugar_paga_completo(clientes, db, hold_service, stripe_falso):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=3)
    await p.guardar(db, _usuario(clientes[0].id), clientes[0].id, _datos())
    # Toda la Luneta: 3 lugares y no queda otro.
    iniciada = await iniciar(db, hold_service, stripe_falso, esc, esc["seat_ids"][:3])
    assert iniciada.orden.total == D("1600.00")
    assert iniciada.orden.descuento == D("800.00")


async def test_cuando_aplica(clientes, db, hold_service, stripe_falso):
    """Cada caso en su propia funcion: 2 lugares de Luneta ($1,600 sin promo)."""
    otro = await funcion_a_la_venta(db, clientes[0].id)

    async def total(armar, client_id=None) -> Decimal:
        esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=6)
        dia = await dia_de(db, esc)
        funcion = await db.get(Performance, esc["performance_id"])
        cid = client_id or clientes[0].id
        promo_ = await p.guardar(
            db, _usuario(cid), cid, _datos(**armar(esc, dia, funcion))
        )
        try:
            iniciada = await iniciar(
                db, hold_service, stripe_falso, esc, esc["seat_ids"][:2]
            )
            return iniciada.orden.total
        finally:
            promo_.activa = False
            await db.flush()

    assert await total(lambda e, d, f: {"dias": [d]}) == D("800.00")
    assert await total(lambda e, d, f: {"dias": [(d + 1) % 7]}) == D("1600.00")
    assert await total(lambda e, d, f: {"activa": False}) == D("1600.00")
    assert await total(lambda e, d, f: {"en_linea": False}) == D("1600.00")
    assert await total(
        lambda e, d, f: {"desde": f.inicia_en.date() + timedelta(days=5)}
    ) == D("1600.00")
    assert await total(
        lambda e, d, f: {"hasta": f.inicia_en.date() - timedelta(days=5)}
    ) == D("1600.00")
    # Otro evento, la otra zona o su zona.
    assert await total(
        lambda e, d, f: {
            "todos_los_eventos": False,
            "eventos": [(otro["event_id"], None)],
        }
    ) == D("1600.00")
    assert await total(
        lambda e, d, f: {
            "todos_los_eventos": False,
            "eventos": [(e["event_id"], [e["zonas"][1]])],
        }
    ) == D("1600.00")
    assert await total(
        lambda e, d, f: {
            "todos_los_eventos": False,
            "eventos": [(e["event_id"], [e["zonas"][0]])],
        }
    ) == D("800.00")
    # De otro organizador: no.
    assert await total(lambda e, d, f: {}, client_id=clientes[1].id) == D("1600.00")


async def test_gana_la_de_mayor_descuento(clientes, db, hold_service, stripe_falso):
    esc = await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=6)
    usuario = _usuario(clientes[0].id)
    await p.guardar(db, usuario, clientes[0].id, _datos())
    treinta = await p.guardar(
        db, usuario, clientes[0].id,
        _datos(nombre="Treinta", tipo="porcentaje", porcentaje=D("30")),
    )  # fmt: skip
    iniciada = await iniciar(db, hold_service, stripe_falso, esc, esc["seat_ids"][:2])
    assert iniciada.orden.promocion_snap == "2x1" and iniciada.orden.total == D("800")
    treinta.porcentaje = D("60")
    await db.flush()
    iniciada = await iniciar(db, hold_service, stripe_falso, esc, esc["seat_ids"][2:4])
    assert iniciada.orden.promocion_snap == "Treinta"
    assert iniciada.orden.total == D("640.00")


async def test_publico_ve_la_promocion(cliente_http, usuarios, clientes, db):
    from app.api.v1.publico import get_redis_publico
    from app.main import app
    from app.models.catalog import Event

    esc = await funcion_a_la_venta(db, clientes[0].id)
    await db.commit()
    await crear(cliente_http, usuarios, nombre="Solo taquilla", en_linea=False)
    a = await crear(cliente_http, usuarios)

    class SinCache:
        async def get(self, _):
            return None

        async def set(self, *a, **k):
            return None

        async def incr(self, *a, **k):
            return 1

        async def zincrby(self, *a, **k):
            return 1

        async def expire(self, *a, **k):
            return True

    app.dependency_overrides[get_redis_publico] = lambda: SinCache()
    try:
        r = await cliente_http.get(
            f"{API}/publico/funciones/{esc['performance_id']}/mapa"
        )
        evento = await db.get(Event, esc["event_id"])
        r2 = await cliente_http.get(f"{API}/publico/eventos/{evento.slug}")
    finally:
        app.dependency_overrides.pop(get_redis_publico, None)
    assert r.status_code == 200, r.text
    esperado = [
        {
            "id": a["id"],
            "nombre": "2x1 de la semana",
            "tipo": "2x1",
            "porcentaje": None,
            "todos_los_tipos": False,
            "zonas": None,
        }
    ]
    assert r.json()["funcion"]["promociones"] == esperado
    assert r2.status_code == 200, r2.text
    assert r2.json()["funciones"][0]["promociones"] == esperado


# --------------------------------------------------------------------------
# Taquilla
# --------------------------------------------------------------------------


async def test_taquilla_cobra_lo_mismo(cliente_http, usuarios, tq, caja_tq, db):
    esc, caja = tq, caja_tq
    await crear(cliente_http, usuarios, nombre="Solo en linea", en_taquilla=False,
                tipo="porcentaje", porcentaje="90")  # fmt: skip
    a = await crear(cliente_http, usuarios)
    r = await cliente_http.get(
        f"{API}/taquilla/funciones/{esc['performance_id']}/mapa",
        headers=de(usuarios, "acceso"),
    )
    assert [x["id"] for x in r.json()["promociones"]] == [a["id"]]

    luneta = esc["seat_ids"][:5]
    r = await vender(cliente_http, usuarios, caja, venta(esc, luneta[:2]))
    assert r.status_code == 201, r.text
    recibo = r.json()
    assert recibo["total"] == "800.00" and recibo["descuento"] == "800.00"
    assert recibo["promocion"] == "2x1 de la semana"
    assert recibo["cambio"] == "4200.00"
    # Impar con lugares libres en la zona: se pide otro.
    r = await vender(cliente_http, usuarios, caja, venta(esc, luneta[2:3]))
    assert r.status_code == 409
    assert r.json()["error"]["detalle"]["motivo"] == "par_incompleto"
    # La cortesia no lleva promocion.
    r = await vender(
        cliente_http, usuarios, caja,
        venta(esc, luneta[2:4], forma="cortesia", motivo_cortesia="Prensa"),
    )  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["total"] == "0.00" and r.json()["promocion"] is None


async def test_sin_red_se_respeta_lo_cobrado(cliente_http, usuarios, tq, caja_tq, db):
    esc, caja = tq, caja_tq
    a = await crear(cliente_http, usuarios)
    r = await reservar(cliente_http, usuarios, caja, esc, cantidad=2)
    assert r.status_code == 200, r.text
    reserva = r.json()
    assert reserva["promociones"][0]["performance_id"] == esc["performance_id"]
    assert reserva["promociones"][0]["promociones"][0]["id"] == a["id"]
    lugares = reserva["lugares"]
    # Se apaga despues de que el equipo bajo la reserva.
    r = await cliente_http.patch(
        f"{PR}/{a['id']}/activa", json={"activa": False}, headers=de(usuarios, "org_a")
    )
    assert r.json()["activa"] is False
    cuerpo = venta(
        esc,
        [x["seat_id"] for x in lugares],
        sin_red=True,
        folios=[1001, 1002],
        vendida_en="2020-01-01T00:00:00Z",
        promocion_id=a["id"],
    )
    r = await vender(cliente_http, usuarios, caja, cuerpo)
    assert r.status_code == 201, r.text
    precios = sorted(D(x["precio"]) for x in lugares)
    assert D(r.json()["total"]) == precios[1]
    assert r.json()["promocion"] == "2x1 de la semana"


# --------------------------------------------------------------------------
# Reportes y borrar
# --------------------------------------------------------------------------


async def test_reportes_y_no_se_borra_usada(cliente_http, usuarios, tq, caja_tq, db):
    esc, caja = tq, caja_tq
    a = await crear(cliente_http, usuarios)
    r = await cliente_http.put(
        f"{PR}/{a['id']}",
        json=promo(client_id=esc_cliente(usuarios), comision_pct="5"),
        headers=de(usuarios, "admin"),
    )
    assert r.status_code == 200, r.text
    r = await vender(cliente_http, usuarios, caja, venta(esc, esc["seat_ids"][:2]))
    assert r.status_code == 201, r.text
    items = (
        await db.scalars(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.folio == r.json()["folio"])
        )
    ).all()
    assert {x.comision_pct_snap for x in items} == {D("5.00")}

    # El correo dice el descuento y cuadra: Subtotal + IVA = Total.
    from app.services.confirmacion import armar_confirmacion

    orden = await db.scalar(select(Order).where(Order.folio == r.json()["folio"]))
    m = await armar_confirmacion(db, orden.uuid)
    assert "Descuento · 2x1 de la semana" in m.html
    assert "- Descuento (2x1 de la semana): \u2212$800.00" in m.texto
    assert f"Subtotal: ${orden.total - orden.iva:,.2f}" in m.texto

    org = de(usuarios, "org_a")
    r = await cliente_http.get(f"{API}/reportes/ventas", headers=org)
    ind = {i["etiqueta"]: i["valor"] for i in r.json()["indicadores"]}
    assert D(ind["Venta bruta"]) == D("800.00")
    assert D(ind["Descuentos por promociones"]) == D("800.00")
    # 5 % (la de la promocion) sobre lo que se cobro.
    assert D(ind["Comisión de PSD"]) == D("40.00")

    r = await cliente_http.get(f"{API}/reportes/promociones", headers=org)
    assert r.status_code == 200, r.text
    fila = r.json()["filas"][0]
    assert fila["promocion"] == "2x1 de la semana" and fila["sin_costo"] == 1
    assert (fila["compras"], fila["boletos"]) == (1, 2)
    assert D(fila["descuento"]) == D("800.00") and D(fila["comision"]) == D("40.00")
    for formato in ("xlsx", "csv", "pdf"):
        r = await cliente_http.get(
            f"{API}/reportes/promociones/descargar",
            params={"formato": formato},
            headers=org,
        )
        assert r.status_code == 200, (formato, r.text)

    # Usada: no se borra, se apaga.
    r = await cliente_http.delete(f"{PR}/{a['id']}", headers=org)
    assert r.status_code == 409
    assert (await cliente_http.get(f"{PR}/{a['id']}", headers=org)).json()["usada"]
    nueva = await crear(cliente_http, usuarios, nombre="Sin usar")
    r = await cliente_http.delete(f"{PR}/{nueva['id']}", headers=org)
    assert r.status_code == 204


# --------------------------------------------------------------------------
# Ayudantes
# --------------------------------------------------------------------------


def esc_cliente(usuarios) -> int:
    return usuarios["org_a"].client_id


def _usuario(client_id: int):
    from app.models.base import RolUsuario
    from app.models.users import User

    return User(id=None, rol=RolUsuario.ORGANIZADOR.value, client_id=client_id)


def _datos(**kw) -> p.Datos:
    datos = {
        "nombre": "2x1",
        "tipo": "2x1",
        "porcentaje": None,
        "dias": list(range(7)),
        "todos_los_tipos": False,
        "en_linea": True,
        "en_taquilla": True,
        "desde": None,
        "hasta": None,
        "todos_los_eventos": True,
        "eventos": [],
    }
    return p.Datos(**{**datos, **kw})
