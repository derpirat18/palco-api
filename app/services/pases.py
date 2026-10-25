"""Pasar un boleto a un acompanante (BOL6).

Quien compro le pasa un boleto a otra persona escribiendo su nombre y su
correo. Al acompanante le llega un correo con su propia liga, que muestra
solo los boletos que le pasaron, con su QR que cambia.

No se emite un boleto nuevo: el boleto cambia de portador
(`holder_customer_id`) y se le cambia la semilla del QR, asi que el QR que
tenia quien compro deja de servir al instante (en el celular, en una
captura o en el paquete sin internet). La `Transfer` guarda quien lo paso,
a quien y la llave de la liga del acompanante.

Reglas (decididas el 25 oct):

- Solo quien compro pasa boletos. El acompanante no puede volver a pasarlo.
- Se pasa y se recupera hasta que abre el escaneo de la funcion (4 h antes).
  Despues, a taquilla: las puertas sin internet ya bajaron la lista.
- Recuperar: mientras no haya entrado. Cambia el QR otra vez y la liga del
  acompanante deja de mostrarlo. Al acompanante se le avisa por correo.
- No se paga ni se cobra nada: no toca el cobro ni la orden.
"""

import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio, RecursoNoEncontrado
from app.models.base import (
    EstadoBoleto,
    EstadoFuncion,
    EstadoOrden,
    EstadoTransferencia,
)
from app.models.catalog import Performance
from app.models.outbox import OutboxEvent
from app.models.sales import Customer, Order, OrderItem, Ticket, Transfer
from app.services.boletos import BoletosDeLaOrden, nombre_corto, vista_de_boletos
from app.services.escaner import horario_de_escaneo

# Contra el ir y venir (cada pase manda un correo): por compra y por dia.
PASES_POR_ORDEN_AL_DIA = 12

BOLETO_NO_EXISTE = "No encontramos ese boleto en tu compra."
PASE_NO_EXISTE = (
    "Este boleto ya no está a tu nombre. Si crees que es un error, pídele a "
    "quien te lo pasó que te lo vuelva a pasar."
)


class NoSePuedePasar(ErrorDeDominio):
    codigo = "no_se_puede_pasar"
    http_status = 409


class DemasiadosPases(ErrorDeDominio):
    codigo = "demasiados_pases"
    http_status = 429


async def _orden_y_boleto(
    db: AsyncSession, orden_uuid: UUID, boleto_uuid: UUID
) -> tuple[Order, Ticket, Performance]:
    # La orden bloqueada: los pases de una misma compra van de uno en uno.
    orden = await db.scalar(
        select(Order).where(Order.uuid == orden_uuid).with_for_update()
    )
    if orden is None:
        raise RecursoNoEncontrado("No encontramos esta compra.")
    boleto = await db.scalar(
        select(Ticket)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(Ticket.uuid == boleto_uuid, OrderItem.order_id == orden.id)
        .with_for_update(of=Ticket)
        .execution_options(populate_existing=True)
    )
    if boleto is None:
        raise RecursoNoEncontrado(BOLETO_NO_EXISTE)
    funcion = await db.get(Performance, orden.performance_id)
    return orden, boleto, funcion


async def _revisar(
    db: AsyncSession, orden: Order, boleto: Ticket, funcion: Performance
) -> None:
    """Lo comun a pasar y recuperar: compra activa, boleto vivo, a tiempo."""
    if orden.estado != EstadoOrden.PAGADA.value:
        raise NoSePuedePasar("Esta compra ya no está activa.")
    if funcion.estado == EstadoFuncion.CANCELADA.value:
        raise NoSePuedePasar("La función se canceló.")
    if boleto.estado == EstadoBoleto.USADO.value:
        raise NoSePuedePasar("Este boleto ya entró.")
    if boleto.estado != EstadoBoleto.VALIDO.value:
        raise NoSePuedePasar("Este boleto ya no permite la entrada.")
    ahora = await db.scalar(select(func.now()))
    if ahora >= horario_de_escaneo(funcion)[0]:
        raise NoSePuedePasar(
            "El acceso a la función ya abrió: los boletos ya no se pueden pasar "
            "ni recuperar desde aquí. Vayan juntos a taquilla con tu folio "
            f"{orden.folio} y una identificación."
        )


async def _persona(db: AsyncSession, email: str, nombre: str, apellidos: str):
    """Un correo, una persona. A una que ya existe no se le cambia el nombre."""
    await db.execute(
        pg_insert(Customer)
        .values(email=email, nombre=nombre or None, apellidos=apellidos or None)
        .on_conflict_do_nothing(index_elements=[Customer.email])
    )
    persona = await db.scalar(
        select(Customer)
        .where(Customer.email == email)
        .execution_options(populate_existing=True)
    )
    if persona.nombre is None:
        persona.nombre = nombre or None
        persona.apellidos = apellidos or None
    return persona


