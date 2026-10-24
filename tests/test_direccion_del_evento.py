"""Direccion personalizada del evento (URL1): psdevents.com/<direccion>.

Lo que importa:

- Una liga que ya se compartio nunca se rompe: la direccion vieja lleva a
  la nueva, y ningun otro evento puede tomarla.
- Publicado, el organizador ya no la cambia (la decide PSD).
- No choca con las paginas del sitio (/panel, /orden...).
- Una direccion vieja de un evento oculto no delata que existe.
"""

import pytest
from sqlalchemy import select

from app.models.catalog import DireccionAnterior
from tests.test_catalog import auth, crear_evento, crear_recinto, token_de

pytestmark = pytest.mark.asyncio

API = "/api/v1"


async def preparar(
    cliente_http, usuarios, nombre="Circo Atayde"
) -> tuple[dict, str, str]:
    tk_psd = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk_psd)
    tk_org = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk_org, venue_id, nombre)
    return evento, tk_org, tk_psd


async def cambiar(cliente_http, tk, event_id, **cuerpo):
    return await cliente_http.patch(
        f"{API}/events/{event_id}", headers=auth(tk), json=cuerpo
    )


async def publicar(cliente_http, tk_psd, event_id) -> None:
    r = await cambiar(cliente_http, tk_psd, event_id, estado="publicado")
    assert r.status_code == 200, r.text


async def test_el_organizador_elige_la_direccion_en_borrador(cliente_http, usuarios):
    evento, tk_org, _ = await preparar(cliente_http, usuarios)
    r = await cambiar(
        cliente_http, tk_org, evento["id"], slug="  Atayde Coatzacoalcos 2026 "
    )
    assert r.status_code == 200, r.text
    # Se normaliza igual que las automaticas.
    assert r.json()["slug"] == "atayde-coatzacoalcos-2026"


async def test_publicado_el_organizador_ya_no_la_cambia(cliente_http, usuarios):
    evento, tk_org, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])

    r = await cambiar(cliente_http, tk_org, evento["id"], slug="otra-direccion")
    assert r.status_code == 403, r.text
    assert "PSD" in r.json()["error"]["mensaje"]

    # Mandar la misma que ya tiene (el formulario manda todo) no es cambiarla.
    r = await cambiar(
        cliente_http, tk_org, evento["id"], slug=evento["slug"], descripcion="Hola"
    )
    assert r.status_code == 200, r.text


async def test_publicado_cambiar_el_nombre_no_rompe_la_liga(cliente_http, usuarios):
    evento, tk_org, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])
    r = await cambiar(cliente_http, tk_org, evento["id"], nombre="Circo Atayde 2027")
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == evento["slug"]


async def test_en_borrador_una_direccion_elegida_no_sigue_al_nombre(
    cliente_http, usuarios
):
    evento, tk_org, _ = await preparar(cliente_http, usuarios)
    await cambiar(cliente_http, tk_org, evento["id"], slug="atayde-coatza")
    r = await cambiar(cliente_http, tk_org, evento["id"], nombre="Otro nombre")
    assert r.json()["slug"] == "atayde-coatza"


async def test_psd_la_cambia_y_la_vieja_lleva_a_la_nueva(cliente_http, usuarios, db):
    evento, _, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])
    viejo = evento["slug"]

    r = await cambiar(cliente_http, tk_psd, evento["id"], slug="atayde-coatza")
    assert r.status_code == 200, r.text

    r = await cliente_http.get(f"{API}/publico/eventos/{viejo}")
    assert r.status_code == 404
    error = r.json()["error"]
    assert error["codigo"] == "evento_movido"
    assert error["detalle"] == {"slug": "atayde-coatza"}

    r = await cliente_http.get(f"{API}/publico/eventos/atayde-coatza")
    assert r.status_code == 200, r.text

    anterior = await db.scalar(
        select(DireccionAnterior).where(DireccionAnterior.slug == viejo)
    )
    assert anterior.cambiado_por == usuarios["admin"].id


async def test_cambiar_dos_veces_las_dos_viejas_llevan_a_la_actual(
    cliente_http, usuarios
):
    evento, _, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])
    primera = evento["slug"]
    await cambiar(cliente_http, tk_psd, evento["id"], slug="segunda")
    await cambiar(cliente_http, tk_psd, evento["id"], slug="tercera")
    for vieja in (primera, "segunda"):
        r = await cliente_http.get(f"{API}/publico/eventos/{vieja}")
        assert r.json()["error"]["detalle"] == {"slug": "tercera"}


