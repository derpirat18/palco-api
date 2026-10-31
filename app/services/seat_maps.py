"""Generacion e importacion de mapas de butacas.

Un teatro de 1,600 lugares son 1,600 filas en `seats`. Nadie las captura a
mano, asi que hay dos caminos:

  1. Generador por bloques: se describe el recinto como rectangulos de
     filas y butacas, y el sistema las posiciona. Cubre la mayoria de los
     casos en minutos.
  2. Importacion: un archivo con una linea por butaca, para recintos con
     forma irregular que el generador no alcanza.

Ambos producen exactamente lo mismo: filas en `zones` y `seats`.
"""

from __future__ import annotations

import csv
import io
import math
import string
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.errors import ErrorDeDominio
from app.core.textos import Color, Linea

# Butacas por encima de esto casi siempre significan un error de captura
# (un cero de mas en butacas_por_fila). Mejor rechazar que generar medio
# millon de filas.
MAX_BUTACAS_POR_MAPA = 50_000


class MapaInvalido(ErrorDeDominio):
    codigo = "mapa_invalido"
    http_status = 422


# --------------------------------------------------------------------------
# Especificacion de entrada
# --------------------------------------------------------------------------

Numeracion = Literal["secuencial", "nones", "pares"]


class RangoFilas(BaseModel):
    """Filas por rango de letras: de la A a la T."""

    desde: str = Field(min_length=1, max_length=4)
    hasta: str = Field(min_length=1, max_length=4)

    def etiquetas(self) -> list[str]:
        alfabeto = string.ascii_uppercase
        a, b = self.desde.upper(), self.hasta.upper()
        if len(a) != 1 or len(b) != 1 or a not in alfabeto or b not in alfabeto:
            raise MapaInvalido(
                "El rango de filas solo admite una letra de la A a la Z. "
                "Para nombres como AA o 'Palco 1', usa la lista explícita."
            )
        if alfabeto.index(a) > alfabeto.index(b):
            raise MapaInvalido(f"El rango de filas {a}-{b} está invertido.")
        return list(alfabeto[alfabeto.index(a) : alfabeto.index(b) + 1])


class BloqueDeButacas(BaseModel):
    """Un rectangulo de butacas dentro de una zona.

    Una zona puede tener varios bloques: Luneta suele partirse en centro,
    izquierda y derecha, separados por pasillos.
    """

    zona: Linea = Field(min_length=1, max_length=80)
    color: Color | None = Field(default=None, max_length=9)

    # Cada fila cabe en 8 caracteres (seats.fila).
    filas: RangoFilas | list[Annotated[Linea, Field(min_length=1, max_length=8)]]
    butacas_por_fila: int = Field(gt=0, le=200)
    # Filas que llevan otra cantidad: {"A": 12, "B": 14}. Las filas de
    # adelante en un teatro suelen ser mas cortas.
    butacas_por_fila_override: dict[str, int] = Field(default_factory=dict)

    numeracion: Numeracion = "secuencial"
    # Numero con el que arranca la fila. Permite que el bloque izquierdo
    # siga la numeracion del centro en lugar de reiniciar.
    numero_inicial: int = Field(default=1, ge=0)
    invertir_numeracion: bool = False

    origen_x: Decimal = Decimal(0)
    origen_y: Decimal = Decimal(0)
    paso_x: Decimal = Decimal(28)
    paso_y: Decimal = Decimal(30)

    # Cuanto se arquea la fila hacia el escenario, en unidades del SVG.
    # 0 es una fila recta. Un teatro real casi siempre tiene algo de arco.
    curvatura: Decimal = Decimal(0)

    # Butacas que no existen (un pasillo a media fila): ["F-7", "F-8"].
    omitir: list[Annotated[Linea, Field(max_length=20)]] = Field(
        default_factory=list, max_length=5000
    )
    # Butacas que existen pero nunca se venden (columna enfrente, sonido).
    kill: list[Annotated[Linea, Field(max_length=20)]] = Field(
        default_factory=list, max_length=5000
    )

    @field_validator("filas")
    @classmethod
    def _filas_no_vacias(cls, v):
        if isinstance(v, list) and not v:
            raise ValueError("La lista de filas no puede estar vacía.")
        return v

    def etiquetas_de_fila(self) -> list[str]:
        if isinstance(self.filas, RangoFilas):
            return self.filas.etiquetas()
        return [str(f).strip() for f in self.filas]


