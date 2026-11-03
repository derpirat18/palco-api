"""Reportes (RP3), Inicio y buscador del panel. Reglas en app/services/reportes.py."""

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual
from app.core.security import PermisoInsuficiente
from app.models.base import RolUsuario
from app.models.catalog import Client, Event, Performance, Venue, Zone
from app.models.users import User
from app.services import inicio as svc_inicio
from app.services import reportes as svc
from app.services import reportes_archivos as archivos

router = APIRouter(tags=["reportes"])


class DefinicionResponse(BaseModel):
    tipo: str
    titulo: str
    pregunta: str
    filtros: list[str]
    por_omision: str


class CatalogoResponse(BaseModel):
    reportes: list[DefinicionResponse]
    proximamente: list[DefinicionResponse]


def _filtros(
    desde: date | None = None,
    hasta: date | None = None,
    por: Literal["compra", "funcion"] | None = None,
    client_id: int | None = None,
    event_id: int | None = None,
    performance_id: int | None = None,
    zone_id: int | None = None,
    canal: Literal["linea", "taquilla", "cortesia"] | None = None,
    tipo: Literal["adulto", "nino", "mayor"] | None = None,
    cajero_id: int | None = None,
    estado: Literal["pagada", "cancelada", "reembolsada"] | None = None,
) -> dict[str, Any]:
    return dict(locals())


def _a_json(valor: Any) -> Any:
    if valor is None or isinstance(valor, (str, int, bool)):
        return valor
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return str(valor)


async def _preparar(db, usuario, reporte: str, datos: dict) -> svc.Filtros:
    definicion = svc.POR_TIPO.get(reporte)
    if definicion is None:
        raise svc.ReporteInvalido("Ese reporte no existe.")
    datos = {**datos, "por": datos["por"] or definicion.por_omision}
    return await svc.acotar(db, usuario, reporte, svc.Filtros(**datos))


async def _nombre(db, columna, id_) -> str | None:
    return await db.scalar(select(columna).where(columna.table.c.id == id_))


async def _describir(db, f: svc.Filtros) -> list[str]:
    """Los filtros en palabras, para el encabezado del Excel y del PDF."""
    lineas = []
    if f.desde or f.hasta:
        de = f.desde.strftime("%d/%m/%Y") if f.desde else "el inicio"
        a = f.hasta.strftime("%d/%m/%Y") if f.hasta else "hoy"
        por = "fecha de compra" if f.por == "compra" else "fecha de la función"
        lineas.append(f"Periodo por {por}: de {de} a {a}")
    if f.client_id:
        lineas.append(f"Organizador: {await _nombre(db, Client.nombre, f.client_id)}")
    if f.event_id:
        lineas.append(f"Evento: {await _nombre(db, Event.nombre, f.event_id)}")
    if f.performance_id:
        fila = (
            await db.execute(
                select(Performance.inicia_en, Venue.zona_horaria)
                .join(Event, Event.id == Performance.event_id)
                .join(Venue, Venue.id == Event.venue_id)
                .where(Performance.id == f.performance_id)
            )
        ).first()
        if fila:
            lineas.append(f"Función: {svc._local(*fila):%d/%m/%Y %H:%M}")
    if f.zone_id:
        lineas.append(f"Zona: {await _nombre(db, Zone.nombre, f.zone_id)}")
    if f.canal:
        lineas.append(f"Canal: {svc.CANALES[f.canal]}")
    if f.tipo:
        lineas.append(f"Tipo de boleto: {svc.TIPOS[f.tipo]}")
    if f.cajero_id:
        lineas.append(f"Cajero: {await _nombre(db, User.nombre, f.cajero_id)}")
    if f.estado:
        lineas.append(f"Estado: {f.estado.capitalize()}")
    return lineas or ["Sin filtros: todo lo que puedes ver"]


@router.get("/reportes", response_model=CatalogoResponse)
async def catalogo(db: DB, usuario: UsuarioActual) -> CatalogoResponse:
    def resp(d) -> DefinicionResponse:
        return DefinicionResponse(
            tipo=d.tipo,
            titulo=d.titulo,
            pregunta=d.pregunta,
            filtros=list(d.filtros),
            por_omision=d.por_omision,
        )

    hay = await svc.disponibles(db, usuario)
    proximos = []
    if usuario.rol in (RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR):
        proximos = [
            DefinicionResponse(
                tipo="liquidacion",
                titulo="Liquidación",
                pregunta="¿Cuánto le toca al organizador?",
                filtros=[],
                por_omision="compra",
            ),
        ]
    return CatalogoResponse(reportes=[resp(d) for d in hay], proximamente=proximos)


