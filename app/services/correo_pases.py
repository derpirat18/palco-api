"""Correos al acompanante (BOL6): "te pasaron un boleto" y "ya no lo tienes".

Con el diseno del correo de confirmacion de Nadia (`correo/pase.hbs.html`
sale de `confirmacion.hbs.html`), sin resumen de pago: al acompanante no
le toca saber cuanto se pago. Como el de confirmacion, se arma desde la
base al momento de enviarlo.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.base import EstadoBoleto, EstadoTransferencia
from app.models.sales import Customer, Order, OrderItem, Ticket, Transfer
from app.services.boletos import boletos_de_la_orden
from app.services.confirmacion import _fila_y_lugar, datos_del_correo
from app.services.correo import Mensaje
from app.services.plantillas import pintar_archivo


def enlace_del_pase(token: str) -> str:
    return f"{settings.FRONT_URL.rstrip('/')}/pase/{token}"


async def armar_correo_de_pase(
    db: AsyncSession, transferencia_id: int, recuperado: bool = False
) -> Mensaje | None:
    """El correo para el acompanante, o None si ya no corresponde mandarlo."""
    pase = await db.get(Transfer, transferencia_id)
    if pase is None:
        return None
    boleto = await db.get(Ticket, pase.ticket_id)
    partida = await db.get(OrderItem, boleto.order_item_id)
    if recuperado:
        if pase.estado != EstadoTransferencia.CANCELADA.value:
            return None
    elif (
        pase.estado != EstadoTransferencia.ACEPTADA.value
        or boleto.holder_customer_id != pase.a_customer_id
        or boleto.estado != EstadoBoleto.VALIDO.value
    ):
        # Lo recupero, entro o se cancelo antes de que saliera el correo.
        return None

    orden = await db.get(Order, partida.order_id)
    vista = await boletos_de_la_orden(db, orden.uuid)
    comprador = await db.get(Customer, orden.customer_id)
    datos = datos_del_correo(vista, comprador, [], cortesia=True)

    de = (orden.nombre_snap or comprador.nombre or "").split(" ")[0] or (
        "Quien compró los boletos"
    )
    fila, lugar = _fila_y_lugar(partida.asiento_snap)
    nombre = vista.evento.nombre
    if recuperado:
        asunto = f"Ya no tienes el boleto para {nombre}"
        titulo, subtitulo = "Ya no tienes este boleto", f"{de} lo recuperó"
        preheader = f"{de} recuperó el boleto para {nombre} que te había pasado."
    else:
        asunto = f"{de} te pasó un boleto para {nombre}"
        titulo, subtitulo = "¡Te pasaron un boleto!", f"De parte de {de}"
        preheader = (
            f"{nombre}, {datos['funcion']['fecha']} · {datos['funcion']['hora']}. "
            "Ábrelo en tu celular para entrar."
        )
    datos.update(
        {
            "asunto": asunto,
            "preheader": preheader,
            "titulo": titulo,
            "subtitulo": subtitulo,
            "recuperado": recuperado,
            "de": de,
            "boton": "Ver mi boleto",
            "boletos_url": enlace_del_pase(pase.token),
            "num_boletos": 1,
            "boletos": [
                {
                    "tipo": "Boleto general",
                    "zona": partida.zona_snap,
                    "fila": fila or "—",
                    "lugar": lugar,
                    "accesible": False,
                }
            ],
        }
    )
    return Mensaje(
        para=pase.para_email,
        asunto=asunto,
        texto=pintar_archivo("correo/pase.txt.hbs", datos, escapar=False),
        html=pintar_archivo("correo/pase.hbs.html", datos),
    )
