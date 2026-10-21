# 0021 — El organizador sube su evento; publicarlo lo hace PSD

Fecha: 2026-10-20 · Estado: aceptado · Decisión de PSD (Nadia)

## Contexto

El MVP pedía que solo PSD cargara eventos. Palco ya deja que el
organizador dé de alta sus eventos, funciones y precios, y PSD decidió
quedarse con eso: con una gira de 8 ciudades y 18 funciones, cada alta
que hace el organizador es una que PSD no captura. Pero entonces el
organizador también podía publicar sin que nadie revisara.

## Decisión

El organizador crea y edita su evento. **Cambiar el estado del evento
(publicarlo o retirarlo) lo hace solo `admin_psd`**, después de revisarlo.
Si el organizador lo intenta, la API responde `403 permiso_insuficiente`
y le pide avisar a PSD. Así queda de forma permanente, no solo para el
arranque.

Ya publicado, el organizador puede seguir corrigiendo textos de su evento.

## Lo que queda

- Un aviso "listo para revisión" para que PSD sepa qué revisar, en lugar
  de que el organizador avise por fuera.
- Qué cambios de un evento ya publicado necesitan otra revisión de PSD
  (precios, fechas): es O1 del tablero.
