"""Catálogos de ciudades (GEO1) y categorías (CAT1). Ver app/models/catalogos.py."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio, RecursoNoEncontrado
from app.core.geo_mx import ESTADOS_MX, PAISES, clave, estado_canonico
from app.models.base import RolUsuario
from app.models.catalog import Event, Venue
from app.models.catalogos import CategoriaDeEvento, Ciudad
from app.models.users import User

APROBADA, SOLICITADA, RECHAZADA = "aprobada", "solicitada", "rechazada"


class DatoFueraDeCatalogo(ErrorDeDominio):
    codigo = "fuera_de_catalogo"
    http_status = 422


class YaEnElCatalogo(ErrorDeDominio):
    codigo = "ya_en_el_catalogo"
    http_status = 409


def _limpio(texto: str) -> str:
    return " ".join(texto.split())


# --------------------------------------------------------------------------
# Ciudades
# --------------------------------------------------------------------------


async def geo(db: AsyncSession) -> list[dict]:
    """Países -> estados -> ciudades, para las listas encadenadas del panel."""
    ciudades = (
        await db.scalars(select(Ciudad).order_by(Ciudad.estado, Ciudad.nombre))
    ).all()
    usos = await recintos_por_ciudad(db)
    por_estado: dict[tuple[str, str], list[Ciudad]] = {}
    for c in ciudades:
        por_estado.setdefault((c.pais, c.estado), []).append(c)
    return [
        {
            "codigo": codigo,
            "nombre": nombre,
            "estados": [
                {
                    "nombre": estado,
                    "ciudades": [
                        {"id": c.id, "nombre": c.nombre, "recintos": usos.get(c.id, 0)}
                        for c in por_estado.get((codigo, estado), [])
                    ],
                }
                for estado in ESTADOS_MX
            ],
        }
        for codigo, nombre in PAISES.items()
    ]


async def agregar_ciudad(
    db: AsyncSession, pais: str, estado: str, nombre: str, actor_id: int | None
) -> Ciudad:
    """Solo PSD (los recintos solo los da de alta PSD)."""
    if pais not in PAISES:
        raise DatoFueraDeCatalogo(
            "Por ahora solo hay recintos en México.", {"campo": "pais"}
        )
    estado_ok = estado_canonico(estado)
    if estado_ok is None:
        raise DatoFueraDeCatalogo("Escoge el estado de la lista.", {"campo": "estado"})
    nombre = _limpio(nombre)
    k = clave(pais, estado_ok, nombre)
    nueva = await db.scalar(
        pg_insert(Ciudad)
        .values(
            pais=pais, estado=estado_ok, nombre=nombre, clave=k, agregada_por=actor_id
        )
        .on_conflict_do_nothing(index_elements=[Ciudad.clave])
        .returning(Ciudad.id)
    )
    if nueva is None:
        existente = await db.scalar(select(Ciudad).where(Ciudad.clave == k))
        raise YaEnElCatalogo(
            f"{existente.nombre} ya está en la lista de {estado_ok}.",
            {"campo": "nombre"},
        )
    return await db.get(Ciudad, nueva)


async def recintos_por_ciudad(db: AsyncSession) -> dict[int, int]:
    """Cuántos recintos usan cada ciudad del catálogo (por id de ciudad)."""
    filas = await db.execute(
        select(Ciudad.id, func.count(Venue.id))
        .join(
            Venue,
            (Venue.pais == Ciudad.pais)
            & (Venue.estado_geo == Ciudad.estado)
            & (Venue.ciudad == Ciudad.nombre),
        )
        .group_by(Ciudad.id)
    )
    return dict(filas.all())


async def quitar_ciudad(
    db: AsyncSession, ciudad_id: int, pasar_a_id: int | None
) -> int:
    """Solo PSD. Si algún recinto la usa, se pasan a otra ciudad del catálogo.
    Devuelve cuántos recintos se pasaron."""
    c = await db.get(Ciudad, ciudad_id, with_for_update=True)
    if c is None:
        raise RecursoNoEncontrado("Esa ciudad ya no está en la lista.")
    donde = (
        (Venue.pais == c.pais)
        & (Venue.estado_geo == c.estado)
        & (Venue.ciudad == c.nombre)
    )
    usan = await db.scalar(select(func.count(Venue.id)).where(donde)) or 0
    if usan:
        destino = await db.get(Ciudad, pasar_a_id) if pasar_a_id else None
        if destino is None or destino.id == c.id:
            raise DatoFueraDeCatalogo(
                f"{c.nombre} la usan {usan} recinto(s): escoge a qué ciudad pasarlos.",
                {"campo": "pasar_a", "recintos": usan},
            )
        await db.execute(
            update(Venue)
            .where(donde)
            .values(pais=destino.pais, estado_geo=destino.estado, ciudad=destino.nombre)
        )
    await db.delete(c)
    await db.flush()
    return usan


async def ciudad_del_catalogo(
    db: AsyncSession, pais: str, estado: str | None, ciudad: str
) -> Ciudad:
    """La ciudad tal como está en el catálogo, o un error que dice qué escoger."""
    estado_ok = estado_canonico(estado)
    if estado_ok is None:
        raise DatoFueraDeCatalogo(
            "Escoge el estado de la lista.", {"campo": "estado_geo"}
        )
    encontrada = await db.scalar(
        select(Ciudad).where(Ciudad.clave == clave(pais, estado_ok, ciudad))
    )
    if encontrada is None:
        raise DatoFueraDeCatalogo(
            f"Escoge la ciudad de la lista de {estado_ok}. Si no está, agrégala.",
            {"campo": "ciudad"},
        )
    return encontrada


# --------------------------------------------------------------------------
# Categorías
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Categorias:
    aprobadas: list[CategoriaDeEvento]
    # PSD: todas las que esperan. Organizador: las suyas, esperando o rechazadas.
    solicitudes: list[CategoriaDeEvento]


async def categorias(db: AsyncSession, usuario: User) -> Categorias:
    aprobadas = (
        await db.scalars(
            select(CategoriaDeEvento)
            .where(CategoriaDeEvento.estado == APROBADA)
            .order_by(CategoriaDeEvento.nombre)
        )
    ).all()
    consulta = select(CategoriaDeEvento).order_by(
        CategoriaDeEvento.solicitada_en.desc()
    )
    if usuario.rol == RolUsuario.ADMIN_PSD:
        consulta = consulta.where(CategoriaDeEvento.estado == SOLICITADA)
    else:
        consulta = consulta.where(
            CategoriaDeEvento.solicitada_por == usuario.id,
            or_(
                CategoriaDeEvento.estado == SOLICITADA,
                CategoriaDeEvento.estado == RECHAZADA,
            ),
        )
    return Categorias(
        list(aprobadas), list((await db.scalars(consulta.limit(50))).all())
    )


async def pedir_categoria(
    db: AsyncSession, nombre: str, usuario: User, evento_id: int | None
) -> CategoriaDeEvento:
    """PSD la agrega aprobada; un organizador la solicita."""
    nombre = _limpio(nombre)
    k = clave(nombre)
    existente = await db.scalar(
        select(CategoriaDeEvento).where(CategoriaDeEvento.clave == k)
    )
    psd = usuario.rol == RolUsuario.ADMIN_PSD
    if existente is not None:
        if existente.estado == APROBADA:
            raise YaEnElCatalogo(
                f"«{existente.nombre}» ya está en la lista: escógela.",
                {"campo": "nombre"},
            )
        if existente.estado == SOLICITADA and not psd:
            raise YaEnElCatalogo(
                f"«{existente.nombre}» ya se pidió y espera a PSD.", {"campo": "nombre"}
            )
        # Rechazada antes: se puede pedir otra vez (o PSD la aprueba directo).
        existente.nombre = nombre
        existente.estado = APROBADA if psd else SOLICITADA
        existente.solicitada_por = usuario.id
        existente.para_evento_id = evento_id
        existente.revisada_por = usuario.id if psd else None
        existente.motivo_rechazo = None
        await db.flush()
        return existente
    nueva = CategoriaDeEvento(
        nombre=nombre,
        clave=k,
        estado=APROBADA if psd else SOLICITADA,
        solicitada_por=usuario.id,
        para_evento_id=evento_id,
        revisada_por=usuario.id if psd else None,
    )
    db.add(nueva)
    await db.flush()
    return nueva


async def eventos_por_categoria(db: AsyncSession) -> dict[str, int]:
    """Cuántos eventos usan cada categoría (por nombre)."""
    filas = await db.execute(
        select(Event.categoria, func.count(Event.id))
        .where(Event.categoria.is_not(None))
        .group_by(Event.categoria)
    )
    return dict(filas.all())


async def _aprobada(db: AsyncSession, categoria_id: int) -> CategoriaDeEvento:
    c = await db.get(CategoriaDeEvento, categoria_id, with_for_update=True)
    if c is None or c.estado != APROBADA:
        raise RecursoNoEncontrado("Esa categoría ya no está en la lista.")
    return c


async def _nombre_libre(
    db: AsyncSession, nombre: str, propia_id: int | None
) -> tuple[str, str]:
    """El nombre limpio y su clave, si ninguna otra categoría lo tiene."""
    nombre = _limpio(nombre)
    k = clave(nombre)
    otra = await db.scalar(
        select(CategoriaDeEvento).where(
            CategoriaDeEvento.clave == k, CategoriaDeEvento.id != propia_id
        )
    )
    if otra is not None and otra.estado != RECHAZADA:
        raise YaEnElCatalogo(
            f"«{otra.nombre}» ya existe. Para juntarlas, quita una y pasa sus "
            "eventos a la otra.",
            {"campo": "nombre"},
        )
    if otra is not None:
        # Una rechazada con ese nombre solo estorba: se borra.
        await db.delete(otra)
        await db.flush()
    return nombre, k


async def renombrar_categoria(db: AsyncSession, categoria_id: int, nombre: str) -> int:
    """Solo PSD. Cambia cómo se escribe y se corrige en todos sus eventos.
    Devuelve cuántos eventos cambiaron."""
    c = await _aprobada(db, categoria_id)
    anterior = c.nombre
    c.nombre, c.clave = await _nombre_libre(db, nombre, c.id)
    r = await db.execute(
        update(Event).where(Event.categoria == anterior).values(categoria=c.nombre)
    )
    await db.flush()
    return r.rowcount or 0


async def quitar_categoria(
    db: AsyncSession, categoria_id: int, pasar_a: str | None
) -> int:
    """Solo PSD. Si algún evento la usa, se pasan a otra categoría de la lista.
    Devuelve cuántos eventos se pasaron."""
    c = await _aprobada(db, categoria_id)
    usan = (
        await db.scalar(select(func.count(Event.id)).where(Event.categoria == c.nombre))
        or 0
    )
    if usan:
        destino = await categoria_del_catalogo(db, pasar_a) if pasar_a else None
        if destino is None or destino == c.nombre:
            raise DatoFueraDeCatalogo(
                f"«{c.nombre}» la usan {usan} evento(s): escoge a qué categoría "
                "pasarlos.",
                {"campo": "pasar_a", "eventos": usan},
            )
        await db.execute(
            update(Event).where(Event.categoria == c.nombre).values(categoria=destino)
        )
    await db.delete(c)
    await db.flush()
    return usan


async def _solicitud(db: AsyncSession, categoria_id: int) -> CategoriaDeEvento:
    c = await db.get(CategoriaDeEvento, categoria_id, with_for_update=True)
    if c is None or c.estado != SOLICITADA:
        raise RecursoNoEncontrado("Esa solicitud ya no está pendiente.")
    return c


async def aprobar_categoria(
    db: AsyncSession, categoria_id: int, actor_id: int, nombre: str | None = None
) -> CategoriaDeEvento:
    """PSD la aprueba (puede corregir cómo se escribe). Se le pone al evento
    desde el que se pidió, si todavía no tiene categoría."""
    c = await _solicitud(db, categoria_id)
    if nombre:
        c.nombre, c.clave = await _nombre_libre(db, nombre, c.id)
    c.estado = APROBADA
    c.revisada_por = actor_id
    c.revisada_en = func.now()
    if c.para_evento_id:
        evento = await db.get(Event, c.para_evento_id)
        if evento is not None and not evento.categoria:
            evento.categoria = c.nombre
    await db.flush()
    return c


async def rechazar_categoria(
    db: AsyncSession, categoria_id: int, actor_id: int, motivo: str
) -> CategoriaDeEvento:
    c = await _solicitud(db, categoria_id)
    c.estado = RECHAZADA
    c.revisada_por = actor_id
    c.revisada_en = func.now()
    c.motivo_rechazo = _limpio(motivo)[:200]
    await db.flush()
    return c


async def categoria_del_catalogo(db: AsyncSession, nombre: str | None) -> str | None:
    """El nombre como está en el catálogo; vacío es sin categoría."""
    if nombre is None or not nombre.strip():
        return None
    c = await db.scalar(
        select(CategoriaDeEvento).where(
            CategoriaDeEvento.clave == clave(nombre),
            CategoriaDeEvento.estado == APROBADA,
        )
    )
    if c is None:
        raise DatoFueraDeCatalogo(
            "Escoge la categoría de la lista. Si no está, solicítala a PSD.",
            {"campo": "categoria"},
        )
    return c.nombre
