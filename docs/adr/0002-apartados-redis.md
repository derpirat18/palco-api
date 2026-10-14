# 0002 — Apartado de butacas en Redis con respaldo en Postgres

Fecha: 2026-10-08 · Estado: aceptado

## Contexto

Al abrir la venta de una función, miles de personas pueden competir por
las mismas butacas. Vender dos veces el mismo lugar es el peor fallo
posible: se descubre en la puerta del teatro, frente al público.

## Decisión

Tres capas:

1. Apartado atómico en Redis con scripts Lua y expiración.
2. `UPDATE` condicional sobre `performance_seats`, que solo avanza si la
   butaca sigue apartada por ese `hold_id`.
3. Índice único parcial en `order_items`.

## Por qué no `SELECT FOR UPDATE`

Con mil transacciones esperando el mismo candado, el pool de conexiones
se agota y la aplicación entera deja de responder. Redis es monohilo:
serializa las peticiones por naturaleza, en microsegundos, sin bloquear
nada más.

## Por qué no Redlock

Añade complejidad y es discutido. Una instancia de Redis con réplica
basta, porque la corrección final la garantiza Postgres.

## Consecuencias

Si Redis se cae se pierden los apartados en vuelo y algunas butacas
vuelven a aparecer libres. Molesto, no catastrófico: la doble venta sigue
siendo imposible.

El pool de conexiones de Redis hay que dimensionarlo, y tiene que ser
`BlockingConnectionPool`. El pool normal levanta `MaxConnectionsError` en
cuanto se agota: bajo una avalancha de compra eso convierte una espera de
milisegundos en un error para el comprador. El bloqueante forma a quien
llega y atiende en orden. Está en `REDIS_MAX_CONNECTIONS` y
`REDIS_POOL_TIMEOUT`.

Agrandar el pool no es la solución. Mil conexiones simultáneas no son
realistas en producción y en Windows ni siquiera se abren: la prueba de
concurrencia usa un pool de 50 y mil corrutinas haciendo fila, que es
exactamente lo que pasa el día que abre una venta.
