"""Organizadores, recintos, mapas de butacas, eventos y funciones."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    EstadoEvento,
    EstadoFuncion,
    PKMixin,
    TimestampMixin,
    UUIDPublicoMixin,
)


class Client(Base, PKMixin, UUIDPublicoMixin, TimestampMixin):
    """Organizador. Cliente de PSD que vende boletos en la plataforma."""

    __tablename__ = "clients"

    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    razon_social: Mapped[str | None] = mapped_column(String(200))
    rfc: Mapped[str | None] = mapped_column(String(13))
    direccion: Mapped[str | None] = mapped_column(Text)
    contacto: Mapped[str | None] = mapped_column(String(160))
    telefono: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    clabe: Mapped[str | None] = mapped_column(String(18))

    # Condiciones comerciales vigentes. Cada orden congela las suyas:
    # ver orders.comision_pct_snap. Cambiar esto NO altera el historico.
    comision_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
    )
    cargo_servicio_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
    )
    cargo_orden: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("50.00")
    )

    # Medicion propia de cada organizador.
    ga_id: Mapped[str | None] = mapped_column(String(40))
    pixel_id: Mapped[str | None] = mapped_column(String(40))

    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    eventos: Mapped[list["Event"]] = relationship(back_populates="cliente")

    __table_args__ = (
        CheckConstraint(
            "comision_pct >= 0 AND comision_pct <= 100", name="comision_rango"
        ),
        CheckConstraint(
            "cargo_servicio_pct >= 0 AND cargo_servicio_pct <= 100",
            name="cargo_servicio_rango",
        ),
    )


class Venue(Base, PKMixin, TimestampMixin):
    """Recinto fisico."""

    __tablename__ = "venues"

    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    direccion: Mapped[str | None] = mapped_column(Text)
    ciudad: Mapped[str] = mapped_column(String(80), nullable=False)
    estado_geo: Mapped[str | None] = mapped_column(String(80))
    pais: Mapped[str] = mapped_column(String(2), nullable=False, default="MX")
    zona_horaria: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/Mexico_City"
    )
    capacidad: Mapped[int | None] = mapped_column(Integer)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    mapas: Mapped[list["SeatMap"]] = relationship(back_populates="recinto")

    __table_args__ = (Index("ix_venues_ciudad", "ciudad"),)


class SeatMap(Base, PKMixin, TimestampMixin):
    """Configuracion de butacas de un recinto.

    Un mismo teatro puede tener varias: completa, sin balcon, con escenario
    adelantado. Cada funcion usa la que corresponde.
    """

    __tablename__ = "seat_maps"

    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venues.id", ondelete="RESTRICT"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    # Caja de dibujo del SVG: {"width": 1000, "height": 700, "escenario": {...}}
    viewport: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    recinto: Mapped["Venue"] = relationship(back_populates="mapas")
    zonas: Mapped[list["Zone"]] = relationship(back_populates="mapa")

    __table_args__ = (UniqueConstraint("venue_id", "nombre", name="venue_id_nombre"),)


class Zone(Base, PKMixin):
    """Zona dentro de un mapa: Luneta, Palcos, Anfiteatro."""

    __tablename__ = "zones"

    seat_map_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("seat_maps.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str | None] = mapped_column(String(9))
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    mapa: Mapped["SeatMap"] = relationship(back_populates="zonas")
    asientos: Mapped[list["Seat"]] = relationship(back_populates="zona")

    __table_args__ = (
        UniqueConstraint("seat_map_id", "nombre", name="seat_map_id_nombre"),
    )


class Seat(Base, PKMixin):
    """Butaca fisica dentro de un mapa. Se define una sola vez."""

    __tablename__ = "seats"

    zone_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    fila: Mapped[str] = mapped_column(String(8), nullable=False)
    numero: Mapped[str] = mapped_column(String(8), nullable=False)
    pos_x: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    pos_y: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    # Butaca que existe fisicamente pero nunca se vende (columna enfrente,
    # sonido malo). Se materializa igual para que el mapa se dibuje completo.
    kill: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    zona: Mapped["Zone"] = relationship(back_populates="asientos")

    __table_args__ = (
        UniqueConstraint("zone_id", "fila", "numero", name="zone_id_fila_numero"),
    )

    @property
    def etiqueta(self) -> str:
        return f"Fila {self.fila} · {self.numero}"


class Event(Base, PKMixin, UUIDPublicoMixin, TimestampMixin):
    """Evento comercial: un espectaculo en una plaza."""

    __tablename__ = "events"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venues.id", ondelete="RESTRICT"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    categoria: Mapped[str | None] = mapped_column(String(60))
    descripcion: Mapped[str | None] = mapped_column(Text)
    imagen_url: Mapped[str | None] = mapped_column(String(500))
    estado: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EstadoEvento.BORRADOR.value
    )
    destacado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Excepciones comerciales para este evento. Nulo = hereda del cliente.
    cargo_servicio_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    cargo_orden: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    cliente: Mapped["Client"] = relationship(back_populates="eventos")
    funciones: Mapped[list["Performance"]] = relationship(back_populates="evento")

    __table_args__ = (
        CheckConstraint(
            "estado IN ('borrador','publicado','archivado')", name="estado_valido"
        ),
        Index("ix_events_estado_destacado", "estado", "destacado"),
    )


class Performance(Base, PKMixin, UUIDPublicoMixin, TimestampMixin):
    """Funcion: una fecha y hora concreta de un evento."""

    __tablename__ = "performances"

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    seat_map_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("seat_maps.id", ondelete="RESTRICT"), nullable=False
    )
    inicia_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    apertura_puertas: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venta_abre_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venta_cierra_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estado: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EstadoFuncion.PROGRAMADA.value
    )
    # Se pone en true cuando se crearon las filas de performance_seats.
    inventario_generado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    evento: Mapped["Event"] = relationship(back_populates="funciones")
    precios: Mapped[list["PerformancePrice"]] = relationship(back_populates="funcion")

    __table_args__ = (
        CheckConstraint(
            "estado IN ('programada','en_venta','agotada','cancelada','realizada')",
            name="estado_valido",
        ),
        Index("ix_performances_inicia_en", "inicia_en"),
    )


class PerformancePrice(Base, PKMixin):
    """Precio base por zona para una funcion.

    Es el precio de referencia. El precio efectivo de cada butaca vive en
    performance_seats.precio_final, que puede traer excepciones.
    """

    __tablename__ = "performance_prices"

    performance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("performances.id", ondelete="CASCADE"), nullable=False
    )
    zone_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="RESTRICT"), nullable=False
    )
    precio: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    funcion: Mapped["Performance"] = relationship(back_populates="precios")

    __table_args__ = (
        UniqueConstraint("performance_id", "zone_id", name="performance_id_zone_id"),
        CheckConstraint("precio >= 0", name="precio_no_negativo"),
    )
