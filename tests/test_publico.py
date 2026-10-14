"""Pruebas de la pagina publica del evento.

Dos cosas importan mas que el resto:

- Que no salga a internet nada del organizador: ni su comision, ni su
  identificador, ni cuantas butacas le quedan.
- Que la pagina y el apartado no se contradigan: si aqui dice "en venta",
  apartar funciona; si dice "proximamente", no.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.core.errors import FuncionNoALaVenta
from app.models.base import EstadoEvento, EstadoFuncion
from app.models.catalog import Client, Event, Performance, Seat
from app.models.inventory import PerformanceSeat
from app.services.apartados import ApartadoService
from app.services.venta import Cargos
from tests.test_apartados import funcion_a_la_venta
from tests.test_inventory import armar_escenario

pytestmark = pytest.mark.asyncio

API = "/api/v1/publico/eventos"


async def pagina(cliente_http, slug: str):
    return await cliente_http.get(f"{API}/{slug}")


async def una_funcion(cliente_http, slug: str) -> dict:
    r = await pagina(cliente_http, slug)
    assert r.status_code == 200, r.text
    funciones = r.json()["funciones"]
    assert len(funciones) == 1
    return funciones[0]


async def slug_de(db, event_id: int) -> str:
    return (await db.get(Event, event_id)).slug


async def zona_mas_barata(db, esc: dict) -> list[int]:
    """Butacas vendibles del Balcon (la zona de $400)."""
    return list(
        (
            await db.scalars(
                select(PerformanceSeat.seat_id)
                .join(Seat, Seat.id == PerformanceSeat.seat_id)
                .where(
                    PerformanceSeat.performance_id == esc["performance_id"],
                    Seat.zone_id == esc["zonas"][1],
                )
            )
        ).all()
    )


async def poner_estado(db, performance_id: int, estado: str, seat_ids=None) -> None:
    consulta = update(PerformanceSeat).where(
        PerformanceSeat.performance_id == performance_id,
        PerformanceSeat.estado != "kill",
    )
    if seat_ids is not None:
        consulta = consulta.where(PerformanceSeat.seat_id.in_(seat_ids))
    await db.execute(consulta.values(estado=estado))


# --------------------------------------------------------------------------
# Lo que se muestra
# --------------------------------------------------------------------------


async def test_pagina_del_evento_por_su_link(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    evento.descripcion = "Funcion familiar de 90 minutos."
    evento.categoria = "Familiares"
    await db.flush()

    r = await pagina(cliente_http, evento.slug)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["nombre"] == evento.nombre
    assert cuerpo["descripcion"] == "Funcion familiar de 90 minutos."
    assert cuerpo["recinto"]["ciudad"] == "CDMX"
    assert cuerpo["recinto"]["zona_horaria"]
    assert cuerpo["cargos"] == {"servicio_pct": "10.00", "por_orden": "50.00"}

    [funcion] = cuerpo["funciones"]
    assert funcion["id"] == esc["performance_id"]
    assert funcion["estado_venta"] == "en_venta"
    assert funcion["disponibilidad"] == "disponible"
    assert funcion["precio_desde"] == "400.00"
    assert funcion["precio_desde_con_cargo"] == "440.00"


async def test_no_sale_nada_del_organizador(cliente_http, db, clientes):
    """Cada campo de esta respuesta sale a internet.

    Si alguien agrega uno, esta prueba lo obliga a decidirlo a proposito.
    """
    esc = await funcion_a_la_venta(db, clientes[0].id)
    cuerpo = (await pagina(cliente_http, await slug_de(db, esc["event_id"]))).json()

    assert set(cuerpo) == {
        "slug",
        "nombre",
        "categoria",
        "descripcion",
        "imagen_url",
        "recinto",
        "cargos",
        "funciones",
    }
    assert set(cuerpo["recinto"]) == {
        "nombre",
        "ciudad",
        "estado_geo",
        "pais",
        "zona_horaria",
    }
    assert set(cuerpo["funciones"][0]) == {
        "id",
        "inicia_en",
        "apertura_puertas",
        "estado_venta",
        "venta_abre_en",
        "disponibilidad",
        "precio_desde",
        "precio_desde_con_cargo",
    }
    texto = str(cuerpo).lower()
    assert "comision" not in texto
    assert clientes[0].nombre.lower() not in texto


async def test_el_link_no_distingue_mayusculas(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    slug = await slug_de(db, esc["event_id"])

    r = await pagina(cliente_http, slug.upper())

    assert r.status_code == 200


# --------------------------------------------------------------------------
# Lo que no existe para el publico
# --------------------------------------------------------------------------


async def igual_que_inexistente(cliente_http, slug: str) -> None:
    oculto = await pagina(cliente_http, slug)
    inexistente = await pagina(cliente_http, "no-existe-este-evento")
    assert oculto.status_code == 404
    assert oculto.json() == inexistente.json()


async def test_evento_en_borrador_no_existe(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.BORRADOR.value
    await db.flush()

    await igual_que_inexistente(cliente_http, evento.slug)


async def test_organizador_dado_de_baja_desaparece(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    cliente = await db.get(Client, clientes[0].id)
    cliente.activo = False
    await db.flush()

    await igual_que_inexistente(cliente_http, await slug_de(db, esc["event_id"]))


async def test_funciones_canceladas_y_pasadas_no_aparecen(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    ahora = await db.scalar(select(func.now()))
    db.add_all(
        [
            Performance(
                event_id=esc["event_id"],
                seat_map_id=esc["seat_map_id"],
                inicia_en=ahora + timedelta(days=30),
                estado=EstadoFuncion.CANCELADA.value,
            ),
            Performance(
                event_id=esc["event_id"],
                seat_map_id=esc["seat_map_id"],
                inicia_en=ahora - timedelta(days=1),
                estado=EstadoFuncion.EN_VENTA.value,
            ),
        ]
    )
    await db.flush()

    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))
    assert funcion["id"] == esc["performance_id"]


# --------------------------------------------------------------------------
# Estado de venta y disponibilidad
# --------------------------------------------------------------------------


async def test_funcion_sin_inventario_se_anuncia_con_precio_de_zona(
    cliente_http, db, clientes
):
    esc = await armar_escenario(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.PUBLICADO.value
    await db.flush()

    funcion = await una_funcion(cliente_http, evento.slug)

    assert funcion["estado_venta"] == "proximamente"
    assert funcion["disponibilidad"] is None
    assert funcion["precio_desde"] == "400.00"


async def test_venta_que_abre_despues(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion_db = await db.get(Performance, esc["performance_id"])
    ahora = await db.scalar(select(func.now()))
    funcion_db.venta_abre_en = ahora + timedelta(days=3)
    await db.flush()

    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))

    assert funcion["estado_venta"] == "proximamente"
    assert funcion["venta_abre_en"] is not None


async def test_agotada(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    await poner_estado(db, esc["performance_id"], "vendido")

    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))

    assert funcion["estado_venta"] == "agotada"
    assert funcion["disponibilidad"] == "agotado"
    assert funcion["precio_desde"] is None


async def test_ultimos_lugares(cliente_http, db, clientes):
    """Una libre de diez vendibles: menos del 12 %."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    todas = esc["seat_ids"]
    assert len(todas) == 10
    await poner_estado(db, esc["performance_id"], "vendido", todas[1:])

    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))

    assert funcion["estado_venta"] == "en_venta"
    assert funcion["disponibilidad"] == "ultimos_lugares"


