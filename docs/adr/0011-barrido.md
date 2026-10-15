# 0011 — Barrido: terminar lo que quedó a medias

Fecha: 2026-10-14 · Estado: aceptado

## Contexto

El flujo de compra se limpia solo mientras hay tráfico: el siguiente
comprador cierra la orden abandonada que le estorba, y Stripe reintenta
el webhook de una captura que falló. Sin tráfico ni reintentos quedan
cosas a medias que cuestan dinero o confunden: butacas pintadas como
apartadas que ya nadie tiene, PaymentIntents vivos de órdenes que nadie
va a pagar, y boletos emitidos cuyo cobro nunca se capturó. Stripe suelta
la autorización de tarjeta a los 7 días; después de eso, son boletos sin
cobro.

## Decisión

Un proceso aparte, `python -m app.workers.barrido` (`.\make.ps1 barrido`),
que da una vuelta cada 60 segundos. En cada vuelta, en este orden:

1. **Órdenes pendientes vencidas** (`CheckoutService.expirar`). Si el
   comprador alcanzó a autorizar, no se cierra a ciegas: se intenta
   vender y `finalizar` decide con Redis. Si no, se **cancela en Stripe
   primero** y después se cierra. Si Stripe todavía no deja cancelar
   (pago en proceso), se queda para la siguiente vuelta: cerrar sin
   cancelar podría dejar un cobro sin orden.
2. **Apartados vencidos**: vuelven a `disponible` con su movimiento en la
   bitácora. **Redis manda**: si la llave sigue siendo de ese apartado,
   no se toca aunque Postgres diga que venció. Pasa de verdad: un
   checkout extiende en Redis, falla en Stripe, y su transacción revierte
   la extensión en Postgres pero no en Redis.
3. **Capturas pendientes**: órdenes pagadas sin capturar hace más de
   2 minutos se pasan otra vez por `finalizar`, que captura con la misma
   llave de idempotencia. Los 2 minutos evitan cruzarse con el flujo
   normal.
4. **Capturas atrasadas**: pagadas sin capturar desde hace más de 5 días
   dejan un hecho `orden.captura_atrasada` en la bandeja de salida y un
   error en el log. **Una sola vez** por orden.

Las órdenes van antes que los apartados: si alguien pagó justo al límite,
su butaca tiene que seguir apartada cuando `finalizar` la busque.

Hay **60 segundos de gracia** sobre cualquier vencimiento: quien dio clic
en "pagar" justo al límite merece unos segundos.

### Errores

Cada tarea usa su propia transacción y cada orden la suya. Un error en
una orden (Stripe no responde para ese pago) se anota, se cuenta y se
reintenta en la siguiente vuelta, sin detener a las demás. Si la vuelta
completa falla (Postgres o Redis caídos un momento), el proceso no muere:
espera y vuelve a intentar.

### Una sola instancia

En producción corre como un servicio aparte de la API, con una tarea.
Aun así se protege con un candado consultivo de Postgres
(`pg_try_advisory_lock`): si por error arrancan dos, la segunda se salta
su vuelta. El candado es de la conexión; si el proceso muere, Postgres lo
suelta solo. Y aunque dos coincidieran, cada orden se toma con candado
(`SELECT … FOR UPDATE`) y todo es idempotente.

## Lo que no es

- No reembolsa: una orden `requiere_reembolso` sigue siendo trabajo
  manual.
- No publica la bandeja de salida; eso es del publicador, que aún no
  existe.
- No limita peticiones por IP: es la otra mitad del #51 y va aparte.
