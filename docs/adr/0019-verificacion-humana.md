# 0019 — Verificación humana con Cloudflare Turnstile al empezar a comprar

Fecha: 2026-10-17 · Estado: aceptado

## Contexto

Los límites por IP (ADR 0016) frenan a un bot torpe, pero no a uno que
reparte sus peticiones entre muchas IP. Para que un bot no acapare el
mapa el día de una venta grande hace falta saber, al menos una vez por
compra, que del otro lado hay una persona (B3 del plan).

Elegimos **Cloudflare Turnstile**: es gratis, casi siempre es invisible
para la persona (no le pide resolver acertijos) y no rastrea al
comprador para publicidad.

## Decisión

**Dónde se pide.** Solo al **empezar** un apartado
(`POST /performances/{id}/apartados` con un `hold_id` nuevo). El front
manda el token que le dio el widget en el encabezado
`X-Palco-Verificacion`.

Una vez aprobado, ese `hold_id` queda marcado en Redis (`humano:{hold_id}`)
durante lo que puede vivir el apartado (`HOLD_TTL_SEGUNDOS` +
`HOLD_TTL_CHECKOUT_SEGUNDOS`). Sumar o quitar butacas, reintentar tras
"alguien más eligió ese lugar" y pagar ya no la piden. El token de
Turnstile se usa una sola vez y vence en 5 minutos: pedirlo en cada clic
obligaría al front a regenerarlo a cada rato.

**Qué responde.**

| Situación | Resultado |
|---|---|
| `TURNSTILE_SECRET_KEY` vacía (local) | Pasa, sin consultar nada |
| El `hold_id` ya se verificó | Pasa |
| Sin token, en blanco o de más de 2048 caracteres | 403 `verificacion_requerida`, sin consultar a Cloudflare |
| Cloudflare no aprueba (token inválido, vencido o ya usado) | 403 `verificacion_requerida` |
| Cloudflare aprueba | Pasa y se marca el `hold_id` |
| Cloudflare no responde (4 s), responde con error o rechaza **nuestra** llave | Pasa, se marca y se registra un error para PSD |

El mensaje es *"No pudimos confirmar que eres una persona. Completa la
verificación e intenta de nuevo."*: no menciona Cloudflare ni tokens. Al
recibir `verificacion_requerida`, el front vuelve a mostrar el widget,
obtiene un token nuevo y reintenta con el mismo `hold_id`.

**Si Cloudflare falla, se deja pasar.** Es una falla nuestra, no del
comprador, y la regla de la casa es no dejar a una persona frente a un
error por algo que no hizo. Mientras dure, siguen protegiendo los
límites (ADR 0016), la puerta del turno (ADR 0017) y el tope de butacas
por orden. El registro `Turnstile no respondio` / `Turnstile rechazo la
peticion` es la alerta (A4): si aparece, la verificación está apagada
de hecho.

**Llaves por entorno.** El front lee la sitekey de
`GET /publico/configuracion` y no la lleva escrita: el mismo front sirve
en sandbox y en producción.

| Entorno | Llaves |
|---|---|
| local | Vacías: apagada |
| sandbox | Las de prueba de Cloudflare (aprueban siempre) o unas reales |
| producción | Reales. La configuración rechaza las de prueba al arrancar |

Fuera de local las dos llaves son obligatorias, y siempre van juntas: con
solo la secreta, el servidor exigiría una verificación que el front no
sabe hacer y nadie podría comprar.

## Lo que no cubre

- **Login del personal**: lo protegen el límite y el candado de 15
  minutos (B5). Si hace falta, se agrega Turnstile ahí con la misma
  función.
- **Revisar `hostname` y `action`** de la respuesta de Cloudflare: la
  sitekey ya está atada a los dominios de PSD en el panel. Se puede
  endurecer después sin cambiar el front.
- **Cuenta de Cloudflare**: la crea PSD (gratuita) y da de alta los
  dominios del front. Para el piloto en sandbox bastan las llaves de
  prueba.
