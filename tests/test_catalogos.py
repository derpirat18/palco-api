"""Catálogos de ciudades (GEO1) y categorías de eventos (CAT1).

Lo que importa:

- Un recinto solo se da de alta con un estado y una ciudad del catálogo, y
  se guarda como está escrita ahí (adiós «Saltillo» y «saltillo»).
- Solo PSD agrega ciudades; no se repiten aunque se escriban distinto.
- Un evento solo lleva una categoría aprobada. El organizador la solicita;
  PSD la aprueba (y se le pone al evento desde el que se pidió) o la
  rechaza con motivo, que el organizador ve.
"""

import pytest

from app.core.geo_mx import clave, estado_canonico, ubicar
from app.models.catalog import Event
from tests.test_catalog import API, auth, crear_evento, crear_recinto, token_de

pytestmark = pytest.mark.asyncio

COORD = "25.4232, -101.0053"


def de(usuarios, quien):
    return auth(token_de(usuarios[quien]))


async def test_normalizacion():
    assert clave("MX", "Chiapas", "Tuxtla  Gutiérrez") == "mx chiapas tuxtla gutierrez"
    assert estado_canonico("coahuila de zaragoza") == "Coahuila"
    assert estado_canonico("CDMX") == "Ciudad de México"
    assert estado_canonico("Narnia") is None
    assert ubicar("cdmx") == ("Ciudad de México", "Ciudad de México")
    assert ubicar("saltillo") == ("Coahuila", "Saltillo")
    assert ubicar("Guadalupe") is None, "hay en Nuevo León y en Zacatecas"
    assert ubicar("Guadalupe", "Zacatecas") == ("Zacatecas", "Guadalupe")


async def test_ciudades_solo_psd_y_sin_repetidas(cliente_http, usuarios):
    url = f"{API}/catalogos/ciudades"
    r = await cliente_http.post(
        url,
        json={"estado": "coahuila", "nombre": "  Saltillo "},
        headers=de(usuarios, "admin"),
    )
    assert r.status_code == 201, r.text
    coahuila = next(e for e in r.json()[0]["estados"] if e["nombre"] == "Coahuila")
    assert [c["nombre"] for c in coahuila["ciudades"]] == ["Saltillo"]
    assert len(r.json()[0]["estados"]) == 32

    r = await cliente_http.post(
        url,
        json={"estado": "Coahuila", "nombre": "SALTILLO"},
        headers=de(usuarios, "admin"),
    )
    assert r.status_code == 409
    assert "Saltillo ya está en la lista de Coahuila" in r.json()["error"]["mensaje"]
    r = await cliente_http.post(
        url, json={"estado": "Narnia", "nombre": "X"}, headers=de(usuarios, "admin")
    )
    assert r.status_code == 422
    r = await cliente_http.post(
        url,
        json={"estado": "Coahuila", "nombre": "Ramos"},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 403
    # Todos los del panel ven la lista.
    r = await cliente_http.get(f"{API}/catalogos/geo", headers=de(usuarios, "acceso"))
    assert r.status_code == 200


async def test_el_recinto_usa_la_ciudad_del_catalogo(cliente_http, usuarios):
    h = de(usuarios, "admin")
    await cliente_http.post(
        f"{API}/catalogos/ciudades",
        json={"estado": "Coahuila", "nombre": "Saltillo"},
        headers=h,
    )
    base = {"nombre": "Teatro de la Ciudad", "coordenadas": COORD}

    r = await cliente_http.post(
        f"{API}/venues",
        json={**base, "estado_geo": "coahuila", "ciudad": "saltillo"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert (r.json()["estado_geo"], r.json()["ciudad"]) == ("Coahuila", "Saltillo")

    r = await cliente_http.post(
        f"{API}/venues",
        json={**base, "estado_geo": "Coahuila", "ciudad": "Ramos"},
        headers=h,
    )
    assert r.status_code == 422
    assert r.json()["error"]["detalle"]["campo"] == "ciudad"
    r = await cliente_http.post(
        f"{API}/venues", json={**base, "ciudad": "Saltillo"}, headers=h
    )
    assert r.status_code == 422, "el estado es obligatorio"


async def test_categorias_solicitud_y_aprobacion(cliente_http, db, usuarios):
    psd, org = de(usuarios, "admin"), de(usuarios, "org_a")
    url = f"{API}/catalogos/categorias"
    assert (
        await cliente_http.post(url, json={"nombre": "Circo"}, headers=psd)
    ).status_code == 201
    vid = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(cliente_http, token_de(usuarios["org_a"]), vid)

    # Solo de la lista, y como está escrita ahí.
    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}", json={"categoria": "circo"}, headers=org
    )
    assert r.status_code == 200 and r.json()["categoria"] == "Circo"
    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}", json={"categoria": "Lucha libre"}, headers=org
    )
    assert r.status_code == 422
    assert "solicítala" in r.json()["error"]["mensaje"]
    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}", json={"categoria": None}, headers=org
    )
    assert r.status_code == 200 and r.json()["categoria"] is None

    # El organizador la solicita desde su evento.
    r = await cliente_http.post(
        url, json={"nombre": "lucha libre", "evento_id": evento["id"]}, headers=org
    )
    assert r.status_code == 201, r.text
    assert [c["nombre"] for c in r.json()["aprobadas"]] == ["Circo"]
    (pedida,) = r.json()["solicitudes"]
    assert pedida["estado"] == "solicitada"
    r = await cliente_http.post(url, json={"nombre": "Lucha  Libre"}, headers=org)
    assert r.status_code == 409, "ya se pidió"

    # PSD la ve, corrige cómo se escribe y la aprueba: se le pone al evento.
    r = await cliente_http.get(url, headers=psd)
    (s,) = r.json()["solicitudes"]
    assert s["solicitante"] == "Organizador A"
    r = await cliente_http.post(
        f"{url}/{s['id']}/aprobar", json={"nombre": "Lucha libre"}, headers=psd
    )
    assert r.status_code == 200
    assert [c["nombre"] for c in r.json()["aprobadas"]] == ["Circo", "Lucha libre"]
    assert r.json()["solicitudes"] == []
    assert (await db.get(Event, evento["id"], populate_existing=True)).categoria == (
        "Lucha libre"
    )


