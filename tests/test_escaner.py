"""Escaner en linea de la puerta (E3a) y asignacion de cuentas a eventos.

Lo que importa:

- Una cuenta de acceso solo ve y escanea los eventos a los que PSD o su
  organizador la asignaron. Para todo lo demas, el evento no existe.
- Cada funcion escanea solo en su horario: de 4 h antes a 12 h despues.
- Una entrada por error se deshace (PSD u organizador) y queda constancia.
- Un boleto entra una sola vez, aunque dos puertas lo lean a la vez.
- Reintentar la misma lectura (la red se cayo) no la vuelve "ya entro".
- La puerta ve zona, asiento y nombre; nunca correo ni telefono.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.base import EstadoFuncion
from app.models.catalog import Client, Performance
from app.models.sales import Order, OrderItem, Scan, Ticket
from app.services.boletos import contenido_del_qr
from app.services.escaner import EscanerService
from tests.test_apartados import funcion_a_la_venta
from tests.test_catalog import API, igual_que_inexistente
from tests.test_cortesias import de, esc, otorgar, revocar, stripe_falso  # noqa: F401

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Ayudantes
# --------------------------------------------------------------------------


async def asignar(cliente_http, usuarios, event_ids, cuenta="acceso", quien="admin"):
    return await cliente_http.put(
        f"{API}/usuarios/{usuarios[cuenta].id}/eventos",
        json={"event_ids": event_ids},
        headers=de(usuarios, quien),
    )


async def boletos_de(db, orden_uuid) -> list[Ticket]:
    return list(
        (
            await db.scalars(
                select(Ticket)
                .join(OrderItem, OrderItem.id == Ticket.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.uuid == orden_uuid)
                .order_by(Ticket.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def abrir_puerta(db, pid, en=timedelta(hours=1)) -> None:
    """La funcion empieza pronto: su horario de escaneo ya esta abierto."""
    funcion = await db.get(Performance, pid)
    funcion.inicia_en = datetime.now(UTC) + en
    await db.flush()


async def leer(cliente_http, usuarios, pid, codigo, quien="acceso", scan_id=None):
    return await cliente_http.post(
        f"{API}/performances/{pid}/escanear",
        json={"codigo": codigo, "client_scan_id": scan_id or uuid.uuid4().hex},
        headers=de(usuarios, quien),
    )


@pytest.fixture
async def puerta(cliente_http, db, esc, usuarios):  # noqa: F811
    """Funcion con una cortesia de dos boletos y la cuenta de acceso asignada."""
    pid = esc["performance_id"]
    await abrir_puerta(db, pid)
    orden = (
        await otorgar(cliente_http, usuarios, pid, esc["seat_ids"][:2], nombre="Lupe")
    ).json()
    assert (await asignar(cliente_http, usuarios, [esc["event_id"]])).status_code == 200
    boletos = await boletos_de(db, orden["uuid"])
    return {"pid": pid, "orden": orden, "boletos": boletos, **esc}


# --------------------------------------------------------------------------
# Asignacion
# --------------------------------------------------------------------------


async def test_sin_asignar_la_cuenta_de_acceso_no_ve_el_evento(
    cliente_http,
    esc,  # noqa: F811
    usuarios,
):
    eid, pid = esc["event_id"], esc["performance_id"]
    h = de(usuarios, "acceso")

    assert (await cliente_http.get(f"{API}/events", headers=h)).json() == []
    igual_que_inexistente(
        await cliente_http.get(f"{API}/events/{eid}", headers=h),
        await cliente_http.get(f"{API}/events/999999", headers=h),
    )
    igual_que_inexistente(
        await cliente_http.get(f"{API}/events/{eid}/performances", headers=h),
        await cliente_http.get(f"{API}/events/999999/performances", headers=h),
    )
    igual_que_inexistente(
        await cliente_http.get(f"{API}/performances/{pid}/lista-de-acceso", headers=h),
        await cliente_http.get(f"{API}/performances/999999/lista-de-acceso", headers=h),
    )
    igual_que_inexistente(
        await cliente_http.get(f"{API}/performances/{pid}/inventario", headers=h),
        await cliente_http.get(f"{API}/performances/999999/inventario", headers=h),
    )
    igual_que_inexistente(
        await leer(cliente_http, usuarios, pid, "ABCDE-FGHJK"),
        await leer(cliente_http, usuarios, 999999, "ABCDE-FGHJK"),
    )
    assert (await cliente_http.get(f"{API}/acceso/funciones", headers=h)).json() == []


async def test_asignada_ve_solo_ese_evento(cliente_http, db, clientes, esc, usuarios):  # noqa: F811
    otro = await funcion_a_la_venta(db, clientes[0].id)  # mismo organizador
    r = await asignar(cliente_http, usuarios, [esc["event_id"]])
    assert r.status_code == 200, r.text
    assert [e["event_id"] for e in r.json()] == [esc["event_id"]]

    h = de(usuarios, "acceso")
    eventos = (await cliente_http.get(f"{API}/events", headers=h)).json()
    assert [e["id"] for e in eventos] == [esc["event_id"]]
    assert (
        await cliente_http.get(f"{API}/events/{otro['event_id']}", headers=h)
    ).status_code == 404

    funciones = (await cliente_http.get(f"{API}/acceso/funciones", headers=h)).json()
    assert [f["performance_id"] for f in funciones] == [esc["performance_id"]]
    assert set(funciones[0]) >= {"evento", "recinto", "zona_horaria", "total", "usados"}

    # Quitarle el evento le corta el acceso en la siguiente peticion.
    assert (await asignar(cliente_http, usuarios, [])).json() == []
    assert (await cliente_http.get(f"{API}/events", headers=h)).json() == []


async def test_el_organizador_sigue_viendo_todos_sus_eventos(
    cliente_http,
    db,
    clientes,
    esc,  # noqa: F811
    usuarios,
):
    otro = await funcion_a_la_venta(db, clientes[0].id)
    h = de(usuarios, "org_a")
    ids = {e["id"] for e in (await cliente_http.get(f"{API}/events", headers=h)).json()}
    assert ids == {esc["event_id"], otro["event_id"]}
    funciones = (await cliente_http.get(f"{API}/acceso/funciones", headers=h)).json()
    assert len(funciones) == 2


async def test_asignan_psd_y_el_organizador_solo_a_eventos_del_mismo_organizador(
    cliente_http,
    db,
    clientes,
    esc,  # noqa: F811
    usuarios,
):
    ajeno = await funcion_a_la_venta(db, clientes[1].id)

    # El organizador asigna a su propio personal (U2).
    r = await asignar(cliente_http, usuarios, [esc["event_id"]], quien="org_a")
    assert r.status_code == 200, r.text
    assert [e["event_id"] for e in r.json()] == [esc["event_id"]]
    r = await asignar(cliente_http, usuarios, [esc["event_id"]], quien="acceso")
    assert r.status_code == 403
    # Para el organizador, un evento ajeno no existe: ni su nombre sale.
    igual_que_inexistente(
        await asignar(cliente_http, usuarios, [ajeno["event_id"]], quien="org_a"),
        await asignar(cliente_http, usuarios, [999999], quien="org_a"),
    )
    # Y el personal de otro organizador tampoco.
    igual_que_inexistente(
        await asignar(cliente_http, usuarios, [ajeno["event_id"]], quien="org_b"),
        await cliente_http.put(
            f"{API}/usuarios/999999/eventos",
            json={"event_ids": [ajeno["event_id"]]},
            headers=de(usuarios, "org_b"),
        ),
    )
    await asignar(cliente_http, usuarios, [])

    r = await asignar(cliente_http, usuarios, [ajeno["event_id"]])
    assert r.status_code == 409
    assert "otro organizador" in r.json()["error"]["mensaje"]

    r = await asignar(cliente_http, usuarios, [esc["event_id"]], cuenta="org_a")
    assert r.status_code == 409

    r = await asignar(cliente_http, usuarios, [999999])
    assert r.status_code == 404

    # Las de PSD no existen para el panel.
    r = await asignar(cliente_http, usuarios, [esc["event_id"]], cuenta="admin")
    assert r.status_code == 404

    lectura = await cliente_http.get(
        f"{API}/usuarios/{usuarios['acceso'].id}/eventos", headers=de(usuarios, "admin")
    )
    assert lectura.json() == []


# --------------------------------------------------------------------------
# Escanear
# --------------------------------------------------------------------------


async def test_valido_entra_y_queda_usado(cliente_http, db, puerta, usuarios):
    b = puerta["boletos"][0]
    r = await leer(cliente_http, usuarios, puerta["pid"], contenido_del_qr(b.uuid))

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["resultado"] == "valido"
    assert cuerpo["boleto"] == {
        "codigo": b.codigo,
        "folio": puerta["orden"]["folio"],
        "zona": "Luneta",
        "asiento": cuerpo["boleto"]["asiento"],
        "titular": "Lupe",
        "cortesia": True,
    }
    assert cuerpo["contador"] == {"total": 2, "usados": 1}
    # Nada de contacto en la pantalla de la puerta.
    assert "@" not in r.text
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "usado"

    scan = await db.scalar(select(Scan).where(Scan.ticket_id == b.id))
    assert (scan.user_id, scan.resultado, scan.offline) == (
        usuarios["acceso"].id,
        "valido",
        False,
    )
    # Se guarda el codigo impreso, no el QR (la credencial del boleto).
    assert scan.codigo_leido == b.codigo


async def test_la_segunda_vez_dice_cuando_y_con_quien_entro(
    cliente_http, puerta, usuarios
):
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    primera = (await leer(cliente_http, usuarios, puerta["pid"], qr)).json()

    r = await leer(cliente_http, usuarios, puerta["pid"], qr, quien="org_a")

    cuerpo = r.json()
    assert cuerpo["resultado"] == "duplicado"
    assert cuerpo["entro_con"] == "Personal de acceso"
    assert cuerpo["entro_en"] == primera["escaneado_en"]
    assert cuerpo["boleto"]["titular"] == "Lupe"
    assert cuerpo["contador"] == {"total": 2, "usados": 1}


async def test_reintentar_la_misma_lectura_no_la_vuelve_duplicado(
    cliente_http, db, puerta, usuarios
):
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    scan_id = uuid.uuid4().hex

    primera = await leer(cliente_http, usuarios, puerta["pid"], qr, scan_id=scan_id)
    otra_vez = await leer(cliente_http, usuarios, puerta["pid"], qr, scan_id=scan_id)

    assert primera.json() == otra_vez.json()
    assert otra_vez.json()["resultado"] == "valido"
    lecturas = await db.scalar(
        select(func.count(Scan.id)).where(Scan.client_scan_id == scan_id)
    )
    assert lecturas == 1


async def test_codigo_impreso_escrito_a_mano(cliente_http, puerta, usuarios):
    codigo = puerta["boletos"][1].codigo
    # Minusculas, sin guion y con espacios: como lo teclea alguien con prisa.
    escrito = f" {codigo.replace('-', '').lower()[:5]} {codigo[6:].lower()} "
    r = await leer(cliente_http, usuarios, puerta["pid"], escrito)
    assert r.json()["resultado"] == "valido"
    assert r.json()["boleto"]["codigo"] == codigo


async def test_lo_que_no_es_un_boleto_es_invalido(cliente_http, puerta, usuarios):
    falsificado = contenido_del_qr(puerta["boletos"][0].uuid, llave="otra-llave")
    for texto in (falsificado, "https://ejemplo.com", "ZZZZZ-ZZZZZ", "hola"):
        r = await leer(cliente_http, usuarios, puerta["pid"], texto)
        assert r.status_code == 200, texto
        assert r.json()["resultado"] == "invalido", texto
        assert r.json()["boleto"] is None
    # El falsificado no tumbo al de verdad.
    real = contenido_del_qr(puerta["boletos"][0].uuid)
    r = await leer(cliente_http, usuarios, puerta["pid"], real)
    assert r.json()["resultado"] == "valido"


async def test_boleto_de_otra_funcion_no_entra_ni_se_gasta(
    cliente_http, db, puerta, usuarios
):
    sabado = Performance(
        event_id=puerta["event_id"],
        seat_map_id=(await db.get(Performance, puerta["pid"])).seat_map_id,
        inicia_en=datetime.now(UTC) + timedelta(hours=2),
        estado=EstadoFuncion.PROGRAMADA.value,
    )
    db.add(sabado)
    await db.flush()
    b = puerta["boletos"][0]

    r = await leer(cliente_http, usuarios, sabado.id, contenido_del_qr(b.uuid))

    cuerpo = r.json()
    assert cuerpo["resultado"] == "otra_funcion"
    assert cuerpo["su_funcion_inicia_en"] is not None
    assert cuerpo["boleto"] is None
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "valido"


async def test_cortesia_revocada_no_entra(cliente_http, puerta, usuarios):
    assert (
        await revocar(cliente_http, usuarios, puerta["orden"]["uuid"])
    ).status_code == 200
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    cuerpo = (await leer(cliente_http, usuarios, puerta["pid"], qr)).json()
    assert (cuerpo["resultado"], cuerpo["motivo"]) == ("cancelado", "revocada")
    assert cuerpo["contador"] == {"total": 0, "usados": 0}


async def test_otro_organizador_no_escanea(cliente_http, puerta, usuarios):
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    igual_que_inexistente(
        await leer(cliente_http, usuarios, puerta["pid"], qr, quien="org_b"),
        await leer(cliente_http, usuarios, 999999, qr, quien="org_b"),
    )
    sin_sesion = await cliente_http.post(
        f"{API}/performances/{puerta['pid']}/escanear",
        json={"codigo": qr, "client_scan_id": uuid.uuid4().hex},
    )
    assert sin_sesion.status_code == 401


async def test_la_lista_de_la_puerta_marca_quien_ya_entro(
    cliente_http, puerta, usuarios
):
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    await leer(cliente_http, usuarios, puerta["pid"], qr)
    lista = await cliente_http.get(
        f"{API}/performances/{puerta['pid']}/lista-de-acceso",
        headers=de(usuarios, "acceso"),
    )
    assert (lista.json()["total"], lista.json()["usados"]) == (2, 1)


async def test_veinte_puertas_el_mismo_boleto_entra_una_vez(motor_con_commit):
    """Con confirmaciones de verdad: cada puerta es su propia transaccion."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.base import RolUsuario
    from app.models.users import User
    from app.services.cortesias import CortesiaService

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Concurrencia escaner")
        s.add(cliente)
        await s.flush()
        esc_ = await funcion_a_la_venta(s, cliente.id)
        await abrir_puerta(s, esc_["performance_id"])
        puerta_ = User(
            email="puerta@concurrencia.mx",
            nombre="Puerta",
            password_hash="x",
            rol=RolUsuario.ACCESO.value,
            client_id=cliente.id,
        )
        s.add(puerta_)
        await s.flush()
        emitida = await CortesiaService(s).otorgar(
            esc_["performance_id"],
            [esc_["seat_ids"][0]],
            email="invitada@correo.mx",
            nombre="Invitada",
            motivo="Prensa",
            actor_id=None,
        )
        await s.commit()
        boleto = (await boletos_de(s, emitida.orden.uuid))[0]
    pid, qr = esc_["performance_id"], contenido_del_qr(boleto.uuid)

    fila_de_espera = asyncio.Semaphore(10)

    async def puerta_lee() -> str:
        async with (
            fila_de_espera,
            AsyncSession(motor_con_commit, expire_on_commit=False) as s,
        ):
            funcion = await s.get(Performance, pid)
            usuario = await s.get(User, puerta_.id)
            r = await EscanerService(s).escanear(funcion, qr, uuid.uuid4().hex, usuario)
            await s.commit()
            return r.resultado

    resultados = await asyncio.gather(*(puerta_lee() for _ in range(20)))

    assert resultados.count("valido") == 1, resultados
    assert resultados.count("duplicado") == 19
    async with AsyncSession(motor_con_commit) as s:
        validos = await s.scalar(
            select(func.count(Scan.id)).where(
                Scan.ticket_id == boleto.id, Scan.resultado == "valido"
            )
        )
    assert validos == 1


