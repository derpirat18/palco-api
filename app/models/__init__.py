"""Registro de modelos.

Alembic importa este modulo para descubrir el metadata completo. Todo
modelo nuevo debe quedar listado aqui o sus migraciones no se generan.
"""

from app.models.acceso import AsignacionDeAcceso, ConsultaDeDatos
from app.models.alertas import Alerta, DestinatarioDeAlertas
from app.models.base import Base
from app.models.catalog import (
    Client,
    DireccionAnterior,
    Event,
    Performance,
    PerformancePrice,
    Seat,
    SeatMap,
    Venue,
    Zone,
)
from app.models.catalogos import CategoriaDeEvento, Ciudad
from app.models.commerce import (
    DiscountCode,
    DiscountRedemption,
    Settlement,
    SettlementItem,
)
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.payments import PaymentEvent
from app.models.sales import (
    AccessDevice,
    Consent,
    Cortesia,
    Customer,
    Order,
    OrderItem,
    Scan,
    Ticket,
    Transfer,
)
from app.models.taquilla import CajaDeTaquilla, PersonalDeTaquilla, VentaDeTaquilla
from app.models.users import User

__all__ = [
    "AccessDevice",
    "Alerta",
    "AsignacionDeAcceso",
    "Base",
    "CajaDeTaquilla",
    "CategoriaDeEvento",
    "Ciudad",
    "Client",
    "Consent",
    "ConsultaDeDatos",
    "Cortesia",
    "Customer",
    "DestinatarioDeAlertas",
    "DireccionAnterior",
    "DiscountCode",
    "DiscountRedemption",
    "Event",
    "InventoryMove",
    "Order",
    "OrderItem",
    "OutboxEvent",
    "PaymentEvent",
    "Performance",
    "PerformancePrice",
    "PerformanceSeat",
    "PersonalDeTaquilla",
    "Scan",
    "Seat",
    "SeatMap",
    "Settlement",
    "SettlementItem",
    "Ticket",
    "Transfer",
    "User",
    "VentaDeTaquilla",
    "Venue",
    "Zone",
]
