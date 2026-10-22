"""Mapas de recintos desde el archivo que ya tiene el recinto (D2).

CSV, Excel, PDF o SVG terminan igual: una lista de butacas con zona, fila,
numero, posicion y color, mas los avisos de lo que conviene revisar. No se
guarda nada aqui: el panel muestra la vista previa, deja nombrar y unir
zonas, y guarda con la importacion de siempre (`importar_mapa_csv`). Asi
hay un solo camino para escribir mapas y uno solo para validarlos.

PDF: sirve el que trae el texto de verdad (hecho en Publisher, Excel,
AutoCAD o Illustrator). Cada numero es una butaca; su fila es la letra mas
cercana en el mismo renglon; su zona es el color de la casilla que lo
contiene. Un PDF escaneado no trae texto y se rechaza con un mensaje que lo
dice. Las zonas salen como "Zona 1", "Zona 2"... por color, de adelante
hacia atras: el nombre y el precio los pone una persona.

SVG: con la convencion de docs/mapas-svg.md (capas por zona, butacas
nombradas FILA-NUMERO) o, sin ella, igual que el PDF.

Todo es entrada no confiable aunque solo PSD la suba: hay topes de tamaño,
de paginas y de butacas, y el Excel se revisa contra bombas de zip antes de
abrirlo.
"""

from __future__ import annotations

import csv
import io
import itertools
import re
import statistics
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.services.seat_maps import MAX_BUTACAS_POR_MAPA, MapaInvalido

# El cuerpo de la peticion tiene tope de 4 MB y el archivo va en base64.
MAX_BYTES = 2_800_000
# Un Excel es un zip: descomprimido no puede crecer sin limite.
MAX_XLSX_DESCOMPRIMIDO = 40_000_000
# Lienzo donde se dibuja el mapa (el mismo de la importacion por CSV).
ANCHO, ALTO = 1000, 700
# Espacio arriba para el escenario y margen alrededor.
ARRIBA, MARGEN = 64, 28

_FILA = re.compile(r"[A-Z]{1,3}")
_NUMERO = re.compile(r"\d{1,4}")


@dataclass
class ButacaLeida:
    zona: str
    fila: str
    numero: str
    x: float
    y: float
    color: str | None = None
    kill: bool = False


@dataclass
class Lectura:
    origen: str
    butacas: list[ButacaLeida]
    avisos: list[str] = field(default_factory=list)

    def zonas(self) -> list[tuple[str, str | None, int]]:
        """(nombre, color, butacas) en el orden en que aparecen."""
        orden: dict[str, tuple[str | None, int]] = {}
        for b in self.butacas:
            color, n = orden.get(b.zona, (b.color, 0))
            orden[b.zona] = (color, n + 1)
        return [(z, c, n) for z, (c, n) in orden.items()]


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------


def leer_archivo(nombre_archivo: str, contenido: bytes) -> Lectura:
    if not contenido:
        raise MapaInvalido("El archivo está vacío.")
    if len(contenido) > MAX_BYTES:
        raise MapaInvalido(
            "El archivo pesa más de 2.8 MB. Si es un PDF, guárdalo de nuevo "
            "sin imágenes o con menor calidad."
        )
    tipo = _tipo(nombre_archivo, contenido)
    if tipo == "pdf":
        lectura = leer_pdf(contenido)
    elif tipo == "svg":
        lectura = leer_svg(contenido)
    elif tipo == "xlsx":
        lectura = leer_xlsx(contenido)
    else:
        try:
            texto = contenido.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = contenido.decode("latin-1")
        lectura = leer_csv(texto)

    if len(lectura.butacas) > MAX_BUTACAS_POR_MAPA:
        raise MapaInvalido(
            f"El archivo trae {len(lectura.butacas):,} butacas, por encima del "
            f"límite de {MAX_BUTACAS_POR_MAPA:,}."
        )
    _al_lienzo(lectura)
    lectura.avisos.extend(revisar(lectura.butacas))
    return lectura


