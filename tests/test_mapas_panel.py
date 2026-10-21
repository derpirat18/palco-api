"""Vista previa y dibujo de mapas para el panel.

La vista previa sale del mismo generador que guarda: lo que PSD revisa en
pantalla es exactamente lo que se guarda. Y no guarda nada: revisar diez
veces un plano no deja diez mapas en el recinto.
"""

import pytest
from sqlalchemy import func, select

from app.models.catalog import Seat, SeatMap
from tests.test_catalog import (
    API,
    SPEC_MINIMA,
    auth,
    crear_mapa,
    crear_recinto,
    token_de,
)

pytestmark = pytest.mark.asyncio

CSV = (
    "zona,fila,numero,x,y,kill\n"
    "Luneta,A,1,100,100,\n"
    "Luneta,A,2,130,100,\n"
    "Gradas,B,1,100,200,si\n"
)


async def test_vista_previa_igual_a_lo_que_se_guarda(cliente_http, db, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)

    r = await cliente_http.post(
        f"{API}/seat-maps/vista-previa", headers=auth(tk), json=SPEC_MINIMA
    )

    assert r.status_code == 200, r.text
    vista = r.json()
    assert set(vista) == {
        "viewport",
        "zonas",
        "butacas",
        "total_butacas",
        "total_vendibles",
    }
    assert set(vista["butacas"][0]) == {"zona", "fila", "numero", "x", "y", "kill"}
    # 3 filas x 4 + 2 filas x 5, con A-1 del balcon en kill.
    assert (vista["total_butacas"], vista["total_vendibles"]) == (22, 21)
    assert [z["nombre"] for z in vista["zonas"]] == ["Luneta", "Balcon"]
    assert {
        (b["zona"], b["fila"], b["numero"]) for b in vista["butacas"] if b["kill"]
    } == {("Balcon", "A", "1")}
    # No guardo nada.
    assert await db.scalar(select(func.count(SeatMap.id))) == 0
    assert await db.scalar(select(func.count(Seat.id))) == 0

    seat_map_id = await crear_mapa(cliente_http, tk, venue_id)
    guardado = await cliente_http.get(
        f"{API}/seat-maps/{seat_map_id}/dibujo", headers=auth(tk)
    )

    assert guardado.status_code == 200, guardado.text
    llave = lambda b: (b["zona"], b["fila"], b["numero"], b["x"], b["y"], b["kill"])  # noqa: E731
    assert sorted(map(llave, guardado.json()["butacas"])) == sorted(
        map(llave, vista["butacas"])
    )
    assert guardado.json()["viewport"] == vista["viewport"]


async def test_vista_previa_de_csv(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    r = await cliente_http.post(
        f"{API}/seat-maps/vista-previa-csv",
        headers=auth(tk),
        json={"nombre": "Plano", "csv": CSV},
    )

    assert r.status_code == 200, r.text
    assert (r.json()["total_butacas"], r.json()["total_vendibles"]) == (3, 2)
    # El viewport se ajusta al contenido del archivo.
    assert r.json()["viewport"]["width"] > 0


async def test_vista_previa_con_errores_explica_que_corregir(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    malo = {
        **SPEC_MINIMA,
        "bloques": [
            {**SPEC_MINIMA["bloques"][0], "filas": {"desde": "C", "hasta": "A"}}
        ],
    }

    r = await cliente_http.post(
        f"{API}/seat-maps/vista-previa", headers=auth(tk), json=malo
    )

    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "mapa_invalido"
    assert "invertido" in r.json()["error"]["mensaje"]

    sin_columnas = await cliente_http.post(
        f"{API}/seat-maps/vista-previa-csv",
        headers=auth(tk),
        json={"nombre": "Plano", "csv": "zona,fila\nA,1\n"},
    )
    assert sin_columnas.status_code == 422
    assert "Faltan columnas" in sin_columnas.json()["error"]["mensaje"]


async def test_solo_psd_usa_la_vista_previa(cliente_http, usuarios):
    tk = token_de(usuarios["org_a"])
    assert (
        await cliente_http.post(
            f"{API}/seat-maps/vista-previa", headers=auth(tk), json=SPEC_MINIMA
        )
    ).status_code == 403
    assert (
        await cliente_http.post(
            f"{API}/seat-maps/vista-previa-csv",
            headers=auth(tk),
            json={"nombre": "P", "csv": CSV},
        )
    ).status_code == 403
    assert (
        await cliente_http.post(f"{API}/seat-maps/vista-previa", json=SPEC_MINIMA)
    ).status_code == 401


async def test_el_organizador_ve_el_dibujo_y_lo_inexistente_es_404(
    cliente_http, usuarios
):
    """Los recintos son compartidos: el organizador ve el mapa donde va a programar."""
    admin = token_de(usuarios["admin"])
    seat_map_id = await crear_mapa(
        cliente_http, admin, await crear_recinto(cliente_http, admin)
    )

    r = await cliente_http.get(
        f"{API}/seat-maps/{seat_map_id}/dibujo",
        headers=auth(token_de(usuarios["org_a"])),
    )
    assert r.status_code == 200
    assert r.json()["total_butacas"] == 22

    nada = await cliente_http.get(f"{API}/seat-maps/999999/dibujo", headers=auth(admin))
    assert nada.status_code == 404