# --------------------------------------------------------------------------
# Horario de escaneo (E3c)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("empieza_en", "motivo"),
    [(timedelta(hours=5), "todavia_no"), (timedelta(hours=-13), "ya_termino")],
)
async def test_fuera_de_horario_no_toca_el_boleto(
    cliente_http, db, puerta, usuarios, empieza_en, motivo
):
    await abrir_puerta(db, puerta["pid"], en=empieza_en)
    b = puerta["boletos"][0]

    r = await leer(cliente_http, usuarios, puerta["pid"], contenido_del_qr(b.uuid))

    cuerpo = r.json()
    assert r.status_code == 200, r.text
    assert (cuerpo["resultado"], cuerpo["motivo"]) == ("fuera_de_horario", motivo)
    assert cuerpo["boleto"] is None
    assert cuerpo["abre_en"] and cuerpo["cierra_en"]
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "valido"
    lecturas = await db.scalar(
        select(func.count(Scan.id)).where(Scan.ticket_id == b.id)
    )
    assert lecturas == 0


async def test_justo_en_el_horario_si_entra(cliente_http, db, puerta, usuarios):
    # 3 h 50 min antes de empezar: el escaneo ya abrió (abre 4 h antes).
    await abrir_puerta(db, puerta["pid"], en=timedelta(hours=3, minutes=50))
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    assert (await leer(cliente_http, usuarios, puerta["pid"], qr)).json()[
        "resultado"
    ] == "valido"


