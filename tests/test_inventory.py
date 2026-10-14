"""Pruebas de materializacion y operacion del inventario.

La prueba que no se toca es `test_materializar_dos_veces_a_la_vez`: si
falla, dos peticiones simultaneas pueden reescribir el inventario de una
funcion que ya estaba vendiendo, y eso devuelve a la venta butacas que ya
tienen dueño.
"""

import asyncio
import itertools
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.security import crear_access_token
from app.models.base import EstadoAsiento, EstadoEvento, EstadoFuncion
from app.models.catalog import (
    Client,
    Event,
    Performance,
    PerformancePrice,
    Seat,
    SeatMap,
    Venue,
    Zone,
)
from app.models.inventory import InventoryMove, PerformanceSeat
from app.services.inventory import (
    ConflictoDeInventario,
    InventarioIncompleto,
    InventoryService,
)

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def igual_que_inexistente(ajeno, inexistente) -> None:
    """Lo ajeno no se distingue de lo que no existe: ni codigo ni cuerpo.

    Si se distinguiera, un organizador podria recorrer identificadores y
    saber cuales existen y de quien son.
    """
    assert ajeno.status_code == 404, ajeno.text
    assert (ajeno.status_code, ajeno.json()) == (
        inexistente.status_code,
        inexistente.json(),
    )


def token_de(usuario) -> str:
    return crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )


# --------------------------------------------------------------------------
# Escenario
# --------------------------------------------------------------------------

# El slug del evento es unico en la base. Una prueba puede armar dos
# escenarios, asi que el nombre no puede depender solo del cliente.
_consecutivo = itertools.count(1)


async def armar_escenario(
    db,
    client_id: int,
    *,
    butacas_por_zona: int = 5,
    kill: tuple[int, ...] = (),
    con_precios: bool = True,
) -> dict:
    """Recinto, mapa de dos zonas, evento, funcion y precios.

    Devuelve los ids que las pruebas necesitan.
    """
    from datetime import UTC, datetime, timedelta

    n = next(_consecutivo)
    recinto = Venue(nombre=f"Recinto {n}", ciudad="CDMX")
    db.add(recinto)
    await db.flush()

    mapa = SeatMap(venue_id=recinto.id, nombre="Mapa", viewport={})
    db.add(mapa)
    await db.flush()

    zonas = []
    for orden, nombre in enumerate(("Luneta", "Balcon")):
        zona = Zone(seat_map_id=mapa.id, nombre=nombre, orden=orden)
        db.add(zona)
        await db.flush()
        zonas.append(zona)
        db.add_all(
            [
                Seat(
                    zone_id=zona.id,
                    fila="A",
                    numero=str(i),
                    pos_x=Decimal(i * 10),
                    pos_y=Decimal(0),
                    kill=(i in kill and orden == 0),
                )
                for i in range(1, butacas_por_zona + 1)
            ]
        )
    await db.flush()

    evento = Event(
        client_id=client_id,
        venue_id=recinto.id,
        nombre=f"Evento {n}",
        slug=f"evento-{n}",
        estado=EstadoEvento.BORRADOR.value,
    )
    db.add(evento)
    await db.flush()

    funcion = Performance(
        event_id=evento.id,
        seat_map_id=mapa.id,
        inicia_en=datetime.now(UTC) + timedelta(days=20),
        estado=EstadoFuncion.PROGRAMADA.value,
    )
    db.add(funcion)
    await db.flush()

    if con_precios:
        db.add_all(
            [
                PerformancePrice(
                    performance_id=funcion.id,
                    zone_id=zonas[0].id,
                    precio=Decimal("800.00"),
                ),
                PerformancePrice(
                    performance_id=funcion.id,
                    zone_id=zonas[1].id,
                    precio=Decimal("400.00"),
                ),
            ]
        )
        await db.flush()

    return {
        "venue_id": recinto.id,
        "seat_map_id": mapa.id,
        "zonas": [z.id for z in zonas],
        "event_id": evento.id,
        "performance_id": funcion.id,
    }


async def butacas_de(db, performance_id: int) -> list[PerformanceSeat]:
    return list(
        (
            await db.scalars(
                select(PerformanceSeat)
                .where(PerformanceSeat.performance_id == performance_id)
                .order_by(PerformanceSeat.id)
            )
        ).all()
    )


# --------------------------------------------------------------------------
# Materializacion
# --------------------------------------------------------------------------