def _tipo(nombre: str, contenido: bytes) -> str:
    """Por el contenido, no solo por la extension: un .csv que es PDF es PDF."""
    if contenido.startswith(b"%PDF"):
        return "pdf"
    if contenido.startswith(b"PK\x03\x04"):
        return "xlsx"
    inicio = contenido[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if inicio.startswith((b"<?xml", b"<svg", b"<!--", b"<!doctype")) and b"<svg" in (
        contenido[:65536].lower()
    ):
        return "svg"
    if nombre.lower().endswith(".svg"):
        raise MapaInvalido("El archivo no es un SVG válido.")
    if nombre.lower().endswith((".pdf", ".xlsx", ".xls")):
        raise MapaInvalido(
            "El archivo no es un PDF ni un Excel válido. Si es Excel antiguo "
            "(.xls), ábrelo y guárdalo como .xlsx."
        )
    return "csv"


# --------------------------------------------------------------------------
# CSV y Excel: las mismas columnas
# --------------------------------------------------------------------------

_REQUERIDAS = ("zona", "fila", "numero", "x", "y")


def leer_csv(texto: str) -> Lectura:
    lector = csv.reader(io.StringIO(texto.lstrip("﻿")))
    return _de_tabla("csv", list(lector))


def leer_xlsx(contenido: bytes) -> Lectura:
    import openpyxl  # solo aqui: no carga en cada arranque de la API

    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_XLSX_DESCOMPRIMIDO:
                raise MapaInvalido("El Excel es demasiado grande para un mapa.")
        libro = openpyxl.load_workbook(
            io.BytesIO(contenido), read_only=True, data_only=True
        )
    except MapaInvalido:
        raise
    except Exception as exc:
        raise MapaInvalido(
            "No pudimos abrir el Excel. Guárdalo de nuevo como .xlsx e "
            "inténtalo otra vez."
        ) from exc
    try:
        hoja = libro.active
        filas = [
            ["" if v is None else _texto_de_celda(v) for v in fila]
            for fila in hoja.iter_rows(
                values_only=True, max_row=MAX_BUTACAS_POR_MAPA + 50
            )
        ]
    finally:
        libro.close()
    return _de_tabla("xlsx", filas)


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )


def _texto_de_celda(v: object) -> str:
    # 12.0 de Excel es la butaca 12, no "12.0".
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _de_tabla(origen: str, filas: list[list[str]]) -> Lectura:
    filas = [f for f in filas if any(str(c).strip() for c in f)]
    if not filas:
        raise MapaInvalido("El archivo está vacío o no tiene encabezados.")
    # "número" o "numero": se escribe como se escribe en Excel.
    encabezado = [_sin_acentos(str(c).strip().lower()) for c in filas[0]]
    faltan = [c for c in _REQUERIDAS if c not in encabezado]
    if faltan:
        raise MapaInvalido(
            f"Faltan columnas en el archivo: {', '.join(faltan)}. La primera "
            "fila debe decir zona, fila, número, x, y (y si quieres, kill y color).",
            {"encontradas": encabezado},
        )
    col = {c: encabezado.index(c) for c in encabezado if c}
    butacas: list[ButacaLeida] = []
    for n, fila in enumerate(filas[1:], start=2):

        def dato(c: str, fila: list[str] = fila) -> str:
            i = col.get(c)
            return str(fila[i]).strip() if i is not None and i < len(fila) else ""

        zona, etiqueta, numero = dato("zona"), dato("fila").upper(), dato("numero")
        if not (zona and etiqueta and numero):
            raise MapaInvalido(f"Renglón {n}: zona, fila y número son obligatorios.")
        try:
            x, y = float(Decimal(dato("x") or "0")), float(Decimal(dato("y") or "0"))
        except InvalidOperation as exc:
            raise MapaInvalido(
                f"Renglón {n}: las coordenadas x, y deben ser números."
            ) from exc
        butacas.append(
            ButacaLeida(
                zona=zona[:80],
                fila=etiqueta[:8],
                numero=numero[:8],
                x=x,
                y=y,
                color=dato("color") or None,
                kill=dato("kill").lower() in {"1", "true", "si", "sí", "x"},
            )
        )
    if not butacas:
        raise MapaInvalido("El archivo no contiene ninguna butaca.")
    return Lectura(origen=origen, butacas=butacas)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


@dataclass
class _Ficha:
    texto: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def leer_pdf(contenido: bytes) -> Lectura:
    from pdfminer.layout import LTChar, LTCurve

    try:
        pagina, tintas = _primera_pagina(contenido)
    except MapaInvalido:
        raise
    except Exception as exc:
        raise MapaInvalido(
            "No pudimos leer el PDF. Si tiene contraseña, quítasela y vuelve "
            "a guardarlo."
        ) from exc
    alto_pagina = pagina.height

    letras: list[LTChar] = []
    casillas: list[tuple[float, float, float, float, str]] = []

    def recorrer(obj) -> None:
        if isinstance(obj, LTChar):
            letras.append(obj)
            return
        if isinstance(obj, LTCurve) and getattr(obj, "fill", False):
            tinta = tintas.get(getattr(obj, "espacio_de_color", ""))
            valor = getattr(obj, "non_stroking_color", None)
            color = _hex(tinta(valor) if tinta else valor)
            # Casillas, no el fondo de toda la hoja ni lineas sueltas.
            if color and 2 < obj.width < pagina.width / 3 and 2 < obj.height < 80:
                # Con y hacia abajo, como las fichas y como el SVG.
                casillas.append(
                    (obj.x0, alto_pagina - obj.y1, obj.x1, alto_pagina - obj.y0, color)
                )
        for hijo in getattr(obj, "_objs", []) or []:
            recorrer(hijo)

    recorrer(pagina)
    return _por_texto("pdf", _fichas(letras, alto_pagina), casillas)


