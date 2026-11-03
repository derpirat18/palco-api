"""Los reportes (RP3) como archivo: Excel, CSV y PDF.

- Excel: una hoja con el resumen (filtros, indicadores y notas) y otra con
  la tabla, con formato de dinero, fecha y porcentaje.
- CSV: solo la tabla, en UTF-8 con BOM (Excel lo abre con acentos), numeros
  sin formato. Un texto que empieza con = + - @ se manda con ' adelante: asi
  nadie mete una formula en un nombre.
- PDF: horizontal, con la marca, los filtros, los indicadores y la tabla.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from app.core.config import settings
from app.services.reportes import Columna, Reporte

FORMATOS = {"xlsx", "csv", "pdf"}
TIPOS_MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
}
_PELIGROSOS = ("=", "+", "-", "@", "\t", "\r")


def texto(valor: Any, formato: str) -> str:
    """Como se lee un valor en pantalla y en el PDF."""
    if valor is None or valor == "":
        return "—" if formato != "texto" else ""
    if formato == "dinero":
        return f"${Decimal(valor):,.2f}"
    if formato == "pct":
        return f"{Decimal(valor):.1f} %"
    if formato == "entero":
        return f"{int(valor):,}"
    if formato == "fechahora" and isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y %H:%M")
    if formato == "fecha" and isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    return str(valor)


def nombre_de_archivo(r: Reporte, formato: str) -> str:
    return f"reporte-{r.tipo}-{datetime.now():%Y-%m-%d}.{formato}"


def generar(r: Reporte, formato: str, filtros: list[str]) -> bytes:
    if formato == "csv":
        return _csv(r)
    if formato == "pdf":
        return _pdf(r, filtros)
    return _xlsx(r, filtros)


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def _seguro(valor: str) -> str:
    return "'" + valor if valor.startswith(_PELIGROSOS) else valor


def _csv(r: Reporte) -> bytes:
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow([c.titulo for c in r.columnas])
    for fila in r.filas:
        renglon = []
        for c in r.columnas:
            v = fila.get(c.clave)
            if v is None:
                renglon.append("")
            elif c.formato in ("dinero", "pct"):
                renglon.append(f"{Decimal(v):.2f}")
            elif c.formato == "entero":
                renglon.append(str(int(v)))
            elif isinstance(v, datetime):
                renglon.append(v.strftime("%d/%m/%Y %H:%M"))
            else:
                renglon.append(_seguro(str(v)))
        w.writerow(renglon)
    return ("﻿" + salida.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

_NUMERO = {
    "dinero": '"$"#,##0.00',
    "pct": '0.0" %"',
    "entero": "#,##0",
    "fechahora": "dd/mm/yyyy hh:mm",
    "fecha": "dd/mm/yyyy",
}
_TITULO = Font(bold=True, color="FFFFFF")
_FONDO = PatternFill("solid", fgColor="1E1B4B")


def _celda(hoja, valor: Any, c: Columna) -> WriteOnlyCell:
    if c.formato == "texto" or isinstance(valor, str):
        celda = WriteOnlyCell(hoja, value=_seguro(valor) if valor else "")
        celda.data_type = "s"
        return celda
    if c.formato in ("dinero", "pct") and valor is not None:
        valor = float(valor)
    celda = WriteOnlyCell(hoja, value=valor)
    if c.formato in _NUMERO:
        celda.number_format = _NUMERO[c.formato]
    return celda


def _xlsx(r: Reporte, filtros: list[str]) -> bytes:
    libro = Workbook(write_only=True)
    resumen = libro.create_sheet("Resumen")
    resumen.column_dimensions["A"].width = 34
    resumen.column_dimensions["B"].width = 40
    titulo = WriteOnlyCell(resumen, value=f"{settings.MARCA} · {r.titulo}")
    titulo.font = Font(bold=True, size=14)
    resumen.append([titulo])
    resumen.append([f"Generado el {datetime.now():%d/%m/%Y %H:%M}"])
    resumen.append([])
    for linea in filtros:
        resumen.append([linea])
    resumen.append([])
    for ind in r.indicadores:
        etiqueta = WriteOnlyCell(resumen, value=ind.etiqueta)
        etiqueta.font = Font(bold=True)
        resumen.append(
            [etiqueta, _celda(resumen, ind.valor, Columna("x", "", ind.formato))]
        )
    if r.notas:
        resumen.append([])
        for nota in r.notas:
            resumen.append([nota])

    hoja = libro.create_sheet(r.titulo[:30])
    hoja.freeze_panes = "A2"
    for i, c in enumerate(r.columnas):
        ancho = 14 if c.formato != "texto" else 26
        hoja.column_dimensions[_letra(i)].width = ancho
    cabeza = []
    for c in r.columnas:
        celda = WriteOnlyCell(hoja, value=c.titulo)
        celda.font = _TITULO
        celda.fill = _FONDO
        celda.alignment = Alignment(vertical="center")
        cabeza.append(celda)
    hoja.append(cabeza)
    for fila in r.filas:
        hoja.append([_celda(hoja, fila.get(c.clave), c) for c in r.columnas])
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


def _letra(i: int) -> str:
    letras = ""
    i += 1
    while i:
        i, resto = divmod(i - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def _pdf(r: Reporte, filtros: list[str]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        LongTable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    noche = colors.HexColor("#080B1C")
    violeta = colors.HexColor("#7938E8")
    gris = colors.HexColor("#565C75")
    linea = colors.HexColor("#E2E8F0")
    claro = colors.HexColor("#F6F8FF")

    salida = io.BytesIO()
    ancho, alto = landscape(letter)
    margen = 12 * mm
    generado = f"Generado el {datetime.now():%d/%m/%Y %H:%M}"

    def marco(lienzo, doc):
        lienzo.saveState()
        lienzo.setFillColor(noche)
        lienzo.rect(0, alto - 18 * mm, ancho, 18 * mm, stroke=0, fill=1)
        lienzo.setFillColor(violeta)
        lienzo.rect(0, alto - 18.8 * mm, ancho, 0.8 * mm, stroke=0, fill=1)
        lienzo.setFillColor(colors.white)
        lienzo.setFont("Helvetica-Bold", 13)
        lienzo.drawString(margen, alto - 11.5 * mm, f"{settings.MARCA}  ·  {r.titulo}")
        lienzo.setFont("Helvetica", 8)
        lienzo.drawRightString(ancho - margen, alto - 11.5 * mm, generado)
        lienzo.setFillColor(gris)
        lienzo.drawRightString(ancho - margen, 7 * mm, f"Página {doc.page}")
        lienzo.restoreState()

    doc = SimpleDocTemplate(
        salida,
        pagesize=landscape(letter),
        leftMargin=margen,
        rightMargin=margen,
        topMargin=24 * mm,
        bottomMargin=14 * mm,
        title=f"{settings.MARCA} · {r.titulo}",
    )
    normal = ParagraphStyle("n", fontName="Helvetica", fontSize=7.5, leading=9.5)
    # Con muchas columnas (Taquilla) el encabezado va mas chico.
    cabeza = ParagraphStyle(
        "c",
        parent=normal,
        fontName="Helvetica-Bold",
        textColor=colors.white,
        fontSize=7 if len(r.columnas) <= 12 else 6,
        leading=8.5 if len(r.columnas) <= 12 else 7.5,
    )
    pequena = ParagraphStyle("p", parent=normal, fontSize=8, textColor=gris)
    piezas: list = []
    if filtros:
        piezas.append(Paragraph(" · ".join(filtros), pequena))
        piezas.append(Spacer(1, 4 * mm))

    # Indicadores en cajas.
    if r.indicadores:
        etiqueta = ParagraphStyle("e", parent=normal, fontSize=7.5, textColor=gris)
        valor = ParagraphStyle(
            "v", parent=normal, fontName="Helvetica-Bold", fontSize=12, leading=15
        )
        cajas = [
            [
                [
                    Paragraph(_escapar(i.etiqueta), etiqueta),
                    Paragraph(texto(i.valor, i.formato), valor),
                ]
                for i in r.indicadores
            ]
        ]
        t = Table(
            cajas,
            colWidths=[(ancho - 2 * margen) / len(r.indicadores)] * len(r.indicadores),
        )
        t.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.5, linea),
                    ("INNERGRID", (0, 0), (-1, -1), 0.5, linea),
                    ("BACKGROUND", (0, 0), (-1, -1), claro),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        piezas += [t, Spacer(1, 5 * mm)]

    # La tabla: el texto ocupa el doble que un numero.
    peso = {"texto": 2.0, "fechahora": 1.35, "fecha": 1.0, "dinero": 1.3, "pct": 0.95}
    pesos = [peso.get(c.formato, 0.9) for c in r.columnas]
    total = sum(pesos)
    anchos = [(ancho - 2 * margen) * p / total for p in pesos]
    datos = [[Paragraph(c.titulo, cabeza) for c in r.columnas]]
    for fila in r.filas:
        # El texto se acomoda en renglones; los numeros no se parten.
        datos.append(
            [
                Paragraph(_escapar(texto(fila.get(c.clave), c.formato)), normal)
                if c.formato == "texto"
                else texto(fila.get(c.clave), c.formato)
                for c in r.columnas
            ]
        )
    if len(datos) == 1:
        datos.append(
            [Paragraph("Sin datos con estos filtros.", pequena)]
            + [""] * (len(r.columnas) - 1)
        )
    tabla = LongTable(datos, colWidths=anchos, repeatRows=1)
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1B4B")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, claro]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, linea),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 7.5),
                ("ALIGN", (0, 1), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    piezas.append(tabla)
    for nota in r.notas:
        piezas += [Spacer(1, 3 * mm), Paragraph(_escapar(nota), pequena)]
    doc.build(piezas, onFirstPage=marco, onLaterPages=marco)
    return salida.getvalue()


def _escapar(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
