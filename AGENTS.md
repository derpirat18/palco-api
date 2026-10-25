# Instrucciones para agentes de código

Este archivo lo lee Codex (y cualquier otro agente) antes de tocar el
repositorio. Es la fuente de verdad de cómo se trabaja aquí.

## Qué es este proyecto

Palco es una plataforma de boletaje. PSD opera la plataforma; los
organizadores (circos, teatros, productores) son clientes que venden
boletos a través de ella. PSD cobra una comisión al organizador; al
comprador no se le cobra nada encima del precio del boleto (ADR 0012).

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

## Regla fija: al usuario se le mantiene bien informado

Quien compra, quien escanea y quien administra siempre sabe qué pasa, por
qué y qué hacer. Antes de pagar, al comprar, en el correo, en la puerta y
cuando algo falla. Nunca un error sin explicación ni un paso sin avisar.

- Antes de pagar se dice lo que importa para entrar (ej. "Para entrar
  necesitas tu celular").
- Si algo cambia lo que la persona ya tiene (un QR que deja de servir, un
  correo con otro contenido), se cambia también lo que ve y lo que se le
  manda.
- Cada rechazo dice el motivo y el siguiente paso ("QR vencido: que abra
  de nuevo sus boletos"; "a taquilla con tu folio").

## La regla de la puerta (BOL3)

En la puerta solo entra el QR que cambia cada 30 s, desde el celular
(`app/services/boletos.py`). El personal de acceso no teclea códigos ni ve
los códigos en su lista. Cualquier problema (sin celular, pantalla rota,
no lee) va a taquilla: PSD o el organizador pueden teclear el código del
boleto, y queda registrado quién lo hizo.

Sin internet (E3b, `app/services/puerta_sin_red.py`): con red, el escáner
baja el paquete de la función (cada boleto con la llave de su QR) y con él
revisa sin red; las lecturas se suben al volver la red y el servidor las
juzga a la hora en que se hicieron. El paquete solo se entrega cerca del
horario de escaneo y a quien escanea esa función, y el celular lo borra al
cerrar el horario y al salir. Dos puertas sin red pueden dejar pasar el
mismo boleto: al subir sale "duplicado" y el escáner avisa "entró dos veces".

Pasar un boleto a un acompañante (BOL6, `app/services/pases.py`): no se
emite otro boleto; cambia el portador y la semilla del QR, así que el QR
anterior deja de servir al instante. Solo quien compró pasa y recupera, y
solo hasta que abre el escaneo (4 h antes): después, a taquilla. El
acompañante entra con su propia liga (`/pase/<token>`) y no puede volver a
pasarlo. Ni a él ni en la bandeja va lo que se pagó.

Reenviar la liga de los boletos (BOL15, `app/services/reenvio.py`): solo al
correo de la compra y con tope por compra. En la computadora no se muestra
ningún QR, ni siquiera el de la liga: la liga es la llave de los boletos.

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
- `app/services/apartados.py`: Postgres alrededor de Redis (ADR 0007)
- `app/services/venta.py`: cuándo una función se vende y cómo se pasa
  de precio capturado a precio con IVA (`Iva`). La usan la página
  pública, el inventario, el apartado y el cobro
- `app/models/inventory.py`
- `app/services/checkout.py`, `app/services/pasarela.py` y
  `app/services/webhooks.py`: cobro y emisión (ADR 0010)
- `app/services/barrido.py`: cierra órdenes, suelta apartados y termina
  capturas sin que nadie lo vea (ADR 0011)
- Cualquier cosa que toque cobros o emisión de boletos

Nunca debilitar ni marcar como `skip` las pruebas de
`tests/test_holds_concurrency.py`, `tests/test_apartados.py`,
`tests/test_checkout.py`, `tests/test_barrido.py` ni `tests/test_auth.py`. Si una falla,
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

Una selección no puede dejar una butaca libre sola en su fila
(`app/services/sueltas.py`, ADR 0008), y con 2 o más lugares van juntos
en la misma fila, sin huecos (ADR 0020). La vecindad sale de la posición
`x` en el mapa, nunca del número de butaca: la luneta va en nones y pares.

## Autorización

Tres roles: `admin_psd` ve todo, `organizador` ve solo su propio cliente,
`acceso` solo escanea, y solo en los eventos a los que lo asignaron
(`verificar_acceso_evento`) y dentro del horario de cada función: de 4
horas antes a 12 después (`horario_de_escaneo`).

Toda verificación pasa por `app/api/deps.py`. Un endpoint **nunca**
compara roles ni `client_id` a mano.

- `requiere_rol(...)` restringe por rol.
- Las cuentas de organizador y de acceso las crea PSD desde el panel
  (`/usuarios`), con una contraseña temporal que sale una sola vez. El
  organizador crea y asigna solo su propio personal de acceso
  (`organizador_de_cuenta_nueva`, `verificar_cuenta_administrable`). Las de
  `admin_psd` solo con `scripts/crear_usuario.py`: el panel no reparte acceso total.
- Regresar a válido un boleto escaneado por error (`entradas/deshacer`) es
  solo de PSD y el organizador, nunca de la puerta, y deja una lectura
  `deshecho` con quién y por qué.
- El organizador crea y edita sus eventos, pero publicarlos o retirarlos
  (el `estado` del evento) lo hace solo `admin_psd` (ADR 0021).
- `alcance_cliente` da el `client_id` al que está limitado el usuario, o
  `None` si es admin. Las consultas filtran por ese valor.
- `verificar_acceso_cliente(usuario, client_id, no_encontrado=...)` se
  llama **después** de cargar el recurso, con el `client_id` real del
  recurso. Confiar en un parámetro de la petición dejaría leer datos
  ajenos cambiando un número en la URL. `no_encontrado` es obligatorio:
  es el mismo mensaje que da el servicio cuando el recurso no existe
  (constantes en `app/core/errors.py`).

El usuario se relee de la base en cada petición, no se confía solo en el
token: así desactivar una cuenta la revoca al instante.

Lo ajeno responde **idéntico** a lo inexistente: mismo 404, mismo cuerpo.
Distinguirlos permite descubrir qué clientes hay sondeando identificadores.
Cada endpoint nuevo con un identificador lleva su prueba con
`igual_que_inexistente`. El 403 queda solo para rol insuficiente, que no
revela nada del recurso.

## Lo público

`/api/v1/publico/...` no pide sesión: todo lo que devuelve sale a
internet. Nunca exponer el cliente, la comisión de PSD, conteos de
butacas ni identificadores internos del organizador. Cada esquema
público tiene una prueba que fija la lista exacta de campos; agregar uno
es una decisión, no un descuido.

Un evento en borrador o de un organizador dado de baja responde igual
que uno inexistente (`visible_al_publico`), y la página y el apartado
deciden "se vende o no" con la misma función (`motivo_no_se_vende`).

El mapa de la función (`/publico/funciones/{id}/mapa`) sigue las reglas
del mapa: cada butaca sale `libre` u `ocupada`, sin decir si se vendió,
está apartada, bloqueada o es cortesía. "Libre" es exactamente lo que el
apartado deja pasar. Las butacas en kill no se envían. Se guarda 2 s en
Redis; lo del propio comprador (`tuya`) se calcula fresco.

La cartelera (`/publico/cartelera`) solo lista eventos visibles con alguna
función próxima, con la misma disponibilidad en niveles y el precio con IVA.
Las visitas a la página del evento solo ordenan "lo más buscado" y nunca
se muestran; contarlas jamás puede tumbar la página que vende.

## Flujo de compra

```
apartar (Redis) → autorizar en Stripe (captura manual)
  → verificar que el apartado sigue siendo nuestro
  → escribir boleto en Postgres → capturar el pago
```

El orden importa. Con captura manual, si se pierde la butaca se cancela
la autorización y el comprador nunca ve un cargo. Un reembolso tarda
días; cancelar una autorización es inmediato.

Detalle en ADR 0010. Lo que no se rompe:

- Webhook y `POST /ordenes/{uuid}/confirmar` llaman a la misma
  `finalizar`, idempotente. El webhook no se cree: se consulta a Stripe.
- Entre emitir boletos y capturar hay un `commit`. Así, si la captura
  falla, los boletos existen y se sabe qué deshacer.
- Máximo 2 tarjetas rechazadas por orden (ADR 0020): al segundo rechazo
  se cancela en Stripe primero, la orden queda cancelada y los lugares
  vuelven a la venta.
- Error **transitorio** de Stripe: la orden queda pagada sin capturar y
  se reintenta. Nunca se cancelan boletos por un error de red. Solo un
  rechazo **definitivo** deshace la venta.
- Los hechos de negocio (`orden.pagada`, `orden.cancelada`) se escriben
  en `outbox_events` en la misma transacción. Nunca se avisa a otro
  sistema directo desde el checkout.
- Toda llamada a Stripe lleva llave de idempotencia derivada del `uuid`
  de la orden (`orden-`, `capturar-`, `cancelar-`).
- El QR del boleto (ADR 0014) lleva solo el uuid y una firma con
  `TICKET_SIGNING_KEY`; nunca datos personales. Solo un boleto `valido`
  de una orden `pagada` muestra QR. Se lee con `leer_qr`, nunca
  separando el texto a mano.
- Empezar un apartado pasa por la puerta del turno (`verificar_turno`,
  ADR 0017). Una ruta nueva que reserve lugares también tiene que pasar
  por ella, o la sala de espera se brinca.
- Empezar un apartado también pide la verificación humana de Turnstile
  (`verificar_humano`, ADR 0019). Si Cloudflare falla se deja pasar y
  se avisa: nunca se le bloquea la compra a una persona por una falla
  nuestra. Las llaves de prueba de Cloudflare nunca van en producción.
- Toda ruta pública nueva lleva su `limite("grupo")` (ADR 0016). Una
  ruta sin límite es la puerta que un bot va a encontrar.
- Ningún correo sale desde una petición ni dentro del cobro. Lo manda
  la bandeja (`.\make.ps1 bandeja`, ADR 0015) a partir de
  `outbox_events`, con reintentos. Un tipo nuevo de aviso se agrega como
  otro "atiende" de `BandejaService`, nunca llamando al cartero directo.
- Una cortesía es una orden de $0 con `canal = 'cortesia'` sobre el
  mismo inventario (ADR 0018). Nace pagada **y con `capturada_en`**: una
  orden pagada sin captura es una que el barrido intenta cobrar.
- El barrido (`.\make.ps1 barrido`) termina lo que queda a medias:
  órdenes vencidas, apartados vencidos, capturas pendientes y avisos de
  capturas atrasadas. Nunca cierra una orden sin cancelar antes en
  Stripe, y nunca suelta una butaca que Redis siga dando por apartada.

## Reglas de datos

- **Dinero**: siempre `Numeric`, nunca `float`.
- **Fechas**: siempre `DateTime(timezone=True)`, siempre en UTC en la
  base. La conversión a hora local es del recinto (`venues.zona_horaria`).
- **El comprador paga el precio del boleto y nada más.** Sin cargo por
  servicio ni por orden. La base lo exige: `ck_orders_total_cuadra`
  y `ck_order_items_importe_es_el_precio`. La comisión de PSD y la de
  Stripe se le descuentan al organizador en su liquidación (ADR 0012).
- **IVA por evento** (ADR 0013): la tasa la escribe el organizador y dice
  si sus precios la incluyen o son "más IVA". El comprador ve y paga
  siempre con IVA. Todo precio capturado pasa por `venta.Iva` antes de
  llegar a una butaca o a la página pública; nunca se suma IVA a mano en
  otro lado. La orden se lee como factura: subtotal (sin IVA) + IVA −
  descuento = total, y congela la tasa en `iva_pct_snap`.
- **Congelar lo que se vendió**: `order_items` guarda `zona_snap` y
  `asiento_snap`; `orders` guarda `comision_pct_snap` y el contacto escrito en
  esa compra (`nombre_snap`, `apellidos_snap`, `email_snap`, `telefono_snap`),
  que es el que usan la puerta, Ventas, el Excel y el correo: el registro del
  comprador no se cambia si su correo ya existía. **Nunca** se muestra el
  teléfono del registro (`customers.telefono`): pudo darse en otra compra,
  con otro organizador, y sería una fuga de datos entre organizadores. Las liquidaciones
  suman esos valores, **nunca** recalculan desde la configuración vigente
  del cliente. Si mañana cambia una comisión, el histórico no se mueve.
- **Enumeraciones**: texto con `CHECK`, no tipo `ENUM` de Postgres.
  Agregar un valor a un ENUM nativo bloquea la tabla.
- **Identificadores**: `BIGINT` autoincremental por dentro, `uuid` hacia
  fuera. `clients`, `events`, `performances`, `customers`, `orders` y
  `tickets` llevan `UUIDPublicoMixin`. Todo lo que sale de Palco y se
  guarda en otro sistema (CRM, webhooks, QR, links que se comparten)
  usa el `uuid`, nunca el `id`. El `uuid` no cambia jamás, ni en una baja.
- **Un correo, un `customer`.** Normalizado en minúsculas. Quien compra
  como invitado hoy y crea cuenta mañana conserva su historial.
- **Texto libre limpio al entrar** (`app/core/textos.py`). Todo campo de
  texto de un esquema de entrada usa `Linea`, `Parrafo` o `NombreDePersona`
  (más `UrlHttps` y `Color`) y lleva `max_length` igual o menor que su
  columna: quita caracteres invisibles y espacios de más antes de medir, y
  un tope más largo que la columna termina en error 500. Un nombre de persona
  empieza con letra, así nunca es una fórmula de Excel en la lista de la
  puerta. SQL y HTML no se "limpian" aquí: los cubren los parámetros de
  SQLAlchemy y el escape de React y del correo; nunca se arma SQL ni HTML
  pegando texto del usuario.
- **Teléfonos en E.164** (`+525512345678`). Todo teléfono que entra pasa
  por `app/core/telefonos.normalizar_telefono` antes de guardarse; la
  base lo exige con `ck_customers_telefono_e164`. Un número inválido se
  rechaza (`422 telefono_invalido`), no se guarda "como venga".
- **Consentimientos: bitácora, no casilla.** No hay columna
  `acepta_marketing`. Cada aceptación o retiro es un registro nuevo en
  `consents` (finalidad, origen, versión del aviso, fecha) vía
  `ConsentimientoService`. Nunca se edita ni se borra uno: lo vigente es
  el más reciente por finalidad. `operacion` y `marketing` se piden por
  separado, nunca con una sola casilla. Ver ADR 0009.

## Migraciones

```bash
make migracion m="descripcion corta"   # genera
make migrar                            # aplica
```

Nunca cambiar la base a mano. Siempre revisar el archivo generado antes
de confirmarlo: el autogenerado se equivoca con índices parciales y
tipos de columna.

El autogenerado tampoco sabe de datos: si una columna se borra, se achica
o cambia de significado, la migración rescata o convierte lo que ya hay
antes, y falla con un mensaje claro si no puede (ejemplo:
`20261014_1622_uuid_consentimientos_telefono.py`).

En AWS las migraciones corren como tarea aparte **antes** de cambiar el
código, mientras la versión anterior sigue atendiendo. Toda migración
tiene que convivir con el código anterior: primero se agrega (columna
nueva con valor por omisión o nula), en otra entrega se usa y hasta una
tercera se quita lo viejo. Renombrar o borrar en un solo paso tumba la
versión que sigue corriendo.

## Imagen y dependencias

- Una sola imagen (`Dockerfile`) para API, barrido, bandeja y
  migraciones; cambia solo el comando. Nada de secretos dentro: todo
  entra por variables de entorno.
- `requirements.lock` fija las versiones de la imagen. Una dependencia
  nueva va en `pyproject.toml` **y** se regenera el lock con Python 3.12.

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

## Errores y comunicación con el usuario

**La regla de PSD:** nunca dejar al usuario frente a un error técnico,
ni hacerle pensar que la plataforma falló cuando se trata de una regla
de negocio.

Se levantan excepciones de `app/core/errors.py`; nunca se arman
respuestas de error a mano en los routers. Toda respuesta de error, venga
de donde venga (dominio, validación, ruta inexistente, falla), tiene la
misma forma:

```json
{"error": {"codigo": "...", "mensaje": "...", "detalle": {}}}
```

Cada error cae en uno de tres casos, y cada uno se comunica distinto:

| Caso | Ejemplo | `mensaje` | PSD |
|---|---|---|---|
| Regla de negocio | Agotado, cancelado, lugar ocupado, venta cerrada | Qué pasó y qué puede hacer ahora | Nada, o el resumen diario |
| Dato a corregir | Teléfono incompleto, falta aceptar el aviso | Qué corregir, en palabras simples (`datos_invalidos` lo hace campo por campo) | Nada |
| Falla nuestra | Excepción sin manejar, Stripe caído | "El sistema no está disponible en este momento…" con un **folio** (`error_interno`) | La traza completa en el registro, con el mismo folio |

Reglas para escribir un `mensaje`:

- Es el texto final que lee una persona: **en español, con acentos**, en
  segunda persona ("Elige otro lugar"), sin jerga ("butaca suelta",
  "token", "materializar" no van para el comprador).
- Dice qué pasó y qué hacer. Si la persona no puede hacer nada, que no
  lo parezca su culpa.
- **Nunca** lleva causas técnicas (nombres de variables, `.env`, Stripe,
  SQL, IP, trazas) ni repite lo que el usuario escribió. Eso va al
  registro (`log`), no a la respuesta.
- El `codigo` es estable y es lo que el front usa para decidir qué hacer;
  el `mensaje` se puede mejorar sin romper nada.
- Lo que va en `detalle` son datos que el front necesita (una fecha, los
  campos a corregir, el folio), nunca datos personales.

## Lo que no se hace

- Credenciales o llaves en el código. Todo por variable de entorno.
- Datos de tarjeta tocando nuestros servidores. La tarjeta se captura
  en el formulario de Stripe (Payment Element) dentro de la página; a
  nosotros solo llega el resultado.
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
