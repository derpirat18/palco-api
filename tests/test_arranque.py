"""La API arranca aunque no pueda crear la carpeta de imágenes locales.

En la imagen de Docker el código es de solo lectura y CI la prueba con
ENTORNO=local: si la carpeta de medios no se puede crear, la API debe
arrancar igual (sin servir /medios) en lugar de caerse.
"""

import os
import subprocess
import sys


def _arrancar(medios_dir: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "ENTORNO": "local",
        "MEDIOS_BUCKET": "",
        "MEDIOS_DIR": medios_dir,
    }
    codigo = (
        "from app.main import app; "
        "print('medios' in {getattr(r, 'name', None) for r in app.routes})"
    )
    return subprocess.run(
        [sys.executable, "-c", codigo], env=env, capture_output=True, text=True
    )


def test_arranca_sin_poder_crear_la_carpeta_de_medios(tmp_path):
    archivo = tmp_path / "no_es_carpeta"
    archivo.write_text("x")
    r = _arrancar(str(archivo / "medios"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False"
    assert "Sin carpeta de medios locales" in r.stderr


def test_con_carpeta_sirve_medios(tmp_path):
    r = _arrancar(str(tmp_path / "medios"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True"
