"""Mapas de recintos desde el archivo que ya tiene el recinto (D2).

CSV, Excel o PDF terminan igual: una lista de butacas con zona, fila,
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
                casillas.append((obj.x0, obj.y0, obj.x1, obj.y1, color))
        for hijo in getattr(obj, "_objs", []) or []:
            recorrer(hijo)

    recorrer(pagina)
    fichas = _fichas(letras, alto_pagina)
    numeros = [f for f in fichas if _NUMERO.fullmatch(f.texto)]
    etiquetas = [f for f in fichas if _FILA.fullmatch(f.texto)]
    if not numeros:
        raise MapaInvalido(
            "No encontramos números de butaca en el PDF. Si es un escaneo o una "
            "foto, no trae el texto que se necesita: pide el archivo original "
            "(Publisher, Excel, AutoCAD) o carga el mapa por bloques."
        )
    if not etiquetas:
        raise MapaInvalido(
            "Encontramos números pero ninguna letra de fila (A, B, AA...). El "
            "PDF necesita las letras de las filas junto a cada renglón."
        )

    # Separacion tipica entre renglones: para saber cuando un numero no
    # tiene fila cerca.
    alturas = sorted({round(e.cy, 1) for e in etiquetas})
    pasos = [b - a for a, b in itertools.pairwise(alturas) if b - a > 1]
    tolerancia = (statistics.median(pasos) * 0.6) if pasos else 10.0

    avisos: list[str] = []
    sin_fila = 0
    colores_por_ficha: list[str | None] = []
    butacas: list[ButacaLeida] = []
    for n in numeros:
        cerca = min(etiquetas, key=lambda e: abs(e.cy - n.cy))
        if abs(cerca.cy - n.cy) > tolerancia:
            sin_fila += 1
            continue
        color = _color_de(n, casillas, alto_pagina)
        colores_por_ficha.append(color)
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
    return Lectura(origen="pdf", butacas=butacas, avisos=avisos)


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


def _color_de(f: _Ficha, casillas, alto_pagina: float) -> str | None:
    """El color de la casilla mas chica que contiene el centro del numero."""
    x, y = f.cx, alto_pagina - f.cy
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
