"""Lista de boletos de una funcion para la puerta.

Es el respaldo del escaner: si falla la camara, el internet o la luz, el
personal de acceso tiene en papel quien entra, con que boleto y en que
lugar. Lleva lo minimo para eso: nada de correos ni telefonos, que en una
hoja impresa en la puerta no se pueden proteger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoBoleto, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Customer, Order, OrderItem, Ticket

# Lo que puede pasar por la puerta: valido, o ya usado (para que quien
# revisa vea "ya entro" en lugar de "no existe").
EN_LA_LISTA = (EstadoBoleto.VALIDO.value, EstadoBoleto.USADO.value)


@dataclass(frozen=True)
class RenglonDeAcceso:
    zona: str
    asiento: str
    codigo: str
    folio: str
    titular: str
    cortesia: bool
    estado: str


@dataclass(frozen=True)
class ListaDeAcceso:
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    apertura_puertas: datetime | None
    boletos: tuple[RenglonDeAcceso, ...]

    @property
    def total(self) -> int:
        return len(self.boletos)

    @property
    def usados(self) -> int:
        return sum(1 for b in self.boletos if b.estado == EstadoBoleto.USADO.value)


def _titular(c: Customer) -> str:
    nombre = " ".join(p for p in (c.nombre, c.apellidos) if p)
    if nombre:
        return nombre
    # Sin nombre (una cortesia capturada solo con correo): el inicio del
    # correo basta para reconocerlo sin escribirlo completo en la hoja.
    usuario = c.email.split("@", 1)[0]
    return f"{usuario[:3]}…"


_FILA_Y_NUMERO = re.compile(r"Fila\s+(\S+)\s+·\s+(\d+)")


def _orden(r: RenglonDeAcceso) -> tuple:
    """Zona, fila y numero como los lee una persona: A-2 antes que A-10."""
    m = _FILA_Y_NUMERO.search(r.asiento)
    if not m:
        return (r.zona, r.asiento, 0)
    fila = m.group(1)
    return (r.zona, len(fila), fila, int(m.group(2)))


class ListaDeAccesoService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def de_funcion(self, funcion: Performance, evento: Event) -> ListaDeAcceso:
        recinto = await self.db.get(Venue, evento.venue_id)
        filas = (
            await self.db.execute(
                select(OrderItem, Ticket, Order, Customer)
                .join(Ticket, Ticket.order_item_id == OrderItem.id)
                .join(Order, Order.id == OrderItem.order_id)
                .join(Customer, Customer.id == Ticket.holder_customer_id)
                .where(
                    Order.performance_id == funcion.id,
                    Order.estado == EstadoOrden.PAGADA.value,
                    OrderItem.anulado.is_(False),
                    Ticket.estado.in_(EN_LA_LISTA),
                )
            )
        ).all()
        renglones = sorted(
            (
                RenglonDeAcceso(
                    zona=partida.zona_snap,
                    asiento=partida.asiento_snap,
                    codigo=boleto.codigo,
                    folio=orden.folio,
                    titular=_titular(titular),
                    cortesia=orden.canal == "cortesia",
                    estado=boleto.estado,
                )
                for partida, boleto, orden, titular in filas
            ),
            key=_orden,
        )
        return ListaDeAcceso(
            evento=evento.nombre,
            recinto=recinto.nombre if recinto else "",
            ciudad=recinto.ciudad if recinto else "",
            zona_horaria=recinto.zona_horaria if recinto else "America/Mexico_City",
            inicia_en=funcion.inicia_en,
            apertura_puertas=funcion.apertura_puertas,
            boletos=tuple(renglones),
        )
