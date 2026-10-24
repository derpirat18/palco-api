"""Correo de confirmacion de compra, con un QR por lugar (ADR 0015).

Se arma desde la base en el momento de enviarlo, no desde el `payload`
de la bandeja: el payload no lleva datos personales a proposito, y asi
el correo refleja el estado real (si la orden se reembolso antes de que
saliera el correo, no se manda).
"""

import io
from datetime import datetime
from decimal import Decimal
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

import segno
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.base import EstadoOrden
from app.models.sales import Customer
from app.services.boletos import BoletosDeLaOrden, boletos_de_la_orden
from app.services.correo import Imagen, Mensaje

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]  # fmt: skip


def fecha_larga(momento: datetime, zona: str) -> str:
    """'viernes 20 de noviembre de 2026, 7:00 p. m.', a la hora del recinto.

    A mano y no con `locale`: el locale depende del sistema donde corre
    (Windows, contenedor) y un servidor sin es_MX escribiria en ingles.
    """
    local = momento.astimezone(ZoneInfo(zona))
    hora = local.hour % 12 or 12
    sufijo = "a. m." if local.hour < 12 else "p. m."
    return (
        f"{_DIAS[local.weekday()]} {local.day} de {_MESES[local.month - 1]} "
        f"de {local.year}, {hora}:{local.minute:02d} {sufijo}"
    )


def pesos(monto: Decimal) -> str:
    return f"${monto:,.2f}"


def tasa(pct: Decimal) -> str:
    """16.00 -> '16 %'; 8.50 -> '8.5 %'."""
    texto = format(pct.normalize(), "f")
    return f"{texto} %"


