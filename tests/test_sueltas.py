"""Pruebas de la regla de butacas sueltas, sin base de datos.

Cada fila se describe con una cadena: `L` libre, `X` ocupada. Las
posiciones van de 28 en 28 unidades, como el generador de mapas.
"""

from decimal import Decimal

from app.services.sueltas import Lugar, butaca_suelta, tramos

PASO = Decimal(28)


def fila(
    patron: str,
    *,
    zona: int = 1,
    nombre: str = "A",
    ids: list[int] | None = None,
    xs: list[Decimal] | None = None,
) -> list[Lugar]:
    ids = ids or list(range(1, len(patron) + 1))
    xs = xs or [PASO * i for i in range(len(patron))]
    return [
        Lugar(seat_id=i, zona_id=zona, fila=nombre, x=x, libre=c == "L")
        for i, x, c in zip(ids, xs, patron, strict=True)
    ]


# --------------------------------------------------------------------------
# Lo basico
# --------------------------------------------------------------------------


def test_desde_la_orilla_no_deja_ninguna_sola():
    assert butaca_suelta(fila("LLLLLL"), {1, 2}) is None


def test_dejar_una_sola_en_la_orilla_no_se_permite():
    assert butaca_suelta(fila("LLLLLL"), {2, 3}) == 1


def test_en_medio_dejando_dos_y_dos_si():
    assert butaca_suelta(fila("LLLLLL"), {3, 4}) is None


def test_un_hueco_de_una_entre_las_elegidas_no():
    assert butaca_suelta(fila("LLLLLL"), {1, 3}) == 2


def test_junto_a_una_ocupada_cuenta_como_orilla():
    assert butaca_suelta(fila("XLLLLX"), {3, 4}) == 2
    assert butaca_suelta(fila("XLLLLX"), {2, 3}) is None


def test_si_es_inevitable_se_permite():
    """Tres libres y una compra de dos: cualquier eleccion deja una."""
    assert butaca_suelta(fila("XLLLX"), {2, 3}) is None
    assert butaca_suelta(fila("XLLLX"), {2, 4}) is None


# --------------------------------------------------------------------------
# Lo que no es culpa de esta compra
# --------------------------------------------------------------------------


def test_una_suelta_que_ya_existia_no_bloquea():
    assert butaca_suelta(fila("XLXLLLL"), {6, 7}) is None


def test_las_filas_que_no_toca_no_importan():
    lugares = fila("LLLL") + fila("XLXL", nombre="B", ids=[11, 12, 13, 14])
    assert butaca_suelta(lugares, {1, 2}) is None


def test_la_misma_fila_de_otra_zona_no_es_vecina():
    """Balcon sigue a Luneta en el dibujo, pero no es la misma fila.

    En Luneta la 8 queda sola en la orilla. Si se tomara a Balcon como su
    continuacion, la 8 parecerian tener vecinas libres y pasaria.
    """
    luneta = fila("XLLLLLLL")
    balcon = fila("LLL", zona=2, ids=[11, 12, 13], xs=[PASO * 8, PASO * 9, PASO * 10])
    assert butaca_suelta(luneta + balcon, {4, 5, 6, 7}) == 8


# --------------------------------------------------------------------------
# Geometria: pasillos y numeracion
# --------------------------------------------------------------------------


def test_un_pasillo_corta_la_fila():
    """Dos bloques con pasillo central: el borde del pasillo es orilla."""
    xs = [Decimal(x) for x in (0, 28, 56, 84, 220, 248, 276, 304, 332)]
    lugares = fila("LLLLLLLLL", xs=xs)

    assert [len(t) for t in tramos(lugares)] == [4, 5]
    # La 5 queda sola contra el pasillo. Sin detectar el pasillo pareceria
    # tener a la 4 libre al lado y pasaria.
    assert butaca_suelta(lugares, {6, 7}) == 5


def test_un_lugar_omitido_a_media_fila_tambien_es_pasillo():
    xs = [Decimal(x) for x in (0, 28, 56, 112, 140)]  # falta el de 84
    assert [len(t) for t in tramos(fila("LLLLL", xs=xs))] == [3, 2]