@router.get("/reportes/opciones")
async def opciones(
    db: DB,
    usuario: UsuarioActual,
    client_id: int | None = None,
    event_id: int | None = None,
) -> dict:
    if not await svc.disponibles(db, usuario):
        raise PermisoInsuficiente("Tu cuenta no tiene acceso a los reportes.")
    return await svc.opciones(db, usuario, client_id, event_id)


@router.get("/reportes/{reporte}")
async def ver(
    reporte: str,
    db: DB,
    usuario: UsuarioActual,
    desde: date | None = None,
    hasta: date | None = None,
    por: Literal["compra", "funcion"] | None = None,
    client_id: int | None = None,
    event_id: int | None = None,
    performance_id: int | None = None,
    zone_id: int | None = None,
    canal: Literal["linea", "taquilla", "cortesia"] | None = None,
    tipo: Literal["adulto", "nino", "mayor"] | None = None,
    cajero_id: int | None = None,
    estado: Literal["pagada", "cancelada", "reembolsada"] | None = None,
) -> dict:
    datos = _filtros(
        desde,
        hasta,
        por,
        client_id,
        event_id,
        performance_id,
        zone_id,
        canal,
        tipo,
        cajero_id,
        estado,
    )
    f = await _preparar(db, usuario, reporte, datos)
    r = await svc.generar(db, reporte, f)
    if reporte in svc.PERSONALES:
        await svc.registrar(db, usuario, reporte, "ver", f, "pantalla")
        await db.commit()
    totales = {
        c.clave: _a_json(sum((fila.get(c.clave) or 0) for fila in r.filas))
        for c in r.columnas
        if c.formato in ("entero", "dinero")
    }
    return {
        "tipo": r.tipo,
        "titulo": r.titulo,
        "por": f.por,
        "indicadores": [
            {"etiqueta": i.etiqueta, "valor": _a_json(i.valor), "formato": i.formato}
            for i in r.indicadores
        ],
        "columnas": [
            {"clave": c.clave, "titulo": c.titulo, "formato": c.formato}
            for c in r.columnas
        ],
        "filas": [
            {k: _a_json(v) for k, v in fila.items()}
            for fila in r.filas[: svc.VISTA_PREVIA]
        ],
        "total_filas": len(r.filas),
        "totales": totales,
        "grafica": None
        if r.grafica is None
        else {
            "titulo": r.grafica.titulo,
            "etiquetas": r.grafica.etiquetas,
            "valores": r.grafica.valores,
            "formato": r.grafica.formato,
        },
        "notas": r.notas,
    }


@router.get("/reportes/{reporte}/descargar")
async def descargar(
    reporte: str,
    db: DB,
    usuario: UsuarioActual,
    formato: Literal["xlsx", "csv", "pdf"] = "xlsx",
    desde: date | None = None,
    hasta: date | None = None,
    por: Literal["compra", "funcion"] | None = None,
    client_id: int | None = None,
    event_id: int | None = None,
    performance_id: int | None = None,
    zone_id: int | None = None,
    canal: Literal["linea", "taquilla", "cortesia"] | None = None,
    tipo: Literal["adulto", "nino", "mayor"] | None = None,
    cajero_id: int | None = None,
    estado: Literal["pagada", "cancelada", "reembolsada"] | None = None,
) -> Response:
    datos = _filtros(
        desde,
        hasta,
        por,
        client_id,
        event_id,
        performance_id,
        zone_id,
        canal,
        tipo,
        cajero_id,
        estado,
    )
    f = await _preparar(db, usuario, reporte, datos)
    r = await svc.generar(db, reporte, f)
    contenido = archivos.generar(r, formato, await _describir(db, f))
    await svc.registrar(db, usuario, reporte, "descargar", f, formato)
    await db.commit()
    nombre = archivos.nombre_de_archivo(r, formato)
    return Response(
        content=contenido,
        media_type=archivos.TIPOS_MIME[formato],
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# --------------------------------------------------------------------------
# Inicio y buscador
# --------------------------------------------------------------------------


def _solo_panel(usuario: User) -> int | None:
    """PSD (todo) y el organizador (lo suyo). El personal no tiene Inicio."""
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return None
    if usuario.rol == RolUsuario.ORGANIZADOR:
        return usuario.client_id
    raise PermisoInsuficiente("Tu cuenta no tiene acceso a esta pantalla.")


@router.get("/panel/inicio")
async def inicio(db: DB, usuario: UsuarioActual) -> dict:
    return await svc_inicio.inicio(db, _solo_panel(usuario))


@router.get("/panel/buscar")
async def buscar(
    db: DB, usuario: UsuarioActual, q: str = Query(min_length=1, max_length=120)
) -> dict:
    resultados = await svc_inicio.buscar(db, usuario, _solo_panel(usuario), q)
    await db.commit()
    return {"resultados": resultados}