class EspecificacionDeMapa(BaseModel):
    """Lo que se recibe para generar un mapa completo."""

    nombre: Linea = Field(min_length=1, max_length=120)
    ancho: int = Field(default=1000, gt=0, le=10_000)
    alto: int = Field(default=700, gt=0, le=10_000)
    bloques: list[BloqueDeButacas] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_desbordar(self):
        total = sum(
            len(b.etiquetas_de_fila()) * b.butacas_por_fila for b in self.bloques
        )
        if total > MAX_BUTACAS_POR_MAPA:
            raise ValueError(
                f"El mapa generaría ~{total:,} butacas, por encima del límite "
                f"de {MAX_BUTACAS_POR_MAPA:,}. Revisa butacas_por_fila."
            )
        return self


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ButacaGenerada:
    zona: str
    fila: str
    numero: str
    pos_x: Decimal
    pos_y: Decimal
    kill: bool = False


@dataclass
class ZonaGenerada:
    nombre: str
    color: str | None
    orden: int
    butacas: list[ButacaGenerada] = field(default_factory=list)
    # Entrada general (EG): cupos sin lugar numerado.
    general: bool = False


@dataclass
class MapaGenerado:
    nombre: str
    viewport: dict
    zonas: list[ZonaGenerada]

    @property
    def total_butacas(self) -> int:
        return sum(len(z.butacas) for z in self.zonas)

    @property
    def total_vendibles(self) -> int:
        return sum(1 for z in self.zonas for b in z.butacas if not b.kill)


# --------------------------------------------------------------------------
# Generador
# --------------------------------------------------------------------------


def _numeros(bloque: BloqueDeButacas, cuantas: int) -> list[str]:
    """Numeracion de una fila.

    En teatros mexicanos es comun partir la luneta en nones y pares: las
    butacas impares de un lado del pasillo central y las pares del otro.
    """
    inicio = bloque.numero_inicial
    if bloque.numeracion == "secuencial":
        nums = [inicio + i for i in range(cuantas)]
    elif bloque.numeracion == "nones":
        primero = inicio if inicio % 2 == 1 else inicio + 1
        nums = [primero + 2 * i for i in range(cuantas)]
    else:  # pares
        primero = inicio if inicio % 2 == 0 else inicio + 1
        nums = [primero + 2 * i for i in range(cuantas)]

    if bloque.invertir_numeracion:
        nums.reverse()
    return [str(n) for n in nums]


def _desplazamiento_por_curva(indice: int, cuantas: int, curvatura: Decimal) -> Decimal:
    """Arco de la fila.

    Las butacas de los extremos se alejan del escenario y las del centro
    quedan al frente. Sin esto el mapa se ve como una hoja de calculo.
    """
    if curvatura == 0 or cuantas <= 1:
        return Decimal(0)
    centro = (cuantas - 1) / 2
    # Normalizado a [-1, 1] y elevado al cuadrado: parabola suave.
    t = (indice - centro) / centro
    return (curvatura * Decimal(str(t * t))).quantize(Decimal("0.01"))


