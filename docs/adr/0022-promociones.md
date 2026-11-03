# 0022 — Promociones: 2x1 o porcentaje por días de la función

Fecha: 2026-11-03 · Estado: aceptado · Decisión de PSD (JuanMa y Nadia)

## Contexto

Los organizadores piden promociones como "martes 2x1" para llenar
funciones flojas. PSD no quiere cupones ni códigos: quiere programarlas
como una alarma (días de la semana, encender y apagar) y que se apliquen
solas, en línea y en taquilla, con la misma regla.

## Decisión

- Cada organizador tiene las suyas (`promociones`, `promocion_eventos`);
  PSD puede editar todas y es el único que pone una **comisión especial**,
  que va por boleto (`order_items.comision_pct_snap`) y sale de lo que paga
  el comprador.
- Tipos: **2x1** y **porcentaje** (1 a 90 %). Cuenta el **día de la
  función**, a la hora del recinto, no el de la compra; opcionalmente
  "solo entre fechas"; en todos los eventos o en algunos eventos y zonas;
  en línea, en taquilla o en las dos. Por omisión solo el boleto de adulto.
- **2x1 en pares, sin impares**: por zona, del más caro al más barato, el
  más barato de cada par va sin costo; si sobra uno se pide otro lugar
  (`par_incompleto`), salvo que sea el último lugar libre de la zona: ese
  paga completo.
- **No se suman**: gana la de mayor descuento.
- **Una sola calculadora** (`app/services/promociones.py`) para el cobro en
  línea, la taquilla y lo que ve el comprador. Lo que decide es el
  servidor al iniciar el pago; la vista del front es solo un aviso.
- **Sin red** la taquilla aplica la promoción que bajó con su reserva, y al
  subir la venta se respeta lo que cobró (`promocion_id`), aunque la hayan
  apagado: el dinero ya se recibió.
- Lo vendido se congela: `orders.promocion_id` y `promocion_snap`, y el
  descuento por boleto (`order_items.descuento`). La orden sigue
  cuadrando como factura: subtotal + IVA − descuento = total, con el IVA
  calculado sobre lo que realmente se cobra.
- Devoluciones: solo por cancelación o reagenda, y se regresa lo pagado
  (ya con descuento).
- Una promoción usada no se borra: se apaga.

## Lo que queda

- Que el contador de PSD revise el desglose de la factura con descuento
  antes de la primera venta con promoción.
- Cupones con código (`discount_codes`) siguen sin usarse.
