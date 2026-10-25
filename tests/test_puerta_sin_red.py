# ruff: noqa: F811  (fixtures importadas de otras pruebas)
"""La puerta sin internet (E3b).

Lo que importa:

- El paquete trae lo necesario para revisar boletos sin red (la llave del
  QR de cada boleto que puede entrar), solo a quien escanea esa funcion y
  solo cerca de su horario. Nunca el codigo impreso ni datos de contacto.
- Lo que se leyo sin red se juzga al subir, a la hora en que se leyo.
- Un boleto que entro por dos puertas sin red sale como "duplicado".
"""

import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.catalog import Performance
from app.models.sales import Scan, Ticket
from app.services.boletos import leer_cualquier_qr, paso_de, qr_rotatorio
from tests.test_boletos import qr_de
from tests.test_catalog import API
from tests.test_cortesias import de, esc, stripe_falso  # noqa: F401
from tests.test_escaner import leer, puerta  # noqa: F401

pytestmark = pytest.mark.asyncio


async def paquete(cliente_http, usuarios, pid, quien="acceso"):
    return await cliente_http.get(
        f"{API}/performances/{pid}/paquete-sin-red", headers=de(usuarios, quien)
    )


async def subir(cliente_http, usuarios, pid, lecturas, quien="acceso"):
    return await cliente_http.post(
        f"{API}/performances/{pid}/escaneos-sin-red",
        json={"lecturas": lecturas},
        headers=de(usuarios, quien),
    )


def lectura(qr, hace_segundos=0, scan_id=None):
    momento = datetime.now(UTC) - timedelta(seconds=hace_segundos)
    return {
        "client_scan_id": scan_id or uuid.uuid4().hex,
        "codigo": qr,
        "escaneado_en": momento.isoformat(),
    }


async def test_el_paquete_trae_lo_necesario_y_nada_mas(
    cliente_http,
    db,
    puerta,
    usuarios,
):
    r = await paquete(cliente_http, usuarios, puerta["pid"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    p = r.json()
    assert (p["paso_segundos"], p["tolerancia_pasos"]) == (30, 1)
    assert len(p["boletos"]) == 2
    texto = json.dumps(p)
    for b in puerta["boletos"]:
        assert b.codigo not in texto, "el codigo impreso no viaja"
    assert "@" not in texto, "ni correos"

    # Con la llave del paquete, el celular revisa el QR que muestra el comprador.
    b = puerta["boletos"][0]
    del_paquete = {x["u"]: x for x in p["boletos"]}
    x = del_paquete[base64.urlsafe_b64encode(b.uuid.bytes).rstrip(b"=").decode()]
    assert (x["estado"], x["titular"], x["cortesia"]) == ("valido", "Lupe", True)
    llave = base64.urlsafe_b64decode(x["k"] + "=")
    leido = leer_cualquier_qr(qr_de(b))
    assert qr_rotatorio(b.uuid, llave, leido.paso) == qr_de(b)


async def test_un_boleto_que_ya_entro_viene_sin_llave(
    cliente_http,
    db,
    puerta,
    usuarios,
):
    b = puerta["boletos"][0]
    await leer(cliente_http, usuarios, puerta["pid"], qr_de(b))
    x = next(
        y
        for y in (await paquete(cliente_http, usuarios, puerta["pid"])).json()[
            "boletos"
        ]
        if y["estado"] == "usado"
    )
    assert x["k"] is None
    assert x["entro_en"] is not None


async def test_solo_quien_escanea_esa_funcion(cliente_http, puerta, usuarios):
    ajeno = await paquete(cliente_http, usuarios, puerta["pid"], quien="org_b")
    inexistente = await paquete(cliente_http, usuarios, 999999, quien="org_b")
    assert ajeno.status_code == 404
    assert ajeno.json() == inexistente.json()
    assert (
        await paquete(cliente_http, usuarios, puerta["pid"], quien="org_a")
    ).status_code == 200


async def test_lejos_del_horario_no_se_entrega(cliente_http, db, puerta, usuarios):
    funcion = await db.get(Performance, puerta["pid"])
    funcion.inicia_en = datetime.now(UTC) + timedelta(days=3)
    await db.flush()
    r = await paquete(cliente_http, usuarios, puerta["pid"])
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "paquete_fuera_de_horario"


async def test_lo_leido_sin_red_se_juzga_a_su_hora(cliente_http, db, puerta, usuarios):
    b = puerta["boletos"][0]
    # Se leyo hace 3 minutos, con el QR de ese momento: hoy ese QR ya vencio,
    # pero se juzga a la hora en que se leyo.
    hace = 180
    qr = qr_de(b, desfase=paso_de(time.time() - hace) - paso_de(time.time()))
    una = lectura(qr, hace_segundos=hace)
    r = await subir(cliente_http, usuarios, puerta["pid"], [una])
    assert r.status_code == 200, r.text
    (res,) = r.json()["resultados"]
    assert res["escaneo"]["resultado"] == "valido"
    assert r.json()["contador"]["usados"] == 1
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "usado"
    scan = await db.scalar(
        select(Scan).where(Scan.client_scan_id == una["client_scan_id"])
    )
    assert scan.offline is True
    assert abs((scan.escaneado_en - datetime.now(UTC)).total_seconds() + hace) < 5

    # Subir otra vez la misma lectura no cambia nada.
    otra_vez = await subir(cliente_http, usuarios, puerta["pid"], [una])
    assert otra_vez.json()["resultados"][0]["escaneo"]["resultado"] == "valido"
    assert otra_vez.json()["contador"]["usados"] == 1


async def test_dos_puertas_sin_red_el_mismo_boleto(cliente_http, puerta, usuarios):
    b = puerta["boletos"][1]
    qr = qr_de(b)
    r = await subir(
        cliente_http, usuarios, puerta["pid"], [lectura(qr, 20), lectura(qr, 10)]
    )
    assert [x["escaneo"]["resultado"] for x in r.json()["resultados"]] == [
        "valido",
        "duplicado",
    ]


async def test_una_captura_vieja_no_pasa_aunque_sea_sin_red(
    cliente_http,
    puerta,
    usuarios,
):
    b = puerta["boletos"][0]
    r = await subir(cliente_http, usuarios, puerta["pid"], [lectura(qr_de(b, -10))])
    escaneo = r.json()["resultados"][0]["escaneo"]
    assert (escaneo["resultado"], escaneo["motivo"]) == ("invalido", "vencido")


async def test_lecturas_de_hace_mas_de_un_dia_no_cuentan(
    cliente_http,
    db,
    puerta,
    usuarios,
):
    b = puerta["boletos"][0]
    r = await subir(
        cliente_http,
        usuarios,
        puerta["pid"],
        [lectura(qr_de(b), hace_segundos=2 * 86400)],
    )
    assert r.json()["resultados"][0] == {
        "client_scan_id": r.json()["resultados"][0]["client_scan_id"],
        "escaneo": None,
        "rechazo": "muy_vieja",
    }
    assert (await db.get(Ticket, b.id, populate_existing=True)).estado == "valido"


async def test_hora_en_el_futuro_se_toma_como_ahora(cliente_http, puerta, usuarios):
    b = puerta["boletos"][0]
    r = await subir(cliente_http, usuarios, puerta["pid"], [lectura(qr_de(b), -600)])
    escaneo = r.json()["resultados"][0]["escaneo"]
    assert escaneo["resultado"] == "valido"
    en = datetime.fromisoformat(escaneo["escaneado_en"])
    assert abs((en - datetime.now(UTC)).total_seconds()) < 5