async def test_funcion_cancelada_no_escanea(cliente_http, db, puerta, usuarios):
    funcion = await db.get(Performance, puerta["pid"])
    funcion.estado = EstadoFuncion.CANCELADA.value
    await db.flush()
    qr = contenido_del_qr(puerta["boletos"][0].uuid)
    cuerpo = (await leer(cliente_http, usuarios, puerta["pid"], qr)).json()
    assert (cuerpo["resultado"], cuerpo["motivo"]) == (
        "fuera_de_horario",
        "funcion_cancelada",
    )


async def test_la_lista_de_funciones_dice_cuando_abre(
    cliente_http, db, puerta, usuarios
):
    await abrir_puerta(db, puerta["pid"], en=timedelta(days=1))
    funciones = (
        await cliente_http.get(
            f"{API}/acceso/funciones", headers=de(usuarios, "acceso")
        )
    ).json()
    f = funciones[0]
    abre = datetime.fromisoformat(f["escaneo_abre_en"])
    inicia = datetime.fromisoformat(f["inicia_en"])
    assert inicia - abre == timedelta(hours=4)


# --------------------------------------------------------------------------
# Deshacer una entrada (E3d)
# --------------------------------------------------------------------------


async def deshacer(cliente_http, usuarios, pid, codigo, quien="org_a", motivo=None):
    return await cliente_http.post(
        f"{API}/performances/{pid}/entradas/deshacer",
        json={"codigo": codigo, "motivo": motivo or "Se escaneó por error"},
        headers=de(usuarios, quien),
    )


