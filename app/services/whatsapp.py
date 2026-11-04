"""WhatsApp Cloud API de Meta: comprobar, leer y mandar mensajes.

- `firma_valida`: Meta firma cada aviso del webhook con la clave secreta
  de la app (HMAC-SHA256 del cuerpo exacto, encabezado X-Hub-Signature-256).
  Sin firma valida no se procesa nada: cualquiera puede llamar al webhook.
- `leer_mensajes`: del aviso de Meta, solo los mensajes que nos escribieron
  (los avisos de "entregado" o "leido" se ignoran).
- `ClienteWhatsApp`: manda texto y ubicaciones, y marca como leido con el
  aviso de "escribiendo...".
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import settings

log = logging.getLogger("palco.asistente")

# Tope de Meta para un texto. Uno mas largo se parte.
MAX_TEXTO = 4096


def firma_valida(cuerpo: bytes, encabezado: str | None, secreto: str) -> bool:
    if not encabezado or not secreto or not encabezado.startswith("sha256="):
        return False
    esperada = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, encabezado.removeprefix("sha256="))


@dataclass(frozen=True)
class MensajeEntrante:
    wamid: str
    telefono: str
    nombre: str | None
    # texto | otro
    tipo: str
    texto: str


def _dicts(valor) -> list[dict]:
    """Solo los diccionarios de una lista: el aviso viene de afuera."""
    return [x for x in valor if isinstance(x, dict)] if isinstance(valor, list) else []


def _dict(valor) -> dict:
    return valor if isinstance(valor, dict) else {}


def leer_mensajes(aviso: dict) -> list[MensajeEntrante]:
    """Los mensajes de un aviso del webhook. Lo que no se entiende se salta."""
    salida: list[MensajeEntrante] = []
    for entrada in _dicts(_dict(aviso).get("entry")):
        for cambio in _dicts(entrada.get("changes")):
            valor = _dict(cambio.get("value"))
            nombres = {
                c.get("wa_id"): _dict(c.get("profile")).get("name")
                for c in _dicts(valor.get("contacts"))
            }
            for m in _dicts(valor.get("messages")):
                wamid, telefono = m.get("id"), m.get("from")
                if not wamid or not telefono or not str(telefono).isdigit():
                    continue
                tipo = m.get("type")
                if tipo == "text":
                    texto = _dict(m.get("text")).get("body")
                elif tipo == "button":
                    texto = _dict(m.get("button")).get("text")
                elif tipo == "interactive":
                    i = _dict(m.get("interactive"))
                    texto = _dict(i.get("button_reply") or i.get("list_reply")).get(
                        "title"
                    )
                else:
                    texto = None
                texto = texto if isinstance(texto, str) else ""
                salida.append(
                    MensajeEntrante(
                        wamid=str(wamid)[:160],
                        telefono=str(telefono)[:20],
                        nombre=(
                            str(nombres.get(telefono))[:120]
                            if nombres.get(telefono)
                            else None
                        ),
                        tipo="texto" if texto else "otro",
                        texto=texto,
                    )
                )
    return salida


def partir(texto: str, tope: int = MAX_TEXTO) -> list[str]:
    """Parte un texto largo por parrafos (o a la fuerza) en piezas <= tope."""
    texto = texto.strip()
    if len(texto) <= tope:
        return [texto] if texto else []
    piezas: list[str] = []
    actual = ""
    for parrafo in texto.split("\n"):
        candidato = f"{actual}\n{parrafo}" if actual else parrafo
        if len(candidato) <= tope:
            actual = candidato
            continue
        if actual:
            piezas.append(actual)
        while len(parrafo) > tope:
            piezas.append(parrafo[:tope])
            parrafo = parrafo[tope:]
        actual = parrafo
    if actual:
        piezas.append(actual)
    return piezas


class FallaDeWhatsApp(Exception):
    pass


class Mensajero(Protocol):
    async def marcar_leido(self, wamid: str) -> None: ...

    async def enviar_texto(self, telefono: str, texto: str) -> str | None: ...

    async def enviar_ubicacion(
        self, telefono: str, lat: float, lng: float, nombre: str, direccion: str
    ) -> str | None: ...


class ClienteWhatsApp:
    def __init__(
        self,
        token: str,
        numero_id: str,
        version: str = "v23.0",
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self.token, self.numero_id, self.version = token, numero_id, version
        self.cliente = cliente

    async def _post(self, cuerpo: dict) -> dict:
        url = f"https://graph.facebook.com/{self.version}/{self.numero_id}/messages"
        encabezados = {"Authorization": f"Bearer {self.token}"}
        propio = self.cliente is None
        http = self.cliente or httpx.AsyncClient(timeout=20.0)
        try:
            r = await http.post(url, headers=encabezados, json=cuerpo)
        except httpx.HTTPError as exc:
            raise FallaDeWhatsApp(
                f"Sin respuesta de Meta: {type(exc).__name__}"
            ) from exc
        finally:
            if propio:
                await http.aclose()
        if r.status_code >= 400:
            # El cuerpo de Meta dice el motivo (token vencido, numero que no
            # esta en la lista de prueba); nunca trae el token.
            raise FallaDeWhatsApp(f"Meta respondio {r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def _wamid(datos: dict) -> str | None:
        mensajes = datos.get("messages") or []
        return mensajes[0].get("id") if mensajes else None

    async def marcar_leido(self, wamid: str) -> None:
        """Las palomitas azules y el "escribiendo..." mientras piensa."""
        await self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": wamid,
                "typing_indicator": {"type": "text"},
            }
        )

    async def enviar_texto(self, telefono: str, texto: str) -> str | None:
        ultimo = None
        for pieza in partir(texto):
            ultimo = self._wamid(
                await self._post(
                    {
                        "messaging_product": "whatsapp",
                        "to": telefono,
                        "type": "text",
                        "text": {"body": pieza, "preview_url": True},
                    }
                )
            )
        return ultimo

    async def enviar_ubicacion(
        self, telefono: str, lat: float, lng: float, nombre: str, direccion: str
    ) -> str | None:
        return self._wamid(
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "to": telefono,
                    "type": "location",
                    "location": {
                        "latitude": lat,
                        "longitude": lng,
                        "name": nombre[:100],
                        "address": direccion[:200],
                    },
                }
            )
        )


def mensajero_por_configuracion() -> ClienteWhatsApp:
    return ClienteWhatsApp(
        settings.WHATSAPP_TOKEN,
        settings.WHATSAPP_NUMERO_ID,
        settings.WHATSAPP_API_VERSION,
    )
