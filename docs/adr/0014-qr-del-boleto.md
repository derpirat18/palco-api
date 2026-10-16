# 0014 — El QR del boleto: firmado, opaco y versionado

Fecha: 2026-10-16 · Estado: aceptado

## Contexto

El checkout ya emite un boleto por lugar, con un `codigo` legible
(`7K2QX-M9P4D`) y una `semilla` de 32 bytes. Faltaba lo que el comprador
lleva a la puerta: un QR que la puerta pueda confiar sin consultar a
nadie más que a la base, y que no se pueda fabricar.

Para el piloto del 26 de octubre basta un QR fijo. El QR que cambia cada
pocos segundos (contra la reventa por captura de pantalla) se enciende
después con la `semilla`.

## Decisión

**Contenido del QR**, 50 caracteres:

```
PAL1.<uuid del boleto en base64url, 22>.<firma en base64url, 22>
```

- La firma es HMAC-SHA256 con `TICKET_SIGNING_KEY` sobre un prefijo de
  dominio, la versión y los 16 bytes del uuid; se guardan los primeros
  16 bytes (128 bits).
- Sin la llave no hay QR válido. La firma de un boleto no sirve para
  otro.
- El QR no lleva datos personales ni de la función: es opaco. La puerta
  busca el boleto por su uuid y ahí ve todo lo demás.
- `PAL1` es la versión. El QR rotatorio será `PAL2`, y los `PAL1` ya
  emitidos se seguirán leyendo.
- La comparación de la firma es de tiempo constante
  (`hmac.compare_digest`).

**"Tus boletos"**: `GET /ordenes/{uuid}/boletos`.

- Se autoriza con el uuid de la orden, como `GET /ordenes/{uuid}`: no se
  adivina y solo lo tiene quien compró (pantalla de confirmación y
  correo).
- Trae evento, función, recinto (con su zona horaria) y, por boleto:
  uuid, código, zona, asiento, estado y QR.
- **Solo lleva QR un boleto `valido` de una orden `pagada`.** Uno
  cancelado o usado, o los de una orden reembolsada, vienen con `qr`
  nulo: no deben verse como algo que todavía sirve.
- Orden aún sin pagar: lista vacía. El front explica el motivo con el
  `estado`.
- Sin correo ni nombre del comprador en la respuesta.

**El QR se dibuja en el front.** La API entrega el texto; el front lo
convierte en imagen. El correo de confirmación (M6) generará su propia
imagen del mismo texto.

## Lo que queda fuera

- **Escáner en puerta**: verificar firma, función y estado, y marcar
  `usado` con un `UPDATE` condicional, para que dos escaneos a la vez no
  dejen entrar dos veces. Antes del 5 de noviembre.
- **QR rotatorio** con la `semilla`.
- **Límite de consultas** a esta vista por IP (B1 del plan).
- **Rotación de `TICKET_SIGNING_KEY`**: cambiarla invalida todos los QR
  emitidos. Si un día hace falta, se agrega un identificador de llave a
  la versión del QR.