async def test_deshacer_regresa_el_boleto_y_deja_constancia(
    cliente_http, db, puerta, usuarios
):
    b = puerta["boletos"][0]
    qr = contenido_del_qr(b.uuid)
    await leer(cliente_http, usuarios, puerta["pid"], qr)

    r = await deshacer(cliente_http, usuarios, puerta["pid"], b.codigo.lower())

    assert r.status_code == 200, r.text
    assert r.json()["contador"] == {"total": 2, "usados": 0}
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "valido"
    constancia = await db.scalar(
        select(Scan).where(Scan.ticket_id == b.id, Scan.resultado == "deshecho")
    )
    assert (constancia.user_id, constancia.nota) == (
        usuarios["org_a"].id,
        "Se escaneó por error",
    )
    # Vuelve a entrar, y el "ya entró" de después dice la entrada nueva.
    otra = (await leer(cliente_http, usuarios, puerta["pid"], qr)).json()
    assert otra["resultado"] == "valido"
    repetida = (
        await leer(cliente_http, usuarios, puerta["pid"], qr, quien="admin")
    ).json()
    assert repetida["resultado"] == "duplicado"
    assert repetida["entro_en"] == otra["escaneado_en"]


async def test_deshacer_lo_que_no_entro_avisa(cliente_http, puerta, usuarios):
    r = await deshacer(
        cliente_http, usuarios, puerta["pid"], puerta["boletos"][1].codigo
    )
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "sin_entrada_que_deshacer"


