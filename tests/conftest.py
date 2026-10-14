"""Configuracion compartida de pruebas.

Las pruebas corren contra Redis y Postgres reales, no contra dobles. El
comportamiento que nos importa (atomicidad, TTL, indices unicos,
restricciones) es precisamente lo que un mock no reproduce.
"""

import os

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

os.environ.setdefault("ENTORNO", "local")

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://localhost:6379/15")
DB_TEST_URL = os.getenv(
    "DATABASE_TEST_URL", "postgresql+asyncpg://palco:palco@localhost:5432/palco_test"
)


# --------------------------------------------------------------------------
# Redis
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_test():
    """Base 15 de Redis, reservada para pruebas. Se limpia en cada test."""
    # Pool acotado y bloqueante, igual que en produccion. Abrir mil
    # sockets de golpe no es realista y en Windows ni siquiera funciona:
    # lo correcto es que las corrutinas hagan fila por una conexion.
    pool = aioredis.BlockingConnectionPool.from_url(
        REDIS_TEST_URL, decode_responses=True, max_connections=50, timeout=30
    )
    cliente = aioredis.Redis(connection_pool=pool)
    await cliente.flushdb()
    yield cliente
    await cliente.flushdb()
    await cliente.aclose()


@pytest_asyncio.fixture
async def hold_service(redis_test, monkeypatch):
    from app.core import redis as redis_mod
    from app.services.holds import HoldService

    # Los scripts Lua se registran contra el cliente de pruebas.
    monkeypatch.setattr(redis_mod, "redis_client", redis_test)
    redis_mod.LuaScripts._cache = {}
    yield HoldService(redis=redis_test)
    redis_mod.LuaScripts._cache = {}


# --------------------------------------------------------------------------
# Base de datos
# --------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def esquema_de_pruebas():
    """Crea el esquema una sola vez por corrida.

    Se hace con el driver sincrono a proposito: un motor async con
    alcance de sesion chocaria con el bucle de eventos por prueba.
    """
    from sqlalchemy import create_engine

    from app.models import Base

    motor = create_engine(DB_TEST_URL.replace("+asyncpg", ""))
    Base.metadata.drop_all(motor)
    Base.metadata.create_all(motor)
    motor.dispose()


@pytest_asyncio.fixture
async def db(esquema_de_pruebas):
    """Sesion dentro de una transaccion que se revierte al terminar.

    Cada prueba arranca con la base en el mismo estado y no deja rastro,
    sin recrear el esquema cada vez.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    motor = create_async_engine(DB_TEST_URL, poolclass=NullPool)
    async with motor.connect() as conn:
        trans = await conn.begin()
        sesion = AsyncSession(bind=conn, expire_on_commit=False)
        yield sesion
        await sesion.close()
        await trans.rollback()
    await motor.dispose()


@pytest_asyncio.fixture
async def motor_con_commit(esquema_de_pruebas):
    """Motor que SI confirma, para probar concurrencia de verdad.

    La fixture `db` envuelve todo en una transaccion que se revierte, asi
    que dos sesiones nunca se ven entre si. Para probar que dos peticiones
    simultaneas no pisan la misma fila hace falta que confirmen de verdad.
    Al terminar se vacian las tablas, porque lo confirmado sobrevive y
    ensuciaria a las demas pruebas.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models import Base

    motor = create_async_engine(DB_TEST_URL, poolclass=NullPool)
    yield motor

    tablas = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with motor.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tablas} RESTART IDENTITY CASCADE"))
    await motor.dispose()


@pytest_asyncio.fixture
async def cliente_http(db):
    """Cliente HTTP contra la app, con la sesion de pruebas inyectada."""
    from httpx import ASGITransport, AsyncClient

    from app.core.db import get_db
    from app.main import app

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Datos de prueba
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def clientes(db):
    """Dos organizadores distintos, para probar aislamiento."""
    from app.models.catalog import Client

    a = Client(nombre="Circo Uno", comision_pct=10)
    b = Client(nombre="Teatro Dos", comision_pct=8)
    db.add_all([a, b])
    await db.flush()
    return a, b


@pytest_asyncio.fixture
async def usuarios(db, clientes):
    """Un usuario por rol. La contraseña de todos es `Secreta-12345`."""
    from app.core.security import hash_password
    from app.models.base import RolUsuario
    from app.models.users import User

    cliente_a, cliente_b = clientes
    pw = hash_password("Secreta-12345")

    admin = User(
        email="admin@psdigital.mx",
        nombre="Admin PSD",
        password_hash=pw,
        rol=RolUsuario.ADMIN_PSD,
        client_id=None,
    )
    org_a = User(
        email="org.a@circo.mx",
        nombre="Organizador A",
        password_hash=pw,
        rol=RolUsuario.ORGANIZADOR,
        client_id=cliente_a.id,
    )
    org_b = User(
        email="org.b@teatro.mx",
        nombre="Organizador B",
        password_hash=pw,
        rol=RolUsuario.ORGANIZADOR,
        client_id=cliente_b.id,
    )
    acceso = User(
        email="puerta@circo.mx",
        nombre="Personal de acceso",
        password_hash=pw,
        rol=RolUsuario.ACCESO,
        client_id=cliente_a.id,
    )
    inactivo = User(
        email="exempleado@circo.mx",
        nombre="Ex empleado",
        password_hash=pw,
        rol=RolUsuario.ORGANIZADOR,
        client_id=cliente_a.id,
        activo=False,
    )
    db.add_all([admin, org_a, org_b, acceso, inactivo])
    await db.flush()
    return {
        "admin": admin,
        "org_a": org_a,
        "org_b": org_b,
        "acceso": acceso,
        "inactivo": inactivo,
    }
