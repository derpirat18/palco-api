# 0010 — Checkout: autorizar, vender, emitir y después capturar

Fecha: 2026-10-14 · Estado: aceptado

## Contexto

Aquí entra el dinero, y dos de las tres reglas que no se negocian viven
aquí: nunca vender dos veces la misma butaca, y nunca cobrar sin emitir ni
emitir sin cobrar. Entre el clic de "pagar" y el boleto pasan segundos
en los que el apartado puede vencer, Stripe puede tardar o caerse, y el
webhook y el front pueden llegar repetidos o al mismo tiempo.

## Decisión

### El formulario de tarjeta va en nuestra página

Stripe Payment Element dentro de Palco: el comprador no sale del sitio,
sigue viendo su contador y su resumen. Los datos de la tarjeta van del
navegador a Stripe; a nuestros servidores solo llega el resultado. Se
descartó la página alojada de Stripe porque su sesión dura mínimo 30
minutos y nuestro apartado 15: el comprador podría pagar sobre un
apartado ya vencido.

Solo tarjeta. OXXO y SPEI se pagan horas o días después y no admiten
captura manual: no caben en un apartado de minutos.

### Iniciar (`POST /performances/{id}/apartados/{hold_id}/checkout`)

1. La función se sigue vendiendo (`funcion_a_la_venta`, la misma regla
   que el apartado y la página pública).
2. El apartado sigue vivo en Redis. Si no: `409 apartado_expirado`, y
   no se llega a Stripe.
3. La selección no deja butacas sueltas. Se revisa otra vez porque el
   vecino pudo cambiar entre apartar y pagar.
4. Se extiende el apartado para pagar, con **tope total** de
   `HOLD_TTL_SEGUNDOS + HOLD_TTL_CHECKOUT_SEGUNDOS` desde la primera
   butaca apartada. Sin tope, repetir el checkout retendría butacas para
   siempre.
5. Las órdenes ajenas abandonadas sobre esas butacas (pendientes y
   vencidas) se cierran como `expirada` y sus partidas se anulan. Si no,
   su partida viva bloquearía la butaca en el índice único.
6. Comprador (un correo, uno solo; de uno existente solo se llenan los
   datos que faltan), orden con importes y condiciones congelados,
   partidas con `zona_snap` y `asiento_snap`, consentimiento de
   `operacion` y, solo si marcó la casilla, de `marketing`.
7. PaymentIntent con `capture_method=manual`, monto en centavos y la
   llave de idempotencia `orden-<uuid>`.

Un apartado tiene a lo más una orden pendiente (índice único parcial).
Repetir con la misma selección y el mismo correo devuelve la misma
orden: el doble clic no abre dos cobros. Si cambió algo, la anterior se
cancela en Stripe primero; si Stripe ya la había autorizado, se responde
`409 pago_en_proceso` en vez de quitarle al comprador un pago que hizo
en otra pestaña.

Total menor a $10.00 MXN: `422 orden_sin_cobro`. Stripe no cobra menos;
una orden en cero es una cortesía y va por otro camino (#48).

### Finalizar (webhook y `POST /ordenes/{uuid}/confirmar`)

Las dos puertas llaman a la misma función, idempotente. El evento del
webhook no se cree: solo dice "revisa este pago". Se consulta a Stripe
el estado real del PaymentIntent y se verifica que corresponda a la
orden (uuid en metadata, monto y moneda); si no, no se vende nada.

Con el pago autorizado, en este orden:

1. Candado sobre la orden (`SELECT … FOR UPDATE`). Webhook y front al
   mismo tiempo quedan en fila; el segundo encuentra el trabajo hecho.
   El candado es sobre **la orden**, no sobre butacas: lo que AGENTS.md
   prohíbe es serializar el apartado.
2. Redis confirma que el apartado sigue siendo nuestro, y un `UPDATE`
   condicional pasa **todas** las butacas a `vendido` o ninguna (capa 2).
3. Boletos (código legible y semilla del QR), orden `pagada`, hecho
   `orden.pagada` en la bandeja de salida, y **commit**.
4. Captura en Stripe, con llave `capturar-<uuid>`.

Si en el paso 2 la butaca ya no es nuestra: se cancela la autorización y
la orden queda `expirada`. El comprador no ve cargo.

### Dos clases de error en la captura

- **Transitorio** (red, 5xx, límite de peticiones): la orden queda
  `pagada` con `capturada_en` nulo y los boletos siguen válidos. El
  webhook contesta error, Stripe reintenta, y el reintento captura con
  la misma llave. **Nunca** se cancelan boletos por un tropiezo de red.
- **Definitivo** (Stripe dice que ya no se puede capturar): se deshace
  la venta. Boletos cancelados, butacas a `disponible`, partidas
  anuladas, orden `cancelada` con motivo `captura_rechazada`, y hecho
  `orden.cancelada`.

### Cobrado sin boletos

Con captura manual solo nosotros cobramos, salvo que alguien capture a
mano en el panel de Stripe. Si eso pasa y las butacas siguen siendo
nuestras, se emite normalmente. Si ya no, la orden queda con motivo
`requiere_reembolso` y un hecho `orden.requiere_reembolso` en la bandeja
de salida, una sola vez. El reembolso todavía es manual.

Dos clics simultáneos sobre el mismo apartado: el índice único de orden
pendiente deja pasar uno; el otro recibe `409 pago_en_proceso` y, al
repetir, la misma orden.

### Webhooks

Cada evento se guarda en `payment_events` con su id de Stripe único
antes de procesarlo. Uno ya procesado responde `duplicado` sin repetir
nada. Si procesar falla, el error queda anotado y se contesta con error
para que Stripe reintente. Sin `STRIPE_WEBHOOK_SECRET` no se acepta
ningún webhook: uno sin verificar dejaría a cualquiera marcar órdenes
como pagadas.

### Bandeja de salida (`outbox_events`)

Los hechos (`orden.pagada`, `orden.cancelada`) se escriben en la misma
transacción que los produce. Un publicador aparte los entregará al CRM, a
la mensajería y a la analítica. Así no se anuncia una venta que se
revirtió ni se pierde una que se confirmó. El `payload` lleva uuid e
importes, no correo ni teléfono.

## Lo que no es

- El barrido (ADR 0011) cierra las órdenes pendientes vencidas y
  termina las capturas pendientes sin esperar a que haya tráfico ni a
  que Stripe reintente.
- No hay publicador de la bandeja de salida: los hechos se acumulan con
  `publicado_en` nulo hasta que exista el consumidor.
- No se envían boletos por correo; es el siguiente paso.
- Sin códigos de descuento: `descuento` queda en cero.
- La autorización de una tarjeta dura 7 días en Stripe. Una orden pagada
  sin capturar más de ese tiempo pierde el cobro: el barrido la avisa a
  los 5 días (ADR 0011).
