"""Pruebas del apartado desde la API: Postgres y Redis juntos.

`test_holds_concurrency.py` prueba Redis solo. Aqui se prueba lo que Redis
no sabe: que una butaca vendida, en kill o bloqueada nunca llega a
apartarse, que el espejo en Postgres coincide con Redis, y que si algo
falla a medias no quedan butacas apartadas que nadie pidio.

La prueba que no se toca es
`test_cien_compradores_misma_butaca_postgres_y_redis_coinciden`: confirma
de verdad en sesiones distintas y es la que demuestra que las dos capas
no se contradicen bajo carga.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.errors import (
    AsientoNoDisponible,
    FuncionNoALaVenta,
    LimiteDeAsientos,
    RecursoNoEncontrado,
)
from app.models.base import EstadoAsiento, EstadoEvento
from app.models.catalog import Client, Event, Performance
from app.models.inventory import InventoryMove, PerformanceSeat
from app.services.apartados import ApartadoService
from app.services.holds import clave_butaca
from app.services.inventory import ConflictoDeInventario, InventoryService
from tests.test_inventory import armar_escenario

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def nuevo_hold() -> str:
    return uuid.uuid4().hex


async def funcion_a_la_venta(db, client_id: int, **kw) -> dict:
    """Escenario completo: materializado, evento publicado, venta abierta."""
    esc = await armar_escenario(db, client_id, **kw)
    pid = esc["performance_id"]
    await InventoryService(db).materializar(pid)
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.PUBLICADO.value
    await InventoryService(db).abrir_venta(pid)
    await db.flush()
    esc["seat_ids"] = list(
        (
            await db.scalars(
                select(PerformanceSeat.seat_id)
                .where(
                    PerformanceSeat.performance_id == pid,
                    PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value,
                )
                .order_by(PerformanceSeat.seat_id)
            )
        ).all()
    )
    return esc


async def fila(db, performance_id: int, seat_id: int) -> PerformanceSeat:
    return await db.scalar(
        select(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == performance_id,
            PerformanceSeat.seat_id == seat_id,
        )
        .execution_options(populate_existing=True)
    )


async def poner_estado(db, performance_id: int, seat_id: int, estado: str) -> None:
    await db.execute(
        update(PerformanceSeat)
        .where(
            PerformanceSeat.performance_id == performance_id,
            PerformanceSeat.seat_id == seat_id,
        )
        .values(estado=estado)
    )


# --------------------------------------------------------------------------
# Apartar
# --------------------------------------------------------------------------


async def test_aparta_y_refleja_en_postgres(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (a, b) = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()

    estado = await ApartadoService(db, hold_service).apartar(pid, [a, b], hold)

    assert [x.seat_id for x in estado.butacas] == [a, b]
    assert all(str(x.precio) == "800.00" for x in estado.butacas)
    assert 0 < estado.segundos_restantes <= settings.HOLD_TTL_SEGUNDOS

    for seat_id in (a, b):
        f = await fila(db, pid, seat_id)
        assert f.estado == EstadoAsiento.APARTADO.value
        assert f.hold_id == hold
    assert await hold_service.es_titular(pid, [a, b], hold)


async def test_postgres_nunca_vence_despues_que_redis(db, clientes, hold_service):
    """Si Postgres venciera despues, la butaca quedaria atorada.

    Redis ya la habria soltado, pero Postgres seguiria diciendo
    "apartada" y nadie podria comprarla hasta que tambien venciera ahi.
    """
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]

    await ApartadoService(db, hold_service).apartar(pid, [seat], nuevo_hold())

    ahora = await db.scalar(select(func.now()))
    ttl_redis = await hold_service.ttl_restante(pid, seat)
    f = await fila(db, pid, seat)
    assert f.hold_expira_en <= ahora + timedelta(seconds=ttl_redis)


async def test_deja_rastro_en_la_bitacora(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()

    await ApartadoService(db, hold_service).apartar(pid, seats, hold)

    movimientos = (
        await db.scalars(select(InventoryMove).where(InventoryMove.hold_id == hold))
    ).all()
    assert len(movimientos) == 2
    assert {m.estado_previo for m in movimientos} == {"disponible"}
    assert {m.estado_nuevo for m in movimientos} == {"apartado"}


async def test_reintento_no_falla_ni_duplica_bitacora(db, clientes, hold_service):
    """Doble clic o red que se corta: la misma peticion dos veces."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)

    await servicio.apartar(pid, seats, hold)
    otra_vez = await servicio.apartar(pid, seats, hold)

    assert [x.seat_id for x in otra_vez.butacas] == seats
    total = await db.scalar(
        select(func.count(InventoryMove.id)).where(InventoryMove.hold_id == hold)
    )
    assert total == 2, "el reintento escribio movimientos que no ocurrieron"