async def test_materializa_con_precio_de_zona_y_marca_kill(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id, butacas_por_zona=5, kill=(1, 2))

    resultado = await InventoryService(db).materializar(esc["performance_id"])

    assert resultado.butacas_creadas == 10
    assert resultado.kill == 2
    assert resultado.vendibles == 8

    filas = await butacas_de(db, esc["performance_id"])
    por_precio = {}
    for f in filas:
        por_precio.setdefault(f.precio_final, 0)
        por_precio[f.precio_final] += 1
    assert por_precio == {Decimal("800.00"): 5, Decimal("400.00"): 5}

    estados = [f.estado for f in filas]
    assert estados.count(EstadoAsiento.KILL.value) == 2
    assert estados.count(EstadoAsiento.DISPONIBLE.value) == 8
    assert all(f.precio_personalizado is False for f in filas)


async def test_marca_la_funcion_como_generada(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)

    assert (await servicio.resumen(esc["performance_id"])).inventario_generado is False
    await servicio.materializar(esc["performance_id"])
    resumen = await servicio.resumen(esc["performance_id"])
    assert resumen.inventario_generado is True
    assert resumen.total == 10
    assert resumen.disponibles == 10


async def test_segunda_materializacion_se_rechaza(db, clientes):
    """Rehacerla pondria de vuelta a la venta butacas ya vendidas."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)

    await servicio.materializar(esc["performance_id"])
    with pytest.raises(ConflictoDeInventario):
        await servicio.materializar(esc["performance_id"])

    assert len(await butacas_de(db, esc["performance_id"])) == 10


async def test_no_materializa_sin_precios(db, clientes):
    """Una butaca sin precio saldria a la venta en cero."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id, con_precios=False)

    with pytest.raises(InventarioIncompleto) as exc:
        await InventoryService(db).materializar(esc["performance_id"])
    assert sorted(exc.value.detalle["zonas"]) == sorted(esc["zonas"])

    # Y no dejo nada a medias ni marco la funcion.
    assert await butacas_de(db, esc["performance_id"]) == []
    funcion = await db.get(Performance, esc["performance_id"])
    assert funcion.inventario_generado is False


async def test_no_materializa_si_falta_el_precio_de_una_sola_zona(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)

    precio = await db.scalar(
        select(PerformancePrice).where(
            PerformancePrice.performance_id == esc["performance_id"],
            PerformancePrice.zone_id == esc["zonas"][1],
        )
    )
    precio.activo = False
    await db.flush()

    with pytest.raises(InventarioIncompleto) as exc:
        await InventoryService(db).materializar(esc["performance_id"])
    assert exc.value.detalle["zonas"] == [esc["zonas"][1]]


# --------------------------------------------------------------------------
# Concurrencia: la prueba que no se toca
# --------------------------------------------------------------------------


async def test_materializar_dos_veces_a_la_vez(motor_con_commit):
    """Dos peticiones simultaneas: una gana, la otra se entera.

    La idempotencia no puede resolverse consultando la bandera y luego
    escribiendo, porque entre la consulta y la escritura cabe la otra
    peticion. La resuelve un UPDATE condicional que solo una puede ganar.

    Esta prueba necesita confirmar de verdad en dos sesiones distintas:
    con la transaccion revertida de las demas pruebas, las dos sesiones
    nunca se verian entre si y el escenario que queremos probar no podria
    ocurrir.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as sesion:
        cliente = Client(nombre="Concurrencia")
        sesion.add(cliente)
        await sesion.flush()
        esc = await armar_escenario(sesion, cliente.id, butacas_por_zona=50)
        await sesion.commit()
        performance_id = esc["performance_id"]

    async def intentar():
        async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
            try:
                resultado = await InventoryService(s).materializar(performance_id)
                await s.commit()
                return resultado
            except ConflictoDeInventario:
                await s.rollback()
                return None

    uno, otro = await asyncio.gather(intentar(), intentar())

    exitosos = [r for r in (uno, otro) if r is not None]
    assert len(exitosos) == 1, "las dos materializaciones creyeron ganar"
    assert exitosos[0].butacas_creadas == 100

    async with AsyncSession(motor_con_commit) as s:
        total = await s.scalar(
            select(func.count(PerformanceSeat.id)).where(
                PerformanceSeat.performance_id == performance_id
            )
        )
    assert total == 100, f"se crearon {total} butacas en lugar de 100"


# --------------------------------------------------------------------------
# Sincronizacion
# --------------------------------------------------------------------------


async def test_sincronizar_agrega_solo_lo_nuevo(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    db.add_all(
        [
            Seat(
                zone_id=esc["zonas"][0],
                fila="B",
                numero=str(i),
                pos_x=Decimal(i * 10),
                pos_y=Decimal(30),
            )
            for i in range(1, 4)
        ]
    )
    await db.flush()

    assert await servicio.sincronizar(esc["performance_id"]) == 3
    assert len(await butacas_de(db, esc["performance_id"])) == 13
    # Correr dos veces no duplica.
    assert await servicio.sincronizar(esc["performance_id"]) == 0


async def test_sincronizar_deja_rastro_en_la_bitacora(db, clientes):
    """Agregar butacas con la funcion viva es anomalo: hay que explicarlo."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    db.add(
        Seat(
            zone_id=esc["zonas"][0],
            fila="Z",
            numero="1",
            pos_x=Decimal(0),
            pos_y=Decimal(0),
        )
    )
    await db.flush()
    await servicio.sincronizar(esc["performance_id"])

    movimientos = list((await db.scalars(select(InventoryMove))).all())
    assert len(movimientos) == 1
    assert movimientos[0].motivo == "sincronizacion de mapa"