def _por_texto(origen: str, fichas: list[_Ficha], casillas) -> Lectura:
    """Butacas de un plano dibujado: cada numero, su fila y el color de su casilla.

    Lo comparten el PDF y el SVG sin convencion. `casillas` son
    (x0, y0, x1, y1, color) con y hacia abajo, igual que las fichas.
    """
    nombre = "PDF" if origen == "pdf" else "SVG"
    numeros = [f for f in fichas if _NUMERO.fullmatch(f.texto)]
    etiquetas = [f for f in fichas if _FILA.fullmatch(f.texto)]
    if not numeros:
        raise MapaInvalido(
            f"No encontramos números de butaca en el {nombre}. Si es un escaneo "
            "o una foto, no trae el texto que se necesita: pide el archivo "
            "original (Publisher, Excel, AutoCAD) o carga el mapa por bloques."
        )
    if not etiquetas:
        raise MapaInvalido(
            "Encontramos números pero ninguna letra de fila (A, B, AA...). El "
            f"{nombre} necesita las letras de las filas junto a cada renglón."
        )

    # Separacion tipica entre renglones: para saber cuando un numero no
    # tiene fila cerca.
    alturas = sorted({round(e.cy, 1) for e in etiquetas})
    pasos = [b - a for a, b in itertools.pairwise(alturas) if b - a > 1]
    tolerancia = (statistics.median(pasos) * 0.6) if pasos else 10.0

    avisos: list[str] = []
    sin_fila = 0
    butacas: list[ButacaLeida] = []
    for n in numeros:
        cerca = min(etiquetas, key=lambda e: abs(e.cy - n.cy))
        if abs(cerca.cy - n.cy) > tolerancia:
            sin_fila += 1
            continue
        color = _color_de(n, casillas)
        butacas.append(
            ButacaLeida(
                zona="", fila=cerca.texto, numero=n.texto, x=n.cx, y=n.cy, color=color
            )
        )
    if sin_fila:
        avisos.append(
            f"{sin_fila} números no tienen una letra de fila en su renglón y no se "
            "tomaron como butacas. Revisa el plano si esperabas más lugares."
        )

    # Zonas por color, de adelante (arriba) hacia atras.
    primera_y: dict[str | None, float] = {}
    for b in butacas:
        primera_y[b.color] = min(primera_y.get(b.color, b.y), b.y)
    con_color = sorted((c for c in primera_y if c), key=lambda c: primera_y[c])
    nombres: dict[str | None, str] = {
        c: f"Zona {i}" for i, c in enumerate(con_color, 1)
    }
    nombres[None] = "Sin color"
    for b in butacas:
        b.zona = nombres[b.color]
    if None in primera_y:
        avisos.append(
            "Algunas butacas no están sobre una casilla de color; quedaron en "
            'la zona "Sin color".'
        )
    return Lectura(origen=origen, butacas=butacas, avisos=avisos)


def _primera_pagina(contenido: bytes):
    """La primera pagina ya acomodada, y como convertir sus tintas a color.

    pdfminer no distingue una tinta Pantone (espacio "Separation") de otra:
    las dos llegan como "Separation" con un solo numero (cuanta tinta). Aqui
    se le pone a cada una su propio nombre y se guarda su formula, para que
    dos verdes Pantone distintos no se confundan con el blanco.
    """
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LAParams, LTCurve
    from pdfminer.pdfcolor import PDFColorSpace
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdftypes import dict_value, resolve1, stream_value
    from pdfminer.psparser import literal_name

    tintas: dict[str, object] = {}

    class Interprete(PDFPageInterpreter):
        def init_resources(self, resources):
            super().init_resources(resources)
            espacios = dict_value(resolve1((resources or {}).get("ColorSpace", {})))
            for nombre, spec in espacios.items():
                spec = resolve1(spec)
                if not (isinstance(spec, list) and len(spec) == 4):
                    continue
                if literal_name(spec[0]) != "Separation":
                    continue
                clave = f"tinta:{nombre}"
                self.csmap[nombre] = PDFColorSpace(clave, 1)
                try:
                    tintas[clave] = _formula_de_tinta(
                        literal_name(resolve1(spec[2])), stream_value(resolve1(spec[3]))
                    )
                except Exception:
                    continue

    class Acomodador(PDFPageAggregator):
        def paint_path(self, gstate, stroke, fill, evenodd, path):
            antes = len(self.cur_item._objs)
            super().paint_path(gstate, stroke, fill, evenodd, path)
            for obj in self.cur_item._objs[antes:]:
                if isinstance(obj, LTCurve):
                    obj.espacio_de_color = getattr(gstate.ncs, "name", "")

    gestor = PDFResourceManager()
    acomodador = Acomodador(gestor, laparams=LAParams())
    interprete = Interprete(gestor, acomodador)
    paginas = PDFPage.get_pages(io.BytesIO(contenido), maxpages=1)
    pagina = next(iter(paginas), None)
    if pagina is None:
        raise MapaInvalido("El PDF no tiene páginas.")
    interprete.process_page(pagina)
    return acomodador.get_result(), tintas


