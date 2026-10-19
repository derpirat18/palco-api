# Palco — API de boletaje

Plataforma de boletaje operada por PSD. Los organizadores venden sus
eventos a través de ella; PSD cobra una comisión al organizador. El
comprador paga el precio del boleto, sin cargos encima.

## Arranque local

Requiere Python 3.12+ y Docker Desktop.

**Windows (PowerShell):**

```powershell
.\make.ps1 instalar   # venv + dependencias + .env
.\make.ps1 arriba     # Postgres, Redis y el buzon de pruebas
.\make.ps1 migrar     # esquema
.\make.ps1 correr     # API en http://localhost:8000
.\make.ps1 barrido    # en otra ventana: cierra lo vencido
.\make.ps1 bandeja    # en otra ventana: manda los correos
```

**macOS y Linux:**

```bash
make instalar
make arriba
make migrar
make correr
```

Sondas de salud:

| Ruta | Para quién | Responde |
|---|---|---|
| `/api/v1/health` | Balanceador | 200 si el proceso vive |
| `/api/v1/health/ready` | Despliegue | Estado de Postgres y Redis |
| `/api/v1/health/trabajadores` | Monitor externo | 200 si el barrido y la bandeja dan vueltas, 503 si alguno se paró. En local da 503 mientras no los tengas corriendo |

Documentación interactiva en `http://localhost:8000/docs`. Los correos
que manda la app en local no salen a internet: se ven en el buzón de
pruebas, `http://localhost:8025`.

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

### La imagen de producción en tu máquina

```powershell
.\make.ps1 imagen
```

Construye la misma imagen que corre en AWS (`Dockerfile`) y levanta en
contenedores las migraciones, la API (`http://localhost:8001`), el barrido
y la bandeja, con tu `.env` y los servicios de `arriba`. El `.env` no
entra a la imagen. `.\make.ps1 abajo` detiene todo.

Las versiones exactas de la imagen están en `requirements.lock`. Si
cambias una dependencia en `pyproject.toml`, regenéralo (ver su
encabezado).

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
