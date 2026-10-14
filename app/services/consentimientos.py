"""Consentimientos del comprador: una bitacora, no un interruptor.

Cada vez que alguien acepta o retira una finalidad se agrega un registro
con cuando, desde donde, para que y que version del aviso vio. Nunca se
edita ni se borra uno: el estado vigente es el mas reciente. Asi, si
alguien reclama "yo nunca acepte", hay respuesta.

Lo usan el checkout, el formulario de las pistas y, cuando exista, el
panel del comprador para retirar su consentimiento. Cuando el CRM herede
estos contactos, hereda tambien esta bitacora.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio
from app.models.sales import Consent

FINALIDADES = frozenset({"operacion", "marketing"})


class ConsentimientoInvalido(ErrorDeDominio):
    codigo = "consentimiento_invalido"
    http_status = 422


@dataclass(frozen=True)
class ConsentimientoVigente:
    finalidad: str
    otorgado: bool
    registrado_en: datetime
    origen: str
    aviso_version: str


class ConsentimientoService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def registrar(
        self,
        customer_id: int,
        finalidad: str,
        otorgado: bool,
        origen: str,
        aviso_version: str,
        order_id: int | None = None,
    ) -> Consent:
        """Agrega un registro. Una finalidad por llamada, a proposito.

        Operar la reserva y recibir promociones son consentimientos
        distintos. Pedirlos juntos, con una sola casilla, invalida el de
        marketing; por eso no hay forma de registrar dos de un jalon.
        """
        if finalidad not in FINALIDADES:
            raise ConsentimientoInvalido(
                "Finalidad desconocida.",
                {"finalidad": finalidad, "permitidas": sorted(FINALIDADES)},
            )
        if not origen.strip() or not aviso_version.strip():
            raise ConsentimientoInvalido(
                "Origen y version del aviso son obligatorios: sin ellos el "
                "registro no prueba nada."
            )
        registro = Consent(
            customer_id=customer_id,
            finalidad=finalidad,
            otorgado=otorgado,
            origen=origen.strip(),
            aviso_version=aviso_version.strip(),
            order_id=order_id,
        )
        self.db.add(registro)
        await self.db.flush()
        return registro

    async def vigentes(self, customer_id: int) -> dict[str, ConsentimientoVigente]:
        """El ultimo registro de cada finalidad. Lo que no aparece, no se dio."""
        # El mas reciente por finalidad. Con ventana y no con DISTINCT ON:
        # funciona igual en SQLAlchemy 2.0 y 2.1 sin avisos de obsolescencia.
        orden = (
            func.row_number()
            .over(
                partition_by=Consent.finalidad,
                order_by=(Consent.registrado_en.desc(), Consent.id.desc()),
            )
            .label("orden")
        )
        recientes = (
            select(Consent.id, orden)
            .where(Consent.customer_id == customer_id)
            .subquery()
        )
        filas = (
            await self.db.scalars(
                select(Consent)
                .join(recientes, recientes.c.id == Consent.id)
                .where(recientes.c.orden == 1)
            )
        ).all()
        return {
            c.finalidad: ConsentimientoVigente(
                finalidad=c.finalidad,
                otorgado=c.otorgado,
                registrado_en=c.registrado_en,
                origen=c.origen,
                aviso_version=c.aviso_version,
            )
            for c in filas
        }

    async def acepta(self, customer_id: int, finalidad: str) -> bool:
        """Si hoy se le puede usar para esa finalidad. Sin registro, no."""
        vigente = (await self.vigentes(customer_id)).get(finalidad)
        return vigente is not None and vigente.otorgado
