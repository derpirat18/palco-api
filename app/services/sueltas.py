"""Regla de butacas sueltas: no dejar una sola butaca libre aislada.

Una butaca sola entre dos ocupadas casi nunca se vende: nadie va al
teatro a sentarse solo en medio de desconocidos si puede evitarlo. Cada
una que queda asi es dinero que el organizador pierde. Por eso una
seleccion no puede dejar una butaca libre sola entre lo que se aparta y
algo ocupado, un pasillo o el final de la fila.

La vecindad sale de la geometria del mapa, no de los numeros. En los
teatros mexicanos la luneta va en nones y pares (la 1, la 3 y la 5 estan
juntas; la 2 esta del otro lado del pasillo), hay numeracion invertida,
pasillos a media fila y mapas importados de Excel con cualquier
numeracion. Lo unico confiable es la posicion: dentro de una zona y una
fila, las butacas ordenadas por `x` son vecinas mientras la distancia
entre ellas sea la normal de la zona. Un salto claramente mayor es un
pasillo.

Excepcion: si la butaca suelta es inevitable no se castiga. Con tres
libres juntas y una compra de dos, cualquier eleccion deja una sola.

Es una regla de negocio, no una garantia de integridad: dos compras
simultaneas en butacas vecinas pueden, entre las dos, dejar una suelta.
Lo que no se puede es vender dos veces la misma butaca, y eso lo cuidan
otras capas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from statistics import median

# Un hueco mayor a esto, en multiplos de la separacion normal de la zona,
# es un pasillo. Un solo lugar omitido ya mide el doble; 1.5 los separa
# con holgura de las variaciones por curvatura o captura a mano.
FACTOR_PASILLO = Decimal("1.5")


@dataclass(frozen=True)
class Lugar:
    seat_id: int
    zona_id: int
    fila: str
    x: Decimal
    # Se puede vender a esta persona: disponible, apartado vencido o ya
    # apartado por ella misma.
    libre: bool


def tramos(lugares: Iterable[Lugar]) -> list[list[Lugar]]:
    """Agrupa en tramos continuos: misma zona, misma fila, sin pasillo.

    Cada tramo va ordenado por `x`. Dos butacas seguidas pertenecen al
    mismo tramo si las separa a lo mas FACTOR_PASILLO veces la separacion
    tipica (mediana) de su zona.
    """
    por_fila: dict[tuple[int, str], list[Lugar]] = defaultdict(list)
    for lugar in lugares:
        por_fila[(lugar.zona_id, lugar.fila)].append(lugar)

    separaciones_por_zona: dict[int, list[Decimal]] = defaultdict(list)
    for (zona_id, _), fila in por_fila.items():
        fila.sort(key=lambda lugar: lugar.x)
        separaciones_por_zona[zona_id].extend(
            b.x - a.x for a, b in pairwise(fila) if b.x > a.x
        )

    resultado: list[list[Lugar]] = []
    for (zona_id, _), fila in por_fila.items():
        separaciones = separaciones_por_zona[zona_id]
        tope = median(separaciones) * FACTOR_PASILLO if separaciones else None
        actual = [fila[0]]
        for anterior, siguiente in pairwise(fila):
            if tope is not None and siguiente.x - anterior.x > tope:
                resultado.append(actual)
                actual = []
            actual.append(siguiente)
        resultado.append(actual)
    return resultado


def butaca_suelta(lugares: Iterable[Lugar], seleccion: set[int]) -> int | None:
    """La primera butaca que la seleccion dejaria sola, o None.

    `lugares` son todas las butacas de las filas que toca la seleccion,
    con `libre` tal como estan antes de apartar. `seleccion` incluye lo
    que la persona ya tenia apartado, para que agregar butacas a su
    carrito se evalue completo.
    """
    for tramo in tramos(lugares):
        if not any(lugar.seat_id in seleccion for lugar in tramo):
            continue
        for corrida in _corridas_libres(tramo):
            elegidas = sum(1 for lugar in corrida if lugar.seat_id in seleccion)
            if elegidas == 0:
                continue
            # Inevitable: sobra exactamente una, la eleccion que sea.
            if len(corrida) - elegidas == 1:
                continue
            for hueco in _huecos(corrida, seleccion):
                if len(hueco) == 1:
                    return hueco[0].seat_id
    return None


def _corridas_libres(tramo: list[Lugar]) -> list[list[Lugar]]:
    """Butacas libres consecutivas dentro de un tramo."""
    corridas: list[list[Lugar]] = []
    actual: list[Lugar] = []
    for lugar in tramo:
        if lugar.libre:
            actual.append(lugar)
        elif actual:
            corridas.append(actual)
            actual = []
    if actual:
        corridas.append(actual)
    return corridas


def _huecos(corrida: list[Lugar], seleccion: set[int]) -> list[list[Lugar]]:
    """Lo que queda libre de una corrida despues de apartar."""
    huecos: list[list[Lugar]] = []
    actual: list[Lugar] = []
    for lugar in corrida:
        if lugar.seat_id in seleccion:
            if actual:
                huecos.append(actual)
                actual = []
        else:
            actual.append(lugar)
    if actual:
        huecos.append(actual)
    return huecos
