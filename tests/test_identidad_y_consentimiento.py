"""UUID publicos, telefono en E.164 en la base y consentimientos como registro.

Es lo que el documento de compatibilidad con el CRM marco como "antes de
la primera venta": despues seria migrar datos vivos.
"""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.security import crear_access_token
from app.models.catalog import Client
from app.models.sales import Consent, Customer
from app.services.consentimientos import ConsentimientoInvalido, ConsentimientoService
from tests.test_apartados import funcion_a_la_venta

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def auth(usuario) -> dict:
    token = crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )
    return {"Authorization": f"Bearer {token}"}


async def comprador(db, email: str = "ana@correo.mx") -> Customer:
    c = Customer(email=email)
    db.add(c)
    await db.flush()
    return c


# --------------------------------------------------------------------------
# UUID publicos
# --------------------------------------------------------------------------


async def test_cada_registro_nace_con_su_uuid(db, clientes):
    a, b = clientes
    assert isinstance(a.uuid, uuid.UUID)
    assert a.uuid != b.uuid


async def test_la_base_pone_el_uuid_aunque_la_fila_llegue_sin_el(db):
    """Una fila insertada por fuera del ORM (un script, una carga masiva)
    tambien tiene que salir con identificador publico."""
    await db.execute(
        text(
            "INSERT INTO customers (email, creado_en, actualizado_en) "
            "VALUES ('directo@correo.mx', now(), now())"
        )
    )
    valor = await db.scalar(
        select(Customer.uuid).where(Customer.email == "directo@correo.mx")
    )
    assert isinstance(valor, uuid.UUID)


async def test_las_respuestas_llevan_el_uuid(cliente_http, db, clientes, usuarios):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    admin = auth(usuarios["admin"])

    cliente = (
        await cliente_http.get(f"{API}/clients/{clientes[0].id}", headers=admin)
    ).json()
    evento = (
        await cliente_http.get(f"{API}/events/{esc['event_id']}", headers=admin)
    ).json()
    funciones = (
        await cliente_http.get(
            f"{API}/events/{esc['event_id']}/performances", headers=admin
        )
    ).json()

    assert uuid.UUID(cliente["uuid"]) == clientes[0].uuid
    assert uuid.UUID(evento["uuid"])
    assert uuid.UUID(funciones[0]["uuid"])


# --------------------------------------------------------------------------
# Telefono en la base
# --------------------------------------------------------------------------


async def test_la_base_rechaza_un_telefono_sin_normalizar(db):
    """Aunque alguien se salte el codigo, la base no deja guardar un
    telefono fuera de E.164."""
    with pytest.raises(IntegrityError, match="ck_customers_telefono_e164"):
        async with db.begin_nested():
            db.add(Customer(email="malo@correo.mx", telefono="5512345678"))
            await db.flush()


async def test_la_base_acepta_e164(db):
    db.add(Customer(email="bien@correo.mx", telefono="+525512345678"))
    await db.flush()


# --------------------------------------------------------------------------
# Consentimientos
# --------------------------------------------------------------------------


async def test_aceptar_y_retirar_queda_en_la_bitacora(db):
    persona = await comprador(db)
    servicio = ConsentimientoService(db)

    await servicio.registrar(persona.id, "marketing", True, "checkout_web", "2026-10")
    assert await servicio.acepta(persona.id, "marketing")

    await servicio.registrar(persona.id, "marketing", False, "correo_baja", "2026-10")
    assert not await servicio.acepta(persona.id, "marketing")

    historia = await db.scalar(
        select(func.count(Consent.id)).where(Consent.customer_id == persona.id)
    )
    assert historia == 2, "retirar no puede borrar que antes acepto"


async def test_lo_vigente_dice_cuando_desde_donde_y_con_que_aviso(db):
    persona = await comprador(db)
    servicio = ConsentimientoService(db)
    await servicio.registrar(
        persona.id, "operacion", True, "formulario_pista", "2026-10"
    )

    vigente = (await servicio.vigentes(persona.id))["operacion"]

    assert vigente.otorgado is True
    assert vigente.origen == "formulario_pista"
    assert vigente.aviso_version == "2026-10"
    assert vigente.registrado_en is not None


async def test_las_finalidades_son_independientes(db):
    """Aceptar operar la reserva no es aceptar promociones."""
    persona = await comprador(db)
    servicio = ConsentimientoService(db)
    await servicio.registrar(persona.id, "operacion", True, "checkout_web", "2026-10")

    assert await servicio.acepta(persona.id, "operacion")
    assert not await servicio.acepta(persona.id, "marketing")


async def test_el_consentimiento_de_uno_no_es_el_de_otro(db):
    """Lo que acepto Ana no autoriza a escribirle a Luis."""
    ana = await comprador(db, "ana@correo.mx")
    luis = await comprador(db, "luis@correo.mx")
    servicio = ConsentimientoService(db)
    await servicio.registrar(ana.id, "marketing", True, "checkout_web", "2026-10")

    assert await servicio.acepta(ana.id, "marketing")
    assert not await servicio.acepta(luis.id, "marketing")
    assert await servicio.vigentes(luis.id) == {}


async def test_sin_registro_no_hay_consentimiento(db):
    persona = await comprador(db)
    assert not await ConsentimientoService(db).acepta(persona.id, "marketing")


@pytest.mark.parametrize(
    "finalidad,origen,aviso",
    [
        ("todo", "checkout_web", "2026-10"),
        ("marketing", "", "2026-10"),
        ("marketing", "checkout_web", " "),
    ],
)
async def test_un_registro_que_no_prueba_nada_se_rechaza(db, finalidad, origen, aviso):
    persona = await comprador(db)
    with pytest.raises(ConsentimientoInvalido):
        await ConsentimientoService(db).registrar(
            persona.id, finalidad, True, origen, aviso
        )


async def test_la_base_rechaza_una_finalidad_inventada(db):
    persona = await comprador(db)
    with pytest.raises(IntegrityError, match="ck_consents_finalidad_valida"):
        async with db.begin_nested():
            db.add(
                Consent(
                    customer_id=persona.id,
                    finalidad="todo",
                    otorgado=True,
                    origen="x",
                    aviso_version="x",
                )
            )
            await db.flush()


async def test_el_organizador_dado_de_baja_conserva_su_uuid(db, clientes):
    """El historico (liquidaciones, CRM) apunta a ese UUID: no puede cambiar."""
    a, _ = clientes
    antes = a.uuid
    cliente = await db.get(Client, a.id)
    cliente.activo = False
    await db.flush()
    assert (await db.get(Client, a.id)).uuid == antes
