# 0006 — Materialización del inventario: cuándo, cómo y qué nunca se rehace

Fecha: 2026-10-13 · Estado: aceptado

## Contexto

Una función programada no puede vender nada hasta que exista una fila de
`performance_seats` por cada butaca del mapa. Esas filas son las que el
índice único y el `UPDATE` condicional protegen: son el piso sobre el que
descansa la garantía de que una butaca no se vende dos veces.

Tres preguntas quedaron abiertas al diseñar el catálogo.

## Decisión 1 — La materialización es explícita, no un efecto de publicar

Es su propia operación (`POST /performances/{id}/inventario`), y abrir la
venta exige que ya exista.

**Por qué.** Publicar un evento con treinta funciones de 1,800 butacas
serían 54,000 filas en una sola petición: lenta, todo o nada, y sin forma
de reintentar la que falló. Separadas, cada función se materializa,
verifica y abre cuando le toca.

Además son cosas distintas: publicar es visibilidad pública, materializar
es preparar inventario. Un evento puede estar publicado con funciones que
todavía no abren venta.

## Decisión 2 — Idempotencia por `UPDATE` condicional, no por consulta previa

```sql
UPDATE performances SET inventario_generado = true
WHERE id = :id AND inventario_generado = false
```

Cero filas afectadas significa que otra petición ganó.

**Por qué.** Consultar la bandera y después escribir deja un hueco entre
las dos sentencias donde cabe otra petición: las dos leen `false`, las dos
creen ganar, y se materializa dos veces. Con el `UPDATE` condicional la
carrera la resuelve Postgres sobre la misma fila, y la perdedora se entera
porque no afectó nada.

Rehacer la materialización no es un desperdicio: es un **desastre**.
Reescribiría el estado de butacas ya vendidas y las pondría de vuelta a la
venta, con boletos emitidos a nombre de alguien más.

Cubierto por `test_materializar_dos_veces_a_la_vez`, que confirma de
verdad en dos sesiones distintas. No se debilita ni se marca como skip.

## Decisión 3 — Ninguna butaca nace sin precio

Materializar falla si alguna zona con butacas no tiene precio activo para
esa función, y dice cuáles.

**Por qué.** `precio_final` es obligatorio en la base. Sin esta validación
habría que inventar un valor, y el único disponible sería cero: butacas a
la venta regaladas, descubiertas cuando alguien ya compró.

## Lo que el mapa puede y no puede hacer después

**Agregar butacas**: se sincroniza con una operación explícita que **solo
inserta**. Queda en la bitácora butaca por butaca, al revés que la
materialización inicial, porque agregar butacas a una función viva es
anómalo y hay que poder explicarlo después.

**Quitar butacas**: imposible, y no por código. La llave foránea de
`performance_seats` hacia `seats` es `RESTRICT`. Borrar una butaca
vendida no es un caso a manejar: es una operación que la base rechaza.

## Precios por excepción

Al materializar, cada butaca toma el precio de su zona con
`precio_personalizado = false`. Fijar el precio de butacas concretas la
marca en `true`, y volver a aplicar los precios de zona respeta esas
butacas. Sin la bandera, el primer cambio de precio de zona borraría todas
las excepciones en silencio.

Ni el repricing ni los cambios de estado tocan butacas vendidas o en
cortesía. Deshacer una venta es cancelar la orden, con su reembolso y su
boleto anulado; no es editar una fila.

## Lo que quedó fuera

- La bitácora **no** registra la materialización inicial butaca por
  butaca. Existe para explicar por qué una butaca cambió de estado, y el
  estado inicial es uniforme y se deduce del mapa. Registrarlo duplicaría
  la escritura de miles de filas sin responder ninguna pregunta.
- Congelar el mapa cuando tiene inventario: `CatalogService.mapa_en_uso`
  existe pero todavía no hay endpoint que edite un mapa, así que no hay
  dónde aplicarlo. Hay que hacerlo cuando ese endpoint exista.
- Operaciones masivas por zona. Hoy los cambios de estado van por lista
  de identificadores, con tope de 2,000 por petición. "Bloquear toda la
  zona de prensa" va a hacer falta y no está.
