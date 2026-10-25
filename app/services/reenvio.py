"""Reenviar la liga de los boletos (BOL15; BOL13 lo usara desde Ventas).

En una computadora los boletos no se muestran: entran desde el celular. Para
llevarlos al celular sin poner en pantalla nada que se pueda fotografiar (la
liga de la compra es la llave de los boletos), el comprador pide que le
reenviemos el correo con la liga, y la abre en su celular.

Reglas:

- Solo al correo de la compra. Nunca a otro: quien tiene la liga de la
  compra no puede llevarse los boletos a su propio correo.
- La respuesta dice a que correo se mando, tapado ("an•••@correo.mx"):
  suficiente para que el comprador sepa donde buscar, sin mostrarlo entero.
- Tope de `REENVIOS_POR_HORA` por compra, contado en la bandeja de salida
  (la base, no Redis): quien tenga la liga no puede llenar de correos al
  comprador.
- Solo si la compra esta pagada y algun boleto todavia puede entrar.
- El correo sale por la bandeja, como el original: con reintentos.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio, RecursoNoEncontrado
from app.models.outbox import OutboxEvent
from app.models.sales import Order
from app.services.confirmacion import armar_confirmacion

TIPO = "orden.reenvio"
REENVIOS_POR_HORA = 3
_VENTANA = timedelta(hours=1)


class DemasiadosReenvios(ErrorDeDominio):
    codigo = "demasiados_reenvios"
    http_status = 429


class SinBoletosPorReenviar(ErrorDeDominio):
    codigo = "sin_boletos_por_reenviar"
    http_status = 409


@dataclass(frozen=True)
class Reenvio:
    enviado_a: str


def tapar_correo(correo: str) -> str:
    """'ana@correo.mx' -> 'an•••@correo.mx'."""
    usuario, arroba, dominio = correo.partition("@")
    if not arroba:
        return "•••"
    visible = usuario[:2] if len(usuario) > 3 else usuario[:1]
    return f"{visible}•••@{dominio}"


async def pedir_reenvio(
    db: AsyncSession, orden_uuid: UUID, actor_id: int | None = None
) -> Reenvio:
    """Deja el reenvio en la bandeja. El que llama confirma la transaccion."""
    # Bloqueo de la orden: dos peticiones a la vez no se saltan el tope.
    orden = await db.scalar(
        select(Order).where(Order.uuid == orden_uuid).with_for_update()
    )
    if orden is None:
        raise RecursoNoEncontrado("No encontramos esta compra.")
    mensaje = await armar_confirmacion(db, orden_uuid)
    if mensaje is None:
        raise SinBoletosPorReenviar(
            "Esta compra ya no tiene boletos tuyos que puedan entrar. "
            "Si tienes dudas, ve a taquilla con tu folio."
        )
    recientes = await db.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.agregado_uuid == orden.uuid,
            OutboxEvent.tipo == TIPO,
            OutboxEvent.creado_en > func.now() - _VENTANA,
        )
    )
    if (recientes or 0) >= REENVIOS_POR_HORA:
        raise DemasiadosReenvios(
            "Ya te reenviamos el correo varias veces. Revisa tu bandeja de "
            "entrada y la de no deseados; si no llega, intenta en una hora."
        )
    db.add(
        OutboxEvent(
            tipo=TIPO,
            agregado_uuid=orden.uuid,
            # Sin datos personales en la bandeja: el correo se arma desde la base.
            payload={"orden_uuid": str(orden.uuid), "actor_id": actor_id},
        )
    )
    await db.flush()
    return Reenvio(enviado_a=tapar_correo(mensaje.para))
