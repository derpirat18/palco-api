"""Imagenes de los eventos (F7): se suben desde el panel, no se pegan ligas.

Lo que llega se trata como no confiable aunque lo suba PSD o un organizador:

- Se abre con Pillow y solo pasa si de verdad es JPG, PNG o WebP. La
  extension o el Content-Type no cuentan.
- Se vuelve a dibujar desde cero como JPG: no viaja nada que no sean los
  pixeles (ni la ubicacion GPS de la foto, ni datos escondidos en el
  archivo). JPG porque es lo unico que todos los correos muestran.
- Se reduce al tamano que usa el sitio (1600 px el lado mayor). No se
  recorta: cada lugar (cartelera, pagina del evento, boletos, correo) la
  encuadra a su medida.
- El nombre lo pone el servidor y cambia en cada subida: la imagen vieja
  sigue sirviendo a quien la tenga en cache, y la nueva se ve al momento.

En AWS va al bucket de medios (lo sirve CloudFront en /medios/...). En
local, sin bucket, a una carpeta que sirve la propia API.
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path
from uuid import UUID

from fastapi.concurrency import run_in_threadpool
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import ErrorDeDominio

# El tope de toda peticion (app/core/proteccion.py). El panel reduce la
# foto en el navegador antes de subirla, asi que una foto de celular cabe.
TAMANO_MAXIMO = settings.MAX_CUERPO_BYTES
MENSAJE_DE_TAMANO = (
    f"La imagen pesa más de {TAMANO_MAXIMO // (1024 * 1024)} MB. Usa una más ligera."
)
LADO_MAXIMO = 1600
LADO_MINIMO = 300
# Una imagen de pocos MB puede descomprimirse a gigas ("bomba"): se rechaza
# antes de dibujarla.
PIXELES_MAXIMOS = 40_000_000
FORMATOS = {"JPEG", "PNG", "WEBP"}
CALIDAD = 85


class ImagenInvalida(ErrorDeDominio):
    codigo = "imagen_invalida"
    http_status = 422


class MediosSinConfigurar(ErrorDeDominio):
    codigo = "medios_sin_configurar"
    http_status = 503


def procesar(datos: bytes) -> bytes:
    """La imagen limpia y reducida, como JPG. Levanta ImagenInvalida."""
    if not datos:
        raise ImagenInvalida("Elige una imagen.")
    if len(datos) > TAMANO_MAXIMO:
        raise ImagenInvalida(MENSAJE_DE_TAMANO)
    try:
        with Image.open(io.BytesIO(datos)) as original:
            if original.format not in FORMATOS:
                raise ImagenInvalida("Sube una imagen JPG, PNG o WebP.")
            ancho, alto = original.size
            if ancho * alto > PIXELES_MAXIMOS:
                raise ImagenInvalida(
                    "La imagen es demasiado grande. Usa una de menos de 40 megapíxeles."
                )
            if min(ancho, alto) < LADO_MINIMO:
                raise ImagenInvalida(
                    "La imagen es muy chica. Para que se vea bien, usa una de al "
                    "menos 1200 px de ancho."
                )
            imagen = ImageOps.exif_transpose(original)
            imagen.load()
    except ImagenInvalida:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise ImagenInvalida(
            "No pudimos leer la imagen. Sube un JPG, PNG o WebP."
        ) from exc

    # Transparencias sobre blanco: el JPG no tiene transparencia.
    if imagen.mode in ("RGBA", "LA") or (
        imagen.mode == "P" and "transparency" in imagen.info
    ):
        fondo = Image.new("RGB", imagen.size, (255, 255, 255))
        fondo.paste(imagen.convert("RGBA"), mask=imagen.convert("RGBA").getchannel("A"))
        imagen = fondo
    else:
        imagen = imagen.convert("RGB")
    imagen.thumbnail((LADO_MAXIMO, LADO_MAXIMO), Image.Resampling.LANCZOS)

    salida = io.BytesIO()
    # Sin exif ni icc: solo pixeles.
    imagen.save(salida, "JPEG", quality=CALIDAD, optimize=True, progressive=True)
    return salida.getvalue()


def _clave(event_uuid: UUID) -> str:
    return f"medios/eventos/{event_uuid}-{secrets.token_hex(4)}.jpg"


def _subir_a_s3(clave: str, datos: bytes) -> None:
    import boto3  # solo en AWS

    cliente = boto3.client("s3", region_name=settings.MEDIOS_REGION or None)
    cliente.put_object(
        Bucket=settings.MEDIOS_BUCKET,
        Key=clave,
        Body=datos,
        ContentType="image/jpeg",
        # El nombre cambia en cada subida: se puede guardar para siempre.
        CacheControl="public, max-age=31536000, immutable",
    )


def _guardar_local(clave: str, datos: bytes) -> None:
    destino = Path(settings.MEDIOS_DIR) / clave.removeprefix("medios/")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)


async def guardar_imagen_de_evento(
    event_uuid: UUID, datos: bytes, base_local: str
) -> str:
    """Procesa, guarda y regresa la direccion publica de la imagen."""
    limpia = await run_in_threadpool(procesar, datos)
    clave = _clave(event_uuid)
    if settings.MEDIOS_BUCKET:
        await run_in_threadpool(_subir_a_s3, clave, limpia)
        base = settings.MEDIOS_URL or settings.FRONT_URL
    elif settings.ENTORNO == "local":
        await run_in_threadpool(_guardar_local, clave, limpia)
        base = base_local
    else:
        raise MediosSinConfigurar(
            "Subir imágenes no está configurado en este entorno. Avísale a PSD."
        )
    return f"{base.rstrip('/')}/{clave}"
