# Mapas de recintos en SVG

El panel lee un SVG de dos formas (`app/services/mapas_archivo.py`). La
primera es la buena: el mapa llega con sus zonas ya nombradas y sin
adivinar nada.

## 1. Con la convención de Palco (recomendada)

Para quien dibuja el plano en Illustrator, Inkscape o Figma:

| Qué | Cómo se nombra | Ejemplo |
|---|---|---|
| Cada **zona** | Una **capa** (o grupo) con el nombre de la zona | `Luneta`, `Palco Oro`, `General` |
| Cada **butaca** | Una figura (círculo, rectángulo o forma) dentro de su zona, nombrada **FILA-NÚMERO** | `A-1`, `A-2`, `AA-15`, `P1-4` (palco 1, lugar 4) |
| El **escenario**, pasillos, letreros | Cualquier otra capa: se ignora | `Escenario`, `Textos` |

Reglas:

- La fila son letras (`A`, `AA`, `ZZ`) y, si hace falta, un número (`P1` para
  palcos). El número de butaca va después de un guion.
- El color de la butaca es el color de su zona en la vista previa (se puede
  cambiar en el panel).
- Una butaca puede ser un grupo (figura + su número escrito): basta con
  nombrar el grupo `A-12`.
- Lo que existe pero no se vende se marca después en el panel ("Se vende"), o
  en el SVG con `data-kill="si"`.

Cómo queda al exportar:

- **Illustrator:** el nombre de la capa queda como `id` del grupo y el de cada
  objeto como su `id` (`A_x2D_12` es `A-12`; `A-12_1_` es un repetido). El
  panel lo entiende así. Exportar con *Archivo > Exportar > Exportar como >
  SVG*, con *Estilo: CSS interno* y *Id. de objetos: Nombres de capa*.
- **Inkscape:** el nombre de capa y de objeto (*Objeto > Propiedades*) queda en
  `inkscape:label`.
- **A mano o desde otro sistema:** `data-zona`, `data-fila`, `data-numero`
  (y opcional `data-kill`) en cada figura.

## 2. Sin convención

Si el SVG solo trae el dibujo (números escritos sobre casillas de color),
se lee igual que un PDF: cada número es una butaca, su fila es la letra del
mismo renglón y su zona el color de la casilla ("Zona 1", "Zona 2"...). Los
números deben ser texto, no contornos.

## Lo que se revisa siempre

La vista previa avisa butacas repetidas, números que se brincan dentro de
una fila y butacas sin zona. Nada se guarda hasta dar "Guardar mapa".

Seguridad: el SVG se lee con la librería estándar de Python (expat 2.4+), que
corta las bombas de entidades y no descarga entidades externas; hay topes de
tamaño (2.8 MB) y de elementos (200,000).