async def test_otro_comprador_no_puede_apartarla(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    servicio = ApartadoService(db, hold_service)
    primero = nuevo_hold()

    await servicio.apartar(pid, [seat], primero)
    with pytest.raises(AsientoNoDisponible) as exc:
        await servicio.apartar(pid, [seat], nuevo_hold())

    assert exc.value.detalle["seat_id"] == seat
    assert (await fila(db, pid, seat)).hold_id == primero


@pytest.mark.parametrize("estado", ["vendido", "cortesia", "bloqueado", "kill"])
async def test_lo_que_no_esta_disponible_nunca_llega_a_redis(
    db, clientes, hold_service, redis_test, estado
):
    """El hueco que este servicio cierra.

    Redis no sabe que una butaca esta vendida: no tiene llave y la
    apartaria. Si llegara a Redis, el comprador avanzaria hasta Stripe
    antes de enterarse.
    """
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (libre, ocupada) = esc["performance_id"], esc["seat_ids"][:2]
    await poner_estado(db, pid, ocupada, estado)

    with pytest.raises(AsientoNoDisponible) as exc:
        await ApartadoService(db, hold_service).apartar(
            pid, [libre, ocupada], nuevo_hold()
        )

    assert exc.value.detalle["seat_id"] == ocupada
    assert await redis_test.keys("hold:*") == [], "algo llego a Redis"
    assert (await fila(db, pid, libre)).estado == "disponible", "no fue todo o nada"


async def test_butaca_de_otra_funcion(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    otra = await funcion_a_la_venta(db, clientes[0].id)

    with pytest.raises(ConflictoDeInventario):
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], [otra["seat_ids"][0]], nuevo_hold()
        )


async def test_tope_por_apartado_no_por_peticion(db, clientes, hold_service):
    """Ocho de un jalon, y despues una mas con el mismo hold: no."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["seat_ids"]
    tope = settings.MAX_ASIENTOS_POR_ORDEN
    assert len(seats) > tope
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)

    await servicio.apartar(pid, seats[:tope], hold)
    with pytest.raises(LimiteDeAsientos):
        await servicio.apartar(pid, [seats[tope]], hold)

    assert (await fila(db, pid, seats[tope])).estado == "disponible"


# --------------------------------------------------------------------------
# Cuando la funcion no se puede comprar
# --------------------------------------------------------------------------


async def test_funcion_sin_abrir_venta(db, clientes, hold_service, redis_test):
    esc = await armar_escenario(db, clientes[0].id)
    pid = esc["performance_id"]
    await InventoryService(db).materializar(pid)
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.PUBLICADO.value
    await db.flush()
    seat = await db.scalar(
        select(PerformanceSeat.seat_id).where(
            PerformanceSeat.performance_id == pid,
            PerformanceSeat.estado == "disponible",
        )
    )

    with pytest.raises(FuncionNoALaVenta) as exc:
        await ApartadoService(db, hold_service).apartar(pid, [seat], nuevo_hold())
    assert exc.value.detalle["motivo"] == "estado"
    assert await redis_test.keys("hold:*") == []


async def test_evento_en_borrador_no_existe_para_el_comprador(
    db, clientes, hold_service
):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.BORRADOR.value
    await db.flush()

    with pytest.raises(RecursoNoEncontrado):
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], esc["seat_ids"][:1], nuevo_hold()
        )


async def test_organizador_dado_de_baja_no_vende(
    db, clientes, hold_service, redis_test
):
    """Si PSD da de baja al organizador, sus eventos dejan de existir para el
    publico: no se aparta nada, aunque la funcion siga en venta."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    cliente = await db.get(Client, clientes[0].id)
    cliente.activo = False
    await db.flush()

    with pytest.raises(RecursoNoEncontrado):
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], esc["seat_ids"][:1], nuevo_hold()
        )
    assert await redis_test.keys("hold:*") == []


async def test_venta_cerrada_por_fecha(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion = await db.get(Performance, esc["performance_id"])
    ahora = await db.scalar(select(func.now()))
    funcion.venta_cierra_en = ahora - timedelta(minutes=1)
    await db.flush()

    with pytest.raises(FuncionNoALaVenta) as exc:
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], esc["seat_ids"][:1], nuevo_hold()
        )
    assert exc.value.detalle["motivo"] == "venta_cerrada"


