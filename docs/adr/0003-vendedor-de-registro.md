# 0003 — PSD como vendedor de registro, sin Stripe Connect al inicio

Fecha: 2026-10-08 · Estado: aceptado

## Contexto

PSD cobra al comprador y liquida al organizador. Stripe considera
"agregación" cobrar en nombre de terceros con una cuenta ordinaria, y
para eso existe Connect.

## Decisión

Arrancar con Stripe estándar, con PSD como vendedor de registro: la venta
es con PSD, el cargo aparece a nombre de PSD, y PSD responde por soporte,
reembolsos y contracargos. La liquidación al organizador ocurre fuera de
Stripe, por transferencia y contra factura.

## Por qué

No es un rodeo: es un modelo distinto y legítimo. Lo que Stripe prohíbe
es aparentar que el comprador le paga al organizador mientras el dinero
pasa por una cuenta ajena. Aquí el comprador efectivamente le compra a
PSD.

Evita semanas de verificación de identidad de cada organizador justo
cuando hay que salir rápido.

## Consecuencias

PSD factura el valor total del boleto como ingreso propio y deduce el
pago al organizador como costo. Eso cambia la declaración y el flujo de
IVA, y hay que validarlo con el contador antes de la primera venta.

Ese, y no el costo de las comisiones, es el argumento real para migrar a
Connect pronto. El modelo de datos ya lo contempla: `clients` tiene
espacio para `stripe_account_id` y las liquidaciones son una entidad
propia desde el día uno.
