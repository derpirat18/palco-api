"""Mapas desde el archivo del recinto: CSV, Excel y PDF (D2).

El PDF de prueba se arma aqui mismo, con lo que traen los planos reales:
casillas de color (RGB y una tinta Pantone con su formula), numeros de
butaca y letras de fila a los lados. Asi la prueba no depende de un plano
de un cliente.
"""

import base64
import io
import zipfile

import pytest

from app.services.mapas_archivo import (
    ALTO,
    ANCHO,
    MAX_BYTES,
    a_csv,
    leer_archivo,
)
from app.services.seat_maps import MapaInvalido, importar_mapa_csv
from tests.test_catalog import API, auth, token_de

# --- un PDF minimo, como lo escribiria Publisher ------------------------------


def _pdf(contenido: str, *, con_tinta: bool = True) -> bytes:
    """Una pagina carta con el contenido dado; F1 es Helvetica y Cs1 una tinta."""
    funcion = b"{dup 0.471 mul exch dup 0 mul exch dup 0.941 mul exch 0 mul}"
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> "
        + (
            b"/ColorSpace << /Cs1 [/Separation /PANTONE#20375#20C "
            b"/DeviceCMYK 6 0 R] >> "
            if con_tinta
            else b""
        )
        + b">> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(contenido.encode())
        + contenido.encode()
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /FunctionType 4 /Domain [0 1] /Range [0 1 0 1 0 1 0 1] "
        b"/Length %d >>\nstream\n" % len(funcion) + funcion + b"\nendstream",
    ]
    salida = io.BytesIO()
    salida.write(b"%PDF-1.7\n")
    posiciones = []
    for i, obj in enumerate(objetos, start=1):
        posiciones.append(salida.tell())
        salida.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = salida.tell()
    salida.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objetos) + 1))
    for p in posiciones:
        salida.write(b"%010d 00000 n \n" % p)
    salida.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objetos) + 1, xref)
    )
    return salida.getvalue()


def _casilla(x: float, y: float, color: str, texto: str) -> str:
    """Una butaca: el color de fondo y su numero encima."""
    return (
        f"{color}\n{x} {y} 16 12 re f\n"
        f"0 g BT /F1 8 Tf {x + 4} {y + 3} Td ({texto}) Tj ET\n"
    )


def _etiqueta(x: float, y: float, fila: str) -> str:
    return f"0 g BT /F1 8 Tf {x} {y + 3} Td ({fila}) Tj ET\n"


ROJO = "1 0 0 rg"
AZUL = "0 0 1 rg"
TINTA = "/Cs1 cs 1 scn"


def _plano() -> str:
    """Fila A en rojo (1-4), fila B en azul (1, 3, 4: falta la 2), fila C en tinta."""
    partes = []
    for fila, y, color, numeros in (
        ("A", 700, ROJO, [4, 3, 2, 1]),
        ("B", 684, AZUL, [4, 3, None, 1]),
        ("C", 668, TINTA, [4, 3, 2, 1]),
    ):
        partes.append(_etiqueta(80, y, fila))
        for i, n in enumerate(numeros):
            if n is not None:
                partes.append(_casilla(100 + i * 18, y, color, str(n)))
        partes.append(_etiqueta(180, y, fila))
    return "".join(partes)


# --- PDF ----------------------------------------------------------------------


def test_pdf_saca_butacas_filas_y_zonas_por_color():
    lectura = leer_archivo("plano.pdf", _pdf(_plano()))

    assert lectura.origen == "pdf"
    assert len(lectura.butacas) == 11
    # De adelante hacia atras: rojo, azul y la tinta Pantone (verde).
    assert lectura.zonas() == [
        ("Zona 1", "#FF0000", 4),
        ("Zona 2", "#0000FF", 3),
        ("Zona 3", "#87FF0F", 4),
    ]
    a1 = next(b for b in lectura.butacas if (b.fila, b.numero) == ("A", "1"))
    a4 = next(b for b in lectura.butacas if (b.fila, b.numero) == ("A", "4"))
    # Mismo orden que el plano (la 4 a la izquierda), dentro del lienzo.
    assert a4.x < a1.x
    assert all(0 <= b.x <= ANCHO and 0 <= b.y <= ALTO for b in lectura.butacas)
    assert any("B: 2" in a for a in lectura.avisos)


