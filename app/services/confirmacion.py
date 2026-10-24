"""Correo de confirmacion de compra (ADR 0015, BOL1).

El diseno es de Nadia (`app/plantillas/correo/`, Handlebars). El correo ya
no lleva QR: los boletos se muestran en el celular con un QR que cambia
cada 30 s (BOL3), y el correo lleva a ellos con "Ver mis boletos".

Se arma desde la base en el momento de enviarlo, no desde el `payload`
de la bandeja: el payload no lleva datos personales a proposito, y asi
el correo refleja el estado real (si la orden se reembolso antes de que
saliera el correo, no se manda).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.base import EstadoOrden
from app.models.sales import Customer, OrderItem
from app.services.boletos import BoletosDeLaOrden, boletos_de_la_orden
from app.services.correo import Mensaje
from app.services.plantillas import pintar_archivo


def pesos(monto: Decimal) -> str:
    return f"${monto:,.2f}"


POR = "\u00d7"  # el signo de multiplicar del diseno
_DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_MESES_CORTOS = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
]  # fmt: skip


def fecha_corta(momento: datetime, zona: str) -> str:
    """'vie, 23 oct 2026', en la hora del recinto."""
    local = momento.astimezone(ZoneInfo(zona))
    return (
        f"{_DIAS_CORTOS[local.weekday()]}, {local.day} "
        f"{_MESES_CORTOS[local.month - 1]} {local.year}"
    )


def hora_corta(momento: datetime, zona: str) -> str:
    """'16:00 h', en la hora del recinto."""
    local = momento.astimezone(ZoneInfo(zona))
    return f"{local.hour}:{local.minute:02d} h"


def enlace_a_boletos(orden_uuid: UUID) -> str:
    return f"{settings.FRONT_URL.rstrip('/')}/orden/{orden_uuid}"


def _fila_y_lugar(asiento: str) -> tuple[str, str]:
    """'Fila A · 2' -> ('A', '2'). Si no trae fila, todo va en lugar."""
    fila, separador, lugar = asiento.partition(" · ")
    if not separador:
        return "", asiento
    return fila.removeprefix("Fila ").strip(), lugar.strip()


async def armar_confirmacion(
    db: AsyncSession,
    orden_uuid: UUID,
    cortesia: bool | None = None,
    para_enviar: bool = True,
) -> Mensaje | None:
    """El correo listo para enviar, o None si ya no corresponde mandarlo.

    Una cortesia no lleva resumen de pago: dice que no tiene costo. Sin
    `cortesia`, se deduce del canal de la orden ("Ver en el navegador").
    """
    vista = await boletos_de_la_orden(db, orden_uuid)
    if cortesia is None:
        cortesia = vista.orden.canal == "cortesia"
    if vista.orden.estado != EstadoOrden.PAGADA.value:
        return None
    # Para enviar, solo si algun boleto puede entrar. "Ver en el navegador"
    # lo sigue mostrando aunque ya hayan entrado todos.
    if para_enviar and not any(b.llave_qr for b in vista.boletos):
        return None
    comprador = await db.get(Customer, vista.orden.customer_id)
    partidas = list(
        (
            await db.scalars(
                select(OrderItem)
                .where(
                    OrderItem.order_id == vista.orden.id, OrderItem.anulado.is_(False)
                )
                .order_by(OrderItem.id)
            )
        ).all()
    )
    return _mensaje(vista, comprador, partidas, cortesia)


def datos_del_correo(
    vista: BoletosDeLaOrden,
    comprador: Customer,
    partidas: list[OrderItem],
    cortesia: bool = False,
) -> dict:
    """Lo que llena la plantilla (la forma de `datos-ejemplo.json` de Nadia)."""
    orden, evento, funcion, recinto = (
        vista.orden,
        vista.evento,
        vista.funcion,
        vista.recinto,
    )
    zona = recinto.zona_horaria
    front = settings.FRONT_URL.rstrip("/")
    # Los que siguen siendo suyos: los que pueden entrar y los que ya entraron.
    validos = [b for b in vista.boletos if b.estado in ("valido", "usado")]

    pago = None
    if not cortesia:
        # Agrupadas por zona y precio: "2 x Luneta, $720.00 por boleto" (con el
        # signo de multiplicar, como en el diseno).
        grupos: dict[tuple[str, Decimal], int] = {}
        for p in partidas:
            clave = (p.zona_snap, p.precio_unitario)
            grupos[clave] = grupos.get(clave, 0) + 1
        pago = {
            "partidas": [
                {
                    "descripcion": f"{n} {POR} {zona_}",
                    "detalle": f"{pesos(precio)} por boleto",
                    "importe": pesos(precio * n),
                }
                for (zona_, precio), n in grupos.items()
            ],
            "descuento": pesos(orden.descuento) if orden.descuento > 0 else None,
            "subtotal": pesos(orden.subtotal),
            "iva_pct": format(orden.iva_pct_snap.normalize(), "f"),
            "iva": pesos(orden.iva),
            "total": pesos(orden.total),
            "moneda": orden.moneda,
            # La tarjeta (Visa •••• 4242) espera autorizacion: viene de Stripe
            # al cobrar, zona critica.
            "metodo": None,
            "pagada_en": (
                f"{fecha_corta(orden.pagada_en, zona).split(', ', 1)[1]}, "
                f"{hora_corta(orden.pagada_en, zona)}"
                if orden.pagada_en
                else ""
            ),
        }

    boletos = []
    for b in validos:
        fila, lugar = _fila_y_lugar(b.asiento)
        boletos.append(
            {
                "tipo": "Boleto general",
                "zona": b.zona,
                "fila": fila or "—",
                "lugar": lugar,
                "accesible": False,
            }
        )

    return {
        "marca": settings.MARCA,
        "assets_url": f"{front}/img/correo",
        "ver_en_navegador_url": f"{front}/orden/{orden.uuid}/correo",
        "folio": orden.folio,
        "num_boletos": len(boletos),
        # Hoy toda compra es como invitado; la invitacion a crear cuenta se
        # enciende cuando existan las cuentas de comprador (R1).
        "es_invitado": True,
        "cuentas": False,
        "comprador": {
            "nombre": orden.nombre_snap or comprador.nombre,
            "email": orden.email_snap or comprador.email,
        },
        "evento": {
            "nombre": evento.nombre,
            "categoria": evento.categoria,
            "imagen_url": evento.imagen_url,
        },
        "funcion": {
            "fecha": fecha_corta(funcion.inicia_en, zona),
            "hora": hora_corta(funcion.inicia_en, zona),
            "apertura": (
                hora_corta(funcion.apertura_puertas, zona)
                if funcion.apertura_puertas
                else None
            ),
        },
        "recinto": {"nombre": recinto.nombre, "ciudad": recinto.ciudad},
        "boletos_url": enlace_a_boletos(orden.uuid),
        "crear_cuenta_url": "",
        "boletos": boletos,
        "pago": pago,
        "redes": settings.REDES_SOCIALES,
        "soporte_email": settings.SOPORTE_EMAIL,
        "enlaces": {
            "inicio": f"{front}/",
            "mi_cuenta": f"{front}/cuenta",
            "devoluciones": f"{front}/legal/politica-de-devoluciones",
            "aviso_privacidad": f"{front}/legal/aviso-de-privacidad",
            "terminos": f"{front}/legal/terminos-y-condiciones",
        },
        "empresa": {
            "razon_social": settings.EMPRESA_RAZON_SOCIAL,
            "domicilio": settings.EMPRESA_DOMICILIO,
        },
        "anio": datetime.now(ZoneInfo(zona)).year,
    }


def html_del_correo(datos: dict) -> str:
    return pintar_archivo("correo/confirmacion.hbs.html", datos)


def _mensaje(
    vista: BoletosDeLaOrden,
    comprador: Customer,
    partidas: list[OrderItem],
    cortesia: bool = False,
) -> Mensaje:
    datos = datos_del_correo(vista, comprador, partidas, cortesia)
    nombre = vista.evento.nombre
    asunto = (
        f"¡Listo! Tus boletos de cortesía para {nombre}"
        if cortesia
        else f"¡Listo! Tus boletos para {nombre}"
    )
    return Mensaje(
        # Al correo escrito en esta compra (OB1).
        para=vista.orden.email_snap or comprador.email,
        asunto=asunto,
        texto=pintar_archivo("correo/confirmacion.txt.hbs", datos, escapar=False),
        html=html_del_correo(datos),
    )
