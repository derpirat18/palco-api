# Instrucciones para agentes de código

Este archivo lo lee Codex (y cualquier otro agente) antes de tocar el
repositorio. Es la fuente de verdad de cómo se trabaja aquí.

## Qué es este proyecto

Palco es una plataforma de boletaje. PSD opera la plataforma; los
organizadores (circos, teatros, productores) son clientes que venden
boletos a través de ella. Se cobra comisión al organizador y cargo por
servicio al comprador.

**Esto maneja dinero real y controla el acceso físico a eventos.** Un
error no es un bug: es un cliente sin asiento en la puerta del teatro, o
un organizador sin su dinero.

## Las tres reglas que no se negocian

1. **Nunca vender dos veces la misma butaca.** Garantizado en base de
   datos, no en lógica de aplicación.
2. **Nunca cobrar sin emitir boleto, ni emitir sin cobrar.**
3. **El control de acceso funciona sin internet.**

Si un cambio pone en riesgo cualquiera de las tres, no se hace. Se
reporta y se pregunta.

## Stack

- Python 3.12+, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2
- PostgreSQL 16, Redis 7
- Front separado: React + TypeScript (otro repositorio)
- Infraestructura: AWS (ECS Fargate, RDS, ElastiCache, CloudFront)
- Pagos: Stripe, con PSD como vendedor de registro

## Estructura

```
app/
  core/       config, db, redis, errores, seguridad
  models/     SQLAlchemy. Un archivo por área del dominio
  schemas/    Pydantic de entrada y salida
  services/   Lógica de negocio. Aquí vive lo que importa
  api/v1/     Routers delgados. Sin lógica de negocio
  workers/    Tareas en segundo plano
migrations/   Alembic
tests/
docs/adr/     Decisiones de arquitectura con su porqué
```

**Capas:** el router valida y delega. El servicio decide. El modelo
persiste. Un router nunca arma consultas ni calcula importes.

## Zona crítica: pedir antes de tocar

Estos archivos concentran la corrección del sistema. Cambios aquí se
revisan con lupa:

- `app/services/holds.py` y `app/services/lua/*.lua`
- `app/models/inventory.py`
- Cualquier cosa que toque cobros o emisión de boletos

Nunca debilitar ni marcar como `skip` las pruebas de
`tests/test_holds_concurrency.py` ni `tests/test_auth.py`. Si una falla,
el bug está en el código.

## Cómo funciona el apartado de butacas

Tres capas, todas necesarias:

1. **Redis** aparta de forma atómica con scripts Lua. Redis es monohilo,
   así que mil peticiones por la misma butaca se serializan solas.
2. **`UPDATE` condicional** sobre `performance_seats`: solo avanza si la
   butaca sigue apartada por ese `hold_id`. Cero filas afectadas
   significa que se perdió.
3. **Índice único parcial** en `order_items`: impide dos partidas vivas
   sobre la misma butaca aunque falle todo lo anterior.

Redis da velocidad, Postgres da verdad. Si Redis se cae se pierden los
apartados en vuelo, pero la doble venta sigue siendo imposible.

Las llaves de Redis llevan hash tag: `hold:{func:<id>}:<seat_id>`. Sin
él, los scripts Lua multi-llave fallan en Redis Cluster.

## Autorización

Tres roles: `admin_psd` ve todo, `organizador` ve solo su propio cliente,
`acceso` solo escanea.

Toda verificación pasa por `app/api/deps.py`. Un endpoint **nunca**
compara roles ni `client_id` a mano.

- `requiere_rol(...)` restringe por rol.
- `alcance_cliente` da el `client_id` al que está limitado el usuario, o
  `None` si es admin. Las consultas filtran por ese valor.
- `verificar_acceso_cliente(usuario, client_id)` se llama **después** de
  cargar el recurso, con el `client_id` real del recurso. Confiar en un
  parámetro de la petición dejaría leer datos ajenos cambiando un número
  en la URL.

El usuario se relee de la base en cada petición, no se confía solo en el
token: así desactivar una cuenta la revoca al instante.

Los mensajes de error no distinguen entre "no existe" y "no es tuyo".
Distinguirlos permite descubrir qué clientes hay sondeando identificadores.

## Flujo de compra

```
apartar (Redis) → autorizar en Stripe (captura manual)
  → verificar que el apartado sigue siendo nuestro
  → escribir boleto en Postgres → capturar el pago
```

El orden importa. Con captura manual, si se pierde la butaca se cancela
la autorización y el comprador nunca ve un cargo. Un reembolso tarda
días; cancelar una autorización es inmediato.

## Reglas de datos

- **Dinero**: siempre `Numeric`, nunca `float`.
- **Fechas**: siempre `DateTime(timezone=True)`, siempre en UTC en la
  base. La conversión a hora local es del recinto (`venues.zona_horaria`).
- **Congelar lo que se vendió**: `order_items` guarda `zona_snap` y
  `asiento_snap`; `orders` guarda `comision_pct_snap`,
  `cargo_servicio_pct_snap` y `cargo_orden_snap`. Las liquidaciones suman
  esos valores, **nunca** recalculan desde la configuración vigente del
  cliente. Si mañana cambia una comisión, el histórico no se mueve.
- **Enumeraciones**: texto con `CHECK`, no tipo `ENUM` de Postgres.
  Agregar un valor a un ENUM nativo bloquea la tabla.
- **Identificadores**: `BIGINT` autoincremental.
- **Un correo, un `customer`.** Normalizado en minúsculas. Quien compra
  como invitado hoy y crea cuenta mañana conserva su historial.

## Migraciones

```bash
make migracion m="descripcion corta"   # genera
make migrar                            # aplica
```

Nunca cambiar la base a mano. Siempre revisar el archivo generado antes
de confirmarlo: el autogenerado se equivoca con índices parciales y
tipos de columna.

## Pruebas

```bash
make test
```

Qué sí se prueba: concurrencia del apartado, cálculo de importes,
transiciones de estado, idempotencia de webhooks, validación de boletos.

No perseguimos cobertura por cobertura. Perseguimos que lo que toca
dinero y acceso esté cubierto.

## Estilo

- `ruff` para formato y linter. Se corre antes de confirmar.
- Tipos en todas las firmas públicas.
- Nombres de dominio en español (`apartar`, `liberar`, `boleto`),
  términos técnicos en inglés (`commit`, `hold`, `webhook`). No traducir
  a medias.
- Docstrings que expliquen **por qué**, no qué. El qué se lee en el
  código.
- Sin comentarios obvios.

## Errores

Se levantan excepciones de `app/core/errors.py`, nunca se arman
respuestas de error a mano en los routers. Hay un solo formato de error
en toda la API.

## Lo que no se hace

- Credenciales o llaves en el código. Todo por variable de entorno.
- Datos de tarjeta tocando nuestros servidores. Checkout alojado de
  Stripe.
- `SELECT ... FOR UPDATE` para apartar butacas. Serializa y tumba el
  pool de conexiones bajo carga.
- Borrar apartados con `DEL` a ciegas. Siempre verificar propiedad
  primero.
- Migraciones que borren columnas con datos sin plan de respaldo.

## Al terminar una tarea

1. `make lint && make test` en verde.
2. Si cambió el esquema, migración generada y revisada.
3. Si la decisión fue de arquitectura, un ADR en `docs/adr/`.
4. Decir qué quedó fuera y qué supuestos se tomaron.