def _formula_de_tinta(alterno: str, funcion):
    """De "cuanta tinta" (0 a 1) al color alterno, con la formula del PDF.

    Solo funciones tipo 2 (interpolacion) y tipo 4 con aritmetica de pila,
    que es lo que escriben Publisher, Illustrator y Excel. Nada se ejecuta:
    se interpreta un puñado de operaciones con un tope de pasos.
    """
    datos = funcion.attrs if hasattr(funcion, "attrs") else funcion
    tipo = datos.get("FunctionType")
    if tipo == 2:
        c0 = [float(v) for v in datos.get("C0", [0.0])]
        c1 = [float(v) for v in datos.get("C1", [1.0])]
        n = float(datos.get("N", 1))

        def formula(t: float) -> tuple[float, ...]:
            return tuple(a + (t**n) * (b - a) for a, b in zip(c0, c1, strict=False))

    elif tipo == 4:
        codigo = funcion.get_data().decode("latin-1")
        fichas = codigo.replace("{", " ").replace("}", " ").split()

        def formula(t: float) -> tuple[float, ...]:
            return tuple(_pila(fichas, [t]))

    else:
        raise ValueError(tipo)

    def a_color(valor) -> tuple[float, ...] | None:
        if isinstance(valor, (tuple, list)):
            valor = valor[0] if valor else 1.0
        resultado = formula(float(valor if valor is not None else 1.0))
        if alterno == "DeviceGray":
            return resultado[:1]
        return resultado[:3] if alterno == "DeviceRGB" else resultado[:4]

    return a_color


def _pila(fichas: list[str], pila: list[float]) -> list[float]:
    for paso, f in enumerate(fichas):
        if paso > 200:
            raise ValueError("formula demasiado larga")
        if f == "dup":
            pila.append(pila[-1])
        elif f == "exch":
            pila[-1], pila[-2] = pila[-2], pila[-1]
        elif f == "pop":
            pila.pop()
        elif f in ("mul", "add", "sub", "div"):
            b, a = pila.pop(), pila.pop()
            pila.append(
                a * b
                if f == "mul"
                else a + b
                if f == "add"
                else a - b
                if f == "sub"
                else a / b
            )
        elif f == "neg":
            pila.append(-pila.pop())
        elif f == "index":
            n = int(pila.pop())
            pila.append(pila[-1 - n])
        else:
            pila.append(float(f))  # cualquier otra palabra: ValueError
    return pila


def _fichas(letras, alto_pagina: float) -> list[_Ficha]:
    """Junta letras contiguas en palabras ("15", "AA") con su caja.

    pdfminer agrupa por renglon, y en un plano un renglon trae decenas de
    numeros: se separa por espacio o por hueco entre letras.
    """
    letras = sorted(letras, key=lambda c: (round(alto_pagina - c.y1, 0), c.x0))
    fichas: list[_Ficha] = []
    actual: _Ficha | None = None
    for c in letras:
        t = c.get_text()
        if not t.strip():
            actual = None
            continue
        y0, y1 = alto_pagina - c.y1, alto_pagina - c.y0
        if (
            actual is not None
            and abs(actual.y0 - y0) < c.height * 0.5
            and 0 <= c.x0 - actual.x1 < c.width * 0.6
        ):
            actual.texto += t
            actual.x1 = c.x1
            actual.y1 = max(actual.y1, y1)
            continue
        actual = _Ficha(t, c.x0, y0, c.x1, y1)
        fichas.append(actual)
    for f in fichas:
        f.texto = f.texto.strip()
    return fichas


def _color_de(f: _Ficha, casillas) -> str | None:
    """El color de la casilla mas chica que contiene el centro del numero."""
    x, y = f.cx, f.cy
    dentro = [
        ((x1 - x0) * (y1 - y0), color)
        for x0, y0, x1, y1, color in casillas
        if x0 <= x <= x1 and y0 <= y <= y1
    ]
    return min(dentro)[1] if dentro else None


