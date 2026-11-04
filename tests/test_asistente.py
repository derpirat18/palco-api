"""El asistente de PSD por WhatsApp (bot, 3 nov).

Sin gastar en IA ni mandar WhatsApp de verdad: una IA falsa con guion y un
mensajero falso que solo anota. Lo que se prueba de verdad: la firma de
Meta, que un mensaje repetido no se conteste dos veces, que las
herramientas solo lean lo publico, los topes de gasto y la traduccion a
cada empresa de IA.
"""

import hashlib
import hmac
import itertools
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.alertas import DestinatarioDeAlertas
from app.models.asistente import ConversacionDelAsistente, MensajeDelAsistente
from app.models.catalog import Event, Venue
from app.services import alertas
from app.services import asistente as asist
from app.services.ia import (
    Anthropic,
    CompatibleConOpenAI,
    FallaDeIA,
    Herramienta,
    Llamada,
    Respuesta,
    Resultado,
    Turno,
)
from app.services.whatsapp import (
    ClienteWhatsApp,
    MensajeEntrante,
    firma_valida,
    leer_mensajes,
    partir,
)
from tests.test_apartados import funcion_a_la_venta
from tests.test_correo import CarteroFalso

pytestmark = pytest.mark.asyncio

API = "/api/v1"
SECRETO = "secreto-de-la-app-de-meta"
TEL = "5215512345678"


# --------------------------------------------------------------------------
# Dobles
# --------------------------------------------------------------------------


class IAFalsa:
    """Contesta con un guion; anota lo que recibio."""

    def __init__(self, *respuestas: Respuesta | Exception) -> None:
        self.guion = list(respuestas)
        self.vistas: list[list[Turno]] = []
        self.sistemas: list[str] = []

    async def responder(self, sistema, turnos, herramientas):
        self.sistemas.append(sistema)
        self.vistas.append(
            [
                Turno(t.rol, t.texto, list(t.llamadas), list(t.resultados))
                for t in turnos
            ]
        )
        r = self.guion.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


_wamids = itertools.count(1)


class MensajeroFalso:
    def __init__(self) -> None:
        self.textos: list[tuple[str, str]] = []
        self.ubicaciones: list[tuple] = []
        self.leidos: list[str] = []

    async def marcar_leido(self, wamid):
        self.leidos.append(wamid)

    async def enviar_texto(self, telefono, texto):
        self.textos.append((telefono, texto))
        return f"wamid.salida.{next(_wamids)}"

    async def enviar_ubicacion(self, telefono, lat, lng, nombre, direccion):
        self.ubicaciones.append((telefono, lat, lng, nombre, direccion))
        return f"wamid.ubicacion.{next(_wamids)}"


def aviso(*mensajes: dict, nombre: str = "María") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": TEL, "profile": {"name": nombre}}],
                            "messages": list(mensajes),
                        },
                    }
                ]
            }
        ],
    }


def texto(wamid: str, cuerpo: str, de: str = TEL) -> dict:
    return {"id": wamid, "from": de, "type": "text", "text": {"body": cuerpo}}


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()


async def llega(db, *mensajes: MensajeEntrante) -> int:
    (cid,) = await asist.registrar_entrantes(db, list(mensajes))
    return cid


def entrante(wamid: str, cuerpo: str = "", tipo: str = "texto") -> MensajeEntrante:
    return MensajeEntrante(wamid, TEL, "María", tipo, cuerpo)


async def contestar(db, redis_test, cid, ia, cartero=None) -> MensajeroFalso:
    m = MensajeroFalso()
    await asist.atender(db, cid, ia=ia, mensajero=m, redis=redis_test, cartero=cartero)
    return m