def test_pdf_sin_texto_explica_que_hacer():
    # Solo casillas, como un plano escaneado o convertido a imagen.
    sin_texto = "1 0 0 rg 100 700 16 12 re f\n"
    with pytest.raises(MapaInvalido) as exc:
        leer_archivo("escaneado.pdf", _pdf(sin_texto))
    assert "escaneo" in exc.value.mensaje


def test_pdf_sin_letras_de_fila():
    with pytest.raises(MapaInvalido) as exc:
        leer_archivo("plano.pdf", _pdf(_casilla(100, 700, ROJO, "1")))
    assert "letra de fila" in exc.value.mensaje


def test_un_archivo_que_dice_pdf_y_no_lo_es():
    with pytest.raises(MapaInvalido):
        leer_archivo("plano.pdf", b"esto no es un pdf")


def test_pdf_danado():
    with pytest.raises(MapaInvalido):
        leer_archivo("plano.pdf", b"%PDF-1.7\nbasura sin objetos")


def test_lo_leido_se_guarda_con_la_importacion_de_siempre():
    lectura = leer_archivo("plano.pdf", _pdf(_plano()))
    mapa = importar_mapa_csv("Prueba", a_csv(lectura.butacas))
    assert sum(len(z.butacas) for z in mapa.zonas) == 11
    assert [z.color for z in mapa.zonas] == ["#FF0000", "#0000FF", "#87FF0F"]


# --- CSV y Excel --------------------------------------------------------------

CSV = (
    "zona,fila,numero,x,y,kill,color\n"
    "Luneta,A,1,100,100,,#7C3AED\n"
    "Luneta,A,2,130,100,si,\n"
)


def test_csv_que_cabe_se_deja_igual():
    lectura = leer_archivo("mapa.csv", CSV.encode())
    assert [(b.x, b.y, b.kill) for b in lectura.butacas] == [
        (100, 100, False),
        (130, 100, True),
    ]
    assert lectura.avisos == []


def test_csv_que_no_cabe_se_ajusta_y_lo_avisa():
    grande = "zona,fila,numero,x,y\nL,A,1,0,0\nL,A,2,5000,0\nL,B,1,0,4000\n"
    lectura = leer_archivo("mapa.csv", grande.encode())
    assert all(0 <= b.x <= ANCHO and 0 <= b.y <= ALTO for b in lectura.butacas)
    assert any("se ajustaron" in a for a in lectura.avisos)


def test_columnas_con_acento_y_mayusculas():
    con_acento = "Zona,Fila,Número,X,Y\nL,A,1,100,100\n"
    assert leer_archivo("mapa.csv", con_acento.encode()).butacas[0].numero == "1"


def test_csv_sin_columnas():
    with pytest.raises(MapaInvalido) as exc:
        leer_archivo("mapa.csv", b"zona,asiento\nL,1\n")
    assert "fila" in exc.value.mensaje


def _xlsx(filas: list[list[object]]) -> bytes:
    import openpyxl

    libro = openpyxl.Workbook()
    for fila in filas:
        libro.active.append(fila)
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


def test_excel_con_las_mismas_columnas():
    datos = _xlsx(
        [
            ["zona", "fila", "numero", "x", "y", "kill"],
            ["Luneta", "a", 1.0, 100, 100, None],
            ["Luneta", "a", 2, 130.5, 100, "si"],
        ]
    )
    lectura = leer_archivo("mapa.xlsx", datos)
    assert lectura.origen == "xlsx"
    # 1.0 de Excel es la butaca "1"; la fila se pasa a mayusculas.
    assert [(b.fila, b.numero, b.kill) for b in lectura.butacas] == [
        ("A", "1", False),
        ("A", "2", True),
    ]