def _hex(color) -> str | None:
    """Color de pdfminer (gris, RGB o CMYK de 0 a 1) a #RRGGBB."""
    if color is None:
        return None
    if isinstance(color, (int, float)):
        color = (color,)
    try:
        valores = [float(v) for v in color]
    except (TypeError, ValueError):
        return None
    if len(valores) == 1:
        r = g = b = valores[0]
    elif len(valores) == 3:
        r, g, b = valores
    elif len(valores) == 4:
        c, m, yy, k = valores
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - yy) * (1 - k)
    else:
        return None
    rgb = [max(0, min(255, round(v * 255))) for v in (r, g, b)]
    # Blanco: el fondo, no una casilla de zona.
    if min(rgb) >= 245:
        return None
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------
#
# Dos formas, en este orden:
#
# 1. Con convencion (docs/mapas-svg.md): cada butaca es una figura cuyo
#    nombre es su asiento ("A-12", "AA-3"), dentro de una capa con el nombre
#    de la zona ("Luneta"). Asi lo exportan Illustrator (id) e Inkscape
#    (inkscape:label) sin tocar codigo. Tambien sirven data-fila,
#    data-numero y data-zona.
# 2. Sin convencion: como el PDF. Numeros con su letra de fila y la zona por
#    el color de la casilla.
#
# Se lee con la libreria estandar: expat 2.4+ corta las bombas de entidades
# y no descarga entidades externas. Ademas hay tope de elementos.

MAX_ELEMENTOS_SVG = 200_000
# "A-12", "AA3", "A 7" o, con fila que lleva numero (palcos), "P1-4".
_ASIENTO = re.compile(
    r"(?P<fila>[A-Za-z]{1,3}\d{0,2})[-_ ](?P<numero>\d{1,4})"
    r"|(?P<fila_sola>[A-Za-z]{1,3})(?P<numero_pegado>\d{1,4})"
)
_FIGURAS = {"circle", "ellipse", "rect", "path", "polygon", "polyline", "use", "line"}
_NOMBRES_DE_COLOR = {
    "black": "#000000", "white": None, "red": "#FF0000", "green": "#008000",
    "blue": "#0000FF", "yellow": "#FFFF00", "orange": "#FFA500",
    "purple": "#800080", "pink": "#FFC0CB", "gray": "#808080", "grey": "#808080",
}  # fmt: skip

Matriz = tuple[float, float, float, float, float, float]
_IDENTIDAD: Matriz = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def leer_svg(contenido: bytes) -> Lectura:
    import xml.etree.ElementTree as ET

    try:
        raiz = ET.fromstring(contenido)
    except ET.ParseError as exc:
        raise MapaInvalido(
            "No pudimos leer el SVG. Vuelve a exportarlo desde el programa de "
            "diseño (Illustrator: Archivo > Exportar > SVG)."
        ) from exc
    if _local(raiz.tag) != "svg":
        raise MapaInvalido("El archivo no es un SVG.")
    if sum(1 for _ in raiz.iter()) > MAX_ELEMENTOS_SVG:
        raise MapaInvalido("El SVG trae demasiados elementos para un mapa.")

    clases = _estilos_por_clase(raiz)
    con_nombre: list[ButacaLeida] = []
    fichas: list[_Ficha] = []
    casillas: list[tuple[float, float, float, float, str]] = []

    def recorrer(el, m: Matriz, relleno: str | None, zona: str | None) -> None:
        etiqueta = _local(el.tag)
        if etiqueta in {"defs", "symbol", "clipPath", "mask", "style", "title", "desc"}:
            return
        m = _por(m, _transformacion(el.get("transform", "")))
        relleno = _relleno(el, clases, relleno)
        asiento = _asiento(el)
        if asiento and (etiqueta in _FIGURAS or etiqueta == "g"):
            caja = _caja_de(el, m) if etiqueta != "g" else _caja_de_grupo(el, m)
            if caja:
                color = relleno if etiqueta != "g" else _relleno_de_grupo(el, clases)
                con_nombre.append(
                    ButacaLeida(
                        zona=(el.get("data-zona") or zona or ""),
                        fila=asiento[0],
                        numero=asiento[1],
                        x=(caja[0] + caja[2]) / 2,
                        y=(caja[1] + caja[3]) / 2,
                        color=_hex_css(color),
                        kill=(
                            el.get("data-kill", "").lower() in {"1", "si", "sí", "true"}
                        ),
                    )
                )
            return
        if etiqueta == "g":
            nombre = _nombre_de(el)
            if nombre and not _ASIENTO.fullmatch(nombre):
                zona = el.get("data-zona") or nombre
            for hijo in el:
                recorrer(hijo, m, relleno, zona)
            return
        if etiqueta == "text":
            fichas.extend(_fichas_de_texto(el, m))
            return
        if etiqueta in _FIGURAS:
            color = _hex_css(relleno)
            caja = _caja_de(el, m)
            if color and caja:
                casillas.append((*caja, color))
            return
        for hijo in el:
            recorrer(hijo, m, relleno, zona)

    recorrer(raiz, _IDENTIDAD, "#000000", None)

    if con_nombre:
        return _de_convencion(con_nombre)
    return _por_texto("svg", fichas, casillas)


