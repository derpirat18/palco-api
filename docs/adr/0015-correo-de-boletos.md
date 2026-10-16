# 0015 — Correo con los boletos, desde la bandeja de salida

Fecha: 2026-10-16 · Estado: aceptado

## Contexto

Al pagar, el comprador necesita sus boletos en su correo: es lo primero
que busca, y es su respaldo si pierde la pantalla de confirmación. El
checkout ya escribe el hecho `orden.pagada` en `outbox_events` en la
misma transacción que vende (ADR 0010), pero nadie lo leía.

Mandar el correo dentro del cobro sería un error: si el proveedor tarda
o se cae, la compra se detiene o se pierde el correo. Y el proveedor de
producción (Amazon SES) se da de alta después del código.

## Decisión

**El correo lo manda la bandeja, nunca el cobro.** Un proceso aparte
(`python -m app.workers.bandeja`, `.\make.ps1 bandeja`) da una vuelta
cada 5 segundos:

- Toma un hecho pendiente con `FOR UPDATE SKIP LOCKED`: dos procesos a
  la vez se reparten el trabajo sin repetirlo y sin esperarse.
- Atiende solo los tipos que conoce (hoy `orden.pagada`). Los demás
  quedan intactos para el publicador que los necesite.
- Si falla, anota el error y no reintenta antes de `reintentar_en`,
  con espera creciente: 30 s, 1 min, 2 min… hasta 1 hora. Tras 8
  intentos se detiene y deja un error en el registro (`palco.bandeja`)
  para la alerta a PSD. El comprador sigue teniendo "Tus boletos".
- Entrega **al menos una vez**: si el correo sale y la base se cae
  antes de marcarlo, saldrá otra vez. Un correo repetido es preferible
  a uno perdido.

**El correo se arma en el momento de enviarlo**, desde la base: el
payload del hecho no lleva datos personales (a propósito) y así refleja
el estado real. Si la orden se reembolsó antes, no se manda.

**Contenido:** evento, fecha y hora a la hora del recinto, apertura de
puertas, lugar, folio, un QR por lugar con su código legible, el
desglose Subtotal + IVA (tasa) = Total, y el enlace
`{FRONT_URL}/boletos/{uuid de la orden}`. Texto plano y HTML; los QR
van pegados como imágenes `cid:` porque Gmail y Outlook bloquean las
`data:`, y una imagen remota no se vería sin señal en la fila.

**Un solo cartero, SMTP**, detrás de la interfaz `Cartero`:

| Entorno | Servidor | Cifrado |
|---|---|---|
| Local | Mailpit (Docker), puerto 1025; se ve en http://localhost:8025 | No |
| Producción | Amazon SES por SMTP, puerto 587, usuario SMTP de SES | STARTTLS obligatorio fuera de `local` |

Fechas en español escritas a mano, sin `locale`: depende del sistema y
un servidor sin `es_MX` escribiría en inglés. `tzdata` va como
dependencia porque Windows no trae la base de zonas horarias.

## Lo que queda fuera

- Verificar el dominio de PSD en SES (SPF, DKIM, DMARC) y salir del
  modo de prueba de SES. Lunes 19, con la cuenta de AWS.
- Rebotes y quejas de SES: hoy no se escuchan.
- Reenviar el correo a petición del comprador o de PSD.
- La alerta en sí (A4): hoy el abandono queda en el registro.
