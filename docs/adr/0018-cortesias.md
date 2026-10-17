# 0018 — Cortesías: una orden de $0 sobre el mismo inventario

Fecha: 2026-10-16 · Estado: aceptado

## Contexto

Los organizadores regalan boletos: prensa, patrocinadores, familia del
elenco. Atayde lo hará desde la primera función (5 de noviembre). Una
cortesía mal resuelta es un lugar vendido dos veces, o un invitado sin
boleto en la puerta.

## Decisión

**Una cortesía es una orden normal** con total $0 y `canal =
'cortesia'`. No es un atajo aparte:

- Sus butacas pasan a `cortesia` con el mismo `UPDATE` condicional que el
  panel, y sus partidas entran al índice único de `order_items` (capa 3).
  Lo que es cortesía no se vende y lo vendido no se regala.
- Se toman de butacas `disponible`, `bloqueado` (lo usual: el organizador
  reserva lugares para invitados) o con un apartado vencido. Nunca de
  vendidas, cortesías, apartados vigentes ni `kill`.
- Se pueden dar antes de que abra la venta; no en funciones canceladas,
  realizadas o sin inventario. Hasta 20 lugares por cortesía.
- La orden nace **pagada y "capturada"**: no hay nada que cobrar, y sin
  `capturada_en` el barrido intentaría cobrarla en Stripe. Hay una prueba
  que lo vigila.
- Sus boletos son boletos de verdad, con QR (ADR 0014), y llegan por
  correo desde la bandeja (ADR 0015) con su propio texto: "Te enviamos
  boletos de cortesía", sin resumen de pago. Va el hecho
  `cortesia.emitida`, no `orden.pagada`: no es una venta.
- La tabla `cortesias` guarda lo que la orden no dice: motivo, quién la
  otorgó y, si se revocó, quién y cuándo. Cada butaca deja su movimiento
  en `inventory_moves` con su estado previo.

**Revocar** cancela los boletos, anula las partidas y devuelve cada butaca
al estado de donde salió (un lugar bloqueado vuelve a bloqueado). No se
puede si algún boleto ya entró. Repetirlo no hace nada.

**Quién:** el organizador dueño del evento, o PSD. Lo ajeno responde igual
que lo inexistente.

## Rutas

| Ruta | Qué |
|---|---|
| `POST /performances/{id}/cortesias` | Otorgar: `seat_ids`, `email`, `nombre`, `motivo` |
| `GET /performances/{id}/cortesias` | Las de la función, vigentes y revocadas |
| `POST /cortesias/{uuid}/revocar` | Revocar |

## Lo que queda fuera

- Un tope de cortesías por función o por organizador, si PSD lo quiere.
- Cortesías de admisión general (con G1).
- En las liquidaciones, las cortesías suman $0 y no pagan comisión; el
  reporte las separa por `canal`.
