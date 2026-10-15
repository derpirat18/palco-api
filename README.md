# Palco — API de boletaje

Plataforma de boletaje operada por PSD. Los organizadores venden sus
eventos a través de ella; PSD cobra una comisión al organizador. El
comprador paga el precio del boleto, sin cargos encima.

## Arranque local

Requiere Python 3.12+ y Docker Desktop.

**Windows (PowerShell):**

```powershell
.\make.ps1 instalar   # venv + dependencias + .env
.\make.ps1 arriba     # Postgres y Redis
.\make.ps1 migrar     # esquema
.\make.ps1 correr     # API en http://localhost:8000
```

**macOS y Linux:**

```bash
make instalar
make arriba
make migrar
make correr
```

Documentación interactiva en `http://localhost:8000/docs`.

El contenedor de Postgres crea dos bases la primera vez que arranca:
`palco_dev` para desarrollo y `palco_test` para las pruebas. Si ya tenías
el volumen creado de antes, la de pruebas no existe y hay que crearla una
sola vez:

```powershell
docker compose exec -T db createdb -U palco palco_test
```

Primer usuario, para poder entrar:

```powershell
.\make.ps1 usuario admin@psdigital.mx "Tu Nombre" admin_psd
```

Pruebas y estilo: `.\make.ps1 test` y `.\make.ps1 lint` (o `make test`,
`make lint`).

## Cómo está organizado

```
app/core/       configuración, base de datos, Redis, errores
app/models/     SQLAlchemy, un archivo por área del dominio
app/services/   lógica de negocio
app/api/v1/     routers
migrations/     Alembic
docs/adr/       decisiones de arquitectura
```

## Antes de tocar el código

Lee **AGENTS.md**. Está escrito para agentes pero aplica igual a
personas: contiene las reglas que no se negocian, cómo funciona el
apartado de butacas y qué zonas del código requieren cuidado especial.

## Lo esencial en tres líneas

El sistema maneja dinero y controla acceso físico a eventos. Nunca se
vende dos veces la misma butaca: lo garantizan Redis, un `UPDATE`
condicional y un índice único parcial, en ese orden. Las pruebas de
`tests/test_holds_concurrency.py` son la red de seguridad y no se tocan.
