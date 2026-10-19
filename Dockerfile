# Imagen de Palco (I1). Una sola imagen para los cuatro procesos; ECS (y
# docker compose) eligen cual corre con el comando:
#
#   API (por omision)  uvicorn app.main:app ...
#   barrido            python -m app.workers.barrido
#   bandeja            python -m app.workers.bandeja
#   migraciones        alembic upgrade head
#   primer admin       python scripts/crear_usuario.py ... (interactivo)
#
# Reglas:
# - Nada de secretos en la imagen. Todo entra por variables de entorno al
#   arrancar (Parameter Store en AWS, .env en local). .dockerignore deja
#   fuera el .env.
# - Versiones exactas de requirements.lock: la imagen de hoy y la de dentro
#   de un mes instalan lo mismo.
# - Corre con un usuario sin privilegios y el codigo es de solo lectura
#   para ese usuario.
# - Sin HEALTHCHECK aqui: la misma imagen corre procesos sin puerto. Cada
#   servicio define el suyo (ECS, docker-compose.yml).
# - Los trabajadores no atienden SIGTERM por su cuenta; en ECS se activa
#   initProcessEnabled y en docker compose `init: true` para que se
#   detengan al instante al desplegar.

ARG PYTHON_VERSION=3.12

# --- Dependencias: se instalan aparte para reusar la capa si solo cambia
# el codigo.
FROM python:${PYTHON_VERSION}-slim-bookworm AS dependencias

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.lock /tmp/requirements.lock
# Solo ruedas precompiladas: si alguna dependencia no trae rueda para la
# arquitectura (ARM o x86), la construccion falla aqui en vez de compilar
# a medias.
RUN pip install --only-binary=:all: -r /tmp/requirements.lock

# --- Imagen final.
FROM python:${PYTHON_VERSION}-slim-bookworm

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 palco \
 && useradd --system --uid 10001 --gid palco --home-dir /app --no-create-home palco

WORKDIR /app

COPY --from=dependencias /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts/crear_usuario.py ./scripts/crear_usuario.py

USER palco

EXPOSE 8000

# --timeout-keep-alive 75: mayor que los 60 s del balanceador de AWS; si
#   fuera menor, el balanceador reusaria conexiones ya cerradas y daria 502.
# --no-proxy-headers: la IP real la saca app/services/limites.py de
#   X-Forwarded-For con PROXIES_DE_CONFIANZA, sin que uvicorn la reescriba.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-keep-alive", "75", "--no-proxy-headers", "--no-server-header"]
