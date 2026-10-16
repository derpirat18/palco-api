# 0017 — Puerta del turno: el servidor exige turno para empezar a comprar

Fecha: 2026-10-16 · Estado: aceptado (apagada hasta tener la sala)

## Contexto

PSD pidió una sala de espera virtual automática (B17) para las ventas
grandes: alrededor de 10 funciones de 1,500 lugares. Una condición de su
especificación: **el backend debe verificar que la persona tenga un turno
válido antes de permitirle iniciar una reserva**; mostrar la fila solo en
pantalla no basta, porque alguien podría llamar a la API directo.

La sala completa (fila, ritmo de entrada, activación automática,
pantalla, monitoreo) necesita pruebas de carga en el sandbox para fijar
sus parámetros, y eso llega después del piloto. Esta decisión cubre solo
la mitad de servidor, para que el front se construya desde el día uno
sabiendo responder "necesitas turno" y la sala se encienda después sin
rehacerlo.

## Decisión

**El pase.** Cuando a alguien le toque su turno, la sala le entregará un
pase firmado con `SECRET_KEY` y un prefijo de dominio propio:

```
T1.<evento uuid>.<id del pase>.<vence, epoch en base 36>.<firma>
```

Es de un evento y vence. Sin la llave no se fabrica, y alterarlo (por
ejemplo, alargar la vigencia) rompe la firma.

**La puerta.** `POST /performances/{id}/apartados` lee el pase del
encabezado `X-Palco-Turno`:

| Situación | Resultado |
|---|---|
| `SALA_DE_ESPERA_HABILITADA=false` | Pasa, sin consultar nada |
| La sala del evento no está activa (`sala:activa:{evento_uuid}` en Redis) | Pasa |
| El `hold_id` ya tenía butacas | Pasa: quien ya compraba sigue hasta su vencimiento normal |
| Sin pase, alterado o de otro evento | 403 `turno_requerido` |
| Pase vencido | 403 `turno_vencido` |
| El pase ya abrió 2 apartados distintos | 403 `turno_usado` |
| Pase válido | Pasa |

El mensaje de `turno_requerido` es el que propuso PSD: *"Hay muchas
personas intentando comprar. Estamos dando acceso por turnos para
mantener una compra estable. Estar en la sala de espera no garantiza
boletos; la disponibilidad se confirma al seleccionar tus lugares."*

Solo se exige pase para **empezar** un apartado. Sumar o quitar butacas,
pagar y consultar no lo piden: ya pasaron la puerta.

**Dos apartados por pase**: alcanza para quien soltó el suyo y volvió a
elegir, y compartir el pase con un bot no le sirve de mucho.

**Sin Redis se deja pasar**, igual que los límites (ADR 0016): el
apartado depende de Redis y fallará por su cuenta.

## Lo que queda para la sala (B17)

- La fila: posición real, ritmo de entrada medido, sin tiempos
  inventados; el mismo lugar ante recargas y varias pestañas.
- Quién y cuándo enciende `sala:activa`: activación y apagado
  automáticos con margen, con umbrales medidos en pruebas de carga.
- La vigencia del pase y cuántos entran por minuto: de las pruebas de
  carga, no a ojo.
- Avisar a la fila si se agota la función, y la pantalla de monitoreo y
  las alertas para PSD.