async def test_venta_que_todavia_no_abre(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion = await db.get(Performance, esc["performance_id"])
    ahora = await db.scalar(select(func.now()))
    funcion.venta_abre_en = ahora + timedelta(hours=1)
    await db.flush()

    with pytest.raises(FuncionNoALaVenta) as exc:
        await ApartadoService(db, hold_service).apartar(
            esc["performance_id"], esc["seat_ids"][:1], nuevo_hold()
        )
    assert exc.value.detalle["motivo"] == "aun_no_abre"
    assert exc.value.detalle["abre_en"] == funcion.venta_abre_en.isoformat()


# --------------------------------------------------------------------------
# Vencimientos: Postgres y Redis no van al mismo paso
# --------------------------------------------------------------------------


async def test_apartado_vencido_en_ambos_se_puede_reapartar(
    db, clientes, hold_service, redis_test
):
    """Carrito abandonado: nadie lo libero, pero ya vencio."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [seat], nuevo_hold())

    await redis_test.delete(clave_butaca(pid, seat))
    ahora = await db.scalar(select(func.now()))
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == seat)
        .values(hold_expira_en=ahora - timedelta(minutes=1))
    )

    segundo = nuevo_hold()
    await servicio.apartar(pid, [seat], segundo)
    assert (await fila(db, pid, seat)).hold_id == segundo


async def test_vencido_en_postgres_pero_vivo_en_redis_decide_redis(
    db, clientes, hold_service
):
    """La ventana del margen: Postgres ya deja pasar, Redis todavia no."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    servicio = ApartadoService(db, hold_service)
    primero = nuevo_hold()
    await servicio.apartar(pid, [seat], primero)

    ahora = await db.scalar(select(func.now()))
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == seat)
        .values(hold_expira_en=ahora - timedelta(seconds=1))
    )

    with pytest.raises(AsientoNoDisponible):
        await servicio.apartar(pid, [seat], nuevo_hold())
    assert (await fila(db, pid, seat)).hold_id == primero
    assert await hold_service.es_titular(pid, [seat], primero)


# --------------------------------------------------------------------------
# Fallas a medias
# --------------------------------------------------------------------------


async def test_si_postgres_falla_se_suelta_redis_sin_tocar_lo_que_ya_tenia(
    db, clientes, hold_service, monkeypatch
):
    """Agregar una butaca y que falle no puede costar las que ya tenia."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (a, b) = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [a], hold)

    async def falla(*_args, **_kwargs):
        raise RuntimeError("se cayo Postgres")

    monkeypatch.setattr(servicio, "_reflejar", falla)
    with pytest.raises(RuntimeError):
        await servicio.apartar(pid, [a, b], hold)

    assert await hold_service.es_titular(pid, [a], hold), "perdio la que ya tenia"
    assert await hold_service.ttl_restante(pid, b) == -2, "quedo apartada en Redis"


async def test_si_el_panel_bloquea_entre_lectura_y_escritura(
    db, clientes, hold_service, monkeypatch
):
    """El `UPDATE` condicional repite la validacion en la base.

    Simula que la butaca se bloqueo desde el panel justo despues de que
    este servicio la leyo como disponible.
    """
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    servicio = ApartadoService(db, hold_service)
    hold = nuevo_hold()

    original = hold_service.apartar

    async def apartar_y_bloquear(*args, **kwargs):
        resultado = await original(*args, **kwargs)
        await poner_estado(db, pid, seat, "bloqueado")
        return resultado

    monkeypatch.setattr(hold_service, "apartar", apartar_y_bloquear)
    with pytest.raises(AsientoNoDisponible):
        await servicio.apartar(pid, [seat], hold)

    assert await hold_service.ttl_restante(pid, seat) == -2
    assert (await fila(db, pid, seat)).hold_id is None


# --------------------------------------------------------------------------
# Liberar y consultar
# --------------------------------------------------------------------------


async def test_liberar_devuelve_al_inventario(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, seats, hold)

    assert await servicio.liberar(pid, hold) == 2

    for seat in seats:
        f = await fila(db, pid, seat)
        assert (f.estado, f.hold_id, f.hold_expira_en) == ("disponible", None, None)
        assert await hold_service.ttl_restante(pid, seat) == -2
    liberados = await db.scalar(
        select(func.count(InventoryMove.id)).where(
            InventoryMove.hold_id == hold,
            InventoryMove.estado_nuevo == "disponible",
        )
    )
    assert liberados == 2


async def test_liberar_una_sola_butaca(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (a, b, c) = esc["performance_id"], esc["seat_ids"][:3]
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [a, b, c], hold)

    assert await servicio.liberar(pid, hold, [b]) == 1

    estado = await servicio.consultar(pid, hold)
    assert [x.seat_id for x in estado.butacas] == [a, c]


async def test_liberar_con_hold_ajeno_no_toca_nada(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    dueno = nuevo_hold()
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, [seat], dueno)

    assert await servicio.liberar(pid, nuevo_hold()) == 0
    assert (await fila(db, pid, seat)).hold_id == dueno
    assert await hold_service.es_titular(pid, [seat], dueno)


async def test_consultar_lo_liberado_ya_no_existe(db, clientes, hold_service):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid = esc["performance_id"]
    hold = nuevo_hold()
    servicio = ApartadoService(db, hold_service)
    await servicio.apartar(pid, esc["seat_ids"][:1], hold)
    await servicio.liberar(pid, hold)

    with pytest.raises(RecursoNoEncontrado):
        await servicio.consultar(pid, hold)


# --------------------------------------------------------------------------
# El panel frente a un comprador
# --------------------------------------------------------------------------


async def ps_id_de(db, performance_id: int, seat_id: int) -> int:
    return (await fila(db, performance_id, seat_id)).id


async def test_el_panel_no_le_quita_la_butaca_a_quien_la_aparto(
    db, clientes, hold_service
):
    """Bloquear una butaca que alguien esta pagando le quita el lugar."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (apartada, libre) = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()
    await ApartadoService(db, hold_service).apartar(pid, [apartada], hold)
    ids = [await ps_id_de(db, pid, apartada), await ps_id_de(db, pid, libre)]

    with pytest.raises(ConflictoDeInventario) as exc:
        await InventoryService(db).cambiar_estado(pid, ids, "bloqueado", "prensa")

    assert exc.value.detalle["butacas"] == [ids[0]]
    assert (await fila(db, pid, apartada)).hold_id == hold
    assert (await fila(db, pid, libre)).estado == "disponible", "no fue todo o nada"