def png_del_qr(texto: str) -> bytes:
    buf = io.BytesIO()
    # Correccion media: aguanta una pantalla rayada o un poco de brillo.
    segno.make(texto, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


def enlace_a_boletos(orden_uuid: UUID) -> str:
    return f"{settings.FRONT_URL.rstrip('/')}/boletos/{orden_uuid}"


async def armar_confirmacion(
    db: AsyncSession, orden_uuid: UUID, cortesia: bool = False
) -> Mensaje | None:
    """El correo listo para enviar, o None si ya no corresponde mandarlo.

    Una cortesia lleva otro texto: no es "tu compra" y no hay resumen de
    pago que mostrar.
    """
    vista = await boletos_de_la_orden(db, orden_uuid)
    if vista.orden.estado != EstadoOrden.PAGADA.value:
        return None
    validos = [b for b in vista.boletos if b.qr]
    if not validos:
        return None
    comprador = await db.get(Customer, vista.orden.customer_id)
    return _mensaje(vista, comprador, validos, cortesia)


def _mensaje(
    vista: BoletosDeLaOrden, comprador: Customer, validos, cortesia: bool = False
) -> Mensaje:
    orden, evento, funcion, recinto = (
        vista.orden,
        vista.evento,
        vista.funcion,
        vista.recinto,
    )
    cuando = fecha_larga(funcion.inicia_en, recinto.zona_horaria)
    puertas = (
        fecha_larga(funcion.apertura_puertas, recinto.zona_horaria)
        if funcion.apertura_puertas
        else None
    )
    partes = [recinto.nombre, recinto.direccion, recinto.ciudad]
    donde = ", ".join(p for p in partes if p)
    enlace = enlace_a_boletos(orden.uuid)
    # El nombre que se escribio en esta compra; el del registro, si es una
    # orden de antes de guardarlo.
    nombre = orden.nombre_snap or comprador.nombre
    saludo = f"Hola, {nombre}" if nombre else "Hola"
    imagenes = [
        Imagen(cid=f"qr-{b.codigo}", png=png_del_qr(b.qr), texto=b.qr) for b in validos
    ]

    renglones_pago = [("Subtotal", pesos(orden.subtotal))]
    renglones_pago.append((f"IVA ({tasa(orden.iva_pct_snap)})", pesos(orden.iva)))
    if orden.descuento > 0:
        renglones_pago.append(("Descuento", f"-{pesos(orden.descuento)}"))
    renglones_pago.append(("Total", pesos(orden.total)))

    if cortesia:
        asunto = f"Tus boletos de cortesía para {evento.nombre} · {orden.folio}"
        entrada = "Te enviamos boletos de cortesía. Estos son tus boletos."
    else:
        asunto = f"Tus boletos para {evento.nombre} · {orden.folio}"
        entrada = "Tu compra está confirmada. Estos son tus boletos."

    # --- Texto plano: para quien no ve HTML y para los filtros de spam.
    lineas = [
        f"{saludo}:",
        "",
        entrada,
        "",
        evento.nombre,
        f"Función: {cuando}",
    ]
    if puertas:
        lineas.append(f"Apertura de puertas: {puertas}")
    lineas += [f"Lugar: {donde}", f"Folio: {orden.folio}", "", "Boletos:"]
    lineas += [f"- {b.zona} · {b.asiento} · código {b.codigo}" for b in validos]
    if not cortesia:
        lineas += ["", "Resumen de pago:"]
        lineas += [f"  {k}: {v}" for k, v in renglones_pago]
    lineas += [
        "",
        f"Ver tus boletos con su QR: {enlace}",
        "",
        "Presenta cada QR en la entrada, desde tu celular o impreso. Cada "
        "boleto entra una sola vez: no compartas tus QR.",
    ]
    texto = "\n".join(lineas)

    # --- HTML: tablas y estilos en linea, lo unico que respetan todos los
    # clientes de correo.
    def fila_pago(k: str, v: str, fuerte: bool = False) -> str:
        peso = "font-weight:bold;" if fuerte else ""
        return (
            f'<tr><td style="padding:4px 0;{peso}">{escape(k)}</td>'
            f'<td style="padding:4px 0;text-align:right;{peso}">{escape(v)}</td></tr>'
        )

    boletos_html = "".join(
        '<tr><td style="padding:16px 0;border-top:1px solid #e5e7eb;'
        'text-align:center">'
        f'<div style="font-size:16px;font-weight:bold">{escape(b.zona)} · '
        f"{escape(b.asiento)}</div>"
        f'<img src="cid:{img.cid}" width="220" height="220" alt="QR del boleto '
        f'{escape(b.codigo)}" style="display:block;margin:8px auto">'
        f'<div style="font-family:monospace;font-size:16px;letter-spacing:2px">'
        f"{escape(b.codigo)}</div></td></tr>"
        for b, img in zip(validos, imagenes, strict=True)
    )
    pago_html = "".join(
        fila_pago(k, v, fuerte=(k == "Total")) for k, v in renglones_pago
    )
    resumen_html = (
        ""
        if cortesia
        else '<h2 style="font-size:16px;margin:16px 0 8px">Resumen de pago</h2>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"{pago_html}</table>"
    )
    puertas_html = (
        f"<p style='margin:4px 0'>Apertura de puertas: {escape(puertas)}</p>"
        if puertas
        else ""
    )
    html = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{escape(asunto)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:8px">
<tr><td style="padding:24px">
<p style="margin:0 0 8px">{escape(saludo)}:</p>
<p style="margin:0 0 16px">{escape(entrada)}</p>
<h1 style="font-size:22px;margin:0 0 8px">{escape(evento.nombre)}</h1>
<p style="margin:4px 0">Función: {escape(cuando)}</p>
{puertas_html}
<p style="margin:4px 0">Lugar: {escape(donde)}</p>
<p style="margin:4px 0 16px">Folio: <strong>{escape(orden.folio)}</strong></p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{boletos_html}</table>
<p style="margin:16px 0;padding:12px;background:#f9fafb;border-radius:6px">Presenta cada QR en la entrada, desde tu celular o impreso. Cada boleto entra una sola vez: no compartas tus QR.</p>
{resumen_html}
<p style="margin:24px 0 0;text-align:center"><a href="{escape(enlace)}" style="display:inline-block;padding:12px 20px;background:#111827;color:#ffffff;text-decoration:none;border-radius:6px">Ver mis boletos</a></p>
</td></tr></table>
</td></tr></table>
</body></html>"""  # noqa: E501

    return Mensaje(
        # Al correo escrito en esta compra (OB1).
        para=orden.email_snap or comprador.email,
        asunto=asunto,
        texto=texto,
        html=html,
        imagenes=imagenes,
    )