def test_excel_bomba_de_zip_se_rechaza_sin_abrirlo():
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * 45_000_000)
    with pytest.raises(MapaInvalido) as exc:
        leer_archivo("mapa.xlsx", salida.getvalue())
    assert "demasiado grande" in exc.value.mensaje


def test_archivo_demasiado_pesado():
    with pytest.raises(MapaInvalido):
        leer_archivo("mapa.csv", b"x" * (MAX_BYTES + 1))


# --- avisos -------------------------------------------------------------------


def test_avisa_butacas_repetidas():
    repetidas = "zona,fila,numero,x,y\nL,A,1,100,100\nG,A,1,100,300\n"
    lectura = leer_archivo("mapa.csv", repetidas.encode())
    assert any("A-1" in a and "más de una vez" in a for a in lectura.avisos)


def test_nones_y_pares_no_son_huecos():
    nones = "zona,fila,numero,x,y\n" + "".join(
        f"L,A,{n},{100 + n * 10},100\n" for n in (1, 3, 5, 7, 9)
    )
    assert leer_archivo("mapa.csv", nones.encode()).avisos == []


# --- por la API ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_lee_un_pdf(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    r = await cliente_http.post(
        f"{API}/seat-maps/leer-archivo",
        headers=auth(tk),
        json={
            "archivo": "MAPA TEATRO.pdf",
            "contenido_base64": base64.b64encode(_pdf(_plano())).decode(),
        },
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"origen", "total", "zonas", "butacas", "avisos"}
    assert (cuerpo["origen"], cuerpo["total"]) == ("pdf", 11)
    assert set(cuerpo["butacas"][0]) == {
        "zona",
        "fila",
        "numero",
        "x",
        "y",
        "color",
        "kill",
    }


