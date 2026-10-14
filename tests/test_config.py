"""Pruebas de la configuracion.

Las llaves de firma son el unico secreto que, mal puesto, no produce
ningun sintoma visible: la app arranca, las pruebas pasan y los boletos
se emiten. El dia que alguien lea el repositorio, los puede falsificar.
Por eso se validan al arrancar y por eso se prueba esa validacion.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = {
    "DATABASE_URL": "postgresql://palco:palco@localhost:5432/palco_dev",
    "REDIS_URL": "redis://localhost:6379/0",
}

LLAVE_A = "a" * 40
LLAVE_B = "b" * 40


def construir(**extra) -> Settings:
    """Settings sin leer .env, para no depender del entorno de quien corre."""
    return Settings(_env_file=None, **{**BASE, **extra})


def test_llaves_validas():
    s = construir(SECRET_KEY=LLAVE_A, TICKET_SIGNING_KEY=LLAVE_B)
    assert s.SECRET_KEY == LLAVE_A
    assert s.TICKET_SIGNING_KEY == LLAVE_B


@pytest.mark.parametrize(
    "campo",
    ["SECRET_KEY", "TICKET_SIGNING_KEY"],
)
def test_rechaza_la_llave_de_ejemplo(campo):
    """El relleno de .env.example pasa el largo minimo: hay que nombrarlo."""
    llaves = {"SECRET_KEY": LLAVE_A, "TICKET_SIGNING_KEY": LLAVE_B}
    llaves[campo] = "cambiame_por_una_llave_larga_y_aleatoria_de_al_menos_32"

    with pytest.raises(ValidationError) as exc:
        construir(**llaves)
    assert campo in str(exc.value)
    assert "secrets.token_urlsafe" in str(exc.value)


def test_rechaza_llaves_iguales():
    """Una sola llave acopla el riesgo de sesiones con el de boletos."""
    with pytest.raises(ValidationError, match="distintas"):
        construir(SECRET_KEY=LLAVE_A, TICKET_SIGNING_KEY=LLAVE_A)


@pytest.mark.parametrize("campo", ["SECRET_KEY", "TICKET_SIGNING_KEY"])
def test_rechaza_llaves_cortas(campo):
    llaves = {"SECRET_KEY": LLAVE_A, "TICKET_SIGNING_KEY": LLAVE_B}
    llaves[campo] = "corta"

    with pytest.raises(ValidationError, match="at least 32"):
        construir(**llaves)


def test_url_async_y_sync_apuntan_a_la_misma_base():
    """Alembic corre sincrono y la app async, sobre la misma base."""
    s = construir(SECRET_KEY=LLAVE_A, TICKET_SIGNING_KEY=LLAVE_B)
    assert s.db_url_async.startswith("postgresql+asyncpg://")
    assert s.db_url_sync.startswith("postgresql://")
    assert s.db_url_sync.endswith("/palco_dev")
    assert s.db_url_async.endswith("/palco_dev")
