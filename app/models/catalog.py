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
    false,
    func,
    true,
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

    # Lo que PSD le descuenta al organizador sobre el precio de sus
    # boletos. Al comprador no se le cobra nada encima (ADR 0012). Cada
    # orden congela la suya: ver orders.comision_pct_snap. Cambiar esto NO
    # altera el historico.
    comision_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
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
    # Entrada general (EG): sin lugar numerado. Sus "butacas" son cupos
    # (fila G, numero 1..N) que el sistema asigna; el comprador escoge
    # cuantos, no cuales, y su boleto dice "Acceso general".
    general: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

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

    # IVA del evento, lo decide el organizador con su contador (ADR 0013).
    # La tasa la escribe el: 16, 8, 0 si esta exento... El comprador ve
    # siempre el precio con IVA; `precios_incluyen_iva` dice si los precios
    # capturados ya lo traen o si el sistema se los suma.
    iva_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("16.00"), server_default="16.00"
    )
    precios_incluyen_iva: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    # --- Taquilla (TQ) ---
    # Hasta cuantos minutos despues de empezar cada funcion vende la taquilla.
    taquilla_minutos_despues: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    # El texto corto del medio fisico (pulsera, gafete), editable; sale
    # tambien en el recibo impreso.
    texto_legal_medio: Mapped[str | None] = mapped_column(Text)
    # Quien paga el medio fisico: entra en la liquidacion.
    medio_lo_paga: Mapped[str | None] = mapped_column(String(12))

    # --- Tipos de boleto (TB) ---
    # Edades que el organizador anuncia para el precio de nino y el de
    # adulto mayor. Son informativas: en la puerta se revisan a ojo.
    edad_nino_hasta: Mapped[int | None] = mapped_column(Integer)
    edad_mayor_desde: Mapped[int | None] = mapped_column(Integer)

    cliente: Mapped["Client"] = relationship(back_populates="eventos")
    funciones: Mapped[list["Performance"]] = relationship(back_populates="evento")

    __table_args__ = (
        CheckConstraint(
            "estado IN ('borrador','publicado','archivado')", name="estado_valido"
        ),
        CheckConstraint("iva_pct >= 0 AND iva_pct <= 100", name="iva_rango"),
        CheckConstraint(
            "taquilla_minutos_despues BETWEEN 0 AND 600", name="taquilla_minutos_rango"
        ),
        CheckConstraint(
            "medio_lo_paga IS NULL OR medio_lo_paga IN ('psd','organizador')",
            name="medio_lo_paga_valido",
        ),
        CheckConstraint(
            "edad_nino_hasta IS NULL OR edad_nino_hasta BETWEEN 1 AND 17",
            name="edad_nino_rango",
        ),
        CheckConstraint(
            "edad_mayor_desde IS NULL OR edad_mayor_desde BETWEEN 50 AND 99",
            name="edad_mayor_rango",
        ),
        Index("ix_events_estado_destacado", "estado", "destacado"),
    )


class DireccionAnterior(Base, PKMixin):
    """Una direccion que tuvo un evento y ya no tiene (URL1).

    Las ligas a un evento circulan en anuncios, mensajes y carteles. Si su
    direccion cambia, la vieja sigue llevando al evento: la pagina publica
    la busca aqui y redirige. Ningun otro evento puede tomar una direccion
    que esta en esta tabla, o se llevaria las visitas ajenas.
    """

    __tablename__ = "event_slug_history"

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    cambiado_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    cambiado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_event_slug_history_event", "event_id"),)


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
    # Tipos de boleto (TB). `precio` es el de adulto. Sin precio, ese tipo
    # no se vende en la zona. Se capturan igual que `precio` (con o sin IVA).
    precio_nino: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    precio_mayor: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    funcion: Mapped["Performance"] = relationship(back_populates="precios")

    __table_args__ = (
        UniqueConstraint("performance_id", "zone_id", name="performance_id_zone_id"),
        CheckConstraint("precio >= 0", name="precio_no_negativo"),
        CheckConstraint(
            "precio_nino IS NULL OR precio_nino >= 0", name="precio_nino_no_negativo"
        ),
        CheckConstraint(
            "precio_mayor IS NULL OR precio_mayor >= 0",
            name="precio_mayor_no_negativo",
        ),
    )