async def estados(db, cid) -> list[tuple[str, str]]:
    filas = (
        await db.scalars(
            select(MensajeDelAsistente)
            .where(MensajeDelAsistente.conversacion_id == cid)
            .order_by(MensajeDelAsistente.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [(f.direccion, f.estado) for f in filas]


@pytest.fixture
def whatsapp_encendido(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN", "token")
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRETO)
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verifica-esto")
    monkeypatch.setattr(settings, "WHATSAPP_NUMERO_ID", "123")
    monkeypatch.setattr(settings, "IA_LLAVE", "llave-ia")


@pytest.fixture
def programadas(cliente_http):
    from app.api.v1.whatsapp import get_programar
    from app.main import app

    lista: list[list[int]] = []
    app.dependency_overrides[get_programar] = lambda: lista.append
    return lista


# --------------------------------------------------------------------------
# Meta: firma, aviso y webhook
# --------------------------------------------------------------------------


async def test_firma_de_meta():
    cuerpo = b'{"a":1}'
    assert firma_valida(cuerpo, firmar(cuerpo), SECRETO)
    assert not firma_valida(cuerpo + b" ", firmar(cuerpo), SECRETO)
    assert not firma_valida(cuerpo, firmar(cuerpo), "otro-secreto")
    assert not firma_valida(cuerpo, None, SECRETO)
    assert not firma_valida(cuerpo, firmar(cuerpo).removeprefix("sha256="), SECRETO)


async def test_lee_solo_mensajes_y_marca_lo_que_no_es_texto():
    a = aviso(
        texto("w1", "Hola"),
        {"id": "w2", "from": TEL, "type": "audio", "audio": {"id": "x"}},
        {"id": "w3", "from": "no-es-numero", "type": "text", "text": {"body": "x"}},
    )
    a["entry"][0]["changes"][0]["value"]["statuses"] = [{"id": "w0", "status": "read"}]

    assert leer_mensajes(a) == [
        MensajeEntrante("w1", TEL, "María", "texto", "Hola"),
        MensajeEntrante("w2", TEL, "María", "otro", ""),
    ]
    assert leer_mensajes({"entry": "basura"}) == []


async def test_partir_textos_largos():
    assert partir("hola") == ["hola"]
    piezas = partir("a" * 30 + "\n" + "b" * 30, tope=40)
    assert piezas == ["a" * 30, "b" * 30]
    assert all(len(p) <= 10 for p in partir("x" * 25, tope=10))


async def test_apagado_el_webhook_no_existe(cliente_http):
    assert (await cliente_http.get(f"{API}/whatsapp/webhook")).status_code == 404
    assert (
        await cliente_http.post(f"{API}/whatsapp/webhook", content=b"{}")
    ).status_code == 404


async def test_conectar_el_webhook(cliente_http, whatsapp_encendido):
    url = f"{API}/whatsapp/webhook"
    bien = {
        "hub.mode": "subscribe",
        "hub.verify_token": "verifica-esto",
        "hub.challenge": "42",
    }

    r = await cliente_http.get(url, params=bien)
    assert (r.status_code, r.text) == (200, "42")
    r = await cliente_http.get(url, params={**bien, "hub.verify_token": "otro"})
    assert r.status_code == 403


async def test_sin_firma_no_entra_nada(
    cliente_http, db, whatsapp_encendido, programadas
):
    cuerpo = json.dumps(aviso(texto("w1", "Hola"))).encode()

    r = await cliente_http.post(
        f"{API}/whatsapp/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": "sha256=00"},
    )

    assert r.status_code == 401
    assert programadas == []
    assert await db.scalar(select(MensajeDelAsistente.id)) is None


async def test_un_mensaje_repetido_se_contesta_una_vez(
    cliente_http, db, whatsapp_encendido, programadas
):
    cuerpo = json.dumps(aviso(texto("w1", "Hola"))).encode()
    encabezado = {
        "x-hub-signature-256": firmar(cuerpo),
        "content-type": "application/json",
    }

    r1 = await cliente_http.post(
        f"{API}/whatsapp/webhook", content=cuerpo, headers=encabezado
    )
    r2 = await cliente_http.post(
        f"{API}/whatsapp/webhook", content=cuerpo, headers=encabezado
    )

    assert r1.status_code == r2.status_code == 200
    assert len(programadas) == 1
    conv = await db.scalar(select(ConversacionDelAsistente))
    assert (conv.telefono, conv.nombre_perfil, conv.estado) == (TEL, "María", "bot")
    assert await estados(db, conv.id) == [("entrada", "pendiente")]


async def test_avisos_de_entregado_no_programan_nada(
    cliente_http, whatsapp_encendido, programadas
):
    a = aviso()
    a["entry"][0]["changes"][0]["value"] = {
        "statuses": [{"id": "w0", "status": "delivered"}]
    }
    cuerpo = json.dumps(a).encode()

    r = await cliente_http.post(
        f"{API}/whatsapp/webhook",
        content=cuerpo,
        headers={"x-hub-signature-256": firmar(cuerpo)},
    )

    assert r.status_code == 200
    assert programadas == []


async def test_el_sitio_lee_el_numero(cliente_http, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_NUMERO_PUBLICO", "15556016883")
    r = await cliente_http.get(f"{API}/publico/configuracion")
    assert r.json()["whatsapp"] == {"numero": "15556016883"}


# --------------------------------------------------------------------------
# Contestar
# --------------------------------------------------------------------------


async def test_contesta_y_recuerda_la_platica(db, redis_test):
    cid = await llega(db, entrante("w1", "Hola"))
    ia = IAFalsa(Respuesta("¡Hola, María! ¿Qué evento buscas?", []))
    m = await contestar(db, redis_test, cid, ia)

    assert m.textos == [(TEL, "¡Hola, María! ¿Qué evento buscas?")]
    assert m.leidos == ["w1"]
    assert await estados(db, cid) == [("entrada", "respondido"), ("salida", "enviado")]
    # La primera vez sabe su nombre de WhatsApp.
    assert "María" in ia.vistas[0][0].texto

    await llega(db, entrante("w2", "Del circo"))
    ia2 = IAFalsa(Respuesta("Va", []))
    await contestar(db, redis_test, cid, ia2)

    vista = ia2.vistas[0]
    assert [t.rol for t in vista] == ["usuario", "asistente", "usuario"]
    assert vista[1].texto == "¡Hola, María! ¿Qué evento buscas?"
    assert vista[2].texto == "Del circo"


async def test_varios_mensajes_seguidos_van_juntos(db, redis_test):
    cid = await llega(db, entrante("w1", "Hola"), entrante("w2", "quiero boletos"))
    ia = IAFalsa(Respuesta("Claro", []))
    m = await contestar(db, redis_test, cid, ia)

    assert len(ia.vistas) == 1 and len(m.textos) == 1
    assert ia.vistas[0][-1].texto.endswith("Hola\nquiero boletos")


async def test_audio_o_foto_sin_gastar_en_ia(db, redis_test):
    cid = await llega(db, entrante("w1", tipo="otro"))
    ia = IAFalsa()
    m = await contestar(db, redis_test, cid, ia)

    assert m.textos == [(TEL, asist.SOLO_TEXTO)]
    assert ia.vistas == []


async def test_si_la_ia_falla_se_disculpa(db, redis_test):
    cid = await llega(db, entrante("w1", "Hola"))
    m = await contestar(db, redis_test, cid, IAFalsa(FallaDeIA("caida")))

    assert m.textos == [(TEL, asist.FALLA)]
    assert await estados(db, cid) == [("entrada", "error"), ("salida", "enviado")]


async def test_tope_por_numero_avisa_una_vez(db, redis_test, monkeypatch):
    monkeypatch.setattr(settings, "ASISTENTE_MENSAJES_POR_NUMERO_AL_DIA", 1)
    cid = await llega(db, entrante("w1", "Hola"))
    await contestar(db, redis_test, cid, IAFalsa(Respuesta("Hola", [])))

    await llega(db, entrante("w2", "otra"))
    m2 = await contestar(db, redis_test, cid, IAFalsa())
    await llega(db, entrante("w3", "y otra"))
    m3 = await contestar(db, redis_test, cid, IAFalsa())

    assert m2.textos == [(TEL, asist.TOPE)]
    assert m3.textos == []
    assert (await estados(db, cid))[-1] == ("entrada", "ignorado")


async def test_tomada_por_una_persona_el_bot_no_se_mete(db, redis_test):
    cid = await llega(db, entrante("w1", "Hola"))
    conv = await db.get(ConversacionDelAsistente, cid)
    conv.estado = "persona"
    await db.flush()

    m = await contestar(db, redis_test, cid, IAFalsa())

    assert m.textos == []
    assert await estados(db, cid) == [("entrada", "ignorado")]


async def test_si_otro_ya_contesta_no_se_duplica(db, redis_test):
    cid = await llega(db, entrante("w1", "Hola"))
    await redis_test.set(f"asist:candado:{cid}", "1")

    m = await contestar(db, redis_test, cid, IAFalsa())

    assert m.textos == []
    assert await estados(db, cid) == [("entrada", "pendiente")]


# --------------------------------------------------------------------------
# Herramientas
# --------------------------------------------------------------------------


async def test_busca_ve_zonas_y_manda_la_liga(db, redis_test, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    pid = esc["performance_id"]
    cid = await llega(db, entrante("w1", "Quiero 2 boletos"))
    ia = IAFalsa(
        Respuesta("", [Llamada("c1", "buscar_eventos", {"texto": evento.nombre})]),
        Respuesta(
            "",
            [
                Llamada("c2", "ver_evento", {"slug": evento.slug}),
                Llamada("c3", "ver_zonas", {"funcion_id": pid}),
            ],
        ),
        Respuesta(
            "",
            [Llamada("c4", "liga_de_compra", {"slug": evento.slug, "funcion_id": pid})],
        ),
        Respuesta("Aquí está tu liga", []),
    )

    m = await contestar(db, redis_test, cid, ia)

    assert m.textos == [(TEL, "Aquí está tu liga")]
    resultados = {
        r.llamada_id: json.loads(r.contenido)
        for v in ia.vistas
        for t in v
        for r in t.resultados
    }
    assert resultados["c1"]["eventos"][0]["slug"] == evento.slug
    funcion = resultados["c2"]["funciones"][0]
    assert (funcion["funcion_id"], funcion["estado_venta"]) == (pid, "en_venta")
    zonas = {z["zona"]: z for z in resultados["c3"]["zonas"]}
    assert zonas["Luneta"]["lugares_disponibles"] == 5
    assert zonas["Luneta"]["precio_adulto_desde"].startswith("$")
    assert resultados["c4"] == {
        "liga": f"{settings.FRONT_URL.rstrip('/')}/{evento.slug}/funcion/{pid}"
    }


async def test_herramientas_no_inventan_ni_dejan_comprar_lo_que_no_se_vende(
    db, clientes
):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    h = asist.Herramientas(db)

    async def usar(nombre, **args):
        return json.loads(await h.ejecutar(Llamada("x", nombre, args)))

    assert "error" in await usar("ver_evento", slug="no-existe")
    assert "error" in await usar("liga_de_compra", slug=evento.slug, funcion_id=999999)
    assert "error" in await usar("borrar_todo")
    assert "error" in await usar("ver_evento", slug=evento.slug, extra="x")
    # Un evento en borrador no existe para el asistente.
    evento.estado = "borrador"
    await db.flush()
    assert "error" in await usar("ver_evento", slug=evento.slug)
    assert (await usar("buscar_eventos"))["eventos"] == []


async def test_como_llegar_manda_la_ubicacion(db, redis_test, clientes):
    esc = await funcion_a_la_venta(db, clientes[0].id)
    evento = await db.get(Event, esc["event_id"])
    recinto = await db.get(Venue, evento.venue_id)
    recinto.direccion, recinto.lat, recinto.lng = (
        "Av. Siempre Viva 742",
        Decimal("19.4326"),
        Decimal("-99.1332"),
    )
    await db.flush()
    cid = await llega(db, entrante("w1", "¿Cómo llego?"))
    ia = IAFalsa(
        Respuesta("", [Llamada("c1", "como_llegar", {"slug": evento.slug})]),
        Respuesta("Te mando la ubicación", []),
    )

    m = await contestar(db, redis_test, cid, ia)

    assert m.textos == [(TEL, "Te mando la ubicación")]
    ((tel, lat, lng, nombre, direccion),) = m.ubicaciones
    assert (tel, lat, lng, nombre) == (TEL, 19.4326, -99.1332, recinto.nombre)
    assert direccion.startswith("Av. Siempre Viva 742")


async def test_pasar_a_persona_avisa_al_equipo(db, redis_test, monkeypatch):
    monkeypatch.setattr(alertas, "activas", lambda: True)
    db.add(DestinatarioDeAlertas(email="nadia@psdigital.mx", nombre="Nadia"))
    await db.flush()
    cid = await llega(db, entrante("w1", "Me cobraron dos veces"))
    ia = IAFalsa(
        Respuesta("", [Llamada("c1", "pasar_a_persona", {"motivo": "Cobro doble"})]),
        Respuesta("Ya le pasé tu caso al equipo", []),
    )
    cartero = CarteroFalso()

    m = await contestar(db, redis_test, cid, ia, cartero)

    assert m.textos == [(TEL, "Ya le pasé tu caso al equipo")]
    (correo,) = cartero.enviados
    assert "persona" in correo.asunto
    assert "Cobro doble" in correo.texto and f"+{TEL}" in correo.texto


async def test_las_instrucciones_llevan_el_nombre_y_las_reglas(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(settings, "ASISTENTE_NOMBRE", "Boletín")
    s = asist.instrucciones(datetime(2026, 11, 4, 18, 0, tzinfo=UTC))
    assert s.startswith("Eres Boletín")
    assert "12:00 h" in s  # hora del centro de México
    assert "Nunca pidas ni aceptes datos de tarjeta" in s


# --------------------------------------------------------------------------
# Empresas de IA
# --------------------------------------------------------------------------

TURNOS = [
    Turno("usuario", "Hola"),
    Turno("usuario", "¿qué hay?"),
    Turno(
        "asistente", "Déjame ver", [Llamada("t1", "buscar_eventos", {"texto": "circo"})]
    ),
    Turno(
        "herramientas", resultados=[Resultado("t1", "buscar_eventos", '{"total": 0}')]
    ),
]
HERRAMIENTA = Herramienta(
    "buscar_eventos", "Busca", {"type": "object", "properties": {}}
)


def http_falso(*respuestas: tuple[int, dict]):
    pedidas: list[httpx.Request] = []
    cola = list(respuestas)

    def manejar(request: httpx.Request) -> httpx.Response:
        pedidas.append(request)
        estado, cuerpo = cola.pop(0)
        return httpx.Response(estado, json=cuerpo)

    return httpx.AsyncClient(transport=httpx.MockTransport(manejar)), pedidas


async def test_anthropic_traduce_y_lee_herramientas(monkeypatch):
    monkeypatch.setattr("app.services.ia.asyncio.sleep", _sin_espera)
    cliente, pedidas = http_falso(
        (529, {"error": "saturada"}),
        (
            200,
            {
                "content": [
                    {"type": "text", "text": "Busco"},
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "ver_evento",
                        "input": {"slug": "x"},
                    },
                ]
            },
        ),
    )
    ia = Anthropic("llave-a", "modelo-a", cliente=cliente)

    r = await ia.responder("sistema", TURNOS, [HERRAMIENTA])

    assert r == Respuesta("Busco", [Llamada("t2", "ver_evento", {"slug": "x"})])
    assert len(pedidas) == 2  # se reintento una vez
    p = pedidas[-1]
    assert str(p.url) == "https://api.anthropic.com/v1/messages"
    assert p.headers["x-api-key"] == "llave-a"
    cuerpo = json.loads(p.content)
    assert cuerpo["model"] == "modelo-a" and cuerpo["system"] == "sistema"
    assert [m["role"] for m in cuerpo["messages"]] == ["user", "assistant", "user"]
    assert [b["text"] for b in cuerpo["messages"][0]["content"]] == [
        "Hola",
        "¿qué hay?",
    ]
    assert cuerpo["messages"][1]["content"][1]["type"] == "tool_use"
    assert cuerpo["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": '{"total": 0}',
    }
    assert cuerpo["tools"][0]["input_schema"] == HERRAMIENTA.parametros


async def test_compatible_con_openai(monkeypatch):
    cliente, pedidas = http_falso(
        (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "t2",
                                    "type": "function",
                                    "function": {
                                        "name": "ver_evento",
                                        "arguments": '{"slug": "x"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        ),
    )
    ia = CompatibleConOpenAI(
        "llave-g",
        "gemini-x",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        cliente,
    )

    r = await ia.responder("sistema", TURNOS, [HERRAMIENTA])

    assert r == Respuesta("", [Llamada("t2", "ver_evento", {"slug": "x"})])
    p = pedidas[0]
    assert (
        str(p.url)
        == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert p.headers["authorization"] == "Bearer llave-g"
    cuerpo = json.loads(p.content)
    assert [m["role"] for m in cuerpo["messages"]] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
    ]
    assert (
        cuerpo["messages"][3]["tool_calls"][0]["function"]["arguments"]
        == '{"texto": "circo"}'
    )
    assert cuerpo["messages"][4] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": '{"total": 0}',
    }
    assert cuerpo["tools"][0]["function"]["name"] == "buscar_eventos"


async def test_llave_mala_es_falla_clara():
    cliente, _ = http_falso((401, {"error": "invalid x-api-key"}))
    with pytest.raises(FallaDeIA, match="401"):
        await Anthropic("mala", cliente=cliente).responder(
            "s", [Turno("usuario", "x")], []
        )


async def test_cliente_de_whatsapp_manda_lo_que_pide_meta():
    cliente, pedidas = http_falso((200, {"messages": [{"id": "wamid.1"}]}))
    wa = ClienteWhatsApp("tok", "123", "v23.0", cliente)

    assert await wa.enviar_texto(TEL, "Hola") == "wamid.1"

    p = pedidas[0]
    assert str(p.url) == "https://graph.facebook.com/v23.0/123/messages"
    assert p.headers["authorization"] == "Bearer tok"
    assert json.loads(p.content)["text"] == {"body": "Hola", "preview_url": True}


async def _sin_espera(_):
    return None


# --------------------------------------------------------------------------
# Configuracion
# --------------------------------------------------------------------------

BASE = {
    "DATABASE_URL": "postgresql://palco:palco@localhost:5432/palco_dev",
    "REDIS_URL": "redis://localhost:6379/0",
    "SECRET_KEY": "a" * 40,
    "TICKET_SIGNING_KEY": "b" * 40,
}
META = {
    "WHATSAPP_TOKEN": "t",
    "WHATSAPP_APP_SECRET": "s",
    "WHATSAPP_VERIFY_TOKEN": "v",
    "WHATSAPP_NUMERO_ID": "1",
}


def construir(**extra) -> Settings:
    return Settings(_env_file=None, **{**BASE, **extra})


async def test_configuracion_del_asistente():
    completo = {**META, "IA_LLAVE": "k"}
    assert not construir().whatsapp_activo
    assert construir(**completo).whatsapp_activo
    assert construir(**{**completo, "IA_LLAVE": " k \n"}).IA_LLAVE == "k"
    assert construir(**completo, IA_PROVEEDOR="gemini", IA_MODELO="m").whatsapp_activo

    # A medias no tumba la API: se queda apagado y dice que falta.
    s = construir(WHATSAPP_TOKEN="t", WHATSAPP_VERIFY_TOKEN="v")
    assert not s.whatsapp_activo
    assert s.faltantes_del_asistente() == [
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_NUMERO_ID",
        "IA_LLAVE",
    ]
    assert construir(**META).faltantes_del_asistente() == ["IA_LLAVE"]
    assert (
        "IA_MODELO"
        in construir(**completo, IA_PROVEEDOR="deepseek").faltantes_del_asistente()
    )
    assert (
        "IA_URL"
        in construir(
            **completo, IA_PROVEEDOR="otro", IA_MODELO="m"
        ).faltantes_del_asistente()
    )

    # Lo que deja Terraform en SSM antes de poner el valor cuenta como vacio.
    s = construir(**{**completo, "WHATSAPP_APP_SECRET": "pendiente"})
    assert s.WHATSAPP_APP_SECRET == "" and not s.whatsapp_activo

    with pytest.raises(ValidationError, match="solo digitos"):
        construir(WHATSAPP_NUMERO_PUBLICO="+1 555 601 6883")
    with pytest.raises(ValidationError, match="https"):
        construir(IA_URL="http://inseguro.example")