def test_la_vecindad_es_por_posicion_no_por_numero():
    """Luneta en nones: la 1, la 3, la 5 y la 7 estan juntas."""
    lugares = fila("LLLL", ids=[1, 3, 5, 7])
    assert butaca_suelta(lugares, {3, 5}) == 1
    assert butaca_suelta(lugares, {1, 3}) is None


def test_numeracion_invertida():
    """Los ids van al reves de la posicion: manda la posicion."""
    lugares = fila("LLLL", ids=[4, 3, 2, 1])
    # La 4 esta en la orilla izquierda; apartar 3 y 2 la deja sola.
    assert butaca_suelta(lugares, {3, 2}) == 4


# --------------------------------------------------------------------------
# Lo que ya tenia apartado
# --------------------------------------------------------------------------


def test_lo_que_ya_tenia_cuenta_como_parte_de_la_seleccion():
    """Si ya tenia la 1 y agrega la 3, la 2 queda sola entre las suyas."""
    assert butaca_suelta(fila("LLLLLL"), {1, 3}) == 2
    assert butaca_suelta(fila("LLLLLL"), {1, 2}) is None


# --------------------------------------------------------------------------
# Con un mapa real del generador
# --------------------------------------------------------------------------


def test_teatro_mexicano_del_generador():
    """Luneta en nones y pares con pasillo central y curvatura, mas balcon.

    Cada fila de luneta tiene que partirse en dos tramos por el pasillo, y
    el balcon ser uno solo. Si la deteccion de pasillos fallara con un
    mapa de verdad, la regla bloquearia o dejaria pasar compras al reves.
    """
    from app.services.seat_maps import (
        BloqueDeButacas,
        EspecificacionDeMapa,
        RangoFilas,
        generar_mapa,
    )

    mapa = generar_mapa(
        EspecificacionDeMapa(
            nombre="Teatro Principal",
            bloques=[
                BloqueDeButacas(
                    zona="Luneta",
                    filas=RangoFilas(desde="A", hasta="C"),
                    butacas_por_fila=12,
                    numeracion="nones",
                    origen_x=Decimal(80),
                    curvatura=Decimal(18),
                ),
                BloqueDeButacas(
                    zona="Luneta",
                    filas=RangoFilas(desde="A", hasta="C"),
                    butacas_por_fila=12,
                    numeracion="pares",
                    origen_x=Decimal(520),
                    curvatura=Decimal(18),
                ),
                BloqueDeButacas(
                    zona="Balcon",
                    filas=RangoFilas(desde="A", hasta="B"),
                    butacas_por_fila=20,
                    origen_x=Decimal(60),
                    origen_y=Decimal(620),
                    kill=["A-1", "A-20"],
                ),
            ],
        )
    )
    lugares, ids = [], {}
    for zona_id, zona in enumerate(mapa.zonas, start=1):
        for b in zona.butacas:
            seat_id = len(lugares) + 1
            ids[(zona.nombre, b.fila, b.numero)] = seat_id
            lugares.append(
                Lugar(
                    seat_id=seat_id,
                    zona_id=zona_id,
                    fila=b.fila,
                    x=b.pos_x,
                    libre=not b.kill,
                )
            )

    largos = sorted(len(t) for t in tramos(lugares))
    assert largos == [12] * 6 + [20] * 2  # 3 filas x 2 lados + 2 de balcon

    # Luneta A: la 1 y la 3 juntas en el lado de nones. Apartar la 3 y la 5
    # deja la 1 sola contra la orilla.
    a = {n: ids[("Luneta", "A", str(n))] for n in (1, 2, 3, 5)}
    assert butaca_suelta(lugares, {a[3], a[5]}) == a[1]
    # La 2 esta del otro lado del pasillo: no es vecina de la 1.
    assert butaca_suelta(lugares, {a[1], a[3]}) is None

    # Balcon A: la A-1 esta en kill, asi que la A-2 es orilla.
    b = {n: ids[("Balcon", "A", str(n))] for n in (2, 3, 4)}
    assert butaca_suelta(lugares, {b[3], b[4]}) == b[2]
