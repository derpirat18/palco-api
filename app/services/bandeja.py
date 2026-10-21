"""Publicador de la bandeja de salida (ADR 0015).

Toma los hechos de `outbox_events` que tienen quien los atienda y los
entrega. Hoy hay uno: `orden.pagada` manda el correo con los boletos.

Reglas:

- Cada hecho se procesa en su propia transaccion, tomado con
  `FOR UPDATE SKIP LOCKED`: dos publicadores a la vez nunca toman el
  mismo, y ninguno espera al otro.
- Si la entrega falla, se anota el error y se reintenta mas tarde, cada
  vez con mas espera. Tras `MAX_INTENTOS` se deja de intentar y se avisa
  en el registro para que PSD lo atienda.
- Los tipos sin quien los atienda no se tocan: quedan para el publicador
  que los necesite (CRM, analitica).
- Entrega "al menos una vez": si el correo sale y se cae la base antes de
  marcarlo, saldra otra vez. Es preferible un correo repetido a uno
  perdido.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent
from app.services.confirmacion import armar_confirmacion
from app.services.correo import Cartero

log = logging.getLogger("palco.bandeja")

MAX_INTENTOS = 8
_ESPERA_INICIAL = timedelta(seconds=30)
_ESPERA_MAXIMA = timedelta(hours=1)


def espera_tras(intentos: int) -> timedelta:
    """30 s, 1 min, 2 min, 4 min... hasta una hora."""
    return min(_ESPERA_INICIAL * (2 ** max(intentos - 1, 0)), _ESPERA_MAXIMA)


@dataclass
class ResultadoDeLaBandeja:
    entregados: int = 0
    reintentos: int = 0
    abandonados: int = 0

    def hubo_algo(self) -> bool:
        return any((self.entregados, self.reintentos, self.abandonados))


Atender = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


class BandejaService:
    def __init__(self, sesiones, cartero: Cartero, lote: int = 50) -> None:
        self.sesiones = sesiones
        self.cartero = cartero
        self.lote = lote
        self.atiende: dict[str, Atender] = {
            "orden.pagada": self._correo_de_confirmacion,
            "cortesia.emitida": self._correo_de_cortesia,
        }

    async def _correo_de_confirmacion(
        self, db: AsyncSession, evento: OutboxEvent
    ) -> None:
        mensaje = await armar_confirmacion(db, UUID(evento.payload["orden_uuid"]))
        if mensaje is None:
            # Se reembolso o cancelo antes de que saliera: nada que mandar.
            log.info(
                "Orden %s ya no esta pagada; no se manda correo.", evento.agregado_uuid
            )
            return
        await self.cartero.enviar(mensaje)

    async def _correo_de_cortesia(self, db: AsyncSession, evento: OutboxEvent) -> None:
        mensaje = await armar_confirmacion(
            db, UUID(evento.payload["orden_uuid"]), cortesia=True
        )
        if mensaje is None:
            # Se revoco antes de que saliera: nada que mandar.
            return
        await self.cartero.enviar(mensaje)

    async def una_vuelta(self) -> ResultadoDeLaBandeja:
        r = ResultadoDeLaBandeja()
        for _ in range(self.lote):
            async with self.sesiones() as db:
                evento = await db.scalar(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.publicado_en.is_(None),
                        OutboxEvent.tipo.in_(list(self.atiende)),
                        OutboxEvent.intentos < MAX_INTENTOS,
                        or_(
                            OutboxEvent.reintentar_en.is_(None),
                            OutboxEvent.reintentar_en <= func.now(),
                        ),
                    )
                    .order_by(OutboxEvent.creado_en)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if evento is None:
                    break
                await self._procesar(db, evento, r)
                await db.commit()
        return r

    async def _procesar(
        self, db: AsyncSession, evento: OutboxEvent, r: ResultadoDeLaBandeja
    ) -> None:
        # Se leen antes: si el savepoint se revierte, el objeto queda
        # expirado y releerlo en async no es posible sin otra consulta.
        ident, tipo, agregado = evento.id, evento.tipo, evento.agregado_uuid
        intentos = evento.intentos + 1
        try:
            # En un savepoint: si quien atiende deja la sesion en error, la
            # anotacion del intento todavia se puede escribir.
            async with db.begin_nested():
                await self.atiende[tipo](db, evento)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            valores: dict = {"intentos": intentos, "ultimo_error": error}
            if intentos >= MAX_INTENTOS:
                r.abandonados += 1
                # Alerta para PSD (A4): un comprador pago y no recibio sus
                # boletos por correo. Puede verlos en "Tus boletos".
                log.error(
                    "Bandeja: %s de %s sin entregar tras %d intentos: %s",
                    tipo,
                    agregado,
                    intentos,
                    error,
                )
            else:
                r.reintentos += 1
                valores["reintentar_en"] = func.now() + espera_tras(intentos)
                # Con el motivo desde el primer intento: un correo rechazado
                # (p. ej. SES en modo de pruebas) se entiende sin esperar a
                # que se agoten los reintentos.
                log.warning(
                    "Bandeja: %s de %s fallo (intento %d), se reintenta: %s",
                    tipo,
                    agregado,
                    intentos,
                    error,
                )
            await db.execute(
                update(OutboxEvent).where(OutboxEvent.id == ident).values(**valores)
            )
            return
        await db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == ident)
            .values(publicado_en=func.now(), ultimo_error=None)
        )
        r.entregados += 1
