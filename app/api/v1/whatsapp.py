"""Webhook de WhatsApp (Meta Cloud API) para el asistente de PSD.

GET: Meta lo llama una vez al conectar el webhook, con el token de
verificacion que inventamos (WHATSAPP_VERIFY_TOKEN); se le regresa su reto.

POST: cada mensaje que le escriben al numero. Se comprueba la firma con la
clave secreta de la app, se guarda lo nuevo y se contesta 200 de inmediato
(Meta reintenta si tardamos). La respuesta al cliente sale en segundo plano.

Es una ruta aparte del webhook de Stripe: no lo toca.
Con WhatsApp apagado (sin sus variables) las dos responden 404.
"""

import hmac
import json
import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import DB
from app.core.config import settings
from app.services.asistente import programar, registrar_entrantes
from app.services.whatsapp import firma_valida, leer_mensajes

log = logging.getLogger("palco.asistente")

router = APIRouter(prefix="/whatsapp", tags=["asistente"])


def get_programar() -> Callable[[list[int]], None]:
    """Dependencia para que las pruebas contesten en linea, con dobles."""
    return programar


Programar = Annotated[Callable[[list[int]], None], Depends(get_programar)]


def _encendido() -> None:
    faltan = settings.faltantes_del_asistente()
    if faltan:
        if len(faltan) < 5:
            # A medias: alguien ya empezo a configurarlo y le falta algo.
            log.warning("Asistente apagado: faltan %s.", ", ".join(faltan))
        raise HTTPException(status_code=404)


@router.get("/webhook", include_in_schema=False, response_class=PlainTextResponse)
async def verificar_webhook(
    modo: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[
        str | None, Query(alias="hub.verify_token", max_length=200)
    ] = None,
    reto: Annotated[str | None, Query(alias="hub.challenge", max_length=200)] = None,
) -> str:
    _encendido()
    if (
        modo == "subscribe"
        and token
        and reto
        and hmac.compare_digest(token, settings.WHATSAPP_VERIFY_TOKEN)
    ):
        return reto
    raise HTTPException(status_code=403)


@router.post("/webhook", include_in_schema=False)
async def recibir_webhook(request: Request, db: DB, programar_: Programar) -> dict:
    _encendido()
    # El cuerpo crudo: la firma se calcula sobre los bytes exactos.
    cuerpo = await request.body()
    if not firma_valida(
        cuerpo, request.headers.get("x-hub-signature-256"), settings.WHATSAPP_APP_SECRET
    ):
        log.warning("Webhook de WhatsApp con firma invalida: se ignora.")
        raise HTTPException(status_code=401)
    try:
        aviso = json.loads(cuerpo)
    except ValueError:
        return {"recibido": True}
    mensajes = leer_mensajes(aviso if isinstance(aviso, dict) else {})
    if mensajes:
        conversaciones = await registrar_entrantes(db, mensajes)
        if conversaciones:
            programar_(conversaciones)
    return {"recibido": True}
