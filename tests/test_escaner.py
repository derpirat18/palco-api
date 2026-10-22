"""Escaner en linea de la puerta (E3a) y asignacion de cuentas a eventos.

Lo que importa:

- Una cuenta de acceso solo ve y escanea los eventos a los que PSD la
  asigno. Para todo lo demas, el evento no existe (mismo 404).
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


async def test_solo_psd_asigna_y_solo_a_eventos_del_mismo_organizador(
    cliente_http,
    db,
    clientes,
    esc,  # noqa: F811
    usuarios,
):
    ajeno = await funcion_a_la_venta(db, clientes[1].id)

    r = await asignar(cliente_http, usuarios, [esc["event_id"]], quien="org_a")
    assert r.status_code == 403
    r = await asignar(cliente_http, usuarios, [esc["event_id"]], quien="acceso")
    assert r.status_code == 403

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
        inicia_en=datetime.now(UTC) + timedelta(days=21),
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
