"""Alertas para PSD por correo (A1, A4; decidido el 25 oct).

Lo que nadie ve en pantalla pero PSD tiene que saber al momento:

- **Cobro sin boletos** (`orden.requiere_reembolso`): alguien pago y no
  tiene boletos; hay que reembolsarle.
- **Correo de boletos que no salio** tras todos los reintentos de la bandeja.
- **Barrido o bandeja detenidos**: las ordenes vencidas no se cierran o los
  correos no salen.
- **Falla inesperada de la API** (un 500), con su folio para buscarla en el
  registro.

Reglas:

- Van directo por SMTP (SES), sin pasar por la bandeja: si la bandeja es lo
  que fallo, la alerta tiene que salir igual.
- La misma `clave` no se repite antes de `ESPERA`: una falla que se repite
  cada segundo no llena el correo.
- Los destinatarios se administran en el panel (solo PSD).
- La revision periodica corre dentro de la API (`vigilar`), con un candado de
  la base para que dos copias de la API no la hagan a la vez.
- Si la API misma esta caida, no hay quien avise. Para eso esta el monitor
  externo (A2), pendiente de platicar con Nadia.
- Nada de datos personales en la alerta: folios e ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.alertas import Alerta, DestinatarioDeAlertas
from app.models.outbox import OutboxEvent
from app.services.correo import Cartero, CorreoNoEnviado, Mensaje

log = logging.getLogger("palco.alertas")

ESPERA = timedelta(minutes=30)
VIGILAR_CADA_SEGUNDOS = 60
# Hechos viejos ya no se avisan: al prender esto no llega el historial entero.
VENTANA_DE_HECHOS = timedelta(days=2)
# Candado de la base (pg_try_advisory_xact_lock) para la revision periodica.
_CANDADO = 7_410_025


def activas() -> bool:
    """Fuera de local, encendidas. En local y en pruebas, apagadas."""
    if settings.ALERTAS_ACTIVAS is not None:
        return settings.ALERTAS_ACTIVAS
    return settings.ENTORNO != "local"


def _mensaje(para: str, titulo: str, detalle: str, prueba: bool) -> Mensaje:
    entorno = settings.ENTORNO
    marca = f"{settings.MARCA} ({entorno})"
    asunto = f"[{marca}] {'Prueba: ' if prueba else ''}{titulo}"
    pie = (
        "Esta es una prueba desde el panel: no pasó nada."
        if prueba
        else "Te llega porque estás en la lista de alertas del panel de PSD."
    )
    texto = f"{titulo}\n\n{detalle}\n\n{pie}\n"
    cuerpo = html.escape(detalle).replace("\n", "<br>")
    html_ = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:21px;color:#1f1d2b;max-width:560px">'
        '<p style="margin:0 0 4px;font-size:12px;color:#6b6980">'
        f"{html.escape(marca)}</p>"
        f'<h1 style="margin:0 0 12px;font-size:20px">{html.escape(titulo)}</h1>'
        f'<p style="margin:0 0 16px">{cuerpo}</p>'
        f'<p style="margin:0;font-size:12px;color:#6b6980">{html.escape(pie)}</p>'
        "</div>"
    )
    return Mensaje(para=para, asunto=asunto, texto=texto, html=html_)


async def alertar(
    db: AsyncSession,
    cartero: Cartero,
    clave: str,
    titulo: str,
    detalle: str,
    prueba: bool = False,
    una_vez: bool = False,
) -> Alerta | None:
    """Avisa a todos los destinatarios, si esta `clave` no se aviso hace poco.

    `una_vez`: para lo que es un hecho unico (un cobro sin boletos), que se
    avisa una sola vez y no cada `ESPERA`. Deja la alerta en la bitacora y
    confirma la transaccion. None: ya se habia avisado.
    """
    if not prueba:
        previas = (
            select(func.count())
            .select_from(Alerta)
            .where(Alerta.clave == clave, Alerta.prueba.is_(False))
        )
        if not una_vez:
            previas = previas.where(Alerta.creada_en > func.now() - ESPERA)
        if await db.scalar(previas):
            return None
    alerta = Alerta(
        clave=clave[:200], titulo=titulo[:200], detalle=detalle, prueba=prueba
    )
    db.add(alerta)
    await db.flush()
    correos = (
        await db.scalars(
            select(DestinatarioDeAlertas.email).order_by(DestinatarioDeAlertas.id)
        )
    ).all()
    enviadas = 0
    for para in correos:
        try:
            await cartero.enviar(_mensaje(para, titulo, detalle, prueba))
            enviadas += 1
        except CorreoNoEnviado:
            log.error("Alerta %s: no salio a un destinatario.", clave)
    alerta.enviada_a = enviadas
    if not correos:
        log.error("Alerta %s sin destinatarios: agrega alguno en el panel.", clave)
    await db.commit()
    return alerta


# --------------------------------------------------------------------------
# Revision periodica
# --------------------------------------------------------------------------


async def revisar(db: AsyncSession, cartero: Cartero, redis) -> int:
    """Una vuelta: busca lo que hay que avisar y lo avisa. Cuantas salieron."""
    # Una sola copia de la API a la vez; las demas se saltan esta vuelta.
    if not await db.scalar(text(f"SELECT pg_try_advisory_xact_lock({_CANDADO})")):
        await db.rollback()
        return 0
    # (clave, titulo, detalle, una_vez)
    pendientes: list[tuple[str, str, str, bool]] = []

    # Cobros sin boletos: cada uno se avisa una vez (su clave es el hecho).
    for hecho in (
        await db.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.tipo == "orden.requiere_reembolso",
                OutboxEvent.creado_en > func.now() - VENTANA_DE_HECHOS,
            )
            .order_by(OutboxEvent.id)
        )
    ).all():
        pendientes.append(
            (
                f"reembolso:{hecho.id}",
                f"Cobro sin boletos: folio {hecho.payload.get('folio', '?')}",
                "Alguien pagó y no tiene boletos (el apartado venció o la compra "
                "se cerró antes de cobrar). Hay que reembolsarle desde Stripe; "
                "búscalo en Ventas o en Stripe por su folio.\n"
                f"Folio: {hecho.payload.get('folio', '?')}",
                True,
            )
        )

    # Correos de la bandeja que ya no se van a reintentar.
    from app.services.bandeja import MAX_INTENTOS  # aqui: bandeja importa correo

    abandonados = (
        await db.execute(
            select(func.count(), func.max(OutboxEvent.id)).where(
                OutboxEvent.publicado_en.is_(None),
                OutboxEvent.intentos >= MAX_INTENTOS,
                OutboxEvent.creado_en > func.now() - VENTANA_DE_HECHOS,
            )
        )
    ).one()
    if abandonados[0]:
        ultimo = await db.get(OutboxEvent, abandonados[1])
        folio = (ultimo.payload or {}).get("folio")
        del_folio = f" del folio {folio}" if folio else ""
        pendientes.append(
            (
                f"bandeja:abandonado:{abandonados[1]}",
                f"{abandonados[0]} correo(s) sin entregar",
                f"La bandeja dejó de reintentar {abandonados[0]} correo(s) tras "
                f"{MAX_INTENTOS} intentos. El último: {ultimo.tipo}{del_folio}."
                f"\nError: {ultimo.ultimo_error or '—'}\n"
                "Quien compró sigue viendo sus boletos con su liga; revisa el "
                "proveedor de correo.",
                True,
            )
        )

    # Procesos de fondo detenidos.
    from app.services.latidos import estado_de_latidos

    for nombre, estado in (await estado_de_latidos(redis)).items():
        if not estado["al_dia"]:
            hace = estado["hace_segundos"]
            pendientes.append(
                (
                    f"latido:{nombre}",
                    f"El proceso «{nombre}» está detenido",
                    (
                        f"No da vuelta desde hace {hace // 60} min."
                        if hace is not None
                        else "No hay registro de que esté corriendo."
                    )
                    + (
                        " Las órdenes vencidas no se cierran y sus lugares no "
                        "se liberan."
                        if nombre == "barrido"
                        else " Los correos de boletos no están saliendo."
                    )
                    + " Revisa el servicio en ECS.",
                    False,
                )
            )
    await db.commit()  # suelta el candado

    enviadas = 0
    for clave, titulo, detalle, una_vez in pendientes:
        if await alertar(db, cartero, clave, titulo, detalle, una_vez=una_vez):
            enviadas += 1
    return enviadas


async def vigilar(sesiones, cartero: Cartero, redis) -> None:
    """Tarea de fondo de la API: revisa cada minuto hasta que se apague."""
    while True:
        await asyncio.sleep(VIGILAR_CADA_SEGUNDOS)
        try:
            async with sesiones() as db:
                await revisar(db, cartero, redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Alertas: la revision fallo")


# --------------------------------------------------------------------------
# Fallas de la API (500)
# --------------------------------------------------------------------------

_tareas: set[asyncio.Task] = set()


def avisar_falla(
    sesiones, cartero: Cartero, folio: str, metodo: str, ruta: str, exc: BaseException
) -> None:
    """Desde el atrapa-fallas: avisa en segundo plano, sin demorar la respuesta."""

    async def _avisar() -> None:
        with contextlib.suppress(Exception):
            async with sesiones() as db:
                await alertar(
                    db,
                    cartero,
                    f"error:{metodo} {ruta}:{type(exc).__name__}"[:200],
                    f"Falla inesperada en {metodo} {ruta}",
                    f"Folio: {folio}\nError: {type(exc).__name__}\n"
                    "La persona vio «El sistema no está disponible en este "
                    "momento». Busca el folio en el registro de la API "
                    "(CloudWatch) para ver la traza completa.",
                )

    tarea = asyncio.get_running_loop().create_task(_avisar())
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
