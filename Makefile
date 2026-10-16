.PHONY: ayuda instalar arriba abajo migrar migracion test lint fmt correr barrido bandeja usuario _servicios

ayuda:
	@echo "instalar   - crea venv e instala dependencias"
	@echo "arriba     - levanta Postgres, Redis y el buzon de pruebas (docker compose)"
	@echo "abajo      - detiene los servicios"
	@echo "migrar     - aplica migraciones pendientes"
	@echo "migracion  - genera migracion:  make migracion m='descripcion'"
	@echo "correr     - arranca la API en modo desarrollo"
	@echo "barrido    - cierra apartados y ordenes vencidas, reintenta cobros (una=1: una vuelta)"
	@echo "bandeja    - manda los correos con boletos; se ven en http://localhost:8025 (una=1)"
	@echo "test       - corre las pruebas"
	@echo "lint       - revisa estilo"
	@echo "fmt        - formatea"
	@echo "usuario    - alta de personal:  make usuario email=a@b.mx nombre='X' rol=admin_psd [cliente=1]"

# Alembic y pytest tardan medio minuto y escupen cien lineas de traceback
# para decir que Postgres no esta prendido. Mejor avisar antes.
_servicios:
	@docker compose exec -T db pg_isready -U palco >/dev/null 2>&1 || \
	  (echo "Postgres no responde. Corre primero: make arriba" && exit 1)
	@docker compose exec -T redis redis-cli ping >/dev/null 2>&1 || \
	  (echo "Redis no responde. Corre primero: make arriba" && exit 1)

instalar:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	@test -f .env || cp .env.example .env

arriba:
	docker compose up -d
	@echo "esperando a Postgres..."
	@until docker compose exec -T db pg_isready -U palco >/dev/null 2>&1; do sleep 1; done
	@echo "listo"

abajo:
	docker compose down

migrar: _servicios
	.venv/bin/alembic upgrade head

migracion: _servicios
	@test -n "$(m)" || (echo "uso: make migracion m='descripcion'" && exit 1)
	.venv/bin/alembic revision --autogenerate -m "$(m)"

correr: _servicios
	.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

barrido: _servicios
	.venv/bin/python -m app.workers.barrido $(if $(una),--una-vez,)

bandeja: _servicios
	.venv/bin/python -m app.workers.bandeja $(if $(una),--una-vez,)

test: _servicios
	.venv/bin/pytest

usuario: _servicios
	@test -n "$(email)" -a -n "$(nombre)" -a -n "$(rol)" || \
	  (echo "uso: make usuario email=a@b.mx nombre='X' rol=admin_psd [cliente=1]" && exit 1)
	.venv/bin/python scripts/crear_usuario.py "$(email)" "$(nombre)" "$(rol)" \
	  $(if $(cliente),--cliente $(cliente),)

lint:
	.venv/bin/ruff check app tests
	.venv/bin/ruff format --check app tests

fmt:
	.venv/bin/ruff format app tests
	.venv/bin/ruff check --fix app tests
