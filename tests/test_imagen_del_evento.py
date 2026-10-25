"""Imagen del evento subida desde el panel (F7).

Lo que importa:

- Solo pasa una imagen de verdad (JPG, PNG o WebP); lo demas se rechaza
  con un mensaje claro, aunque diga Content-Type image/png.
- Se guarda limpia: sin los datos de la foto (GPS), derecha, reducida y
  como JPG (lo unico que muestran todos los correos).
- Solo PSD y el organizador del evento la cambian.
"""

import io

import pytest
from PIL import Image

from app.core.config import settings
from app.services import medios
from tests.test_catalog import auth, crear_evento, crear_recinto, token_de

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def imagen(
    formato="JPEG", tamano=(2400, 1200), modo="RGB", color=(200, 30, 60), **kw
) -> bytes:
    buf = io.BytesIO()
    Image.new(modo, tamano, color).save(buf, formato, **kw)
    return buf.getvalue()


@pytest.fixture
def carpeta(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIOS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIOS_BUCKET", "")
    return tmp_path


async def preparar(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    return await crear_evento(cliente_http, tk, venue_id), tk


async def subir(cliente_http, tk, event_id, datos, tipo="image/jpeg"):
    return await cliente_http.put(
        f"{API}/events/{event_id}/imagen",
        content=datos,
        headers={**auth(tk), "Content-Type": tipo},
    )


def guardada(carpeta, url: str) -> Image.Image:
    ruta = carpeta / url.split("/medios/", 1)[1]
    return Image.open(ruta)


async def test_sube_y_queda_limpia_y_reducida(cliente_http, usuarios, carpeta):
    evento, tk = await preparar(cliente_http, usuarios)
    # Una foto de celular: de lado (Orientation 6) y con ubicacion GPS.
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x8825] = {2: (19.0, 26.0, 0.0)}
    r = await subir(
        cliente_http, tk, evento["id"], imagen(tamano=(3200, 1800), exif=exif)
    )
    assert r.status_code == 200, r.text
    url = r.json()["imagen_url"]
    assert "/medios/eventos/" in url and url.endswith(".jpg")

    img = guardada(carpeta, url)
    assert img.format == "JPEG"
    # Se enderezo (de lado: 1800 x 3200) y el lado mayor quedo en 1600.
    assert img.size == (900, 1600)
    assert not img.getexif(), "sin datos de la foto: ni GPS ni orientacion"


async def test_png_con_transparencia_queda_sobre_blanco(
    cliente_http, usuarios, carpeta
):
    evento, tk = await preparar(cliente_http, usuarios)
    datos = imagen("PNG", modo="RGBA", color=(0, 0, 0, 0))
    r = await subir(cliente_http, tk, evento["id"], datos, "image/png")
    assert r.status_code == 200, r.text
    img = guardada(carpeta, r.json()["imagen_url"]).convert("RGB")
    assert img.getpixel((10, 10)) == (255, 255, 255)


async def test_webp_tambien(cliente_http, usuarios, carpeta):
    evento, tk = await preparar(cliente_http, usuarios)
    r = await subir(cliente_http, tk, evento["id"], imagen("WEBP"), "image/webp")
    assert r.status_code == 200, r.text
    assert guardada(carpeta, r.json()["imagen_url"]).format == "JPEG"


async def test_cada_subida_tiene_otro_nombre(cliente_http, usuarios, carpeta):
    evento, tk = await preparar(cliente_http, usuarios)
    a = (await subir(cliente_http, tk, evento["id"], imagen())).json()["imagen_url"]
    b = (await subir(cliente_http, tk, evento["id"], imagen())).json()["imagen_url"]
    assert a != b, "la nueva se ve al momento aunque la vieja este en cache"


@pytest.mark.parametrize(
    ("datos", "tipo", "frase"),
    [
        (b"<?php echo 1; ?>", "image/png", "No pudimos leer"),
        (b"", "image/jpeg", "Elige una imagen"),
        (imagen("GIF", modo="P"), "image/png", "JPG, PNG o WebP"),
        (imagen(), "application/pdf", "JPG, PNG o WebP"),
        (imagen(tamano=(120, 90)), "image/jpeg", "muy chica"),
        # 50 megapixeles en pocos KB: se rechaza antes de dibujarla.
        (
            imagen("PNG", tamano=(10000, 5000), modo="1", color=1),
            "image/png",
            "demasiado grande",
        ),
    ],
)
async def test_lo_que_no_es_una_imagen_valida(
    cliente_http, usuarios, carpeta, datos, tipo, frase
):
    evento, tk = await preparar(cliente_http, usuarios)
    r = await subir(cliente_http, tk, evento["id"], datos, tipo)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["codigo"] == "imagen_invalida"
    assert frase in r.json()["error"]["mensaje"]
    assert list(carpeta.rglob("*.jpg")) == []


async def test_mas_de_4_mb(cliente_http, usuarios, carpeta):
    """El tope de toda peticion; el panel reduce la foto antes de subirla."""
    evento, tk = await preparar(cliente_http, usuarios)
    r = await subir(cliente_http, tk, evento["id"], b"\xff" * (4 * 1024 * 1024 + 1))
    assert r.status_code == 413
    with pytest.raises(medios.ImagenInvalida, match="4 MB"):
        medios.procesar(b"\xff" * (4 * 1024 * 1024 + 1))


async def test_solo_psd_y_su_organizador(cliente_http, usuarios, carpeta):
    evento, _ = await preparar(cliente_http, usuarios)
    datos = imagen()
    ajeno = await subir(cliente_http, token_de(usuarios["org_b"]), evento["id"], datos)
    inexistente = await subir(cliente_http, token_de(usuarios["org_b"]), 999999, datos)
    assert ajeno.status_code == 404
    assert ajeno.json() == inexistente.json()
    puerta = await subir(
        cliente_http, token_de(usuarios["acceso"]), evento["id"], datos
    )
    assert puerta.status_code == 403
    psd = await subir(cliente_http, token_de(usuarios["admin"]), evento["id"], datos)
    assert psd.status_code == 200


async def test_en_aws_va_al_bucket(cliente_http, usuarios, carpeta, monkeypatch):
    subidas = []
    monkeypatch.setattr(settings, "MEDIOS_BUCKET", "palco-sandbox-medios")
    monkeypatch.setattr(settings, "MEDIOS_URL", "https://pruebas.psdevents.com/")
    monkeypatch.setattr(
        medios, "_subir_a_s3", lambda clave, datos: subidas.append((clave, datos))
    )
    evento, tk = await preparar(cliente_http, usuarios)
    r = await subir(cliente_http, tk, evento["id"], imagen())
    assert r.status_code == 200, r.text
    ((clave, datos),) = subidas
    assert r.json()["imagen_url"] == f"https://pruebas.psdevents.com/{clave}"
    assert clave.startswith(f"medios/eventos/{evento['uuid']}-")
    assert datos[:3] == b"\xff\xd8\xff", "JPG"
    assert list(carpeta.rglob("*.jpg")) == [], "nada en disco"


async def test_fuera_de_local_sin_bucket_avisa(
    cliente_http, usuarios, carpeta, monkeypatch
):
    evento, tk = await preparar(cliente_http, usuarios)
    monkeypatch.setattr(settings, "ENTORNO", "dev")
    r = await subir(cliente_http, tk, evento["id"], imagen())
    assert r.status_code == 503
    assert r.json()["error"]["codigo"] == "medios_sin_configurar"
