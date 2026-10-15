"""Webhooks de Stripe: registrar, deduplicar y reconciliar.

Stripe entrega cada evento al menos una vez, a veces dos, a veces fuera
de orden. Por eso:

- Cada evento se guarda en `payment_events` antes de procesarlo, con su
  id de Stripe unico. Uno ya procesado se contesta 200 sin repetir nada.
- El evento no se cree: solo dice "revisa este pago". `finalizar`
  consulta a Stripe el estado real del PaymentIntent y decide con eso.
  Asi da igual si llego primero `succeeded` o `amount_capturable_updated`.
- Si procesar falla, el error queda anotado y se contesta con error para
  que Stripe reintente.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payments import PaymentEvent
from app.services.checkout import CheckoutService


async def procesar_evento(
    db: AsyncSession, evento: dict, checkout: CheckoutService
) -> str:
    """Devuelve que se hizo: procesado, duplicado o ignorado."""
    await db.execute(
        pg_insert(PaymentEvent)
        .values(
            stripe_event_id=evento["id"],
            tipo=evento["type"],
            payload=evento,
        )
        .on_conflict_do_nothing(index_elements=[PaymentEvent.stripe_event_id])
    )
    registro = await db.scalar(
        select(PaymentEvent)
        .where(PaymentEvent.stripe_event_id == evento["id"])
        .execution_options(populate_existing=True)
    )
    # Se guarda antes de confirmar: despues de un rollback el objeto queda
    # vencido y leerlo pediria la base fuera de lugar.
    registro_id, ya_procesado = registro.id, registro.procesado_en is not None
    await db.commit()
    if ya_procesado:
        return "duplicado"

    resultado = "ignorado"
    try:
        if evento["type"].startswith("payment_intent."):
            intento_id = evento["data"]["object"]["id"]
            orden = await checkout.finalizar_por_intento(intento_id)
            if orden is not None:
                resultado = "procesado"
    except Exception as exc:
        await db.rollback()
        await db.execute(
            update(PaymentEvent)
            .where(PaymentEvent.id == registro_id)
            .values(error=f"{type(exc).__name__}: {exc}"[:2000])
        )
        await db.commit()
        raise

    await db.execute(
        update(PaymentEvent)
        .where(PaymentEvent.id == registro_id)
        .values(procesado_en=func.now(), error=None)
    )
    await db.commit()
    return resultado