async def test_precio_desde_solo_de_lo_que_se_puede_comprar(cliente_http, db, clientes):
    """Si la zona barata esta bloqueada, "desde" no puede anunciarla."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    await poner_estado(
        db, esc["performance_id"], "bloqueado", await zona_mas_barata(db, esc)
    )

    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))

    assert funcion["precio_desde"] == "800.00"
    assert funcion["precio_desde_con_cargo"] == "880.00"


async def test_apartado_vigente_no_cuenta_como_libre_y_el_vencido_si(
    cliente_http, db, clientes, hold_service
):
    """Misma condicion de "libre" que usa el apartado."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, slug = esc["performance_id"], await slug_de(db, esc["event_id"])
    baratas = await zona_mas_barata(db, esc)
    await ApartadoService(db, hold_service).apartar(pid, baratas, uuid.uuid4().hex)

    assert (await una_funcion(cliente_http, slug))["precio_desde"] == "800.00"

    ahora = await db.scalar(select(func.now()))
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.seat_id.in_(baratas))
        .values(hold_expira_en=ahora - timedelta(minutes=1))
    )
    assert (await una_funcion(cliente_http, slug))["precio_desde"] == "400.00"


# --------------------------------------------------------------------------
# Cargos
# --------------------------------------------------------------------------


async def test_el_evento_puede_tener_cargo_propio(cliente_http, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    evento.cargo_servicio_pct = Decimal("5.00")
    await db.flush()

    r = await pagina(cliente_http, evento.slug)

    assert r.json()["cargos"]["servicio_pct"] == "5.00"
    assert r.json()["funciones"][0]["precio_desde_con_cargo"] == "420.00"


async def test_el_cargo_se_redondea_al_centavo_hacia_arriba_en_la_mitad():
    cargos = Cargos(servicio_pct=Decimal("10.00"), por_orden=Decimal("50.00"))
    assert cargos.con_cargo_de_servicio(Decimal("333.33")) == Decimal("366.66")
    assert cargos.con_cargo_de_servicio(Decimal("0.05")) == Decimal("0.06")
    # 0.165: el redondeo bancario daria 0.16; el comercial, 0.17.
    assert cargos.con_cargo_de_servicio(Decimal("0.15")) == Decimal("0.17")


# --------------------------------------------------------------------------
# La pagina y el apartado dicen lo mismo
# --------------------------------------------------------------------------


async def test_si_la_pagina_dice_en_venta_se_puede_apartar(
    cliente_http, db, clientes, hold_service
):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))
    assert funcion["estado_venta"] == "en_venta"

    await ApartadoService(db, hold_service).apartar(
        esc["performance_id"], esc["seat_ids"][:1], uuid.uuid4().hex
    )


async def test_si_la_pagina_dice_proximamente_no_se_puede_apartar(
    cliente_http, db, clientes, hold_service
):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion_db = await db.get(Performance, esc["performance_id"])
    ahora = await db.scalar(select(func.now()))
    funcion_db.venta_abre_en = ahora + timedelta(hours=2)
    await db.flush()
    funcion = await una_funcion(cliente_http, await slug_de(db, esc["event_id"]))
    assert funcion["estado_venta"] == "proximamente"

    with pytest.raises(FuncionNoALaVenta):
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], esc["seat_ids"][:1], uuid.uuid4().hex
        )