async def test_categoria_rechazada_con_motivo(cliente_http, usuarios):
    psd, org = de(usuarios, "admin"), de(usuarios, "org_a")
    url = f"{API}/catalogos/categorias"
    r = await cliente_http.post(url, json={"nombre": "Fiestas"}, headers=org)
    (s,) = r.json()["solicitudes"]

    r = await cliente_http.post(
        f"{url}/{s['id']}/rechazar", json={"motivo": "Usa Festivales"}, headers=psd
    )
    assert r.status_code == 200

    (mia,) = (await cliente_http.get(url, headers=org)).json()["solicitudes"]
    assert (mia["estado"], mia["motivo_rechazo"]) == ("rechazada", "Usa Festivales")
    # Otro organizador no ve solicitudes ajenas, ni aprueba.
    otro = (await cliente_http.get(url, headers=de(usuarios, "org_b"))).json()
    assert otro["solicitudes"] == []
    r = await cliente_http.post(f"{url}/{s['id']}/aprobar", json={}, headers=org)
    assert r.status_code == 403
    # Rechazada, se puede pedir otra vez.
    r = await cliente_http.post(url, json={"nombre": "Fiestas"}, headers=org)
    assert r.status_code == 201
    assert r.json()["solicitudes"][0]["estado"] == "solicitada"


async def test_no_pide_para_eventos_ajenos(cliente_http, usuarios):
    vid = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(cliente_http, token_de(usuarios["org_a"]), vid)
    r = await cliente_http.post(
        f"{API}/catalogos/categorias",
        json={"nombre": "Magia", "evento_id": evento["id"]},
        headers=de(usuarios, "org_b"),
    )
    assert r.status_code == 404


async def test_quitar_categoria_pasa_sus_eventos(cliente_http, db, usuarios):
    psd, org = de(usuarios, "admin"), de(usuarios, "org_a")
    url = f"{API}/catalogos/categorias"
    for n in ("Circo", "Circos", "Prueba"):
        await cliente_http.post(url, json={"nombre": n}, headers=psd)
    vid = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(cliente_http, token_de(usuarios["org_a"]), vid)
    await cliente_http.patch(
        f"{API}/events/{evento['id']}", json={"categoria": "Circos"}, headers=org
    )
    ids = {
        c["nombre"]: c
        for c in (await cliente_http.get(url, headers=psd)).json()["aprobadas"]
    }
    assert ids["Circos"]["eventos"] == 1 and ids["Prueba"]["eventos"] == 0

    # Sin uso: se quita directo.
    r = await cliente_http.post(
        f"{url}/{ids['Prueba']['id']}/quitar", json={}, headers=psd
    )
    assert r.status_code == 200
    assert "Prueba" not in [c["nombre"] for c in r.json()["aprobadas"]]

    # Con uso: hay que decir a cuál pasar sus eventos (de la lista, y otra).
    r = await cliente_http.post(
        f"{url}/{ids['Circos']['id']}/quitar", json={}, headers=psd
    )
    assert r.status_code == 422 and r.json()["error"]["detalle"]["eventos"] == 1
    for malo in ("Circos", "Lucha libre"):
        r = await cliente_http.post(
            f"{url}/{ids['Circos']['id']}/quitar", json={"pasar_a": malo}, headers=psd
        )
        assert r.status_code == 422, malo
    r = await cliente_http.post(
        f"{url}/{ids['Circos']['id']}/quitar", json={"pasar_a": "circo"}, headers=psd
    )
    assert r.status_code == 200
    assert [(c["nombre"], c["eventos"]) for c in r.json()["aprobadas"]] == [
        ("Circo", 1)
    ]
    assert (
        await db.get(Event, evento["id"], populate_existing=True)
    ).categoria == "Circo"

    # Solo PSD.
    r = await cliente_http.post(
        f"{url}/{ids['Circo']['id']}/quitar", json={}, headers=org
    )
    assert r.status_code == 403


