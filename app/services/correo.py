"""Envio de correo (ADR 0015).

Igual que con Stripe, el resto del sistema habla con un `Cartero` y no
sabe quien entrega. En local entrega Mailpit; en produccion, Amazon SES.
Los dos hablan SMTP, asi que hoy basta una implementacion.

Ningun correo sale desde una peticion del comprador: los manda la
bandeja de salida (app/services/bandeja.py), con reintentos. Si el
proveedor se cae, el cobro y los boletos no se enteran.
"""

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol

from app.core.config import settings

log = logging.getLogger("palco.correo")


@dataclass(frozen=True)
class Imagen:
    """Imagen incrustada en el HTML, referida como `cid:<cid>`."""

    cid: str
    png: bytes
    # Lo que dice la imagen (el texto del QR). No viaja en el correo; sirve
    # para las pruebas y para la bitacora.
    texto: str = ""


@dataclass(frozen=True)
class Mensaje:
    para: str
    asunto: str
    texto: str
    html: str
    imagenes: list[Imagen] = field(default_factory=list)


class CorreoNoEnviado(Exception):
    """El proveedor no acepto el correo. La bandeja reintenta."""


class Cartero(Protocol):
    async def enviar(self, mensaje: Mensaje) -> None: ...


def armar_mime(mensaje: Mensaje, remitente: str) -> EmailMessage:
    """Texto plano + HTML, con las imagenes pegadas al HTML.

    Imagenes por `cid:` y no como `data:` en el HTML: Gmail y Outlook
    bloquean las `data:`, y un enlace a un servidor nuestro haria que el
    QR no se vea sin conexion, justo en la fila de la puerta.
    """
    correo = EmailMessage()
    correo["From"] = remitente
    correo["To"] = mensaje.para
    correo["Subject"] = mensaje.asunto
    correo["Date"] = formatdate(localtime=False)
    dominio = remitente.rsplit("@", 1)[-1].rstrip(">").strip() or "palco.local"
    correo["Message-ID"] = make_msgid(domain=dominio)

    correo.set_content(mensaje.texto)
    correo.add_alternative(mensaje.html, subtype="html")
    parte_html = correo.get_payload()[1]
    for img in mensaje.imagenes:
        parte_html.add_related(
            img.png,
            maintype="image",
            subtype="png",
            cid=f"<{img.cid}>",
            filename=f"{img.cid}.png",
        )
    return correo


class CarteroSMTP:
    def __init__(
        self,
        host: str,
        puerto: int,
        usuario: str = "",
        password: str = "",
        starttls: bool = False,
        remitente: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.puerto = puerto
        self.usuario = usuario
        self.password = password
        self.starttls = starttls
        self.remitente = remitente
        self.timeout = timeout

    def _enviar(self, correo: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.puerto, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.usuario:
                smtp.login(self.usuario, self.password)
            smtp.send_message(correo)

    async def enviar(self, mensaje: Mensaje) -> None:
        correo = armar_mime(mensaje, self.remitente)
        try:
            # smtplib bloquea; en un hilo para no detener a nadie mas.
            await asyncio.to_thread(self._enviar, correo)
        except (smtplib.SMTPException, OSError) as exc:
            # Nunca se anota el destinatario: el registro no guarda datos
            # personales.
            log.warning("El proveedor de correo no acepto el envio: %r", exc)
            raise CorreoNoEnviado(str(exc)) from exc


def cartero_por_configuracion() -> CarteroSMTP:
    return CarteroSMTP(
        host=settings.CORREO_SMTP_HOST,
        puerto=settings.CORREO_SMTP_PUERTO,
        usuario=settings.CORREO_SMTP_USUARIO,
        password=settings.CORREO_SMTP_PASSWORD,
        starttls=settings.CORREO_SMTP_STARTTLS,
        remitente=settings.CORREO_REMITENTE,
    )