def generar_mapa(spec: EspecificacionDeMapa) -> MapaGenerado:
    """Convierte la especificacion en zonas y butacas posicionadas."""
    zonas: dict[str, ZonaGenerada] = {}
    vistas: set[tuple[str, str, str]] = set()

    for bloque in spec.bloques:
        zona = zonas.get(bloque.zona)
        if zona is None:
            zona = ZonaGenerada(
                nombre=bloque.zona, color=bloque.color, orden=len(zonas)
            )
            zonas[bloque.zona] = zona

        etiquetas = bloque.etiquetas_de_fila()
        omitir = {o.strip().upper() for o in bloque.omitir}
        kill = {k.strip().upper() for k in bloque.kill}

        for i_fila, fila in enumerate(etiquetas):
            cuantas = bloque.butacas_por_fila_override.get(
                fila, bloque.butacas_por_fila
            )
            numeros = _numeros(bloque, cuantas)

            for i_butaca, numero in enumerate(numeros):
                clave = f"{fila}-{numero}".upper()
                if clave in omitir:
                    continue

                llave = (bloque.zona, fila, numero)
                if llave in vistas:
                    raise MapaInvalido(
                        f"La butaca {fila}-{numero} de {bloque.zona} aparece "
                        "dos veces. Revisa numero_inicial de los bloques."
                    )
                vistas.add(llave)

                x = bloque.origen_x + bloque.paso_x * i_butaca
                y = (
                    bloque.origen_y
                    + bloque.paso_y * i_fila
                    + _desplazamiento_por_curva(i_butaca, cuantas, bloque.curvatura)
                )

                zona.butacas.append(
                    ButacaGenerada(
                        zona=bloque.zona,
                        fila=fila,
                        numero=numero,
                        pos_x=x.quantize(Decimal("0.01")),
                        pos_y=y.quantize(Decimal("0.01")),
                        kill=clave in kill,
                    )
                )

    mapa = MapaGenerado(
        nombre=spec.nombre,
        viewport={"width": spec.ancho, "height": spec.alto},
        zonas=list(zonas.values()),
    )

    if mapa.total_butacas == 0:
        raise MapaInvalido("El mapa no generó ninguna butaca.")

    return mapa


# --------------------------------------------------------------------------
# Importacion
# --------------------------------------------------------------------------

COLUMNAS_REQUERIDAS = {"zona", "fila", "numero", "x", "y"}


def importar_mapa_csv(
    nombre: str, contenido: str, ancho: int = 1000, alto: int = 700
) -> MapaGenerado:
    """Carga un mapa desde CSV.

    Columnas: zona, fila, numero, x, y, y opcionalmente kill y color.
    Es la valvula de escape para recintos con forma que el generador por
    bloques no alcanza.
    """
    lector = csv.DictReader(io.StringIO(contenido.lstrip("﻿")))

    if lector.fieldnames is None:
        raise MapaInvalido("El archivo está vacío o no tiene encabezados.")

    encabezados = {h.strip().lower() for h in lector.fieldnames}
    faltantes = COLUMNAS_REQUERIDAS - encabezados
    if faltantes:
        raise MapaInvalido(
            f"Faltan columnas en el archivo: {', '.join(sorted(faltantes))}.",
            {"encontradas": sorted(encabezados)},
        )

    zonas: dict[str, ZonaGenerada] = {}
    vistas: set[tuple[str, str, str]] = set()

    for n_linea, fila_csv in enumerate(lector, start=2):
        datos = {
            (k or "").strip().lower(): (v or "").strip() for k, v in fila_csv.items()
        }

        nombre_zona = datos.get("zona", "")
        etiqueta_fila = datos.get("fila", "")
        numero = datos.get("numero", "")

        if not (nombre_zona and etiqueta_fila and numero):
            raise MapaInvalido(
                f"Línea {n_linea}: zona, fila y número son obligatorios."
            )

        try:
            x = Decimal(datos.get("x") or "0")
            y = Decimal(datos.get("y") or "0")
        except Exception as exc:
            raise MapaInvalido(
                f"Línea {n_linea}: las coordenadas deben ser numéricas."
            ) from exc

        llave = (nombre_zona, etiqueta_fila, numero)
        if llave in vistas:
            raise MapaInvalido(
                f"Línea {n_linea}: la butaca {etiqueta_fila}-{numero} de "
                f"{nombre_zona} está repetida."
            )
        vistas.add(llave)

        zona = zonas.get(nombre_zona)
        if zona is None:
            zona = ZonaGenerada(
                nombre=nombre_zona,
                color=datos.get("color") or None,
                orden=len(zonas),
            )
            zonas[nombre_zona] = zona

        zona.butacas.append(
            ButacaGenerada(
                zona=nombre_zona,
                fila=etiqueta_fila,
                numero=numero,
                pos_x=x.quantize(Decimal("0.01")),
                pos_y=y.quantize(Decimal("0.01")),
                kill=datos.get("kill", "").lower() in {"1", "true", "si", "sí", "x"},
            )
        )

    if not zonas:
        raise MapaInvalido("El archivo no contiene ninguna butaca.")

    total = sum(len(z.butacas) for z in zonas.values())
    if total > MAX_BUTACAS_POR_MAPA:
        raise MapaInvalido(
            f"El archivo trae {total:,} butacas, por encima del límite de "
            f"{MAX_BUTACAS_POR_MAPA:,}."
        )

    return MapaGenerado(
        nombre=nombre,
        viewport={"width": ancho, "height": alto},
        zonas=list(zonas.values()),
    )


