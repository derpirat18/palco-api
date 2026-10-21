# 0008 — No dejar butacas sueltas, con vecindad por posición

Fecha: 2026-10-14 · Estado: aceptado, ampliado por el ADR 0020 (lugares juntos en la misma fila)

## Contexto

Una butaca sola entre dos ocupadas casi nunca se vende. El prototipo
pedía "lugares consecutivos en la misma fila", pero eso le impide a una
familia de seis sentarse en dos filas de tres, y no evita el problema
real: el comprador puede elegir lugares juntos y aun así dejar uno suelto
contra el pasillo.

## Decisión

Regla **sin butacas sueltas**: una selección no puede dejar una sola
butaca libre entre lo que se aparta y algo ocupado, un pasillo o el
final de la fila. Se permite cuando es inevitable (tres libres juntas y
una compra de dos). Lo que la persona ya tenía apartado cuenta como
parte de su selección.

Se valida en el servidor, dentro del apartado, después de disponibilidad
y tope y antes de tocar Redis. Responde `409 deja_butaca_suelta` con el
`seat_id` que quedaría solo, para que el front lo señale en el mapa.

## La vecindad sale de la posición, no del número

Dentro de una zona y una fila, las butacas ordenadas por `x` son vecinas
mientras la distancia sea la normal de la zona (mediana). Un salto mayor
a 1.5 veces esa distancia es un pasillo.

**Por qué.** La luneta mexicana va en nones y pares: la 1, la 3 y la 5
están juntas y la 2 del otro lado del pasillo. Hay numeración invertida,
pasillos a media fila (`omitir`) y mapas importados con cualquier
numeración. Lo único que todos tienen en común es la posición. Probado
con el mapa del generador (`test_teatro_mexicano_del_generador`).

## Lo que no es

Es una regla de negocio, no una garantía de integridad. Dos compras
simultáneas en butacas vecinas pueden, entre las dos, dejar una suelta;
cuesta más evitarlo (serializar compras por fila) de lo que vale.

## Lo que queda

- **El front debería aplicar la misma regla** al seleccionar, para no
  dejar elegir algo que el servidor va a rechazar. El servidor manda.
- **Liberar no la valida.** Si alguien suelta la butaca de en medio de su
  propia selección, puede dejar una suelta. Hay que revisarla otra vez al
  iniciar el cobro.
- **Admisión general** (ADR 0005) no tiene posiciones con significado: la
  regla tendrá que saltarse esas zonas cuando existan.
