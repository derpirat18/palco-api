"""Pruebas del generador e importador de mapas de butacas."""

from decimal import Decimal

import pytest

from app.services.seat_maps import (
    BloqueDeButacas,
    EspecificacionDeMapa,
    MapaInvalido,
    RangoFilas,
    calcular_viewport,
    exportar_mapa_csv,
    generar_mapa,
    importar_mapa_csv,
)

pytestmark = pytest.mark.asyncio


def bloque(**kwargs) -> BloqueDeButacas:
    base = {
        "zona": "Luneta",
        "filas": RangoFilas(desde="A", hasta="C"),
        "butacas_por_fila": 10,
    }
    return BloqueDeButacas(**{**base, **kwargs})


def spec(*bloques, nombre="Mapa de prueba") -> EspecificacionDeMapa:
    return EspecificacionDeMapa(nombre=nombre, bloques=list(bloques or [bloque()]))


# --------------------------------------------------------------------------
# Generacion basica
# --------------------------------------------------------------------------


async def test_genera_la_cantidad_correcta():
    mapa = generar_mapa(spec(bloque()))
    assert mapa.total_butacas == 30  # 3 filas x 10
    assert len(mapa.zonas) == 1
    assert mapa.zonas[0].nombre == "Luneta"


async def test_rango_de_filas_se_expande():
    assert RangoFilas(desde="A", hasta="E").etiquetas() == ["A", "B", "C", "D", "E"]


async def test_rango_invertido_se_rechaza():
    with pytest.raises(MapaInvalido):
        RangoFilas(desde="T", hasta="A").etiquetas()


async def test_lista_explicita_de_filas():
    mapa = generar_mapa(spec(bloque(filas=["Palco 1", "Palco 2"], butacas_por_fila=4)))
    assert mapa.total_butacas == 8
    assert {b.fila for b in mapa.zonas[0].butacas} == {"Palco 1", "Palco 2"}


async def test_varias_zonas():
    mapa = generar_mapa(
        spec(
            bloque(zona="Luneta"),
            bloque(zona="Balcon", filas=RangoFilas(desde="A", hasta="B")),
        )
    )
    assert {z.nombre for z in mapa.zonas} == {"Luneta", "Balcon"}
    assert mapa.total_butacas == 30 + 20


async def test_dos_bloques_de_la_misma_zona_se_fusionan():
    """Luneta izquierda y derecha son una sola zona con dos bloques."""
    mapa = generar_mapa(
        spec(
            bloque(zona="Luneta", numeracion="nones", numero_inicial=1),
            bloque(zona="Luneta", numeracion="pares", numero_inicial=2),
        )
    )
    assert len(mapa.zonas) == 1
    assert mapa.total_butacas == 60


# --------------------------------------------------------------------------
# Numeracion
# --------------------------------------------------------------------------


async def test_numeracion_secuencial():
    mapa = generar_mapa(spec(bloque(butacas_por_fila=5)))
    fila_a = [b.numero for b in mapa.zonas[0].butacas if b.fila == "A"]
    assert fila_a == ["1", "2", "3", "4", "5"]


async def test_numeracion_nones():
    """Comun en teatros mexicanos: nones de un lado del pasillo."""
    mapa = generar_mapa(spec(bloque(butacas_por_fila=5, numeracion="nones")))
    fila_a = [b.numero for b in mapa.zonas[0].butacas if b.fila == "A"]
    assert fila_a == ["1", "3", "5", "7", "9"]


async def test_numeracion_pares():
    mapa = generar_mapa(
        spec(bloque(butacas_por_fila=5, numeracion="pares", numero_inicial=2))
    )
    fila_a = [b.numero for b in mapa.zonas[0].butacas if b.fila == "A"]
    assert fila_a == ["2", "4", "6", "8", "10"]


async def test_numero_inicial_corrido():
    """El bloque derecho continua la numeracion del centro."""
    mapa = generar_mapa(spec(bloque(butacas_por_fila=4, numero_inicial=21)))
    fila_a = [b.numero for b in mapa.zonas[0].butacas if b.fila == "A"]
    assert fila_a == ["21", "22", "23", "24"]


async def test_numeracion_invertida():
    mapa = generar_mapa(spec(bloque(butacas_por_fila=4, invertir_numeracion=True)))
    fila_a = [b.numero for b in mapa.zonas[0].butacas if b.fila == "A"]
    assert fila_a == ["4", "3", "2", "1"]


# --------------------------------------------------------------------------
# Geometria
# --------------------------------------------------------------------------


