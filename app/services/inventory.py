"""Materializacion y operacion del inventario.

Materializar es crear una fila de `performance_seats` por cada butaca del
mapa de una funcion. Antes de eso la funcion existe pero no puede vender
nada; despues, cada butaca es una fila con estado y precio propios, y es
sobre esas filas que opera el apartado.

Esta es zona critica: aqui nacen las filas que el indice unico y el
`UPDATE` condicional protegen. Un error de este modulo es una butaca
vendida dos veces o un comprador sin asiento en la puerta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FUNCION_NO_EXISTE, ErrorDeDominio, RecursoNoEncontrado
from app.models.base import EstadoAsiento, EstadoFuncion
from app.models.catalog import Performance, PerformancePrice, Seat, Zone
from app.models.inventory import InventoryMove, PerformanceSeat

# Las butacas se insertan por lotes. Un teatro mediano son miles de filas
# y mandarlas en una sola sentencia hace un paquete enorme contra el
# servidor; de una en una tarda minutos.
TAMANO_LOTE = 1_000

# Estados sobre los que no se puede operar a la ligera: hay dinero o un
# compromiso de por medio.
ESTADOS_COMPROMETIDOS = frozenset(
    {EstadoAsiento.VENDIDO.value, EstadoAsiento.CORTESIA.value}
)

# A donde puede llevar el panel una butaca. Lo demas nace de una orden
# (vendido, cortesia) o del servicio de apartados (apartado).
ESTADOS_DEL_PANEL = frozenset(
    {
        EstadoAsiento.DISPONIBLE.value,
        EstadoAsiento.BLOQUEADO.value,
        EstadoAsiento.KILL.value,
    }
)


def _apartado_vigente(fila: PerformanceSeat, ahora: datetime) -> bool:
    return (
        fila.estado == EstadoAsiento.APARTADO.value
        and fila.hold_expira_en is not None
        and fila.hold_expira_en > ahora
    )


class ConflictoDeInventario(ErrorDeDominio):
    codigo = "conflicto_de_inventario"
    http_status = 409


class InventarioIncompleto(ErrorDeDominio):
    """Falta configuracion para poder materializar."""

    codigo = "inventario_incompleto"
    http_status = 422


@dataclass(frozen=True)
class _ButacaDelMapa:
    seat_id: int
    zone_id: int
    kill: bool


@dataclass(frozen=True)
class ResultadoMaterializacion:
    performance_id: int
    butacas_creadas: int
    vendibles: int
    kill: int


@dataclass(frozen=True)
class ResumenInventario:
    performance_id: int
    inventario_generado: bool
    total: int
    por_estado: dict[str, int]

    @property
    def disponibles(self) -> int:
        return self.por_estado.get(EstadoAsiento.DISPONIBLE.value, 0)


class InventoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------ lectura

    async def obtener_funcion(self, performance_id: int) -> Performance:
        funcion = await self.db.get(Performance, performance_id)
        if funcion is None:
            raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
        return funcion

    async def resumen(self, performance_id: int) -> ResumenInventario:
        funcion = await self.obtener_funcion(performance_id)
        filas = (
            await self.db.execute(
                select(PerformanceSeat.estado, func.count(PerformanceSeat.id))
                .where(PerformanceSeat.performance_id == performance_id)
                .group_by(PerformanceSeat.estado)
            )
        ).all()
        por_estado = {estado: int(cuantas) for estado, cuantas in filas}
        return ResumenInventario(
            performance_id=performance_id,
            inventario_generado=funcion.inventario_generado,
            total=sum(por_estado.values()),
            por_estado=por_estado,
        )

    # ----------------------------------------------------- materializacion

    async def materializar(self, performance_id: int) -> ResultadoMaterializacion:
        """Crea el inventario de una funcion. Se corre una sola vez.

        La idempotencia no se resuelve consultando la bandera y luego
        escribiendo: entre la consulta y la escritura cabe otra peticion.
        Se resuelve con un `UPDATE` condicional que solo una de las dos
        puede ganar. La perdedora afecta cero filas y se entera asi.

        Volver a materializar nunca debe ser posible: reescribiria el
        estado de butacas ya vendidas y las pondria de vuelta a la venta.

        No se escribe bitacora por butaca aqui. La bitacora existe para
        explicar por que una butaca cambio de estado, y el estado inicial
        es uniforme y se deduce del mapa; registrarlo duplicaria la
        escritura de miles de filas sin responder ninguna pregunta.
        """
        funcion = await self.obtener_funcion(performance_id)

        butacas = await self._butacas_del_mapa(funcion.seat_map_id)
        if not butacas:
            raise InventarioIncompleto(
                "El mapa de butacas de esta funcion no tiene butacas."
            )

        precios = await self._precios_por_zona(performance_id)
        self._verificar_precios(butacas, precios)

        # La guarda. Si otra peticion gano la carrera, esta afecta cero
        # filas y no escribe una sola butaca.
        gano = await self.db.execute(
            update(Performance)
            .where(
                Performance.id == performance_id,
                Performance.inventario_generado.is_(False),
            )
            .values(inventario_generado=True)
        )
        if gano.rowcount == 0:
            raise ConflictoDeInventario(
                "El inventario de esta funcion ya fue generado.",
                {"performance_id": performance_id},
            )

        filas = [
            {
                "performance_id": performance_id,
                "seat_id": butaca.seat_id,
                "estado": (
                    EstadoAsiento.KILL.value
                    if butaca.kill
                    else EstadoAsiento.DISPONIBLE.value
                ),
                "precio_final": precios[butaca.zone_id],
                "precio_personalizado": False,
            }
            for butaca in butacas
        ]
        await self._insertar_por_lotes(filas)

        matados = sum(1 for b in butacas if b.kill)
        return ResultadoMaterializacion(
            performance_id=performance_id,
            butacas_creadas=len(filas),
            vendibles=len(filas) - matados,
            kill=matados,
        )

    async def sincronizar(self, performance_id: int) -> int:
        """Agrega al inventario las butacas que el mapa gano despues.

        Solo inserta. Nunca borra ni modifica: una butaca que desaparecio
        del mapa pero ya se vendio tiene que seguir existiendo, y la llave
        foranea con RESTRICT lo garantiza aunque alguien lo intente.

        Esto queda registrado en la bitacora butaca por butaca, al reves
        que la materializacion inicial: aqui es una anomalia y hay que
        poder explicarla despues.
        """
        funcion = await self.obtener_funcion(performance_id)
        if not funcion.inventario_generado:
            raise ConflictoDeInventario(
                "Esta funcion todavia no tiene inventario. Materializalo primero."
            )

        butacas = await self._butacas_del_mapa(funcion.seat_map_id)
        ya_estan = set(
            (
                await self.db.scalars(
                    select(PerformanceSeat.seat_id).where(
                        PerformanceSeat.performance_id == performance_id
                    )
                )
            ).all()
        )
        faltantes = [b for b in butacas if b.seat_id not in ya_estan]
        if not faltantes:
            return 0

        precios = await self._precios_por_zona(performance_id)
        self._verificar_precios(faltantes, precios)

        filas = [
            {
                "performance_id": performance_id,
                "seat_id": b.seat_id,
                "estado": (
                    EstadoAsiento.KILL.value
                    if b.kill
                    else EstadoAsiento.DISPONIBLE.value
                ),
                "precio_final": precios[b.zone_id],
                "precio_personalizado": False,
            }
            for b in faltantes
        ]

        # ON CONFLICT DO NOTHING por si dos sincronizaciones corren juntas:
        # la restriccion unica decide y ninguna truena.
        creados: list[int] = []
        for inicio in range(0, len(filas), TAMANO_LOTE):
            lote = filas[inicio : inicio + TAMANO_LOTE]
            resultado = await self.db.execute(
                pg_insert(PerformanceSeat)
                .values(lote)
                .on_conflict_do_nothing(index_elements=["performance_id", "seat_id"])
                .returning(PerformanceSeat.id)
            )
            creados.extend(resultado.scalars().all())

        if creados:
            await self.db.execute(
                insert(InventoryMove).values(
                    [
                        {
                            "performance_seat_id": ps_id,
                            "estado_previo": None,
                            "estado_nuevo": EstadoAsiento.DISPONIBLE.value,
                            "motivo": "sincronizacion de mapa",
                        }
                        for ps_id in creados
                    ]
                )
            )

        await self.db.flush()
        return len(creados)

    # -------------------------------------------------------- operaciones

    async def cambiar_estado(
        self,
        performance_id: int,
        performance_seat_ids: list[int],
        estado_nuevo: str,
        motivo: str,
        actor_id: int | None = None,
    ) -> int:
        """Bloquea, libera o inutiliza butacas desde el panel.

        Desde el panel solo se llega a `disponible`, `bloqueado` o `kill`.
        `vendido` y `cortesia` nacen de una orden, con su boleto; puestos a
        mano dejarian una butaca "vendida" sin cobro ni QR, y una cortesia
        sin boleto con la que el invitado no entra. `apartado` solo lo
        escribe el servicio de apartados.

        Tampoco toca lo que tiene dueño: vendidas, cortesias y apartados
        vigentes. Bloquear una butaca que alguien esta pagando le quita el
        lugar a medio cobro. Un apartado ya vencido si se puede tocar, y
        se limpia su `hold_id`.

        La escritura es un `UPDATE` condicional que repite esas reglas en
        la base: entre la lectura y la escritura un comprador pudo apartar
        la butaca. Si alguna fila no avanza, no se toca ninguna.
        """
        if estado_nuevo not in ESTADOS_DEL_PANEL:
            raise ConflictoDeInventario(
                "Desde el panel una butaca solo puede quedar disponible, "
                "bloqueada o en kill. Las ventas y cortesias se emiten con una "
                "orden.",
                {"estado": estado_nuevo, "permitidos": sorted(ESTADOS_DEL_PANEL)},
            )

        ahora = await self.db.scalar(select(func.now()))
        filas = list(
            (
                await self.db.scalars(
                    select(PerformanceSeat).where(
                        PerformanceSeat.id.in_(performance_seat_ids),
                        PerformanceSeat.performance_id == performance_id,
                    )
                )
            ).all()
        )
        encontradas = {f.id for f in filas}
        ajenas = set(performance_seat_ids) - encontradas
        if ajenas:
            raise ConflictoDeInventario(
                "Hay butacas que no pertenecen a esta funcion.",
                {"butacas": sorted(ajenas)},
            )

        comprometidas = [f.id for f in filas if f.estado in ESTADOS_COMPROMETIDOS]
        if comprometidas:
            raise ConflictoDeInventario(
                "Hay butacas vendidas o en cortesia. Eso se deshace cancelando "
                "la orden, no cambiando el estado.",
                {"butacas": sorted(comprometidas)},
            )

        apartadas = [f.id for f in filas if _apartado_vigente(f, ahora)]
        if apartadas:
            raise ConflictoDeInventario(
                "Hay butacas que un comprador tiene apartadas en este momento. "
                "Intenta cuando venza el apartado.",
                {"butacas": sorted(apartadas)},
            )

        a_cambiar = {f.id: f.estado for f in filas if f.estado != estado_nuevo}
        if not a_cambiar:
            return 0

        # Savepoint: si alguna fila no avanza se deshacen tambien las que si,
        # sin depender de que quien llama haga rollback.
        async with self.db.begin_nested():
            cambiadas = set(
                (
                    await self.db.scalars(
                        update(PerformanceSeat)
                        .where(
                            PerformanceSeat.id.in_(list(a_cambiar)),
                            PerformanceSeat.performance_id == performance_id,
                            PerformanceSeat.estado.not_in(ESTADOS_COMPROMETIDOS),
                            or_(
                                PerformanceSeat.estado != EstadoAsiento.APARTADO.value,
                                PerformanceSeat.hold_expira_en <= func.now(),
                            ),
                        )
                        .values(
                            estado=estado_nuevo,
                            hold_id=None,
                            hold_expira_en=None,
                            version=PerformanceSeat.version + 1,
                        )
                        .returning(PerformanceSeat.id)
                        .execution_options(synchronize_session="fetch")
                    )
                ).all()
            )
            perdidas = sorted(set(a_cambiar) - cambiadas)
            if perdidas:
                raise ConflictoDeInventario(
                    "Algunas butacas cambiaron mientras se procesaba la "
                    "operacion. Actualiza el mapa e intenta de nuevo.",
                    {"butacas": perdidas},
                )

        await self.db.execute(
            insert(InventoryMove).values(
                [
                    {
                        "performance_seat_id": ps_id,
                        "estado_previo": previo,
                        "estado_nuevo": estado_nuevo,
                        "motivo": motivo,
                        "actor_tipo": "user" if actor_id else None,
                        "actor_id": actor_id,
                    }
                    for ps_id, previo in a_cambiar.items()
                ]
            )
        )
        await self.db.flush()
        return len(a_cambiar)

    async def fijar_precio_butacas(
        self, performance_id: int, performance_seat_ids: list[int], precio: Decimal
    ) -> int:
        """Precio por excepcion para butacas concretas.

        Las marca como personalizadas para que un cambio posterior de
        precio de zona no se las lleve de vuelta.
        """
        resultado = await self.db.execute(
            update(PerformanceSeat)
            .where(
                PerformanceSeat.id.in_(performance_seat_ids),
                PerformanceSeat.performance_id == performance_id,
                # Repreciar lo vendido no tiene sentido: la orden ya
                # congelo su importe y la butaca ya no se vuelve a vender.
                PerformanceSeat.estado.not_in(ESTADOS_COMPROMETIDOS),
            )
            .values(precio_final=precio, precio_personalizado=True)
        )
        await self.db.flush()
        return resultado.rowcount

    async def aplicar_precios_de_zona(self, performance_id: int) -> int:
        """Baja al inventario los precios de zona vigentes.

        Respeta las butacas con precio personalizado y las comprometidas.
        Se corre despues de cambiar precios de una funcion que ya tiene
        inventario.
        """
        await self.obtener_funcion(performance_id)
        precios = await self._precios_por_zona(performance_id)

        tocadas = 0
        for zone_id, precio in precios.items():
            resultado = await self.db.execute(
                update(PerformanceSeat)
                .where(
                    PerformanceSeat.performance_id == performance_id,
                    PerformanceSeat.precio_personalizado.is_(False),
                    PerformanceSeat.estado.not_in(ESTADOS_COMPROMETIDOS),
                    PerformanceSeat.seat_id.in_(
                        select(Seat.id).where(Seat.zone_id == zone_id)
                    ),
                )
                .values(precio_final=precio)
            )
            tocadas += resultado.rowcount

        await self.db.flush()
        return tocadas

    async def abrir_venta(self, performance_id: int) -> Performance:
        """Pone la funcion en venta.

        Sin inventario no se abre: la funcion aparecería a la venta con
        cero butacas y el comprador veria un mapa vacio.
        """
        funcion = await self.obtener_funcion(performance_id)

        if not funcion.inventario_generado:
            raise ConflictoDeInventario(
                "No se puede abrir venta sin inventario materializado."
            )
        if funcion.estado == EstadoFuncion.CANCELADA.value:
            raise ConflictoDeInventario("La funcion esta cancelada.")

        funcion.estado = EstadoFuncion.EN_VENTA.value
        await self.db.flush()
        return funcion

    # ----------------------------------------------------------- privados

    async def _butacas_del_mapa(self, seat_map_id: int) -> list[_ButacaDelMapa]:
        filas = (
            await self.db.execute(
                select(Seat.id, Seat.zone_id, Seat.kill)
                .join(Zone, Zone.id == Seat.zone_id)
                .where(Zone.seat_map_id == seat_map_id)
                .order_by(Seat.zone_id, Seat.id)
            )
        ).all()
        return [
            _ButacaDelMapa(seat_id=sid, zone_id=zid, kill=k) for sid, zid, k in filas
        ]

    async def _precios_por_zona(self, performance_id: int) -> dict[int, Decimal]:
        filas = (
            await self.db.execute(
                select(PerformancePrice.zone_id, PerformancePrice.precio).where(
                    PerformancePrice.performance_id == performance_id,
                    PerformancePrice.activo.is_(True),
                )
            )
        ).all()
        return {zone_id: precio for zone_id, precio in filas}

    def _verificar_precios(
        self, butacas: list[_ButacaDelMapa], precios: dict[int, Decimal]
    ) -> None:
        """Ninguna butaca puede nacer sin precio.

        Es la validacion que mas importa de este modulo: una butaca
        materializada sin precio saldria a la venta con importe cero.
        """
        sin_precio = sorted({b.zone_id for b in butacas} - set(precios))
        if sin_precio:
            raise InventarioIncompleto(
                "Hay zonas sin precio para esta funcion. Fijalos antes de "
                "materializar el inventario.",
                {"zonas": sin_precio},
            )

    async def _insertar_por_lotes(self, filas: list[dict]) -> None:
        for inicio in range(0, len(filas), TAMANO_LOTE):
            await self.db.execute(
                insert(PerformanceSeat).values(filas[inicio : inicio + TAMANO_LOTE])
            )
        await self.db.flush()
