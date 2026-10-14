# 0005 — Admisión general como lugares sin asignar, no como contador de cupo

Fecha: 2026-10-13 · Estado: aceptado, pendiente de construir

## Contexto

Atayde opera también pistas de hielo, donde no hay butacas: se vende
acceso. Aparecerán más casos así (ferias, conciertos de pie, galerías de
general en teatros). El modelo actual asume que toda venta apunta a una
butaca concreta.

## Decisión

Una zona puede marcarse como admisión general, con un cupo. Al generar el
mapa se crean esas N entradas como filas de `seats` igual que cualquier
butaca, pero sin significado posicional: el comprador nunca ve un número
y el boleto dice solo el nombre de la zona.

El comprador pide una cantidad, no lugares. El servidor toma N
disponibles de esa zona y las aparta con el mecanismo de siempre.

**No** se implementa un contador de cupo que se decrementa por venta.

## Por qué

Todo el sistema descansa en una sola invariante: una butaca, una venta.
La sostienen tres capas —apartado atómico en Redis, `UPDATE` condicional
sobre `performance_seats`, índice único parcial en `order_items`— y está
probada contra mil peticiones simultáneas por el mismo lugar.

Un contador de cupo sería una **segunda** invariante, con su propio
mecanismo de apartado, su propia prueba de concurrencia y su propio modo
de fallar. Dos rutas distintas hacia la misma garantía significan dos
auditorías y, el día que algo salga mal, la duda de cuál falló.

Con lugares sin asignar la garantía es literalmente la misma: cada
entrada es una fila con su índice único. El código nuevo se reduce a
"apártame N cualesquiera de esta zona" y a dibujar un bloque en vez de
sillas.

El costo es N filas por función en lugar de un entero. Para los aforos
que maneja esto —miles, no millones— es irrelevante: el generador ya
tiene tope de 50,000 butacas por mapa.

## Consecuencias

- La materialización de inventario **no cambia**: las entradas de general
  se materializan como cualquier butaca. Por eso este módulo puede ir
  después del de inventario sin retrabajo.
- `zones` necesita dos columnas: la bandera y el cupo. Migración chica.
- El front dibuja un bloque con el cupo disponible, no butacas.
- Un recinto mixto —luneta numerada y galería general— sale gratis: la
  decisión es por zona, no por evento.

## El caso de las pistas de hielo

No es boletaje: es captura de datos con la reserva como pretexto. El
valor que PSD aporta a quien renta la pista es saber quién vino, cuándo y
cuánto se quedó. Eso impone requisitos que el resto de la plataforma no
tenía:

**La entrada a veces se cobra y a veces no.** Una orden con total cero no
puede pasar por la pasarela: sin autorización, sin captura, sin webhook.
Se emite el boleto y ya. Hay que contemplarlo **desde el diseño del flujo
de compra**, no parchearlo cuando todo asuma que siempre hay un cobro.

**La renta de patines se hace en taquilla, no en línea.** Se escanea el
boleto al entregar los patines y otra vez al devolverlos. Esto resuelve
el problema del escaneo de salida: nadie se va sin sus zapatos, asi que
la devolucion es un punto de paso obligatorio y el tiempo de permanencia
queda completo. Tambien evita construir venta de productos con inventario
por talla en linea. Deja tres datos: permanencia real, demanda por talla
y hora, e ingreso por visitante si se cobra.

**Entrada y salida son eventos distintos sobre el mismo boleto.** Hoy el
segundo escaneo se rechaza como reutilizacion. El de salida no puede
rechazarse asi.

**La credencial debe ser abstracta.** Hoy se dibuja como QR; la intencion
es migrar a RFID, que en una pista es operativamente superior porque la
gente trae guantes mojados y el telefono guardado. Si la identidad del
boleto se modela como credencial y no como "un QR", cambiar el portador
es un campo y no un rediseño.

## Datos personales

El producto que se vende es el **agregado**, no la lista. Visitantes por
franja, codigos postales, tasa de repeticion, permanencia, tallas: nada
identificable, y es lo que un patrocinador realmente ocupa. Transferir
datos identificados a un tercero exige decirlo en el aviso de privacidad
y consentimiento para esa finalidad; es otro producto y otro riesgo.

Para que eso funcione, el modelo separa identidad de conducta: la persona
se guarda una vez, las visitas son eventos aparte, y los reportes salen
de los eventos cruzados con atributos gruesos sin tocar el nombre.

Reglas del formulario:

- Nombre, un solo medio de contacto, codigo postal, rango de edad.
- Acompañantes por cantidad y rango de edad. **De los menores se cuentan
  cabezas, no se capturan datos**: evita el consentimiento parental y la
  carga que trae, sin perder la mezcla de edades que interesa.
- Dos consentimientos **separados**: uno para operar la reserva, otro
  para marketing. Juntarlos invalida el segundo.
- Nada de CURP, RFC ni direccion.
- **Ningun campo de condicion medica.** Es dato sensible. Si hace falta
  deslinde de responsabilidad, va firmado en taquilla, no en la base.

Falta validar el aviso de privacidad con abogado, y definir por contrato
de quien son los datos: del cliente que renta la pista o de PSD. Eso no
es tramite, es arquitectura — decide si viven bajo un client_id o si se
agregan entre clientes.

## Lo que falta definir

1. **Acceso libre por dia contra turnos por horario.** Lo pedido es
   acceso libre. Dos problemas: con un solo tope diario nada impide que
   todos lleguen a la misma hora, y el escaneo de entrada queda como
   unico dato temporal. Con turnos se conoce la hora antes de que
   lleguen, y salen ocupacion por franja, no-show y puntualidad sin
   esfuerzo. En el modelo, un turno es una funcion: cero codigo nuevo.
2. **Si se permite reentrada**, mas alla del par entrada/salida.
3. **Si la renta de patines se cobra**, y con que forma de pago en
   taquilla.

Sin fecha comprometida al momento de escribir esto.