async def test_posiciones_en_cuadricula():
    mapa = generar_mapa(
        spec(
            bloque(
                butacas_por_fila=3,
                filas=RangoFilas(desde="A", hasta="B"),
                origen_x=Decimal(100),
                origen_y=Decimal(200),
                paso_x=Decimal(30),
                paso_y=Decimal(40),
            )
        )
    )
    porcoord = {(b.fila, b.numero): (b.pos_x, b.pos_y) for b in mapa.zonas[0].butacas}
    assert porcoord[("A", "1")] == (Decimal("100.00"), Decimal("200.00"))
    assert porcoord[("A", "3")] == (Decimal("160.00"), Decimal("200.00"))
    assert porcoord[("B", "1")] == (Decimal("100.00"), Decimal("240.00"))


async def test_curvatura_separa_los_extremos():
    """Las butacas de orilla quedan mas lejos del escenario que las del
    centro. Sin esto el mapa se ve como hoja de calculo."""
    mapa = generar_mapa(
        spec(
            bloque(
                butacas_por_fila=5,
                filas=["A"],
                origen_y=Decimal(100),
                curvatura=Decimal(20),
            )
        )
    )
    ys = [b.pos_y for b in sorted(mapa.zonas[0].butacas, key=lambda b: b.pos_x)]
    assert ys[0] > ys[2]  # extremo izquierdo mas atras que el centro
    assert ys[4] > ys[2]  # extremo derecho tambien
    assert ys[0] == ys[4]  # simetrico


async def test_sin_curvatura_la_fila_es_recta():
    mapa = generar_mapa(spec(bloque(butacas_por_fila=5, filas=["A"])))
    ys = {b.pos_y for b in mapa.zonas[0].butacas}
    assert len(ys) == 1


# --------------------------------------------------------------------------
# Excepciones del recinto
# --------------------------------------------------------------------------


async def test_omitir_deja_hueco_de_pasillo():
    mapa = generar_mapa(
        spec(bloque(butacas_por_fila=6, filas=["A"], omitir=["A-3", "A-4"]))
    )
    assert mapa.total_butacas == 4
    assert "3" not in {b.numero for b in mapa.zonas[0].butacas}


async def test_kill_existe_pero_no_se_vende():
    """La butaca aparece en el mapa para que se dibuje completo, pero
    nunca entra al inventario vendible."""
    mapa = generar_mapa(spec(bloque(butacas_por_fila=4, filas=["A"], kill=["A-1"])))
    assert mapa.total_butacas == 4
    assert mapa.total_vendibles == 3
    muertas = [b for b in mapa.zonas[0].butacas if b.kill]
    assert len(muertas) == 1 and muertas[0].numero == "1"


async def test_filas_de_distinto_largo():
    """Las primeras filas de un teatro suelen ser mas cortas."""
    mapa = generar_mapa(
        spec(
            bloque(
                filas=["A", "B", "C"],
                butacas_por_fila=10,
                butacas_por_fila_override={"A": 6, "B": 8},
            )
        )
    )
    assert mapa.total_butacas == 6 + 8 + 10


# --------------------------------------------------------------------------
# Validaciones
# --------------------------------------------------------------------------


async def test_rechaza_butaca_duplicada_entre_bloques():
    """Dos bloques con la misma numeracion producirian butacas repetidas
    y el inventario quedaria inconsistente."""
    with pytest.raises(MapaInvalido):
        generar_mapa(
            spec(
                bloque(zona="Luneta", filas=["A"], butacas_por_fila=5),
                bloque(zona="Luneta", filas=["A"], butacas_por_fila=5),
            )
        )


async def test_rechaza_mapa_desproporcionado():
    """Un cero de mas en butacas_por_fila no debe generar medio millon de
    filas en la base."""
    with pytest.raises(ValueError):
        EspecificacionDeMapa(
            nombre="Absurdo",
            bloques=[
                BloqueDeButacas(
                    zona="Z",
                    filas=[str(i) for i in range(500)],
                    butacas_por_fila=200,
                )
            ],
        )


async def test_rechaza_rango_de_filas_multiletra():
    with pytest.raises(MapaInvalido):
        RangoFilas(desde="AA", hasta="AZ").etiquetas()


# --------------------------------------------------------------------------
# Importacion
# --------------------------------------------------------------------------


CSV_OK = """zona,fila,numero,x,y,kill
Luneta,A,1,100,200,
Luneta,A,2,130,200,
Luneta,A,3,160,200,1
Balcon,A,1,100,400,
"""


