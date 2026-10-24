"""Compradores, ordenes, partidas, boletos, transferencias y accesos."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    EstadoBoleto,
    EstadoOrden,
    EstadoTransferencia,
    PKMixin,
    TimestampMixin,
    UUIDPublicoMixin,
)


class Customer(Base, PKMixin, UUIDPublicoMixin, TimestampMixin):
    """Comprador.

    Un correo, un cliente. Si compra tres veces como invitado con el mismo
    correo es la misma fila, para que al crear cuenta en la fase 2 su
    historial ya este ahi. El correo se guarda normalizado en minusculas.
    """

    __tablename__ = "customers"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    nombre: Mapped[str | None] = mapped_column(String(120))
    apellidos: Mapped[str | None] = mapped_column(String(120))
    # Siempre en E.164 (+525512345678). Se normaliza al escribir con
    # app.core.telefonos y la base lo exige con un CHECK.
    telefono: Mapped[str | None] = mapped_column(String(16))
    # Nulo mientras sea invitado. En fase 2 se llena al activar cuenta.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verificado_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cuenta_activada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # El consentimiento no vive aqui: un si/no no dice cuando, desde donde,
    # para que ni con que aviso. Ver Consent.

    ordenes: Mapped[list["Order"]] = relationship(back_populates="comprador")

    __table_args__ = (
        CheckConstraint(
            r"telefono IS NULL OR telefono ~ '^\+[1-9][0-9]{7,14}$'",
            name="telefono_e164",
        ),
    )

    @property
    def es_invitado(self) -> bool:
        return self.cuenta_activada_en is None


class Consent(Base, PKMixin):
    """Un consentimiento dado o retirado, con todo lo que lo prueba.

    Es una bitacora: solo se inserta. El estado vigente de cada finalidad
    es el registro mas reciente. Si alguien reclama, lo que preguntan es
    cuando acepto, desde donde, para que y que texto leyo; con un booleano
    no hay respuesta, y no se puede reconstruir despues.

    Cada finalidad es un registro aparte. Operar la reserva y recibir
    promociones son consentimientos distintos: juntarlos invalida el de
    marketing (ADR 0005).
    """

    __tablename__ = "consents"

    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    finalidad: Mapped[str] = mapped_column(String(20), nullable=False)
    # True: lo otorga. False: lo retira. Retirar tambien se registra.
    otorgado: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Desde donde: checkout_web, formulario_pista, taquilla, panel...
    origen: Mapped[str] = mapped_column(String(40), nullable=False)
    # Version del aviso de privacidad que se mostro, p. ej. "2026-10".
    aviso_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # Orden en la que se dio, si fue al comprar.
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL")
    )
    registrado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "finalidad IN ('operacion','marketing')", name="finalidad_valida"
        ),
        Index("ix_consents_vigente", "customer_id", "finalidad", "registrado_en"),
    )


class Order(Base, PKMixin, UUIDPublicoMixin, TimestampMixin):
    """Orden de compra. Siempre pertenece a una sola funcion."""

    __tablename__ = "orders"

    folio: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    performance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("performances.id", ondelete="RESTRICT"), nullable=False
    )
    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EstadoOrden.PENDIENTE.value
    )

    # --- Importes ---
    # Como en una factura: subtotal (sin IVA) + IVA - descuento = total, y
    # la base lo exige con `total_cuadra` (ADR 0013). El total es lo que
    # paga el comprador: el precio de sus boletos, sin cargos encima
    # (ADR 0012).
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    iva: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )
    descuento: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    moneda: Mapped[str] = mapped_column(String(3), nullable=False, default="MXN")

    # --- Condiciones congeladas al momento de la compra ---
    # Las liquidaciones suman ESTOS valores, nunca recalculan desde la
    # configuracion vigente del cliente. Si manana cambia la comision, el
    # historico no se mueve.
    comision_pct_snap: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Tasa de IVA del evento al momento de la compra.
    iva_pct_snap: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=0, server_default="0"
    )

    # --- Pago ---
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(80))
    # Evita cobrar dos veces cuando el comprador da doble clic.
    idempotency_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    pagada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Pagada y capturada no son lo mismo: los boletos se emiten antes de
    # capturar (ADR 0010). Una orden pagada sin captura es una que hay que
    # reintentar capturar, no una venta perdida.
    capturada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expira_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Apartado del que salio la orden. Nulo en ventas que no pasan por
    # apartado (taquilla, cortesias).
    hold_id: Mapped[str | None] = mapped_column(String(40))
    # Por que se cerro sin cobrarse: apartado_vencido, reemplazada,
    # pago_cancelado, captura_rechazada...
    cerrada_motivo: Mapped[str | None] = mapped_column(String(40))

    # --- Atribucion ---
    utm_source: Mapped[str | None] = mapped_column(String(120))
    utm_medium: Mapped[str | None] = mapped_column(String(120))
    utm_campaign: Mapped[str | None] = mapped_column(String(160))
    utm_content: Mapped[str | None] = mapped_column(String(160))
    canal: Mapped[str | None] = mapped_column(String(40))
    # A nombre de quien se hizo esta compra, tal como se escribio. El
    # comprador (un registro por correo) no se cambia si ya existia: asi un
    # correo ajeno no le cambia los datos a su dueno, pero la puerta y el
    # correo de esta compra si llevan el nombre que se escribio aqui.
    # Nulo en ordenes de antes de existir la columna.
    nombre_snap: Mapped[str | None] = mapped_column(String(120))
    apellidos_snap: Mapped[str | None] = mapped_column(String(120))
    # El contacto de esta compra, tal como se escribio (OB1). El registro del
    # comprador es uno por correo para toda la plataforma: su telefono es el
    # de su primera compra, quiza con otro organizador. Ventas, el Excel y el
    # correo de los boletos usan estos, nunca los del registro. En una
    # cortesia el telefono es opcional.
    email_snap: Mapped[str | None] = mapped_column(String(255))
    telefono_snap: Mapped[str | None] = mapped_column(String(16))

    comprador: Mapped["Customer"] = relationship(back_populates="ordenes")
    partidas: Mapped[list["OrderItem"]] = relationship(back_populates="orden")

    __table_args__ = (
        CheckConstraint(
            "estado IN ('pendiente','pagada','cancelada','reembolsada','expirada')",
            name="estado_valido",
        ),
        CheckConstraint("total >= 0", name="total_no_negativo"),
        CheckConstraint("total = subtotal + iva - descuento", name="total_cuadra"),
        CheckConstraint("iva >= 0 AND iva <= total", name="iva_valido"),
        CheckConstraint(
            r"telefono_snap IS NULL OR telefono_snap ~ '^\+[1-9][0-9]{7,14}$'",
            name="telefono_snap_e164",
        ),
        Index("ix_orders_performance_estado", "performance_id", "estado"),
        Index("ix_orders_customer", "customer_id"),
        # Un apartado, a lo mas una orden esperando pago. Dos ordenes vivas
        # sobre el mismo apartado son dos cobros posibles por las mismas
        # butacas.
        Index(
            "uq_orders_hold_pendiente",
            "hold_id",
            unique=True,
            postgresql_where=text("estado = 'pendiente'"),
        ),
        # El webhook llega con el PaymentIntent, no con la orden.
        Index(
            "uq_orders_payment_intent",
            "stripe_payment_intent_id",
            unique=True,
            postgresql_where=text("stripe_payment_intent_id IS NOT NULL"),
        ),
    )


class OrderItem(Base, PKMixin):
    """Una butaca dentro de una orden.

    Congela la descripcion de zona y asiento: si manana renombran la zona,
    el boleto y la factura siguen diciendo lo que decian al comprar.
    """

    __tablename__ = "order_items"

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    performance_seat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("performance_seats.id", ondelete="RESTRICT"),
        nullable=False,
    )

    zona_snap: Mapped[str] = mapped_column(String(80), nullable=False)
    asiento_snap: Mapped[str] = mapped_column(String(40), nullable=False)

    precio_unitario: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # IVA contenido en el importe (ADR 0013).
    iva: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
    )
    descuento: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=0
    )
    importe: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Se pone en true al cancelar o reembolsar. Es lo que libera la butaca
    # para el indice unico parcial de abajo.
    anulado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    orden: Mapped["Order"] = relationship(back_populates="partidas")
    boleto: Mapped["Ticket"] = relationship(back_populates="partida", uselist=False)

    __table_args__ = (
        CheckConstraint(
            "importe = precio_unitario - descuento", name="importe_es_el_precio"
        ),
        CheckConstraint("iva >= 0 AND iva <= importe", name="iva_valido"),
        # Capa 3 de la garantia: ni un bug de aplicacion puede producir dos
        # partidas vivas sobre la misma butaca. El indice parcial permite
        # revender una butaca cuya orden se cancelo.
        Index(
            "uq_order_items_asiento_vivo",
            "performance_seat_id",
            unique=True,
            postgresql_where=(~anulado),
        ),
        Index("ix_order_items_order", "order_id"),
    )


class Ticket(Base, PKMixin, UUIDPublicoMixin):
    """Boleto emitido. Uno por partida.

    Esta separado de la partida para que pueda transferirse sin tocar la
    orden original, que sigue perteneciendo a quien pago.
    """

    __tablename__ = "tickets"

    order_item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order_items.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    # Portador actual. Cambia al aceptarse una transferencia.
    holder_customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    # Referencia publica legible. Va impresa bajo el QR para busqueda
    # manual cuando el escaner no lee.
    codigo: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    # Semilla del QR rotatorio. Se genera desde el dia uno aunque la
    # rotacion se encienda despues: agregarla luego obligaria a migrar
    # boletos ya emitidos.
    semilla: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EstadoBoleto.VALIDO.value
    )
    emitido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    partida: Mapped["OrderItem"] = relationship(back_populates="boleto")
    escaneos: Mapped[list["Scan"]] = relationship(back_populates="boleto")

    __table_args__ = (
        CheckConstraint(
            "estado IN ('valido','transferido','usado','cancelado')",
            name="estado_valido",
        ),
        Index("ix_tickets_holder", "holder_customer_id"),
    )


class Transfer(Base, PKMixin):
    """Transferencia de un boleto a otra persona."""

    __tablename__ = "transfers"

    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    de_customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    para_email: Mapped[str] = mapped_column(String(255), nullable=False)
    a_customer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT")
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EstadoTransferencia.PENDIENTE.value
    )
    expira_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    aceptada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "estado IN ('pendiente','aceptada','cancelada','expirada')",
            name="estado_valido",
        ),
        Index("ix_transfers_ticket", "ticket_id"),
    )


class AccessDevice(Base, PKMixin, TimestampMixin):
    """Dispositivo autorizado para escanear en puerta."""

    __tablename__ = "access_devices"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Puerta o zona asignada. Separar accesos por zona es lo que evita el
    # doble ingreso cuando dos dispositivos operan sin conexion.
    puerta: Mapped[str | None] = mapped_column(String(60))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ultima_sync_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Scan(Base, PKMixin):
    """Lectura de un boleto en puerta.

    `client_scan_id` lo genera el dispositivo localmente. Es lo que hace
    idempotente la sincronizacion: puede subir el mismo lote diez veces
    sin duplicar registros.
    """

    __tablename__ = "scans"

    ticket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="SET NULL")
    )
    performance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("performances.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("access_devices.id", ondelete="SET NULL")
    )
    # Quien escaneo, cuando es desde el panel en linea (E3a): "ya entro a
    # las 7:42 pm con Lupita".
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    codigo_leido: Mapped[str | None] = mapped_column(String(80))
    resultado: Mapped[str] = mapped_column(String(16), nullable=False)
    escaneado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    client_scan_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sincronizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    nota: Mapped[str | None] = mapped_column(Text)

    boleto: Mapped["Ticket"] = relationship(back_populates="escaneos")

    __table_args__ = (
        # deshecho: alguien del personal regreso a valido un boleto que se
        # escaneo por error (E3d). La fila queda como constancia de quien.
        CheckConstraint(
            "resultado IN ('valido','duplicado','otra_funcion','invalido',"
            "'cancelado','deshecho')",
            name="resultado_valido",
        ),
        Index("ix_scans_performance_escaneado", "performance_id", "escaneado_en"),
        Index("ix_scans_ticket", "ticket_id"),
    )


class Cortesia(Base, PKMixin):
    """Por que y quien otorgo una cortesia (C1).

    La cortesia en si es una orden normal con total $0 y `canal =
    'cortesia'`: sus butacas pasan por las mismas tres capas que una venta
    y sus boletos son boletos de verdad, con QR. Aqui queda lo que la orden
    no dice: el motivo, quien la dio y, si se revoco, quien y cuando.
    """

    __tablename__ = "cortesias"

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    motivo: Mapped[str] = mapped_column(String(120), nullable=False)
    otorgada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    otorgada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revocada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    revocada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
