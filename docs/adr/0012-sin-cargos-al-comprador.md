# 0012 — El comprador paga el boleto; PSD cobra al organizador

Fecha: 2026-10-14 · Estado: aceptado

## Contexto

El sistema nació con dos cobros al comprador encima del precio: un cargo
por servicio (porcentaje por boleto) y un cargo por orden (fijo por
compra). En la primera compra de prueba con Stripe, dos boletos de $850
salieron en $1,920: $170 de servicio y $50 por orden. JuanMa y Nadia
decidieron que no es el modelo de PSD.

## Decisión

**El comprador paga el precio del boleto y nada más.** Dos boletos de
$850 cuestan $1,700, y eso es lo que cobra Stripe.

**PSD cobra al organizador**, en su liquidación, dos cosas:

1. **Su comisión**: un solo porcentaje por organizador
   (`clients.comision_pct`), congelado en cada orden
   (`orders.comision_pct_snap`). Ese es el "cargo por servicio" del
   negocio: no hay un segundo porcentaje.
2. **La comisión de Stripe** de cada cobro. La absorbe el organizador, no
   PSD.

Con 10 % de comisión, de esos $1,700 PSD retiene $170 más lo que Stripe
haya cobrado por esa transacción, y liquida el resto al organizador.

## Cómo queda en el sistema

- Desaparecen `cargo_servicio_pct` y `cargo_orden` de organizadores y
  eventos, `cargo_servicio`, `cargo_orden` y sus snapshots de las órdenes,
  y `cargo_servicio` de las partidas. No queda configuración que alguien
  pueda volver a encender por error.
- La base exige que lo cobrado sea el precio:
  `total = subtotal - descuento` en `orders` e
  `importe = precio_unitario - descuento` en `order_items`. Aunque un bug
  del checkout sumara algo, la orden no se guarda.
- La página pública ya no publica `cargos` ni `precio_desde_con_cargo`:
  `precio_desde` es el precio final. Con eso se resuelve también la duda
  de cómo anunciar el precio total, porque no hay nada que sumar al pagar.

## Migración

No hubo ventas reales, solo órdenes de prueba. Una orden de prueba con
cargos conserva su `total`: la diferencia contra el `subtotal` sigue
mostrando lo que se cobró de cargos. Esas filas no cumplen la regla
nueva, así que en esa base la restricción se crea `NOT VALID`: se exige a
todo lo nuevo sin reescribir el pasado. En una base sin esas filas se
crea validada.

## Pendiente

- **Liquidaciones**: necesitan la comisión de Stripe por orden. Stripe la
  informa en la transacción de saldo (`balance_transaction.fee`) después
  de capturar. Hay que guardarla en la orden al capturar para que la
  liquidación la sume, igual que suma la comisión congelada.
- **Mínimo de Stripe**: sigue en $10.00 MXN por cobro. Un boleto más
  barato no se puede cobrar con tarjeta.
- **Contador**: con PSD como vendedor de registro (ADR 0003), el IVA y la
  factura se calculan sobre $1,700 cobrados al comprador. Validarlo junto
  con lo que ya estaba pendiente.