def exportar_mapa_csv(mapa: MapaGenerado) -> str:
    """Exporta a CSV, para revisar o reimportar un mapa generado."""
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow(["zona", "fila", "numero", "x", "y", "kill"])
    for zona in mapa.zonas:
        for b in zona.butacas:
            escritor.writerow(
                [b.zona, b.fila, b.numero, b.pos_x, b.pos_y, "1" if b.kill else ""]
            )
    return salida.getvalue()


def calcular_viewport(mapa: MapaGenerado, margen: int = 40) -> dict:
    """Ajusta la caja de dibujo al contenido real.

    Util despues de importar, cuando no se sabe de antemano que tan grande
    es el recinto.
    """
    xs = [b.pos_x for z in mapa.zonas for b in z.butacas]
    ys = [b.pos_y for z in mapa.zonas for b in z.butacas]
    if not xs:
        return mapa.viewport
    # math.ceil sobre Decimal ya devuelve int: sin pasar por float, que
    # es justo lo que no queremos tocando coordenadas.
    return {
        "width": math.ceil(max(xs)) + margen,
        "height": math.ceil(max(ys)) + margen,
    }


# --------------------------------------------------------------------------
# Entrada general (EG)
# --------------------------------------------------------------------------

FILA_GENERAL = "G"
MAX_CUPO_GENERAL = 20000


class ZonaGeneralSpec(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    capacidad: int = Field(ge=1, le=MAX_CUPO_GENERAL)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class MapaGeneralSpec(BaseModel):
    """Un mapa sin butacas numeradas: cada zona es un cupo (General, VIP)."""

    nombre: str = Field(min_length=1, max_length=120)
    zonas: list[ZonaGeneralSpec] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _nombres_unicos(self):
        nombres = [z.nombre.strip().lower() for z in self.zonas]
        if len(set(nombres)) != len(nombres):
            raise ValueError("Cada zona necesita un nombre distinto.")
        return self


def mapa_general(spec: MapaGeneralSpec) -> MapaGenerado:
    """Cada zona con su cupo: N lugares sin posicion (no se dibujan)."""
    return MapaGenerado(
        nombre=spec.nombre.strip(),
        viewport={"general": True},
        zonas=[
            ZonaGenerada(
                nombre=z.nombre.strip(),
                color=z.color,
                orden=i,
                general=True,
                butacas=[
                    ButacaGenerada(
                        zona=z.nombre.strip(),
                        fila=FILA_GENERAL,
                        numero=str(n),
                        pos_x=Decimal(0),
                        pos_y=Decimal(0),
                    )
                    for n in range(1, z.capacidad + 1)
                ],
            )
            for i, z in enumerate(spec.zonas)
        ],
    )