async def test_importa_csv():
    mapa = importar_mapa_csv("Teatro Principal", CSV_OK)
    assert mapa.total_butacas == 4
    assert mapa.total_vendibles == 3
    assert {z.nombre for z in mapa.zonas} == {"Luneta", "Balcon"}


async def test_importa_csv_con_bom():
    """Excel en Windows guarda con BOM y rompe el primer encabezado."""
    mapa = importar_mapa_csv("Con BOM", "﻿" + CSV_OK)
    assert mapa.total_butacas == 4


async def test_csv_sin_columnas_obligatorias():
    with pytest.raises(MapaInvalido) as exc:
        importar_mapa_csv("Malo", "zona,fila\nLuneta,A\n")
    assert "numero" in exc.value.mensaje


async def test_csv_con_butaca_repetida():
    repetido = "zona,fila,numero,x,y\nLuneta,A,1,0,0\nLuneta,A,1,10,0\n"
    with pytest.raises(MapaInvalido) as exc:
        importar_mapa_csv("Repetido", repetido)
    assert "repetida" in exc.value.mensaje


async def test_csv_con_coordenada_no_numerica():
    malo = "zona,fila,numero,x,y\nLuneta,A,1,aqui,0\n"
    with pytest.raises(MapaInvalido):
        importar_mapa_csv("Malo", malo)


async def test_csv_vacio():
    with pytest.raises(MapaInvalido):
        importar_mapa_csv("Vacio", "")


# --------------------------------------------------------------------------
# Ida y vuelta
# --------------------------------------------------------------------------


async def test_exportar_e_importar_conserva_todo():
    original = generar_mapa(
        spec(
            bloque(zona="Luneta", butacas_por_fila=8, kill=["A-1"]),
            bloque(zona="Balcon", filas=["A"], butacas_por_fila=5),
        )
    )
    copia = importar_mapa_csv("Copia", exportar_mapa_csv(original))

    assert copia.total_butacas == original.total_butacas
    assert copia.total_vendibles == original.total_vendibles

    def huella(m):
        return sorted(
            (b.zona, b.fila, b.numero, b.pos_x, b.pos_y, b.kill)
            for z in m.zonas
            for b in z.butacas
        )

    assert huella(copia) == huella(original)


async def test_viewport_se_ajusta_al_contenido():
    mapa = importar_mapa_csv("Ajustado", CSV_OK)
    vp = calcular_viewport(mapa, margen=40)
    assert vp["width"] == 200  # x maxima 160 + 40
    assert vp["height"] == 440  # y maxima 400 + 40


# --------------------------------------------------------------------------
# Caso realista
# --------------------------------------------------------------------------


async def test_teatro_completo_estilo_mexicano():
    """Luneta partida en nones y pares con pasillo central, mas balcon.

    Es la forma tipica de un teatro mexicano y el caso que tiene que
    funcionar sin pelear con el generador.
    """
    mapa = generar_mapa(
        EspecificacionDeMapa(
            nombre="Teatro Principal",
            ancho=1000,
            alto=800,
            bloques=[
                BloqueDeButacas(
                    zona="Luneta",
                    color="#7F77DD",
                    filas=RangoFilas(desde="A", hasta="T"),
                    butacas_por_fila=12,
                    numeracion="nones",
                    origen_x=Decimal(80),
                    origen_y=Decimal(150),
                    curvatura=Decimal(18),
                ),
                BloqueDeButacas(
                    zona="Luneta",
                    filas=RangoFilas(desde="A", hasta="T"),
                    butacas_por_fila=12,
                    numeracion="pares",
                    origen_x=Decimal(520),
                    origen_y=Decimal(150),
                    curvatura=Decimal(18),
                ),
                BloqueDeButacas(
                    zona="Balcon",
                    color="#1D9E75",
                    filas=RangoFilas(desde="A", hasta="F"),
                    butacas_por_fila=20,
                    origen_x=Decimal(60),
                    origen_y=Decimal(620),
                    kill=["A-1", "A-20"],
                ),
            ],
        )
    )

    assert mapa.total_butacas == (20 * 12 * 2) + (6 * 20)  # 600
    assert mapa.total_vendibles == 598  # dos butacas de visibilidad mala
    assert len(mapa.zonas) == 2

    luneta = next(z for z in mapa.zonas if z.nombre == "Luneta")
    numeros_a = sorted(int(b.numero) for b in luneta.butacas if b.fila == "A")
    # Nones del 1 al 23 y pares del 2 al 24, sin huecos ni repetidos.
    assert numeros_a == list(range(1, 25))