async def test_renombrar_categoria_corrige_sus_eventos(cliente_http, db, usuarios):
    psd, org = de(usuarios, "admin"), de(usuarios, "org_a")
    url = f"{API}/catalogos/categorias"
    for n in ("Teatro", "Teatro B"):
        await cliente_http.post(url, json={"nombre": n}, headers=psd)
    vid = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(cliente_http, token_de(usuarios["org_a"]), vid)
    await cliente_http.patch(
        f"{API}/events/{evento['id']}", json={"categoria": "Teatro B"}, headers=org
    )
    ids = {
        c["nombre"]: c["id"]
        for c in (await cliente_http.get(url, headers=psd)).json()["aprobadas"]
    }

    r = await cliente_http.patch(
        f"{url}/{ids['Teatro B']}", json={"nombre": "Teatro  bilingüe"}, headers=psd
    )
    assert r.status_code == 200
    assert "Teatro bilingüe" in [c["nombre"] for c in r.json()["aprobadas"]]
    assert (await db.get(Event, evento["id"], populate_existing=True)).categoria == (
        "Teatro bilingüe"
    )
    # No se puede llamar igual que otra: para juntarlas, se quita una.
    r = await cliente_http.patch(
        f"{url}/{ids['Teatro B']}", json={"nombre": "teatro"}, headers=psd
    )
    assert r.status_code == 409
    assert "quita una" in r.json()["error"]["mensaje"]
    r = await cliente_http.patch(
        f"{url}/{ids['Teatro']}", json={"nombre": "Teatros"}, headers=org
    )
    assert r.status_code == 403


async def test_aprobar_con_nombre_de_otra_no_truena(cliente_http, usuarios):
    psd, org = de(usuarios, "admin"), de(usuarios, "org_a")
    url = f"{API}/catalogos/categorias"
    await cliente_http.post(url, json={"nombre": "Circo"}, headers=psd)
    (s,) = (
        await cliente_http.post(url, json={"nombre": "Circos"}, headers=org)
    ).json()["solicitudes"]
    r = await cliente_http.post(
        f"{url}/{s['id']}/aprobar", json={"nombre": "Circo"}, headers=psd
    )
    assert r.status_code == 409


async def test_quitar_ciudad_pasa_sus_recintos(cliente_http, usuarios):
    h = de(usuarios, "admin")
    for n in ("Saltillo", "Saltilo"):
        r = await cliente_http.post(
            f"{API}/catalogos/ciudades",
            json={"estado": "Coahuila", "nombre": n},
            headers=h,
        )
    coahuila = next(e for e in r.json()[0]["estados"] if e["nombre"] == "Coahuila")
    ids = {c["nombre"]: c["id"] for c in coahuila["ciudades"]}
    r = await cliente_http.post(
        f"{API}/venues",
        json={
            "nombre": "Teatro",
            "coordenadas": COORD,
            "estado_geo": "Coahuila",
            "ciudad": "Saltilo",
        },
        headers=h,
    )
    vid = r.json()["id"]
    geo = (await cliente_http.get(f"{API}/catalogos/geo", headers=h)).json()
    coahuila = next(e for e in geo[0]["estados"] if e["nombre"] == "Coahuila")
    assert {c["nombre"]: c["recintos"] for c in coahuila["ciudades"]}["Saltilo"] == 1

    url = f"{API}/catalogos/ciudades/{ids['Saltilo']}/quitar"
    r = await cliente_http.post(url, json={}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["detalle"]["recintos"] == 1
    r = await cliente_http.post(url, json={"pasar_a_id": ids["Saltillo"]}, headers=h)
    assert r.status_code == 200
    coahuila = next(e for e in r.json()[0]["estados"] if e["nombre"] == "Coahuila")
    assert [(c["nombre"], c["recintos"]) for c in coahuila["ciudades"]] == [
        ("Saltillo", 1)
    ]
    recintos = (await cliente_http.get(f"{API}/venues", headers=h)).json()
    (v,) = [x for x in recintos if x["id"] == vid]
    assert (v["estado_geo"], v["ciudad"]) == ("Coahuila", "Saltillo")
    r = await cliente_http.post(
        f"{API}/catalogos/ciudades/{ids['Saltillo']}/quitar",
        json={},
        headers=de(usuarios, "org_a"),
    )
    assert r.status_code == 403
