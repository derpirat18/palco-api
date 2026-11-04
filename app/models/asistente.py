"""Asistente de PSD por WhatsApp (bot, 3 nov).

Una conversacion por numero de WhatsApp y sus mensajes. Sirven para que el
asistente recuerde lo que se dijo, para no contestar dos veces el mismo
mensaje (Meta reintenta) y, despues, para que una persona retome la platica.

Guardan el numero y lo que la persona escribio: son datos personales. Por
ahora solo los lee la API para contestar; cualquier pantalla que los
muestre debe dejar rastro en la bitacora (`data_access_log`).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, UUIDPublicoMixin

# Conversacion: bot (contesta el asistente) | persona (alguien del equipo la
# tomo). Mensaje de entrada: pendiente -> respondido | ignorado | error; de
# salida: enviado | error.


class ConversacionDelAsistente(Base, PKMixin, UUIDPublicoMixin):
    __tablename__ = "asistente_conversaciones"

    canal: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    # Como lo manda Meta (wa_id): solo digitos con lada, p. ej. 5215512345678.
    telefono: Mapped[str] = mapped_column(String(20), nullable=False)
    # El nombre de su perfil de WhatsApp, si lo comparte.
    nombre_perfil: Mapped[str | None] = mapped_column(String(120))
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="bot")
    ultimo_mensaje_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("canal", "telefono"),
        CheckConstraint("canal IN ('whatsapp')", name="canal"),
        CheckConstraint("estado IN ('bot', 'persona')", name="estado"),
    )


class MensajeDelAsistente(Base, PKMixin):
    __tablename__ = "asistente_mensajes"

    conversacion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("asistente_conversaciones.id", ondelete="CASCADE"),
        nullable=False,
    )
    direccion: Mapped[str] = mapped_column(String(10), nullable=False)
    # El id de Meta (wamid). Unico: Meta reintenta el webhook y un mensaje
    # repetido no se contesta dos veces.
    wamid: Mapped[str | None] = mapped_column(String(160), unique=True)
    # texto | otro (audio, foto, sticker...) | ubicacion
    tipo: Mapped[str] = mapped_column(String(20), nullable=False, default="texto")
    texto: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estado: Mapped[str] = mapped_column(String(20), nullable=False)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("direccion IN ('entrada', 'salida')", name="direccion"),
        CheckConstraint(
            "estado IN ('pendiente', 'respondido', 'ignorado', 'error', 'enviado')",
            name="estado",
        ),
        Index(
            "ix_asistente_mensajes_conversacion_creado", "conversacion_id", "creado_en"
        ),
    )