def _de_convencion(butacas: list[ButacaLeida]) -> Lectura:
    """Zonas por capa; lo que no venga en una capa con nombre, por color."""
    avisos: list[str] = []
    sin_capa = [b for b in butacas if not b.zona]
    if sin_capa:
        colores: dict[str | None, float] = {}
        for b in sin_capa:
            colores[b.color] = min(colores.get(b.color, b.y), b.y)
        orden = sorted((c for c in colores if c), key=lambda c: colores[c])
        nombres: dict[str | None, str] = {
            c: f"Zona {i}" for i, c in enumerate(orden, 1)
        }
        nombres[None] = "Sin zona"
        for b in sin_capa:
            b.zona = nombres[b.color]
        avisos.append(
            f"{len(sin_capa)} butacas no están dentro de una capa con nombre de "
            "zona; se agruparon por color."
        )
    return Lectura(origen="svg", butacas=butacas, avisos=avisos)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _nombre_de(el) -> str:
    """El nombre que le puso la persona: data-, inkscape:label o id de Illustrator."""
    for clave, valor in el.attrib.items():
        if _local(clave) == "label" and valor.strip():
            return valor.strip()
    crudo = el.get("id", "")
    # Illustrator codifica simbolos (_x2D_ es "-") y numera repetidos (_1_).
    crudo = re.sub(r"_x([0-9A-Fa-f]{2,4})_", lambda m: chr(int(m.group(1), 16)), crudo)
    crudo = re.sub(r"_\d+_$", "", crudo)
    return crudo.replace("_", " ").strip()


def _asiento(el) -> tuple[str, str] | None:
    fila, numero = el.get("data-fila"), el.get("data-numero")
    if fila and numero:
        return fila.strip().upper()[:8], numero.strip()[:8]
    m = _ASIENTO.fullmatch(_nombre_de(el))
    if not m:
        return None
    fila = m.group("fila") or m.group("fila_sola")
    numero = m.group("numero") or m.group("numero_pegado")
    return fila.upper(), str(int(numero))


def _estilos_por_clase(raiz) -> dict[str, str]:
    """`.st0{fill:#F7EE41;}` de Illustrator: clase -> relleno."""
    reglas: dict[str, str] = {}
    for el in raiz.iter():
        if _local(el.tag) != "style" or not el.text:
            continue
        for selectores, cuerpo in re.findall(r"([^{}]+)\{([^}]*)\}", el.text):
            relleno = re.search(r"(?:^|;)\s*fill\s*:\s*([^;]+)", cuerpo)
            if not relleno:
                continue
            for sel in selectores.split(","):
                sel = sel.strip()
                if sel.startswith(".") and re.fullmatch(r"\.[\w-]+", sel):
                    reglas[sel[1:]] = relleno.group(1).strip()
    return reglas


def _relleno(el, clases: dict[str, str], heredado: str | None) -> str | None:
    estilo = re.search(r"(?:^|;)\s*fill\s*:\s*([^;]+)", el.get("style", ""))
    if estilo:
        return estilo.group(1).strip()
    if el.get("fill"):
        return el.get("fill")
    for clase in el.get("class", "").split():
        if clase in clases:
            return clases[clase]
    return heredado


def _relleno_de_grupo(el, clases: dict[str, str]) -> str | None:
    """El color de una butaca dibujada como grupo (figura + numero)."""
    for hijo in el.iter():
        if _local(hijo.tag) in _FIGURAS:
            color = _relleno(hijo, clases, None)
            if _hex_css(color):
                return color
    return None


def _hex_css(color: str | None) -> str | None:
    if not color:
        return None
    color = color.strip().lower()
    if color in {"none", "transparent"} or color.startswith("url("):
        return None
    if color in _NOMBRES_DE_COLOR:
        return _NOMBRES_DE_COLOR[color]
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", color)
    if m:
        h = m.group(1)
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return None if h == "ffffff" else "#" + h.upper()
    m = re.fullmatch(
        r"rgba?\(\s*([\d.]+)%?\s*,\s*([\d.]+)%?\s*,\s*([\d.]+)%?.*\)", color
    )
    if m:
        rgb = [max(0, min(255, round(float(v)))) for v in m.groups()]
        return None if min(rgb) >= 250 else "#{:02X}{:02X}{:02X}".format(*rgb)
    return None


def _num(valor: str | None, defecto: float = 0.0) -> float:
    m = re.match(r"\s*(-?[\d.]+(?:e-?\d+)?)", valor or "")
    try:
        return float(m.group(1)) if m else defecto
    except ValueError:
        return defecto