async def test_el_panel_si_toca_un_apartado_vencido_y_lo_limpia(
    db, clientes, hold_service
):
    """Un carrito abandonado no puede impedir bloquear la butaca."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seat = esc["performance_id"], esc["seat_ids"][0]
    await ApartadoService(db, hold_service).apartar(pid, [seat], nuevo_hold())
    ahora = await db.scalar(select(func.now()))
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == seat)
        .values(hold_expira_en=ahora - timedelta(minutes=1))
    )

    cambiadas = await InventoryService(db).cambiar_estado(
        pid, [await ps_id_de(db, pid, seat)], "bloqueado", "prensa"
    )

    f = await fila(db, pid, seat)
    assert cambiadas == 1
    assert (f.estado, f.hold_id, f.hold_expira_en) == ("bloqueado", None, None)


async def test_el_panel_pierde_la_carrera_contra_un_comprador(
    db, clientes, hold_service, monkeypatch
):
    """El panel leyo la butaca libre, pero un comprador la aparto antes de
    que escribiera. El `UPDATE` condicional tiene que detenerlo aunque la
    validacion previa haya dicho que si, y no tocar ninguna otra butaca.
    """
    import app.services.inventory as inventario

    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (apartada, libre) = esc["performance_id"], esc["seat_ids"][:2]
    hold = nuevo_hold()
    await ApartadoService(db, hold_service).apartar(pid, [apartada], hold)
    ids = [await ps_id_de(db, pid, apartada), await ps_id_de(db, pid, libre)]

    # La lectura "no ve" el apartado, como si hubiera llegado despues.
    monkeypatch.setattr(inventario, "_apartado_vigente", lambda *_: False)
    with pytest.raises(ConflictoDeInventario) as exc:
        await InventoryService(db).cambiar_estado(pid, ids, "bloqueado", "prensa")

    assert exc.value.detalle["butacas"] == [ids[0]]
    f = await fila(db, pid, apartada)
    assert (f.estado, f.hold_id) == ("apartado", hold)
    assert (await fila(db, pid, libre)).estado == "disponible", "no fue todo o nada"
    movimientos = await db.scalar(
        select(func.count(InventoryMove.id)).where(InventoryMove.motivo == "prensa")
    )
    assert movimientos == 0


# --------------------------------------------------------------------------
# Concurrencia real: sesiones distintas que confirman
# --------------------------------------------------------------------------


async def test_cien_compradores_misma_butaca_postgres_y_redis_coinciden(
    motor_con_commit, hold_service
):
    """Exactamente un ganador, y Postgres dice el mismo nombre que Redis.

    Con la transaccion revertida de las demas pruebas las sesiones nunca
    se verian entre si. Aqui cada comprador confirma de verdad.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Concurrencia apartados")
        s.add(cliente)
        await s.flush()
        esc = await funcion_a_la_venta(s, cliente.id)
        await s.commit()
    pid, seat = esc["performance_id"], esc["seat_ids"][0]

    # Postgres de pruebas no aguanta cien conexiones a la vez. La fila de
    # espera es realista: es lo que hace el pool en produccion.
    fila_de_espera = asyncio.Semaphore(20)

    async def intentar() -> str | None:
        hold = nuevo_hold()
        async with (
            fila_de_espera,
            AsyncSession(motor_con_commit, expire_on_commit=False) as s,
        ):
            try:
                await ApartadoService(s, hold_service).apartar(pid, [seat], hold)
                await s.commit()
                return hold
            except AsientoNoDisponible:
                await s.rollback()
                return None

    resultados = await asyncio.gather(*(intentar() for _ in range(100)))
    ganadores = [r for r in resultados if r is not None]

    assert len(ganadores) == 1, f"ganaron {len(ganadores)} de 100"
    async with AsyncSession(motor_con_commit) as s:
        f = await fila(s, pid, seat)
        movimientos = await s.scalar(
            select(func.count(InventoryMove.id)).where(
                InventoryMove.performance_seat_id == f.id
            )
        )
    assert f.hold_id == ganadores[0], "Postgres y Redis tienen dueños distintos"
    assert await hold_service.es_titular(pid, [seat], ganadores[0])
    assert movimientos == 1


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@pytest.fixture
def api(cliente_http, hold_service):
    """Cliente HTTP con el Redis de pruebas en el endpoint."""
    from app.api.v1.apartados import get_hold_service
    from app.main import app

    app.dependency_overrides[get_hold_service] = lambda: hold_service
    return cliente_http