async def test_no_sincroniza_sin_materializar(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    with pytest.raises(ConflictoDeInventario):
        await InventoryService(db).sincronizar(esc["performance_id"])


# --------------------------------------------------------------------------
# Estados y precios
# --------------------------------------------------------------------------


async def test_bloquear_butacas_escribe_bitacora(db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    filas = await butacas_de(db, esc["performance_id"])
    ids = [f.id for f in filas[:3]]

    afectadas = await servicio.cambiar_estado(
        esc["performance_id"],
        ids,
        EstadoAsiento.BLOQUEADO.value,
        motivo="prensa",
        actor_id=usuarios["admin"].id,
    )
    assert afectadas == 3

    movimientos = list((await db.scalars(select(InventoryMove))).all())
    assert len(movimientos) == 3
    assert {m.motivo for m in movimientos} == {"prensa"}
    assert {m.estado_previo for m in movimientos} == {EstadoAsiento.DISPONIBLE.value}
    assert {m.actor_id for m in movimientos} == {usuarios["admin"].id}


async def test_no_se_cambia_el_estado_de_una_vendida(db, clientes):
    """Deshacer una venta es cancelar la orden, no editar el estado."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    filas = await butacas_de(db, esc["performance_id"])
    filas[0].estado = EstadoAsiento.VENDIDO.value
    await db.flush()

    with pytest.raises(ConflictoDeInventario) as exc:
        await servicio.cambiar_estado(
            esc["performance_id"],
            [filas[0].id, filas[1].id],
            EstadoAsiento.BLOQUEADO.value,
            motivo="intento",
        )
    assert exc.value.detalle["butacas"] == [filas[0].id]
    # Y no toco a la otra: la operacion es todo o nada.
    assert filas[1].estado == EstadoAsiento.DISPONIBLE.value


@pytest.mark.parametrize("estado", ["vendido", "cortesia", "apartado"])
async def test_el_panel_no_pone_estados_que_nacen_de_una_orden(db, clientes, estado):
    """Una butaca "vendida" a mano no tiene cobro ni QR.

    Y una cortesia sin boleto deja al invitado afuera. Las dos nacen de
    una orden; el apartado, del servicio de apartados.
    """
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])
    fila = (await butacas_de(db, esc["performance_id"]))[0]

    with pytest.raises(ConflictoDeInventario) as exc:
        await servicio.cambiar_estado(
            esc["performance_id"], [fila.id], estado, motivo="a mano"
        )

    assert exc.value.detalle["estado"] == estado
    assert fila.estado == EstadoAsiento.DISPONIBLE.value


async def test_butaca_de_otra_funcion_se_rechaza(db, clientes):
    cliente_a, _ = clientes
    uno = await armar_escenario(db, cliente_a.id)
    otro = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(uno["performance_id"])
    await servicio.materializar(otro["performance_id"])

    ajena = (await butacas_de(db, otro["performance_id"]))[0]
    with pytest.raises(ConflictoDeInventario):
        await servicio.cambiar_estado(
            uno["performance_id"],
            [ajena.id],
            EstadoAsiento.BLOQUEADO.value,
            motivo="intento",
        )


async def test_precio_personalizado_sobrevive_al_precio_de_zona(db, clientes):
    """Es el punto de tener la bandera: si no, el repricing lo borraria."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    filas = await butacas_de(db, esc["performance_id"])
    especial = filas[0]
    await servicio.fijar_precio_butacas(
        esc["performance_id"], [especial.id], Decimal("1500.00")
    )

    precio = await db.scalar(
        select(PerformancePrice).where(
            PerformancePrice.performance_id == esc["performance_id"],
            PerformancePrice.zone_id == esc["zonas"][0],
        )
    )
    precio.precio = Decimal("900.00")
    await db.flush()

    await servicio.aplicar_precios_de_zona(esc["performance_id"])
    await db.refresh(especial)

    assert especial.precio_final == Decimal("1500.00")
    otras = [
        f for f in await butacas_de(db, esc["performance_id"]) if f.id != especial.id
    ]
    de_luneta = [f for f in otras if f.precio_final == Decimal("900.00")]
    assert len(de_luneta) == 4


async def test_no_se_reprecia_lo_vendido(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    filas = await butacas_de(db, esc["performance_id"])
    vendida = filas[0]
    vendida.estado = EstadoAsiento.VENDIDO.value
    await db.flush()

    afectadas = await servicio.fijar_precio_butacas(
        esc["performance_id"], [vendida.id], Decimal("1.00")
    )
    assert afectadas == 0
    await db.refresh(vendida)
    assert vendida.precio_final == Decimal("800.00")


# --------------------------------------------------------------------------
# Apertura de venta
# --------------------------------------------------------------------------


async def test_no_abre_venta_sin_inventario(db, clientes):
    """Se veria a la venta con el mapa vacio."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    with pytest.raises(ConflictoDeInventario):
        await InventoryService(db).abrir_venta(esc["performance_id"])


async def test_abre_venta_con_inventario(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    funcion = await servicio.abrir_venta(esc["performance_id"])
    assert funcion.estado == EstadoFuncion.EN_VENTA.value


async def test_no_abre_venta_de_una_cancelada(db, clientes):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    servicio = InventoryService(db)
    await servicio.materializar(esc["performance_id"])

    funcion = await db.get(Performance, esc["performance_id"])
    funcion.estado = EstadoFuncion.CANCELADA.value
    await db.flush()

    with pytest.raises(ConflictoDeInventario):
        await servicio.abrir_venta(esc["performance_id"])


# --------------------------------------------------------------------------
# API y aislamiento
# --------------------------------------------------------------------------


async def test_endpoint_materializa_y_resume(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id, kill=(1,))
    tk = auth(token_de(usuarios["org_a"]))

    r = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk
    )
    assert r.status_code == 201, r.text
    assert r.json() == {
        "performance_id": esc["performance_id"],
        "butacas_creadas": 10,
        "vendibles": 9,
        "kill": 1,
    }

    r = await cliente_http.get(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk
    )
    assert r.status_code == 200
    assert r.json()["disponibles"] == 9
    assert r.json()["por_estado"]["kill"] == 1


async def test_endpoint_segunda_vez_responde_409(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    tk = auth(token_de(usuarios["org_a"]))
    url = f"{API}/performances/{esc['performance_id']}/inventario"

    assert (await cliente_http.post(url, headers=tk)).status_code == 201
    assert (await cliente_http.post(url, headers=tk)).status_code == 409


async def test_endpoint_sin_precios_responde_422(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id, con_precios=False)
    r = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario",
        headers=auth(token_de(usuarios["org_a"])),
    )
    assert r.status_code == 422
    assert "zonas" in r.json()["error"]["detalle"]


async def test_organizador_no_materializa_funcion_ajena(
    cliente_http, db, clientes, usuarios
):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    tk_b = auth(token_de(usuarios["org_b"]))
    r = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk_b
    )
    igual_que_inexistente(
        r,
        await cliente_http.post(f"{API}/performances/999999/inventario", headers=tk_b),
    )


async def test_organizador_no_ve_inventario_ajeno(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    tk_b = auth(token_de(usuarios["org_b"]))
    r = await cliente_http.get(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk_b
    )
    igual_que_inexistente(
        r, await cliente_http.get(f"{API}/performances/999999/inventario", headers=tk_b)
    )


async def test_rol_acceso_no_toca_inventario(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    r = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario",
        headers=auth(token_de(usuarios["acceso"])),
    )
    assert r.status_code == 403


async def test_endpoint_abrir_venta(cliente_http, db, clientes, usuarios):
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    tk = auth(token_de(usuarios["org_a"]))

    sin_inventario = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/abrir-venta", headers=tk
    )
    assert sin_inventario.status_code == 409

    await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk
    )
    con_inventario = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/abrir-venta", headers=tk
    )
    assert con_inventario.status_code == 200
    assert con_inventario.json()["estado"] == "en_venta"


async def test_motivo_obligatorio_en_cambio_de_estado(
    cliente_http, db, clientes, usuarios
):
    """Sin motivo la bitacora no resuelve una disputa en taquilla."""
    cliente_a, _ = clientes
    esc = await armar_escenario(db, cliente_a.id)
    tk = auth(token_de(usuarios["org_a"]))
    await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario", headers=tk
    )
    fila = (await butacas_de(db, esc["performance_id"]))[0]

    r = await cliente_http.post(
        f"{API}/performances/{esc['performance_id']}/inventario/estado",
        headers=tk,
        json={
            "performance_seat_ids": [fila.id],
            "estado": "bloqueado",
            "motivo": "",
        },
    )
    assert r.status_code == 422
