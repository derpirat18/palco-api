# 0020 — Lugares juntos en la misma fila y tope de tarjetas rechazadas

Fecha: 2026-10-20 · Estado: aceptado · Decisión de PSD (Nadia)

## Lugares juntos (historia VIS-14)

### Contexto

El ADR 0008 eligió solo "no dejar butacas sueltas" y permitía grupos
repartidos en varias filas. Las historias de usuario v1.2 piden además
que, con 2 o más boletos, los lugares estén en la misma fila y sean
consecutivos ("No dejes lugares vacíos"). PSD lo confirmó: en la gira de
Circo Atayde en Teatro casi todos compran en familia y esperan sentarse
juntos.

### Decisión

Se suman las dos reglas, en este orden:

1. **No dejar una butaca suelta** (ADR 0008): `409 deja_butaca_suelta`
   con el `seat_id` que quedaría solo, para señalarlo en el mapa.
2. **Lugares juntos**: con 2 o más lugares, todos en la misma zona y
   fila, sin pasillo entre ellos y sin huecos (libres, ocupados o
   bloqueados): `409 lugares_separados`.

Se evalúan sobre toda la selección (lo ya apartado más lo nuevo) al
apartar y otra vez al iniciar el cobro, porque soltar un lugar de en
medio puede separar los demás.

"Juntos" se mide por posición, igual que en el ADR 0008: dentro de un
tramo (misma zona y fila, sin pasillo), las butacas ordenadas por `x`.
Una butaca que nunca se vende (kill) no cuenta como hueco: nadie se
sienta ahí.

### Lo que cambia

Un grupo de 2 + 2 en filas distintas se compra en dos compras. Si PSD
quiere permitirlo después, se agrega como excepción (pendiente §10 de
las historias).

## Tope de tarjetas rechazadas

### Contexto

Si el banco rechaza la tarjeta, el comprador puede intentar con otra
mientras dura el apartado. Sin tope, alguien puede tener lugares
bloqueados todo ese tiempo probando tarjetas.

### Decisión

Máximo **2 tarjetas rechazadas por orden** (`MAX_RECHAZOS_POR_ORDEN`).
Al segundo rechazo:

1. Se cancela el intento en Stripe, para que no se pueda probar otra
   tarjeta sobre esa orden. Si Stripe no deja cancelar (un pago en
   proceso), no se cierra nada y se resuelve en el siguiente aviso o en
   el barrido.
2. La orden queda `cancelada` con motivo `pago_rechazado`.
3. Los lugares vuelven a la venta al momento (Redis y Postgres). Si eso
   falla, vencen solos con el apartado.

Los rechazos se cuentan con los avisos `payment_intent.payment_failed`
guardados en `payment_events`: cada intento fallido trae su propio id, un
aviso repetido no se guarda dos veces, e incluye 3D Secure fallido. Por
eso depende del webhook.

### Lo que queda

- El front tiene que avisar cuando la orden queda cancelada después del
  segundo rechazo y regresar al mapa.
- Si una tercera tarjeta alcanza a autorizarse justo mientras se cancela,
  la autorización se suelta y la persona no ve ningún cargo.
