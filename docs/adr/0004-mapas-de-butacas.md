# 0004 — Mapas de butacas por generador y por archivo, sin editor visual

Fecha: 2026-10-13 · Estado: aceptado

## Contexto

Cada recinto necesita su mapa de butacas antes de poder vender: zonas,
filas, numeración y la posición de cada butaca para dibujar el SVG. Un
teatro mediano son entre 600 y 1,800 butacas. La forma obvia de
capturarlas es un editor visual de arrastrar y soltar.

## Decisión

Dos caminos, ninguno visual:

1. **Generador por bloques.** Se describe el recinto como rectángulos de
   filas y butacas: zona, rango de filas, butacas por fila, numeración,
   origen, paso y curvatura. Soporta lo que de verdad aparece en teatros
   mexicanos: luneta partida en nones y pares por el pasillo central,
   filas con distinta cantidad de butacas, pasillos a media fila
   (`omitir`), butacas que existen pero no se venden (`kill`) y
   numeración invertida.
2. **Importación de archivo.** Una línea por butaca: zona, fila, número,
   x, y, kill. Para recintos con forma que no cabe en rectángulos.

Los dos producen exactamente lo mismo: filas en `zones` y `seats`. El
viewport se puede autoajustar al contenido después de importar.

El editor visual queda fuera del alcance inicial.

## Por qué

El editor visual es de dos a tres semanas de front y arrastra
mantenimiento indefinido, y resuelve un problema que no tenemos: los
recintos se capturan una vez y no cambian. Atayde son tres plazas. Con
el generador, un teatro queda listo en minutos; con la importación,
cualquier forma rara sale de un Excel que el organizador ya tiene.

Capturar 1,800 butacas a mano en una pantalla tampoco es un buen plan:
el generador es más rápido y comete menos errores que una persona
arrastrando cuadritos.

Si el día que PSD tenga veinte recintos la captura se vuelve el cuello
de botella, se construye el editor encima de este mismo modelo de datos.
No hay nada que deshacer para llegar ahí.

## Consecuencias

- La captura inicial de cada recinto la hace PSD, no el organizador. Los
  endpoints de mapas son `admin_psd` únicamente.
- Un mapa con funciones programadas no se puede modificar
  (`mapa_en_uso`): cambiarlo movería butacas ya vendidas.
- Hace falta documentar el formato del archivo para el equipo de
  operación, con un ejemplo por tipo de recinto.