async def test_api_apartar_reintentar_consultar_liberar(api, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["seat_ids"][:2]
    hold = str(uuid.uuid4())
    url = f"{API}/performances/{pid}/apartados"

    r = await api.post(url, json={"hold_id": hold, "seat_ids": seats})
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["hold_id"] == hold
    assert [b["seat_id"] for b in cuerpo["butacas"]] == seats
    assert cuerpo["butacas"][0]["precio"] == "800.00"
    assert cuerpo["segundos_restantes"] > 0

    r = await api.post(url, json={"hold_id": hold, "seat_ids": seats})
    assert r.status_code == 201, "el reintento del mismo comprador fallo"

    r = await api.post(url, json={"hold_id": str(uuid.uuid4()), "seat_ids": seats})
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "asiento_no_disponible"

    r = await api.get(f"{url}/{hold}")
    assert r.status_code == 200

    r = await api.delete(f"{url}/{hold}")
    assert r.status_code == 204

    r = await api.get(f"{url}/{hold}")
    assert r.status_code == 404


async def test_api_no_pide_sesion_pero_si_un_uuid4(api, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    url = f"{API}/performances/{esc['performance_id']}/apartados"

    for malo in ("abc", "1", str(uuid.uuid1())):
        r = await api.post(url, json={"hold_id": malo, "seat_ids": esc["seat_ids"][:1]})
        assert r.status_code == 422, malo


async def test_api_liberar_solo_una(api, db, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    pid, (a, b) = esc["performance_id"], esc["seat_ids"][:2]
    hold = str(uuid.uuid4())
    url = f"{API}/performances/{pid}/apartados"
    await api.post(url, json={"hold_id": hold, "seat_ids": [a, b]})

    r = await api.delete(f"{url}/{hold}", params={"seat_id": [a]})
    assert r.status_code == 204

    r = await api.get(f"{url}/{hold}")
    assert [x["seat_id"] for x in r.json()["butacas"]] == [b]


async def test_api_dice_cuando_abre_la_venta(api, db, clientes):
    """El motivo tiene que llegar al front, con la fecha como texto."""
    esc = await funcion_a_la_venta(db, clientes[0].id)
    funcion = await db.get(Performance, esc["performance_id"])
    ahora = await db.scalar(select(func.now()))
    funcion.venta_abre_en = ahora + timedelta(days=3)
    await db.flush()

    r = await api.post(
        f"{API}/performances/{esc['performance_id']}/apartados",
        json={"hold_id": str(uuid.uuid4()), "seat_ids": esc["seat_ids"][:1]},
    )

    assert r.status_code == 409
    error = r.json()["error"]
    assert error["codigo"] == "funcion_no_a_la_venta"
    assert error["detalle"]["motivo"] == "aun_no_abre"
    assert error["detalle"]["abre_en"] == funcion.venta_abre_en.isoformat()
