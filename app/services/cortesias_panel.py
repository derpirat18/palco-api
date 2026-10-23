"""Lo que el panel muestra de cada cortesia ademas de lo que guarda C1:
el codigo de cada boleto y si ya entro. Solo lee: dar y revocar siguen
en `CortesiaService`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoBoleto
from app.models.sales import Ticket


async def boletos_por_partida(
    db: AsyncSession, partida_ids: list[int]
) -> dict[int, tuple[str, bool]]:
    """{partida: (codigo, ya entro)} de las partidas que tienen boleto."""
    if not partida_ids:
        return {}
    filas = (
        await db.execute(
            select(Ticket.order_item_id, Ticket.codigo, Ticket.estado).where(
                Ticket.order_item_id.in_(partida_ids)
            )
        )
    ).all()
    return {
        pid: (codigo, estado == EstadoBoleto.USADO.value)
        for pid, codigo, estado in filas
    }
