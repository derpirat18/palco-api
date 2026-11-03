"""Correo "Tus boletos se cancelaron" (3 nov): cuando el jefe de taquilla
corrige una venta por error del cajero.

Con el diseno de los correos de Nadia (`correo/cancelacion.hbs.html` sale de
`pase.hbs.html`). Dice que boletos ya no sirven y como se le devolvio el
dinero; si era cortesia, sin dinero. Se arma desde la base al enviarlo: si
la venta ya no esta corregida, no sale.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoOrden
from app.models.sales import Customer, Order, OrderItem
from app.models.taquilla import VentaDeTaquilla
from app.services.boletos import boletos_de_la_orden
from app.services.confirmacion import _fila_y_lugar, datos_del_correo, pesos
from app.services.correo import Mensaje
from app.services.plantillas import pintar_archivo

# El aviso en la bandeja (lo escribe `TaquillaService.anular`).
VENTA_CORREGIDA = "taquilla.corregida"


def texto_de_devolucion(devolucion: str | None, devuelto) -> str | None:
    if devolucion == "efectivo":
        return f"Te devolvimos {pesos(devuelto)} en efectivo en la taquilla."
    if devolucion == "terminal":
        return (
            f"Cancelamos el cobro de {pesos(devuelto)} en la terminal. Según tu "
            "banco, puede tardar unos días en reflejarse en tu estado de cuenta."
        )
    return None


async def armar_correo_de_cancelacion(
    db: AsyncSession, orden_uuid: UUID
) -> Mensaje | None:
    orden = await db.scalar(select(Order).where(Order.uuid == orden_uuid))
    if orden is None or orden.estado != EstadoOrden.CANCELADA.value:
        return None
    venta = await db.scalar(
        select(VentaDeTaquilla).where(VentaDeTaquilla.order_id == orden.id)
    )
    if venta is None or venta.anulada_en is None:
        return None
    comprador = await db.get(Customer, orden.customer_id)
    para = orden.email_snap or (comprador.email if comprador else None)
    if not para:
        return None
    vista = await boletos_de_la_orden(db, orden.uuid)
    datos = datos_del_correo(vista, comprador, [], cortesia=True)
    partidas = (
        await db.scalars(
            select(OrderItem)
            .where(OrderItem.order_id == orden.id)
            .order_by(OrderItem.id)
        )
    ).all()
    boletos = []
    for p in partidas:
        fila, lugar = _fila_y_lugar(p.asiento_snap)
        boletos.append({"zona": p.zona_snap, "fila": fila or "—", "lugar": lugar})
    cortesia = venta.forma_pago == "cortesia"
    nombre = vista.evento.nombre
    n = len(boletos)
    if cortesia:
        asunto = f"Se cancelaron tus cortesías para {nombre}"
        titulo = "Se cancelaron tus cortesías" if n > 1 else "Se canceló tu cortesía"
    else:
        asunto = f"Se cancelaron tus boletos para {nombre}"
        titulo = "Se cancelaron tus boletos" if n > 1 else "Se canceló tu boleto"
    devolucion = texto_de_devolucion(venta.devolucion, venta.devuelto)
    datos.update(
        {
            "asunto": asunto,
            "preheader": (
                f"La taquilla corrigió tu compra (folio {orden.folio}). "
                + (devolucion or "Estos boletos ya no sirven para entrar.")
            ),
            "titulo": titulo,
            "subtitulo": f"Folio {orden.folio}",
            "cortesia": cortesia,
            "devolucion": devolucion,
            "num_boletos": n,
            "boletos": boletos,
        }
    )
    return Mensaje(
        para=para,
        asunto=asunto,
        texto=pintar_archivo("correo/cancelacion.txt.hbs", datos, escapar=False),
        html=pintar_archivo("correo/cancelacion.hbs.html", datos),
    )