def _por(a: Matriz, b: Matriz) -> Matriz:
    """a x b: primero b (lo de adentro), luego a."""
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def _transformacion(texto: str) -> Matriz:
    import math

    m = _IDENTIDAD
    for nombre, args in re.findall(r"(\w+)\s*\(([^)]*)\)", texto):
        v = [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", args)]
        if nombre == "matrix" and len(v) == 6:
            t = tuple(v)
        elif nombre == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif nombre == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif nombre == "rotate" and v:
            a = math.radians(v[0])
            c, s_ = math.cos(a), math.sin(a)
            t = (c, s_, -s_, c, 0, 0)
            if len(v) == 3:
                t = _por(_por((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        else:
            continue
        m = _por(m, t)  # type: ignore[arg-type]
    return m


def _aplicar(m: Matriz, x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _caja(m: Matriz, puntos: list[tuple[float, float]]):
    if not puntos:
        return None
    ps = [_aplicar(m, x, y) for x, y in puntos]
    xs, ys = [p[0] for p in ps], [p[1] for p in ps]
    return min(xs), min(ys), max(xs), max(ys)


def _caja_de(el, m: Matriz):
    """Caja de una figura ya transformada, para su centro y su color."""
    e = _local(el.tag)
    g = el.get
    if e == "circle":
        cx, cy, r = _num(g("cx")), _num(g("cy")), _num(g("r"))
        return _caja(m, [(cx - r, cy - r), (cx + r, cy + r)])
    if e == "ellipse":
        cx, cy = _num(g("cx")), _num(g("cy"))
        rx, ry = _num(g("rx")), _num(g("ry"))
        return _caja(m, [(cx - rx, cy - ry), (cx + rx, cy + ry)])
    if e in {"rect", "use"}:
        x, y = _num(g("x")), _num(g("y"))
        w, h = _num(g("width")), _num(g("height"))
        return _caja(m, [(x, y), (x + w, y + h)])
    if e == "line":
        return _caja(
            m, [(_num(g("x1")), _num(g("y1"))), (_num(g("x2")), _num(g("y2")))]
        )
    if e in {"polygon", "polyline"}:
        v = [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", g("points", ""))]
        return _caja(m, list(zip(v[::2], v[1::2], strict=False)))
    if e == "path":
        return _caja(m, _puntos_de_ruta(g("d", "")))
    return None


def _caja_de_grupo(el, m: Matriz):
    cajas = []
    for hijo in el:
        if _local(hijo.tag) in _FIGURAS:
            c = _caja_de(hijo, _por(m, _transformacion(hijo.get("transform", ""))))
            if c:
                cajas.append(c)
    if not cajas:
        return None
    return (
        min(c[0] for c in cajas),
        min(c[1] for c in cajas),
        max(c[2] for c in cajas),
        max(c[3] for c in cajas),
    )


def _puntos_de_ruta(d: str) -> list[tuple[float, float]]:
    """Los puntos de una ruta: basta para su caja (las curvas, por sus extremos)."""
    fichas = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", d)
    puntos: list[tuple[float, float]] = []
    x = y = x0 = y0 = 0.0
    cmd = ""
    i = 0
    tam = {
        "M": 2,
        "L": 2,
        "H": 1,
        "V": 1,
        "C": 6,
        "S": 4,
        "Q": 4,
        "T": 2,
        "A": 7,
        "Z": 0,
    }
    while i < len(fichas) and len(puntos) < 5000:
        f = fichas[i]
        if f.isalpha():
            cmd = f
            i += 1
            if cmd in "Zz":
                x, y = x0, y0
            continue
        n = tam.get(cmd.upper(), 0)
        if n == 0 or i + n > len(fichas):
            break
        v = [float(t) for t in fichas[i : i + n]]
        i += n
        rel = cmd.islower()
        c = cmd.upper()
        if c == "H":
            x = x + v[0] if rel else v[0]
        elif c == "V":
            y = y + v[0] if rel else v[0]
        else:
            nx, ny = v[-2], v[-1]
            x, y = (x + nx, y + ny) if rel else (nx, ny)
        if c == "M":
            x0, y0 = x, y
            cmd = "l" if rel else "L"  # pares que siguen a un M son lineas
        puntos.append((x, y))
    return puntos


def _fichas_de_texto(el, m: Matriz) -> list[_Ficha]:
    """Un <text> como ficha: su posicion es la linea base, se estima su caja."""
    texto = "".join(el.itertext()).strip()
    if not texto:
        return []
    tam = _num(el.get("font-size"), 0) or _num(
        (re.search(r"font-size\s*:\s*([\d.]+)", el.get("style", "")) or [None, "10"])[
            1
        ],
        10,
    )
    x, y = _num(el.get("x")), _num(el.get("y"))
    # Un <tspan> con su propia posicion (Illustrator lo hace asi a veces).
    if not el.get("x"):
        for hijo in el:
            if _local(hijo.tag) == "tspan" and hijo.get("x"):
                x, y = _num(hijo.get("x")), _num(hijo.get("y"), y)
                break
    ancho = len(texto) * tam * 0.55
    ancla = (
        el.get("text-anchor")
        or (
            re.search(r"text-anchor\s*:\s*(\w+)", el.get("style", ""))
            or [None, "start"]
        )[1]
    )
    x0 = x - ancho / 2 if ancla == "middle" else x - ancho if ancla == "end" else x
    caja = _caja(m, [(x0, y - tam * 0.75), (x0 + ancho, y + tam * 0.2)])
    if not caja:
        return []
    # Un texto con varios numeros ("15 14 13"): cada palabra en su parte del ancho.
    fichas = []
    largo = len(texto)
    for palabra in re.finditer(r"\S+", texto):
        a, b = palabra.start() / largo, palabra.end() / largo
        fichas.append(
            _Ficha(
                palabra.group(),
                caja[0] + (caja[2] - caja[0]) * a,
                caja[1],
                caja[0] + (caja[2] - caja[0]) * b,
                caja[3],
            )
        )
    return fichas


# --------------------------------------------------------------------------
# Comun
# --------------------------------------------------------------------------


def _al_lienzo(lectura: Lectura) -> None:
    """Escala y centra en el lienzo de 1000 x 700, dejando arriba el escenario.

    El PDF viene en puntos de la hoja y un CSV puede venir en cualquier
    escala; lo que ya cabe se deja como esta.
    """
    bs = lectura.butacas
    xs, ys = [b.x for b in bs], [b.y for b in bs]
    cabe = (
        min(xs) >= MARGEN / 2
        and max(xs) <= ANCHO - MARGEN / 2
        and min(ys) >= ARRIBA / 2
        and max(ys) <= ALTO - MARGEN / 2
    )
    if lectura.origen != "pdf" and cabe:
        return
    ancho_m, alto_m = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    k = min((ANCHO - 2 * MARGEN) / ancho_m, (ALTO - ARRIBA - MARGEN) / alto_m)
    dx = (ANCHO - ancho_m * k) / 2
    x0, y0 = min(xs), min(ys)
    for b in bs:
        b.x = round(dx + (b.x - x0) * k, 1)
        b.y = round(ARRIBA + (b.y - y0) * k, 1)
    if lectura.origen != "pdf":
        lectura.avisos.append(
            "Las posiciones no cabían en el dibujo y se ajustaron a su tamaño."
        )


def revisar(butacas: list[ButacaLeida]) -> list[str]:
    """Lo que una persona tiene que confirmar contra el plano antes de guardar."""
    avisos: list[str] = []
    vistas = Counter((b.fila, b.numero) for b in butacas)
    repetidas = sorted(k for k, n in vistas.items() if n > 1)
    if repetidas:
        muestra = ", ".join(f"{f}-{n}" for f, n in repetidas[:8])
        avisos.append(
            f"{len(repetidas)} butacas aparecen más de una vez ({muestra}"
            f"{'…' if len(repetidas) > 8 else ''}). Si son lugares distintos, "
            "deben quedar en zonas distintas."
        )
    por_fila: dict[str, set[int]] = defaultdict(set)
    for b in butacas:
        if b.numero.isdigit():
            por_fila[b.fila].add(int(b.numero))
    huecos = []
    for fila, numeros in por_fila.items():
        faltan = sorted(set(range(1, max(numeros) + 1)) - numeros)
        # Nones o pares: la otra mitad no falta, es la numeracion del teatro.
        if faltan and len(faltan) < len(numeros) / 2:
            huecos.append(f"{fila}: {', '.join(map(str, faltan[:6]))}")
    if huecos:
        avisos.append(
            "Números que no aparecen dentro de una fila (revisa que de verdad no "
            "existan): " + "; ".join(sorted(huecos, key=_orden_fila)) + "."
        )
    return avisos


def _orden_fila(texto: str) -> tuple[int, str]:
    fila = texto.split(":")[0]
    return (len(fila), fila)


def a_csv(butacas: list[ButacaLeida]) -> str:
    salida = io.StringIO()
    escritor = csv.writer(salida, lineterminator="\n")
    escritor.writerow(["zona", "fila", "numero", "x", "y", "kill", "color"])
    for b in butacas:
        escritor.writerow(
            [b.zona, b.fila, b.numero, b.x, b.y, "si" if b.kill else "", b.color or ""]
        )
    return salida.getvalue()
