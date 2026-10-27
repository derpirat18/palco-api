"""Cuenta del comprador (R1): entrar con un codigo al correo, sus compras y su perfil.

Reglas:

- **Sin contrasenas.** El comprador escribe su correo y le mandamos un
  codigo de 6 digitos. Vence en 10 minutos y aguanta 5 intentos; despues
  hay que pedir otro. Entrar y recuperar la cuenta es lo mismo.
- **Un correo, un comprador** (como en el checkout). Quien entra con un
  correo ve todas las compras hechas con el, tambien las que hizo como
  invitado: el codigo prueba que el correo es suyo.
- **Comprar sigue sin cuenta.** Lo que se escribe en una compra se queda
  en esa compra (OB1); el perfil solo se cambia en Mi perfil.
- **Cambiar de correo** se confirma con un codigo al correo nuevo. Sus
  compras pasan al nuevo. Si el nuevo ya tenia compras, las dos cuentas se
  juntan en una: el comprador probo que los dos correos son suyos.
- **Topes:** 3 codigos por correo cada 15 minutos y 10 al dia, ademas del
  limite por IP y la verificacion humana para pedir el de entrar. Asi nadie
  usa el sitio para llenar de correos a otra persona.
- Los codigos viven solo en Redis, cifrados con la llave del servidor; no
  se guardan en la base ni pasan por la bandeja de salida.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from redis.asyncio.client import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.core.security import TokenInvalido, crear_token_comprador
from app.models.base import Base, EstadoOrden, EstadoTransferencia
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Customer, Order, OrderItem, Ticket, Transfer
from app.services.correo import Mensaje
from app.services.plantillas import pintar_archivo

log = logging.getLogger("palco.cuenta")

VIDA_DEL_CODIGO = 10 * 60
INTENTOS = 5
CODIGOS_POR_RATO = 3
RATO = 15 * 60
CODIGOS_AL_DIA = 10
ENTRAR, CAMBIAR = "entrar", "cambiar"


class CodigoInvalido(ErrorDeDominio):
    codigo = "codigo_invalido"
    http_status = 422


class DemasiadosCodigos(ErrorDeDominio):
    codigo = "demasiados_codigos"
    http_status = 429


class CorreoOcupado(ErrorDeDominio):
    codigo = "correo_ocupado"
    http_status = 409


def normalizar_correo(correo: str) -> str:
    return correo.strip().lower()


def _huella(texto: str) -> str:
    """HMAC con la llave del servidor: ni el correo ni el codigo quedan en
    Redis tal cual."""
    return hmac.new(
        settings.SECRET_KEY.encode(), texto.encode(), hashlib.sha256
    ).hexdigest()


def _clave(proposito: str, correo: str) -> str:
    return f"cuenta:codigo:{proposito}:{_huella(correo)[:32]}"


# --------------------------------------------------------------------------
# Codigos
# --------------------------------------------------------------------------


async def _contar_envio(redis: Redis, correo: str) -> None:
    """Topes por correo: 3 cada 15 minutos y 10 al dia."""
    h = _huella(correo)[:32]
    rato, dia = f"cuenta:envios:{h}", f"cuenta:envios-dia:{h}"
    try:
        n = await redis.incr(rato)
        if n == 1:
            await redis.expire(rato, RATO)
        d = await redis.incr(dia)
        if d == 1:
            await redis.expire(dia, 24 * 3600)
    except RedisError:
        log.warning("Tope de codigos sin Redis: se deja pasar.")
        return
    if n > CODIGOS_POR_RATO or d > CODIGOS_AL_DIA:
        raise DemasiadosCodigos(
            "Ya te mandamos varios códigos. Revisa tu correo (también la carpeta "
            "de no deseados) o intenta en unos minutos.",
        )


async def nuevo_codigo(
    redis: Redis, proposito: str, correo: str, customer_id: int | None = None
) -> str:
    """Genera el codigo, lo guarda (cifrado) y lo devuelve para mandarlo.

    Pedir uno nuevo invalida el anterior.
    """
    await _contar_envio(redis, correo)
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    valor = json.dumps({"h": _huella(f"{correo}:{codigo}"), "i": 0, "c": customer_id})
    await redis.set(_clave(proposito, correo), valor, ex=VIDA_DEL_CODIGO)
    return codigo


async def comprobar_codigo(
    redis: Redis,
    proposito: str,
    correo: str,
    codigo: str,
    customer_id: int | None = None,
) -> None:
    """Pasa si el codigo es el vigente; si no, cuenta el intento."""
    clave = _clave(proposito, correo)
    guardado = await redis.get(clave)
    vencido = CodigoInvalido(
        "Ese código ya venció o se usaron todos sus intentos. Pide uno nuevo."
    )
    if guardado is None:
        raise vencido
    datos = json.loads(guardado)
    if datos.get("c") != customer_id:
        raise vencido
    codigo = "".join(ch for ch in (codigo or "") if ch.isdigit())
    if hmac.compare_digest(datos["h"], _huella(f"{correo}:{codigo}")):
        await redis.delete(clave)
        return
    datos["i"] += 1
    if datos["i"] >= INTENTOS:
        await redis.delete(clave)
        raise vencido
    ttl = await redis.ttl(clave)
    await redis.set(clave, json.dumps(datos), ex=max(int(ttl), 1))
    quedan = INTENTOS - datos["i"]
    raise CodigoInvalido(
        f"El código no coincide. Te quedan {quedan} "
        f"{'intento' if quedan == 1 else 'intentos'}.",
        {"intentos": quedan},
    )


def correo_del_codigo(para: str, codigo: str, proposito: str) -> Mensaje:
    front = settings.FRONT_URL.rstrip("/")
    cambiar = proposito == CAMBIAR
    asunto = (
        f"{codigo} es tu código para cambiar tu correo"
        if cambiar
        else f"{codigo} es tu código para entrar a {settings.MARCA}"
    )
    datos = {
        "asunto": asunto,
        "marca": settings.MARCA,
        "codigo": codigo,
        "cambiar": cambiar,
        "minutos": VIDA_DEL_CODIGO // 60,
        "inicio_url": f"{front}/",
        "assets_url": f"{front}/img/correo",
        "soporte_email": settings.SOPORTE_EMAIL,
        "aviso_privacidad": f"{front}/legal/aviso-de-privacidad",
        "anio": datetime.now().year,
    }
    return Mensaje(
        para=para,
        asunto=asunto,
        texto=pintar_archivo("correo/codigo.txt.hbs", datos, escapar=False),
        html=pintar_archivo("correo/codigo.hbs.html", datos),
    )


# --------------------------------------------------------------------------
# Sesion
# --------------------------------------------------------------------------


async def entrar(db: AsyncSession, correo: str) -> tuple[Customer, str]:
    """Despues de comprobar el codigo: el comprador (nuevo o de antes) y su token."""
    await db.execute(
        pg_insert(Customer)
        .values(email=correo)
        .on_conflict_do_nothing(index_elements=[Customer.email])
    )
    c = await db.scalar(
        select(Customer)
        .where(Customer.email == correo)
        .execution_options(populate_existing=True)
    )
    if c.email_verificado_en is None:
        c.email_verificado_en = func.now()
    if c.cuenta_activada_en is None:
        c.cuenta_activada_en = func.now()
    await db.flush()
    await db.refresh(c)
    return c, crear_token_comprador(str(c.uuid), c.sesion_version)


async def comprador_del_token(db: AsyncSession, payload: dict) -> Customer:
    try:
        uuid = UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenInvalido("Tu sesión terminó. Vuelve a entrar.") from exc
    c = await db.scalar(select(Customer).where(Customer.uuid == uuid))
    if (
        c is None
        or c.cuenta_activada_en is None
        or payload.get("v") != c.sesion_version
    ):
        raise TokenInvalido("Tu sesión terminó. Vuelve a entrar.")
    return c


async def salir_de_todos(db: AsyncSession, c: Customer) -> str:
    """Cierra todas sus sesiones; devuelve un token nuevo para este equipo."""
    c.sesion_version += 1
    await db.flush()
    return crear_token_comprador(str(c.uuid), c.sesion_version)


# --------------------------------------------------------------------------
# Compras y pases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Compra:
    uuid: UUID
    folio: str
    estado: str
    evento: str
    imagen_url: str | None
    inicia_en: datetime
    zona_horaria: str
    recinto: str
    ciudad: str
    boletos: int
    total: object
    cortesia: bool


@dataclass(frozen=True)
class PaseRecibido:
    token: str
    de: str
    evento: str
    imagen_url: str | None
    inicia_en: datetime
    zona_horaria: str
    recinto: str
    ciudad: str
    boletos: int


async def compras(db: AsyncSession, c: Customer) -> list[Compra]:
    """Sus compras pagadas (y las canceladas o reembolsadas, para que las
    vea), la funcion mas reciente primero."""
    vivos = (
        select(OrderItem.order_id, func.count(OrderItem.id).label("n"))
        .where(OrderItem.anulado.is_(False))
        .group_by(OrderItem.order_id)
        .subquery()
    )
    filas = (
        await db.execute(
            select(Order, Event, Performance, Venue, vivos.c.n)
            .join(Performance, Performance.id == Order.performance_id)
            .join(Event, Event.id == Performance.event_id)
            .join(Venue, Venue.id == Event.venue_id)
            .outerjoin(vivos, vivos.c.order_id == Order.id)
            .where(
                Order.customer_id == c.id,
                Order.estado.in_(
                    [
                        EstadoOrden.PAGADA.value,
                        EstadoOrden.CANCELADA.value,
                        EstadoOrden.REEMBOLSADA.value,
                    ]
                ),
                Order.pagada_en.is_not(None),
            )
            .order_by(Performance.inicia_en.desc(), Order.id.desc())
            .limit(200)
        )
    ).all()
    return [
        Compra(
            uuid=o.uuid,
            folio=o.folio,
            estado=o.estado,
            evento=e.nombre,
            imagen_url=e.imagen_url,
            inicia_en=p.inicia_en,
            zona_horaria=v.zona_horaria,
            recinto=v.nombre,
            ciudad=v.ciudad,
            boletos=n or 0,
            total=o.total,
            cortesia=o.canal == "cortesia",
        )
        for o, e, p, v, n in filas
    ]


async def pases_recibidos(db: AsyncSession, c: Customer) -> list[PaseRecibido]:
    """Los boletos que le pasaron y todavia tiene, uno por compra."""
    filas = (
        await db.execute(
            select(Transfer, Order, Event, Performance, Venue, Customer.nombre)
            .join(Ticket, Ticket.id == Transfer.ticket_id)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Performance, Performance.id == Order.performance_id)
            .join(Event, Event.id == Performance.event_id)
            .join(Venue, Venue.id == Event.venue_id)
            .join(Customer, Customer.id == Transfer.de_customer_id)
            .where(
                Transfer.a_customer_id == c.id,
                Transfer.estado == EstadoTransferencia.ACEPTADA.value,
                Ticket.holder_customer_id == c.id,
                Order.customer_id != c.id,
            )
            .order_by(Performance.inicia_en.desc(), Transfer.id.desc())
        )
    ).all()
    por_orden: dict[int, PaseRecibido] = {}
    cuantos: dict[int, int] = {}
    for t, o, e, p, v, de in filas:
        cuantos[o.id] = cuantos.get(o.id, 0) + 1
        if o.id in por_orden:
            continue
        por_orden[o.id] = PaseRecibido(
            token=t.token,
            de=(o.nombre_snap or de or "Quien compró").split()[0],
            evento=e.nombre,
            imagen_url=e.imagen_url,
            inicia_en=p.inicia_en,
            zona_horaria=v.zona_horaria,
            recinto=v.nombre,
            ciudad=v.ciudad,
            boletos=0,
        )
    return [
        PaseRecibido(**{**p.__dict__, "boletos": cuantos[oid]})
        for oid, p in por_orden.items()
    ]


# --------------------------------------------------------------------------
# Cambio de correo
# --------------------------------------------------------------------------


def _columnas_hacia_comprador() -> list:
    """Toda columna que apunta a customers.id, para juntar dos cuentas sin
    olvidar ninguna tabla (tambien las que se agreguen despues)."""
    columnas = []
    for tabla in Base.metadata.tables.values():
        for fk in tabla.foreign_keys:
            if fk.column.table.name == "customers" and fk.column.name == "id":
                columnas.append(fk.parent)
    return columnas


async def cambiar_correo(db: AsyncSession, c: Customer, nuevo: str) -> Customer:
    """Despues de comprobar el codigo del correo nuevo. Devuelve con quien
    queda la sesion: el mismo comprador, o el del correo nuevo si se juntan."""
    anterior = c.email
    if nuevo == anterior:
        return c
    otro = await db.scalar(
        select(Customer).where(Customer.email == nuevo).with_for_update()
    )
    if otro is None:
        c.email = nuevo
        destino = c
    else:
        # Se juntan: todo lo del correo anterior pasa al del correo nuevo.
        for col in _columnas_hacia_comprador():
            await db.execute(
                update(col.table).where(col == c.id).values({col.name: otro.id})
            )
        for campo in ("nombre", "apellidos", "telefono"):
            if getattr(otro, campo) is None:
                setattr(otro, campo, getattr(c, campo))
        if otro.email_verificado_en is None:
            otro.email_verificado_en = func.now()
        if otro.cuenta_activada_en is None:
            otro.cuenta_activada_en = func.now()
        await db.flush()
        # Sin pasar por el ORM: ya no le queda nada, y asi no intenta tocar
        # las compras que tenia cargadas.
        db.expunge(c)
        await db.execute(delete(Customer).where(Customer.id == c.id))
        destino = otro
    # Sus compras, a su correo nuevo: ahi le llegan los recordatorios y reenvios.
    await db.execute(
        update(Order)
        .where(Order.customer_id == destino.id, Order.email_snap == anterior)
        .values(email_snap=nuevo)
    )
    await db.flush()
    await db.refresh(destino)
    log.info("Comprador cambio de correo%s.", " (cuentas juntas)" if otro else "")
    return destino
