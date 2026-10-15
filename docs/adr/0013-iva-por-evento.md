# 0013 — IVA por evento: la tasa la escribe el organizador

Fecha: 2026-10-14 · Estado: aceptado

## Contexto

El IVA de un boleto no es uno solo. La Ley del IVA (art. 15) exenta el
boleto de entrada a espectáculos públicos, salvo teatro y circo; y aun
esos pueden quedar exentos si el organizador paga el impuesto local
sobre espectáculos donde hay convenio con el SAT (art. 41). En la
frontera la tasa es 8 %. Cada organizador lo resuelve con su contador, y
Palco no puede adivinarlo.

Además, la Ley Federal de Protección al Consumidor (art. 7 bis) pide
exhibir el monto total a pagar, con impuestos incluidos.

## Decisión

Cada **evento** tiene dos datos que decide el organizador:

- `iva_pct`: la tasa, **la escribe él** (16, 8, 0 si está exento, o la
  que le indique su contador). Entre 0 y 100, con dos decimales.
- `precios_incluyen_iva`: si los precios que captura ya traen el IVA, o
  si son "más IVA" y el sistema se lo suma.

**El comprador ve y paga siempre el precio con IVA.** La opción solo
cambia de dónde sale ese precio:

| Captura | Opción | Paga el comprador | IVA desglosado |
|---|---|---|---|
| $850 | IVA incluido, 16 % | $850.00 | $117.24 |
| $850 | Más IVA, 16 % | $986.00 | $136.00 |
| $850 | Exento (0 %) | $850.00 | $0.00 |

Por omisión: 16 % e IVA incluido. Así un evento que nadie configuró no le
cambia el precio a nadie.

## Una sola regla, en un solo lugar

`venta.Iva` convierte el precio capturado en precio al público (redondeo
comercial al centavo) y calcula el IVA contenido en un precio. Los tres
caminos por los que un precio capturado llega al comprador la usan:

1. Generar el inventario y aplicar precios de zona (`_precios_por_zona`).
2. El precio por excepción de butacas sueltas (`fijar_precio_butacas`).
3. El "desde" de la página pública cuando aún no hay inventario.

Los precios de zona se guardan como los capturó el organizador; en
`performance_seats.precio_final` queda lo que paga el comprador.

## La orden desglosa y congela

La orden se lee como una factura: **subtotal (sin IVA) + IVA − descuento
= total**, y la base lo exige (`ck_orders_total_cuadra`). El total es lo
que paga el comprador y lo que cobra Stripe: la suma exacta de los
precios al público. El IVA se desglosa partida por partida y el subtotal
es el total menos ese IVA.

| Modo, 2 × $850 al 16 % | Subtotal | IVA | Total |
|---|---|---|---|
| IVA incluido | 1,465.52 | 234.48 | 1,700.00 |
| Más IVA | 1,700.00 | 272.00 | 1,972.00 |

Con "más IVA", el subtotal es exactamente lo que capturó el organizador.

Cada partida guarda su precio al público y el IVA que contiene; la orden
guarda la suma (`orders.iva`) y la tasa del momento
(`orders.iva_pct_snap`). Si el organizador corrige la tasa después, lo
vendido no se mueve: la factura y la liquidación usan lo congelado. La
base exige que el IVA no sea negativo ni mayor que el importe.

Cuando existan códigos de descuento, el descuento tendrá que reducir la
base y su IVA juntos; hoy siempre es cero.

## Qué se puede cambiar y cuándo

- Con **IVA incluido**, la tasa se puede corregir siempre: el precio al
  público no cambia, solo el desglose de las compras siguientes.
- Pasar de incluido a "más IVA" o al revés, o cambiar la tasa estando en
  "más IVA", **cambia lo que paga el comprador**. Eso solo se permite
  antes de generar el inventario de cualquier función del evento:
  después responde `409 conflicto_de_catalogo`.

## Pendiente (con el contador)

- **IVA de la comisión de PSD.** La comisión es un servicio de PSD al
  organizador y lleva su propio IVA. Va en la liquidación y en la
  factura de PSD.
- **Base de la comisión.** Si el porcentaje de PSD se calcula sobre el
  precio con IVA o sin él. El sistema ya tiene los dos datos por orden.
- **IVA como vendedor de registro (ADR 0003).** Quién declara el IVA del
  boleto cuando el cobro es a nombre de PSD.
