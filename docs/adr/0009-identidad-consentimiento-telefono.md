# 0009 — UUID públicos, consentimiento como bitácora y teléfonos en E.164

Fecha: 2026-10-14 · Estado: aceptado

## Contexto

El documento de compatibilidad con el CRM marcó tres cosas como "antes
de la primera venta". Hoy cambiarlas es una migración sobre datos de
prueba; con compradores reales sería limpiar datos vivos.

## Decisión 1: `uuid` hacia fuera, `id` por dentro

`clients`, `events`, `performances`, `customers`, `orders` y `tickets`
tienen una columna `uuid` única, que pone la base (`gen_random_uuid()`)
aunque la fila llegue por fuera del ORM. Las respuestas la incluyen.

**Por qué.** Un `id` autoincremental dice cuántas órdenes llevamos, se
adivina el siguiente y choca cuando dos sistemas se juntan. El CRM, los
webhooks y el QR de un boleto guardan el `uuid`. El `id` sigue siendo la
llave interna: los joins con `BIGINT` son más baratos y no hace falta
tocar ninguna llave foránea.

## Decisión 2: consentimiento como registro, por finalidad

Se elimina `customers.acepta_marketing`. Tabla `consents`, solo de
agregar: `finalidad` (`operacion` | `marketing`), `otorgado`, `origen`
(checkout_web, formulario_pista, correo_baja…), `aviso_version`,
`order_id` opcional y `registrado_en`. Lo vigente es el registro más
reciente por finalidad; el empate dentro de una misma transacción lo
rompe el `id`.

**Por qué.** Ante un "yo nunca acepté", una casilla en verdadero no
prueba nada; un registro con fecha, origen y versión del aviso sí. Las
finalidades van separadas porque aceptar que te avisen de tu función no
es aceptar promociones, y pedirlas con una sola casilla invalida la de
marketing. `ConsentimientoService.registrar` acepta una finalidad por
llamada a propósito.

**Migración.** Quien tenía `acepta_marketing = true` queda con un
registro `marketing` con origen `migracion` y aviso `anterior`, para que
se distinga de un consentimiento dado con el aviso vigente. El
`downgrade` reconstruye la columna desde el registro más reciente.

## Decisión 3: teléfonos en E.164

Todo teléfono se guarda como `+<país><número>` (`+525512345678`) con
`normalizar_telefono` (librería `phonenumbers`), y la base lo exige con
un `CHECK`. Sin código de país se asume México. Antes de validar se
quitan los prefijos que México eliminó en 2019 y la gente sigue
escribiendo: `044`, `045`, `01` y el `1` de `+52 1`. Un número que no es
válido para su país se rechaza con `422 telefono_invalido`.

**Por qué.** "55 1234 5678" y "+52 (55) 1234-5678" son el mismo número;
guardados como llegan son dos contactos para el CRM y dos mensajes de
WhatsApp para la misma persona.

**Migración.** Normaliza los teléfonos existentes con la misma regla
(copiada en la migración para que no dependa del código de la app). Si
alguno no se puede normalizar, la migración falla completa y lista los
`customer_id` a corregir: no se pierde un teléfono en silencio.

## Pendiente

Aún no hay endpoint que cree compradores: el checkout (#17) será el
primero en usar `normalizar_telefono` y `ConsentimientoService`, con las
dos casillas separadas.