async def test_regresar_a_una_direccion_anterior(cliente_http, usuarios, db):
    evento, _, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])
    original = evento["slug"]
    await cambiar(cliente_http, tk_psd, evento["id"], slug="temporal")
    r = await cambiar(cliente_http, tk_psd, evento["id"], slug=original)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == original
    assert (
        await cliente_http.get(f"{API}/publico/eventos/{original}")
    ).status_code == 200
    r = await cliente_http.get(f"{API}/publico/eventos/temporal")
    assert r.json()["error"]["detalle"] == {"slug": original}


async def test_nadie_toma_la_direccion_de_otro_evento(cliente_http, usuarios):
    uno, tk_org, tk_psd = await preparar(cliente_http, usuarios)
    tk_b = token_de(usuarios["org_b"])
    venue_id = await crear_recinto(cliente_http, tk_psd, "Teatro Dos")
    dos = await crear_evento(cliente_http, tk_b, venue_id, "Teatro de verano")

    # Ni la actual...
    r = await cambiar(cliente_http, tk_b, dos["id"], slug=uno["slug"])
    assert r.status_code == 409, r.text
    assert r.json()["error"]["codigo"] == "direccion_no_valida"
    assert r.json()["error"]["detalle"] == {"campo": "slug"}

    # ...ni una que ya tuvo, porque sus ligas siguen circulando.
    await publicar(cliente_http, tk_psd, uno["id"])
    await cambiar(cliente_http, tk_psd, uno["id"], slug="circo-nuevo")
    r = await cambiar(cliente_http, tk_b, dos["id"], slug=uno["slug"])
    assert r.status_code == 409, r.text

    # Y un evento nuevo con el mismo nombre tampoco la recibe automatica.
    tres = await crear_evento(cliente_http, tk_org, venue_id, "Circo Atayde")
    assert tres["slug"] not in {uno["slug"], "circo-nuevo"}


@pytest.mark.parametrize(
    "reservada", ["panel", "Orden", "boletos", "mis-boletos", "legal"]
)
async def test_no_choca_con_las_paginas_del_sitio(cliente_http, usuarios, reservada):
    evento, tk_org, _ = await preparar(cliente_http, usuarios)
    r = await cambiar(cliente_http, tk_org, evento["id"], slug=reservada)
    assert r.status_code == 409, r.text
    assert "sitio" in r.json()["error"]["mensaje"]


async def test_un_evento_llamado_panel_no_se_come_el_panel(cliente_http, usuarios):
    evento, _, _ = await preparar(cliente_http, usuarios, nombre="Panel")
    assert evento["slug"] == "panel-2"


@pytest.mark.parametrize("texto", ["ab", "--", "x" * 81, "¡¡!!"])
async def test_largo_y_caracteres(cliente_http, usuarios, texto):
    evento, tk_org, _ = await preparar(cliente_http, usuarios)
    r = await cambiar(cliente_http, tk_org, evento["id"], slug=texto)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["codigo"] == "direccion_no_valida"


async def test_la_vieja_de_un_evento_oculto_no_lo_delata(cliente_http, usuarios):
    evento, _, tk_psd = await preparar(cliente_http, usuarios)
    await publicar(cliente_http, tk_psd, evento["id"])
    viejo = evento["slug"]
    await cambiar(cliente_http, tk_psd, evento["id"], slug="nueva-direccion")
    await cambiar(cliente_http, tk_psd, evento["id"], estado="borrador")

    oculto = await cliente_http.get(f"{API}/publico/eventos/{viejo}")
    inexistente = await cliente_http.get(f"{API}/publico/eventos/no-existe-este")
    assert oculto.status_code == 404
    assert oculto.json() == inexistente.json()


async def test_en_borrador_no_aparta_direcciones(cliente_http, usuarios, db):
    """Un borrador no tiene ligas circulando: probar direcciones no las gasta."""
    evento, tk_org, _ = await preparar(cliente_http, usuarios)
    await cambiar(cliente_http, tk_org, evento["id"], slug="prueba-uno")
    await cambiar(cliente_http, tk_org, evento["id"], slug="prueba-dos")
    assert (await db.scalars(select(DireccionAnterior))).all() == []
    otro = await crear_evento(cliente_http, tk_org, evento["venue_id"], "Prueba uno")
    assert otro["slug"] == "prueba-uno"


async def test_otro_organizador_no_cambia_la_direccion_ajena(cliente_http, usuarios):
    evento, _, _ = await preparar(cliente_http, usuarios)
    tk_b = token_de(usuarios["org_b"])
    ajeno = await cambiar(cliente_http, tk_b, evento["id"], slug="robada")
    inexistente = await cambiar(cliente_http, tk_b, 999999, slug="robada")
    assert ajeno.status_code == 404
    assert ajeno.json() == inexistente.json()