@pytest.mark.asyncio
async def test_api_base64_roto(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    r = await cliente_http.post(
        f"{API}/seat-maps/leer-archivo",
        headers=auth(tk),
        json={"archivo": "mapa.pdf", "contenido_base64": "no es base64!!"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "mapa_invalido"


@pytest.mark.asyncio
async def test_solo_psd_lee_archivos(cliente_http, usuarios):
    tk = token_de(usuarios["org_a"])
    r = await cliente_http.post(
        f"{API}/seat-maps/leer-archivo",
        headers=auth(tk),
        json={
            "archivo": "mapa.csv",
            "contenido_base64": base64.b64encode(CSV.encode()).decode(),
        },
    )
    assert r.status_code == 403


# --- SVG ----------------------------------------------------------------------

# Como lo exporta Illustrator: capas con id, "-" codificado como _x2D_,
# repetidos con _1_, colores en clases y la capa movida con transform.
SVG_ILLUSTRATOR = """<?xml version="1.0" encoding="utf-8"?>
<svg version="1.1" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
<style type="text/css">
  .st0{fill:#7C3AED;}
  .st1,.st2{fill:#16A34A;}
</style>
<g id="Escenario"><rect x="100" y="10" width="200" height="20" fill="#222222"/></g>
<g id="Luneta" transform="translate(50,60)">
  <circle id="A_x2D_1" class="st0" cx="0" cy="0" r="6"/>
  <circle id="A_x2D_2" class="st0" cx="20" cy="0" r="6"/>
  <circle id="A_x2D_3_1_" class="st0" cx="40" cy="0" r="6"/>
</g>
<g id="Balcon_General">
  <g id="B-1">
    <rect class="st1" x="50" y="150" width="12" height="12"/>
    <text x="53" y="160">1</text>
  </g>
  <path id="B-2" class="st2" d="M70 150 h12 v12 h-12 z"/>
</g>
</svg>"""


def test_svg_de_illustrator_con_capas_por_zona():
    lectura = leer_archivo("plano.svg", SVG_ILLUSTRATOR.encode())
    assert lectura.origen == "svg"
    assert lectura.zonas() == [
        ("Luneta", "#7C3AED", 3),
        ("Balcon General", "#16A34A", 2),
    ]
    asientos = {(b.fila, b.numero) for b in lectura.butacas}
    assert asientos == {("A", "1"), ("A", "2"), ("A", "3"), ("B", "1"), ("B", "2")}
    a1 = next(b for b in lectura.butacas if (b.fila, b.numero) == ("A", "1"))
    a3 = next(b for b in lectura.butacas if (b.fila, b.numero) == ("A", "3"))
    b1 = next(b for b in lectura.butacas if (b.fila, b.numero) == ("B", "1"))
    # El translate de la capa cuenta: la A queda arriba de la B y en orden.
    assert a1.x < a3.x and a1.y < b1.y
    assert lectura.avisos == []


def test_svg_de_inkscape_y_con_data():
    svg = """<svg xmlns="http://www.w3.org/2000/svg"
      xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">
      <g inkscape:label="Palcos" inkscape:groupmode="layer">
        <rect inkscape:label="P1-1" x="10" y="10" width="10" height="10"
          style="fill:#e11d48"/>
        <rect inkscape:label="P1-2" x="30" y="10" width="10" height="10"
          style="fill:#e11d48"/>
      </g>
      <circle data-zona="Pista" data-fila="AA" data-numero="7" data-kill="si"
        cx="50" cy="80" r="5" fill="#000"/>
    </svg>"""
    lectura = leer_archivo("plano.svg", svg.encode())
    assert [(b.zona, b.fila, b.numero, b.kill) for b in lectura.butacas] == [
        ("Palcos", "P1", "1", False),
        ("Palcos", "P1", "2", False),
        ("Pista", "AA", "7", True),
    ]


def test_svg_sin_convencion_se_lee_como_el_pdf():
    partes = ['<svg xmlns="http://www.w3.org/2000/svg">']
    for fila, y, color in (("A", 20, "#FF0000"), ("B", 40, "#0000FF")):
        partes.append(f'<text x="5" y="{y + 9}" font-size="8">{fila}</text>')
        for i, n in enumerate((3, 2, 1)):
            x = 20 + i * 18
            partes.append(
                f'<rect x="{x}" y="{y}" width="16" height="12" fill="{color}"/>'
            )
            partes.append(f'<text x="{x + 4}" y="{y + 9}" font-size="8">{n}</text>')
    partes.append("</svg>")
    lectura = leer_archivo("plano.svg", "".join(partes).encode())
    assert len(lectura.butacas) == 6
    assert lectura.zonas() == [("Zona 1", "#FF0000", 3), ("Zona 2", "#0000FF", 3)]


def test_svg_sin_butacas():
    with pytest.raises(MapaInvalido) as exc:
        leer_archivo(
            "plano.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        )
    assert "SVG" in exc.value.mensaje


def test_svg_con_bomba_de_entidades_se_rechaza():
    entidades = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {c} "{("&" + p + ";") * 10}">'
        for p, c in zip("abcdefg", "bcdefgh", strict=True)
    )
    bomba = f'<?xml version="1.0"?><!DOCTYPE svg [{entidades}]><svg>&h;</svg>'
    with pytest.raises(MapaInvalido):
        leer_archivo("plano.svg", bomba.encode())


def test_svg_no_lee_archivos_del_servidor():
    externa = (
        '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<svg xmlns="http://www.w3.org/2000/svg"><text x="1" y="1">&x;</text></svg>'
    )
    with pytest.raises(MapaInvalido):
        leer_archivo("plano.svg", externa.encode())


def test_un_xml_que_no_es_svg():
    with pytest.raises(MapaInvalido):
        leer_archivo("plano.svg", b"<?xml version='1.0'?><html><svg></svg></html>")
