# 0007 — Apartado desde la API: Postgres antes y después de Redis

Fecha: 2026-10-13 · Estado: aceptado

## Contexto

El apartado en Redis (ADR 0002) resuelve la carrera, pero Redis solo sabe
si una llave existe. Una butaca vendida, en kill, bloqueada o en cortesía
no tiene llave, y Redis la apartaría sin problema. La capa 2 lo frenaría
al cobrar, pero para entonces el comprador ya pasó por la autorización de
Stripe.

## Decisión

`app/services/apartados.py` envuelve a `holds.py`:

1. **Antes de Redis**, Postgres: la función está `en_venta`, con
   inventario, evento publicado y dentro de su ventana de venta; cada
   butaca está `disponible`, o `apartado` por este mismo `hold_id`, o
   `apartado` pero ya vencida.
2. **Redis** aparta, todo o nada.
3. **Después de Redis**, un `UPDATE` condicional deja el apartado en
   `performance_seats` (`estado`, `hold_id`, `hold_expira_en`) y en la
   bitácora. El `UPDATE` repite la condición del paso 1 en la base: entre
   la lectura y la escritura pudo llegar una venta o un bloqueo del panel.
   Si alguna fila no avanza, se suelta en Redis lo que esta petición había
   tomado y se responde que la butaca no está disponible.

## El vencimiento en Postgres nunca es posterior al de Redis

`hold_expira_en` se escribe como el menor TTL real de Redis menos cinco
segundos, con `now()` de la base.

**Por qué.** Si Postgres venciera después, la butaca quedaría atorada:
Redis ya la habría soltado, pero el paso 1 seguiría rechazándola hasta que
Postgres también la diera por vencida. Con Postgres siempre antes, en la
duda Postgres deja pasar y Redis decide, que es quien resuelve la carrera.

Cubierto por `test_postgres_nunca_vence_despues_que_redis` y
`test_vencido_en_postgres_pero_vivo_en_redis_decide_redis`.

## El `hold_id` lo genera el front

Es un UUID4 que el front crea antes del primer clic y manda en cada
intento. Es la credencial del apartado: con él se consulta y se libera.

**Por qué.** Si lo generara el servidor, un reintento de la primera
petición llegaría sin id, recibiría uno nuevo y chocaría contra el
apartado que ya tenía el mismo comprador. Junto con el Lua idempotente
(`SET ... NX`), repetir la petición es seguro y no alarga el apartado.

Los endpoints son públicos porque el comprador puede ser invitado. Un
UUID4 tiene 122 bits aleatorios: no se adivina.

## El tope es por apartado, no por petición

Se suma lo que el `hold_id` ya tenía. Sin eso, el mismo id acumularía
ocho butacas por llamada sin límite.

## Lo que quedó fuera

- **Límite de peticiones por IP.** Hoy cualquiera puede generar UUIDs y
  apartar ocho butacas con cada uno. El día que abra una venta fuerte, un
  bot puede vaciar el mapa durante diez minutos, una y otra vez. Hace
  falta antes de producción, en la aplicación o en el WAF.
- **Barrido de apartados vencidos.** Un carrito abandonado deja la fila en
  `apartado` hasta que alguien más la pide; el apartado ya funciona con
  eso, pero el resumen de inventario y el mapa la muestran ocupada. Falta
  el worker que use `ix_perf_seats_hold_expira`.
- ~~**El panel puede tocar butacas apartadas.**~~ Resuelto el 2026-10-13:
  `cambiar_estado` ya no toca apartados vigentes, solo lleva a
  `disponible`, `bloqueado` o `kill`, y escribe con un `UPDATE`
  condicional dentro de un savepoint. La columna `version` no es un
  bloqueo optimista (no está configurada como `version_id_col`); la
  protección real es la condición del `UPDATE`.
- **Admisión general** (ADR 0005): "apártame N de esta zona" irá encima
  de este mismo servicio.
