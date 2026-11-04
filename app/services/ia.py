"""La IA que piensa por el asistente, de la empresa que se escoja.

Un solo "enchufe" (`ProveedorIA`) y dos conectores:

- `Anthropic`: la API de Claude.
- `CompatibleConOpenAI`: la forma de OpenAI (/chat/completions). La hablan
  OpenAI, Gemini, DeepSeek, Mistral, Groq, xAI y muchas mas; solo cambia la
  direccion. Para una que no este en `DIRECCIONES`: IA_PROVEEDOR=otro e
  IA_URL.

Cambiar de empresa es cambiar IA_PROVEEDOR, IA_MODELO e IA_LLAVE. Una
empresa que no hable como ninguna de las dos se agrega con otro conector
que cumpla `ProveedorIA`, sin tocar el asistente.

El asistente habla con estas clases en un formato propio (`Turno`,
`Llamada`, `Herramienta`); cada conector lo traduce al de su empresa.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from app.core.config import settings

log = logging.getLogger("palco.asistente")

MODELO_ANTHROPIC_DE_OMISION = "claude-sonnet-5-5"

DIRECCIONES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
}

# Respuestas cortas: es WhatsApp. Tambien acota el gasto por mensaje.
MAX_TOKENS = 700
# Con OpenAI el tope incluye lo que el modelo "piensa" (GPT-5 y similares).
MAX_TOKENS_OPENAI = 2500
ESPERA_SEGUNDOS = 45.0
# Saturada, limitada o caida un momento: se reintenta una vez.
_REINTENTABLES = {408, 409, 429, 500, 502, 503, 504, 529}


class FallaDeIA(Exception):
    """La IA no contesto (caida, llave mala, respuesta rara)."""


@dataclass(frozen=True)
class Herramienta:
    nombre: str
    descripcion: str
    # JSON Schema de los argumentos.
    parametros: dict


@dataclass(frozen=True)
class Llamada:
    """La IA pide usar una herramienta."""

    id: str
    nombre: str
    argumentos: dict


@dataclass(frozen=True)
class Resultado:
    llamada_id: str
    nombre: str
    contenido: str


@dataclass
class Turno:
    """Un paso de la platica.

    usuario: lo que escribio la persona. asistente: lo que contesto la IA
    (texto y/o llamadas). herramientas: lo que regresaron las herramientas.
    """

    rol: Literal["usuario", "asistente", "herramientas"]
    texto: str = ""
    llamadas: list[Llamada] = field(default_factory=list)
    resultados: list[Resultado] = field(default_factory=list)


@dataclass(frozen=True)
class Respuesta:
    texto: str
    llamadas: list[Llamada]


class ProveedorIA(Protocol):
    async def responder(
        self, sistema: str, turnos: list[Turno], herramientas: list[Herramienta]
    ) -> Respuesta: ...


async def _post(
    cliente: httpx.AsyncClient | None, url: str, encabezados: dict, cuerpo: dict
) -> dict:
    """POST con un reintento si la empresa esta saturada o caida un momento."""
    propio = cliente is None
    http = cliente or httpx.AsyncClient(timeout=ESPERA_SEGUNDOS)
    try:
        for intento in (1, 2):
            try:
                r = await http.post(url, headers=encabezados, json=cuerpo)
            except httpx.HTTPError as exc:
                if intento == 2:
                    raise FallaDeIA(
                        f"Sin respuesta de la IA: {type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(1.5)
                continue
            if r.status_code in _REINTENTABLES and intento == 1:
                await asyncio.sleep(1.5)
                continue
            if r.status_code >= 400:
                # El cuerpo puede decir por que (llave mala, modelo que no
                # existe); nunca trae la llave.
                raise FallaDeIA(f"La IA respondio {r.status_code}: {r.text[:300]}")
            return r.json()
        raise FallaDeIA("La IA no respondio")  # pragma: no cover
    finally:
        if propio:
            await http.aclose()


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class Anthropic:
    def __init__(
        self,
        llave: str,
        modelo: str = MODELO_ANTHROPIC_DE_OMISION,
        url: str = "https://api.anthropic.com/v1",
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self.llave, self.modelo, self.url, self.cliente = llave, modelo, url, cliente

    @staticmethod
    def mensajes(turnos: list[Turno]) -> list[dict]:
        """A su formato. Dos turnos seguidos del mismo lado se juntan."""
        salida: list[dict] = []
        for t in turnos:
            if t.rol == "usuario":
                rol, bloques = "user", [{"type": "text", "text": t.texto}]
            elif t.rol == "asistente":
                rol = "assistant"
                bloques = [{"type": "text", "text": t.texto}] if t.texto else []
                bloques += [
                    {
                        "type": "tool_use",
                        "id": c.id,
                        "name": c.nombre,
                        "input": c.argumentos,
                    }
                    for c in t.llamadas
                ]
            else:
                rol = "user"
                bloques = [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.llamada_id,
                        "content": r.contenido,
                    }
                    for r in t.resultados
                ]
            if not bloques:
                continue
            if salida and salida[-1]["role"] == rol:
                salida[-1]["content"].extend(bloques)
            else:
                salida.append({"role": rol, "content": bloques})
        return salida

    async def responder(
        self, sistema: str, turnos: list[Turno], herramientas: list[Herramienta]
    ) -> Respuesta:
        cuerpo = {
            "model": self.modelo,
            "max_tokens": MAX_TOKENS,
            "system": sistema,
            "messages": self.mensajes(turnos),
            "tools": [
                {
                    "name": h.nombre,
                    "description": h.descripcion,
                    "input_schema": h.parametros,
                }
                for h in herramientas
            ],
        }
        datos = await _post(
            self.cliente,
            f"{self.url.rstrip('/')}/messages",
            {
                "x-api-key": self.llave,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            cuerpo,
        )
        textos, llamadas = [], []
        for b in datos.get("content") or []:
            if b.get("type") == "text":
                textos.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                llamadas.append(Llamada(b["id"], b["name"], b.get("input") or {}))
        return Respuesta("\n".join(t for t in textos if t).strip(), llamadas)


# --------------------------------------------------------------------------
# Compatible con OpenAI (OpenAI, Gemini, DeepSeek, Mistral, Groq, xAI...)
# --------------------------------------------------------------------------


class CompatibleConOpenAI:
    """`campo_tokens`: OpenAI ya no acepta `max_tokens` en sus modelos nuevos
    (GPT-5, o3...) y pide `max_completion_tokens`; las demas empresas siguen
    con `max_tokens`. En los modelos que "razonan", ese tope incluye lo que
    piensan antes de contestar: por eso con OpenAI es mas holgado."""

    def __init__(
        self,
        llave: str,
        modelo: str,
        url: str,
        cliente: httpx.AsyncClient | None = None,
        campo_tokens: str = "max_tokens",
        tokens: int = MAX_TOKENS,
    ) -> None:
        self.llave, self.modelo, self.url, self.cliente = llave, modelo, url, cliente
        self.campo_tokens, self.tokens = campo_tokens, tokens

    @staticmethod
    def mensajes(sistema: str, turnos: list[Turno]) -> list[dict]:
        salida: list[dict] = [{"role": "system", "content": sistema}]
        for t in turnos:
            if t.rol == "usuario":
                salida.append({"role": "user", "content": t.texto})
            elif t.rol == "asistente":
                m: dict = {"role": "assistant", "content": t.texto or None}
                if t.llamadas:
                    m["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.nombre,
                                "arguments": json.dumps(
                                    c.argumentos, ensure_ascii=False
                                ),
                            },
                        }
                        for c in t.llamadas
                    ]
                salida.append(m)
            else:
                salida.extend(
                    {
                        "role": "tool",
                        "tool_call_id": r.llamada_id,
                        "content": r.contenido,
                    }
                    for r in t.resultados
                )
        return salida

    async def responder(
        self, sistema: str, turnos: list[Turno], herramientas: list[Herramienta]
    ) -> Respuesta:
        cuerpo = {
            "model": self.modelo,
            self.campo_tokens: self.tokens,
            "messages": self.mensajes(sistema, turnos),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": h.nombre,
                        "description": h.descripcion,
                        "parameters": h.parametros,
                    },
                }
                for h in herramientas
            ],
        }
        datos = await _post(
            self.cliente,
            f"{self.url.rstrip('/')}/chat/completions",
            {
                "Authorization": f"Bearer {self.llave}",
                "content-type": "application/json",
            },
            cuerpo,
        )
        try:
            mensaje = datos["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FallaDeIA("La IA respondio sin mensaje") from exc
        llamadas = []
        for c in mensaje.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                argumentos = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                argumentos = {}
            llamadas.append(Llamada(c.get("id", ""), fn.get("name", ""), argumentos))
        return Respuesta((mensaje.get("content") or "").strip(), llamadas)


def proveedor_por_configuracion() -> ProveedorIA:
    """El conector que diga la configuracion (IA_PROVEEDOR)."""
    p = settings.IA_PROVEEDOR
    if p == "anthropic":
        return Anthropic(
            settings.IA_LLAVE,
            settings.IA_MODELO or MODELO_ANTHROPIC_DE_OMISION,
            settings.IA_URL or "https://api.anthropic.com/v1",
        )
    url = settings.IA_URL or DIRECCIONES[p]
    if p == "openai":
        return CompatibleConOpenAI(
            settings.IA_LLAVE,
            settings.IA_MODELO,
            url,
            campo_tokens="max_completion_tokens",
            tokens=MAX_TOKENS_OPENAI,
        )
    return CompatibleConOpenAI(settings.IA_LLAVE, settings.IA_MODELO, url)