async def test_deshacer_solo_psd_y_el_organizador(cliente_http, db, puerta, usuarios):
    b = puerta["boletos"][0]
    await leer(cliente_http, usuarios, puerta["pid"], contenido_del_qr(b.uuid))
    # La puerta no: podría dejar entrar dos veces el mismo boleto.
    r = await deshacer(cliente_http, usuarios, puerta["pid"], b.codigo, quien="acceso")
    assert r.status_code == 403
    igual_que_inexistente(
        await deshacer(cliente_http, usuarios, puerta["pid"], b.codigo, quien="org_b"),
        await deshacer(cliente_http, usuarios, 999999, b.codigo, quien="org_b"),
    )
    # Un código que no es de esta función no existe aquí.
    r = await deshacer(cliente_http, usuarios, puerta["pid"], "ZZZZZ-ZZZZZ")
    assert r.status_code == 404
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "usado"
    assert (
        await deshacer(cliente_http, usuarios, puerta["pid"], b.codigo, quien="admin")
    ).status_code == 200


async def test_dos_a_la_vez_deshacen_una_vez(motor_con_commit):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.base import RolUsuario
    from app.models.users import User
    from app.services.cortesias import CortesiaService
    from app.services.escaner import SinEntradaQueDeshacer

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Concurrencia deshacer")
        s.add(cliente)
        await s.flush()
        esc_ = await funcion_a_la_venta(s, cliente.id)
        await abrir_puerta(s, esc_["performance_id"])
        org = User(
            email="org@deshacer.mx",
            nombre="Org",
            password_hash="x",
            rol=RolUsuario.ORGANIZADOR.value,
            client_id=cliente.id,
        )
        s.add(org)
        await s.flush()
        emitida = await CortesiaService(s).otorgar(
            esc_["performance_id"],
            [esc_["seat_ids"][0]],
            email="invitada@deshacer.mx",
            nombre="Invitada",
            motivo="Prensa",
            actor_id=None,
        )
        await s.commit()
        boleto = (await boletos_de(s, emitida.orden.uuid))[0]
        funcion = await s.get(Performance, esc_["performance_id"])
        await EscanerService(s).escanear(
            funcion, contenido_del_qr(boleto.uuid), uuid.uuid4().hex, org
        )
        await s.commit()
    pid = esc_["performance_id"]

    async def deshace() -> bool:
        async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
            try:
                await EscanerService(s).deshacer_entrada(
                    await s.get(Performance, pid),
                    boleto.codigo,
                    await s.get(User, org.id),
                    "por error",
                )
                await s.commit()
                return True
            except SinEntradaQueDeshacer:
                await s.rollback()
                return False

    resultados = await asyncio.gather(*(deshace() for _ in range(5)))
    assert resultados.count(True) == 1, resultados