async def pasar(
    db: AsyncSession,
    orden_uuid: UUID,
    boleto_uuid: UUID,
    nombre: str,
    apellidos: str,
    email: str,
) -> Transfer:
    """Le pasa el boleto a otra persona. El que llama confirma la transaccion."""
    orden, boleto, funcion = await _orden_y_boleto(db, orden_uuid, boleto_uuid)
    await _revisar(db, orden, boleto, funcion)
    if boleto.holder_customer_id != orden.customer_id:
        portador = await db.get(Customer, boleto.holder_customer_id)
        raise NoSePuedePasar(
            f"Este boleto ya se lo pasaste a {nombre_corto(portador)}. "
            "Recupéralo primero si quieres pasárselo a alguien más."
        )
    correo = email.strip().lower()
    comprador = await db.get(Customer, orden.customer_id)
    if correo in {comprador.email.lower(), (orden.email_snap or "").lower()}:
        raise NoSePuedePasar(
            "Ese es el correo de esta compra. Escribe el de la persona que te acompaña."
        )
    pases_de_hoy = await db.scalar(
        select(func.count())
        .select_from(Transfer)
        .join(Ticket, Ticket.id == Transfer.ticket_id)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(
            OrderItem.order_id == orden.id,
            Transfer.creado_en > func.now() - timedelta(days=1),
        )
    )
    if (pases_de_hoy or 0) >= PASES_POR_ORDEN_AL_DIA:
        raise DemasiadosPases(
            "Ya pasaste boletos de esta compra muchas veces hoy. Intenta mañana "
            "o ve a taquilla con tu folio."
        )

    persona = await _persona(db, correo, nombre.strip(), apellidos.strip())
    boleto.holder_customer_id = persona.id
    # QR nuevo: el que tenia quien compro deja de servir.
    boleto.semilla = secrets.token_bytes(32)
    pase = Transfer(
        ticket_id=boleto.id,
        de_customer_id=orden.customer_id,
        para_email=correo,
        a_customer_id=persona.id,
        token=secrets.token_urlsafe(32),
        estado=EstadoTransferencia.ACEPTADA.value,
        aceptada_en=func.now(),
    )
    db.add(pase)
    await db.flush()
    # Sin datos personales en la bandeja: el correo se arma desde la base.
    db.add(
        OutboxEvent(
            tipo="boleto.pasado",
            agregado_uuid=orden.uuid,
            payload={"transferencia_id": pase.id, "boleto": str(boleto.uuid)},
        )
    )
    await db.flush()
    return pase


async def recuperar(db: AsyncSession, orden_uuid: UUID, boleto_uuid: UUID) -> None:
    """Regresa el boleto a quien compro, con QR nuevo."""
    orden, boleto, funcion = await _orden_y_boleto(db, orden_uuid, boleto_uuid)
    await _revisar(db, orden, boleto, funcion)
    pase = await db.scalar(
        select(Transfer)
        .where(
            Transfer.ticket_id == boleto.id,
            Transfer.estado == EstadoTransferencia.ACEPTADA.value,
        )
        .order_by(Transfer.id.desc())
        .limit(1)
        .with_for_update()
    )
    if boleto.holder_customer_id == orden.customer_id or pase is None:
        raise NoSePuedePasar("Este boleto ya es tuyo.")
    pase.estado = EstadoTransferencia.CANCELADA.value
    boleto.holder_customer_id = orden.customer_id
    boleto.semilla = secrets.token_bytes(32)
    db.add(
        OutboxEvent(
            tipo="boleto.recuperado",
            agregado_uuid=orden.uuid,
            payload={"transferencia_id": pase.id, "boleto": str(boleto.uuid)},
        )
    )
    await db.flush()


async def pase_vigente(db: AsyncSession, token: str) -> tuple[Transfer, Order]:
    """El pase de esta liga, si el acompanante todavia tiene el boleto."""
    pase = await db.scalar(
        select(Transfer)
        .where(Transfer.token == token)
        .execution_options(populate_existing=True)
    )
    if pase is None or pase.estado != EstadoTransferencia.ACEPTADA.value:
        raise RecursoNoEncontrado(PASE_NO_EXISTE)
    fila = (
        await db.execute(
            select(Ticket, Order)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Ticket.id == pase.ticket_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    boleto, orden = fila
    if boleto.holder_customer_id != pase.a_customer_id:
        raise RecursoNoEncontrado(PASE_NO_EXISTE)
    return pase, orden


async def boletos_del_pase(db: AsyncSession, token: str) -> BoletosDeLaOrden:
    """Lo que ve el acompanante con su liga: los boletos de esa compra que tiene."""
    pase, orden = await pase_vigente(db, token)
    return await vista_de_boletos(db, orden, pase.a_customer_id, comprador=False)