# --------------------------------------------------------------------------
# Mapa para cortesias (C4)
# --------------------------------------------------------------------------


async def test_mapa_del_panel_dice_que_se_puede_dar(cliente_http, puerta, usuarios):
    r = await cliente_http.get(
        f"{API}/performances/{puerta['pid']}/mapa-panel", headers=de(usuarios, "org_a")
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"viewport", "zonas", "butacas"}
    por_id = {b["id"]: b for b in cuerpo["butacas"]}
    ya_dadas = puerta["seat_ids"][:2]
    assert all(
        (por_id[s]["estado"], por_id[s]["situacion"]) == ("ocupada", "cortesia")
        for s in ya_dadas
    )
    libre = por_id[puerta["seat_ids"][2]]
    assert (libre["estado"], libre["situacion"]) == ("libre", "disponible")
    # Lo que dice libre se puede dar de verdad.
    dada = await otorgar(cliente_http, usuarios, puerta["pid"], [libre["id"]])
    assert dada.status_code == 201, dada.text


async def test_mapa_del_panel_solo_psd_y_el_organizador(cliente_http, puerta, usuarios):
    ruta = f"{API}/performances/{puerta['pid']}/mapa-panel"
    assert (
        await cliente_http.get(ruta, headers=de(usuarios, "acceso"))
    ).status_code == 403
    igual_que_inexistente(
        await cliente_http.get(ruta, headers=de(usuarios, "org_b")),
        await cliente_http.get(
            f"{API}/performances/999999/mapa-panel", headers=de(usuarios, "org_b")
        ),
    )


async def test_la_lista_de_cortesias_dice_codigo_y_si_ya_entro(
    cliente_http, puerta, usuarios
):
    b = puerta["boletos"][0]
    await leer(cliente_http, usuarios, puerta["pid"], contenido_del_qr(b.uuid))
    lista = (
        await cliente_http.get(
            f"{API}/performances/{puerta['pid']}/cortesias",
            headers=de(usuarios, "org_a"),
        )
    ).json()
    lugares = {lugar["codigo"]: lugar["entro"] for lugar in lista[0]["lugares"]}
    assert lugares == {b.codigo: True, puerta["boletos"][1].codigo: False}
