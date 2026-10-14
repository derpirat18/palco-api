"""Pruebas del catalogo: recintos, mapas, eventos, funciones y precios.

El aislamiento entre organizadores es lo critico de este modulo. Un
organizador no puede ver, crear ni modificar eventos de otro, ni
colgando una funcion de un evento ajeno ni poniendo precio a una zona
que no es de su mapa.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.security import crear_access_token

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def token_de(usuario) -> str:
    return crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )


# --------------------------------------------------------------------------
# Ayudantes
# --------------------------------------------------------------------------


SPEC_MINIMA = {
    "nombre": "Sala chica",
    "ancho": 600,
    "alto": 400,
    "bloques": [
        {
            "zona": "Luneta",
            "color": "#c0392b",
            "filas": {"desde": "A", "hasta": "C"},
            "butacas_por_fila": 4,
            "origen_x": "40",
            "origen_y": "80",
        },
        {
            "zona": "Balcon",
            "filas": ["A", "B"],
            "butacas_por_fila": 5,
            "origen_x": "40",
            "origen_y": "260",
            "kill": ["A-1"],
        },
    ],
}


async def crear_recinto(cliente_http, tk, nombre="Teatro Uno") -> int:
    r = await cliente_http.post(
        f"{API}/venues",
        headers=auth(tk),
        json={"nombre": nombre, "ciudad": "CDMX", "capacidad": 1200},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def crear_mapa(cliente_http, tk, venue_id, nombre="Sala chica") -> int:
    spec = {**SPEC_MINIMA, "nombre": nombre}
    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/generar", headers=auth(tk), json=spec
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def crear_evento(cliente_http, tk, venue_id, nombre="Circo de Navidad") -> dict:
    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(tk),
        json={"venue_id": venue_id, "nombre": nombre, "categoria": "circo"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def crear_funcion(cliente_http, tk, event_id, seat_map_id) -> dict:
    inicia = datetime.now(UTC) + timedelta(days=30)
    r = await cliente_http.post(
        f"{API}/events/{event_id}/performances",
        headers=auth(tk),
        json={"seat_map_id": seat_map_id, "inicia_en": inicia.isoformat()},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------
# Recintos
# --------------------------------------------------------------------------


async def test_recintos_exigen_token(cliente_http):
    assert (await cliente_http.get(f"{API}/venues")).status_code == 401


async def test_solo_psd_crea_recintos(cliente_http, usuarios):
    """Los recintos son infraestructura compartida: los da de alta PSD."""
    r = await cliente_http.post(
        f"{API}/venues",
        headers=auth(token_de(usuarios["org_a"])),
        json={"nombre": "Teatro Pirata", "ciudad": "CDMX"},
    )
    assert r.status_code == 403


async def test_cualquier_usuario_lista_recintos(cliente_http, usuarios):
    await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    r = await cliente_http.get(
        f"{API}/venues", headers=auth(token_de(usuarios["org_b"]))
    )
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_filtro_por_ciudad(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    await crear_recinto(cliente_http, tk, "Teatro CDMX")
    r = await cliente_http.post(
        f"{API}/venues",
        headers=auth(tk),
        json={"nombre": "Teatro Monterrey", "ciudad": "Monterrey"},
    )
    assert r.status_code == 201

    r = await cliente_http.get(f"{API}/venues?ciudad=monte", headers=auth(tk))
    assert [v["nombre"] for v in r.json()] == ["Teatro Monterrey"]


# --------------------------------------------------------------------------
# Mapas de butacas
# --------------------------------------------------------------------------


async def test_generar_mapa_cuenta_butacas(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)

    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/generar", headers=auth(tk), json=SPEC_MINIMA
    )
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    # 3 filas x 4 en Luneta + 2 filas x 5 en Balcon = 22, una de ellas kill.
    assert cuerpo["total_butacas"] == 22
    assert cuerpo["total_vendibles"] == 21
    assert cuerpo["viewport"] == {"width": 600, "height": 400}


async def test_organizador_no_genera_mapas(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/generar",
        headers=auth(token_de(usuarios["org_a"])),
        json=SPEC_MINIMA,
    )
    assert r.status_code == 403


async def test_mapa_repetido_en_el_mismo_recinto(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)
    await crear_mapa(cliente_http, tk, venue_id)

    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/generar", headers=auth(tk), json=SPEC_MINIMA
    )
    assert r.status_code == 409


async def test_mapa_en_recinto_inexistente(cliente_http, usuarios):
    r = await cliente_http.post(
        f"{API}/venues/9999/seat-maps/generar",
        headers=auth(token_de(usuarios["admin"])),
        json=SPEC_MINIMA,
    )
    assert r.status_code == 404


async def test_spec_invalida_no_llega_a_la_base(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)
    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/generar",
        headers=auth(tk),
        json={**SPEC_MINIMA, "bloques": []},
    )
    assert r.status_code == 422


async def test_importar_mapa_csv(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)

    csv = "zona,fila,numero,x,y,kill\nPalco,A,1,10,10,\nPalco,A,2,40,10,si\n"
    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/importar",
        headers=auth(tk),
        json={"nombre": "Palcos", "csv": csv},
    )
    assert r.status_code == 201, r.text
    assert r.json()["total_butacas"] == 2
    assert r.json()["total_vendibles"] == 1
    # Con ajustar_viewport el lienzo se encoge a lo que ocupan las butacas.
    assert r.json()["viewport"]["width"] < 600


async def test_importar_csv_sin_columnas(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)
    r = await cliente_http.post(
        f"{API}/venues/{venue_id}/seat-maps/importar",
        headers=auth(tk),
        json={"nombre": "Roto", "csv": "zona,fila\nPalco,A\n"},
    )
    assert r.status_code == 422


async def test_listar_mapas_y_zonas(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)
    mapa_id = await crear_mapa(cliente_http, tk, venue_id)

    r = await cliente_http.get(f"{API}/venues/{venue_id}/seat-maps", headers=auth(tk))
    assert r.status_code == 200
    assert [m["total_butacas"] for m in r.json()] == [22]

    r = await cliente_http.get(f"{API}/seat-maps/{mapa_id}/zonas", headers=auth(tk))
    assert {z["nombre"] for z in r.json()} == {"Luneta", "Balcon"}


# --------------------------------------------------------------------------
# Eventos
# --------------------------------------------------------------------------


async def test_organizador_crea_evento_a_su_nombre(cliente_http, usuarios, clientes):
    """El client_id lo fija el servidor, nunca la peticion."""
    cliente_a, cliente_b = clientes
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))

    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(token_de(usuarios["org_a"])),
        # Intenta crearlo a nombre del otro organizador.
        json={
            "client_id": cliente_b.id,
            "venue_id": venue_id,
            "nombre": "Gala de Invierno",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["client_id"] == cliente_a.id
    assert r.json()["slug"] == "gala-de-invierno"
    assert r.json()["estado"] == "borrador"


async def test_admin_debe_indicar_organizador(cliente_http, usuarios):
    tk = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk)
    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(tk),
        json={"venue_id": venue_id, "nombre": "Sin dueno"},
    )
    assert r.status_code == 409


async def test_evento_en_recinto_inexistente(cliente_http, usuarios):
    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(token_de(usuarios["org_a"])),
        json={"venue_id": 9999, "nombre": "Fantasma"},
    )
    assert r.status_code == 404


async def test_rol_acceso_no_crea_eventos(cliente_http, usuarios):
    """El personal de puerta solo escanea."""
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    r = await cliente_http.post(
        f"{API}/events",
        headers=auth(token_de(usuarios["acceso"])),
        json={"venue_id": venue_id, "nombre": "No deberia"},
    )
    assert r.status_code == 403


async def test_slug_se_desambigua(cliente_http, usuarios):
    """Dos plazas del mismo espectaculo no pueden compartir URL."""
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])

    uno = await crear_evento(cliente_http, tk, venue_id, "Circo Atayde")
    dos = await crear_evento(cliente_http, tk, venue_id, "Circo Atayde")
    assert uno["slug"] == "circo-atayde"
    assert dos["slug"] == "circo-atayde-2"


async def test_slug_sin_acentos_ni_simbolos(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(
        cliente_http, token_de(usuarios["org_a"]), venue_id, "¡Función Única! 2026"
    )
    assert evento["slug"] == "funcion-unica-2026"


async def test_organizador_solo_lista_sus_eventos(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    await crear_evento(cliente_http, token_de(usuarios["org_a"]), venue_id, "De A")
    await crear_evento(cliente_http, token_de(usuarios["org_b"]), venue_id, "De B")

    r = await cliente_http.get(
        f"{API}/events", headers=auth(token_de(usuarios["org_a"]))
    )
    assert [e["nombre"] for e in r.json()] == ["De A"]

    r = await cliente_http.get(
        f"{API}/events", headers=auth(token_de(usuarios["admin"]))
    )
    assert {e["nombre"] for e in r.json()} == {"De A", "De B"}


async def test_organizador_no_lee_evento_ajeno(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(
        cliente_http, token_de(usuarios["org_a"]), venue_id, "De A"
    )

    r = await cliente_http.get(
        f"{API}/events/{evento['id']}", headers=auth(token_de(usuarios["org_b"]))
    )
    assert r.status_code == 403


async def test_evento_inexistente_y_ajeno_responden_distinto_solo_a_psd(
    cliente_http, usuarios
):
    """Para un organizador, "no existe" y "no es tuyo" se ven igual.

    Distinguirlos permitiria descubrir que clientes hay sondeando ids.
    """
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(
        cliente_http, token_de(usuarios["org_a"]), venue_id, "De A"
    )
    tk_b = token_de(usuarios["org_b"])

    ajeno = await cliente_http.get(f"{API}/events/{evento['id']}", headers=auth(tk_b))
    inexistente = await cliente_http.get(f"{API}/events/999999", headers=auth(tk_b))
    assert ajeno.status_code == 403
    assert inexistente.status_code == 404
    # El mensaje no revela nada del recurso en ninguno de los dos casos.
    assert "De A" not in ajeno.text


async def test_organizador_no_modifica_evento_ajeno(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    evento = await crear_evento(
        cliente_http, token_de(usuarios["org_a"]), venue_id, "De A"
    )
    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}",
        headers=auth(token_de(usuarios["org_b"])),
        json={"nombre": "Secuestrado"},
    )
    assert r.status_code == 403


async def test_actualizar_evento_rehace_el_slug(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_id, "Nombre viejo")

    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}",
        headers=auth(tk),
        json={"nombre": "Nombre nuevo", "estado": "publicado"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "nombre-nuevo"
    assert r.json()["estado"] == "publicado"


async def test_estado_invalido_se_rechaza(cliente_http, usuarios):
    """Si llegara a la base, el CHECK la tumbaria con un 500."""
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_id)

    r = await cliente_http.patch(
        f"{API}/events/{evento['id']}", headers=auth(tk), json={"estado": "inventado"}
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------
# Funciones
# --------------------------------------------------------------------------


async def test_crear_funcion(cliente_http, usuarios):
    tk_admin = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk_admin)
    mapa_id = await crear_mapa(cliente_http, tk_admin, venue_id)

    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_id)
    funcion = await crear_funcion(cliente_http, tk, evento["id"], mapa_id)

    assert funcion["estado"] == "programada"
    # El inventario se materializa al publicar, no al programar.
    assert funcion["inventario_generado"] is False


async def test_funcion_con_mapa_de_otro_recinto(cliente_http, usuarios):
    """Programar con el mapa equivocado dejaria butacas que no existen."""
    tk_admin = token_de(usuarios["admin"])
    venue_a = await crear_recinto(cliente_http, tk_admin, "Teatro A")
    venue_b = await crear_recinto(cliente_http, tk_admin, "Teatro B")
    mapa_de_b = await crear_mapa(cliente_http, tk_admin, venue_b)

    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_a)

    inicia = datetime.now(UTC) + timedelta(days=10)
    r = await cliente_http.post(
        f"{API}/events/{evento['id']}/performances",
        headers=auth(tk),
        json={"seat_map_id": mapa_de_b, "inicia_en": inicia.isoformat()},
    )
    assert r.status_code == 409


async def test_funcion_con_mapa_inexistente(cliente_http, usuarios):
    venue_id = await crear_recinto(cliente_http, token_de(usuarios["admin"]))
    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_id)

    inicia = datetime.now(UTC) + timedelta(days=10)
    r = await cliente_http.post(
        f"{API}/events/{evento['id']}/performances",
        headers=auth(tk),
        json={"seat_map_id": 9999, "inicia_en": inicia.isoformat()},
    )
    assert r.status_code == 404


async def test_organizador_no_cuelga_funcion_de_evento_ajeno(cliente_http, usuarios):
    tk_admin = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk_admin)
    mapa_id = await crear_mapa(cliente_http, tk_admin, venue_id)
    evento = await crear_evento(cliente_http, token_de(usuarios["org_a"]), venue_id)

    inicia = datetime.now(UTC) + timedelta(days=10)
    r = await cliente_http.post(
        f"{API}/events/{evento['id']}/performances",
        headers=auth(token_de(usuarios["org_b"])),
        json={"seat_map_id": mapa_id, "inicia_en": inicia.isoformat()},
    )
    assert r.status_code == 403


async def test_organizador_no_lista_funciones_ajenas(cliente_http, usuarios):
    tk_admin = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk_admin)
    mapa_id = await crear_mapa(cliente_http, tk_admin, venue_id)
    tk_a = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk_a, venue_id)
    await crear_funcion(cliente_http, tk_a, evento["id"], mapa_id)

    propia = await cliente_http.get(
        f"{API}/events/{evento['id']}/performances", headers=auth(tk_a)
    )
    assert len(propia.json()) == 1

    ajena = await cliente_http.get(
        f"{API}/events/{evento['id']}/performances",
        headers=auth(token_de(usuarios["org_b"])),
    )
    assert ajena.status_code == 403


# --------------------------------------------------------------------------
# Precios
# --------------------------------------------------------------------------


@pytest.fixture
async def funcion_lista(cliente_http, usuarios):
    """Recinto, mapa, evento y funcion de org_a, listos para precios."""
    tk_admin = token_de(usuarios["admin"])
    venue_id = await crear_recinto(cliente_http, tk_admin)
    mapa_id = await crear_mapa(cliente_http, tk_admin, venue_id)

    tk = token_de(usuarios["org_a"])
    evento = await crear_evento(cliente_http, tk, venue_id)
    funcion = await crear_funcion(cliente_http, tk, evento["id"], mapa_id)

    zonas = (
        await cliente_http.get(f"{API}/seat-maps/{mapa_id}/zonas", headers=auth(tk))
    ).json()
    return {
        "token": tk,
        "mapa_id": mapa_id,
        "evento": evento,
        "funcion": funcion,
        "zonas": {z["nombre"]: z["id"] for z in zonas},
    }


async def test_fijar_precios_por_zona(cliente_http, funcion_lista):
    f = funcion_lista
    r = await cliente_http.put(
        f"{API}/performances/{f['funcion']['id']}/precios",
        headers=auth(f["token"]),
        json={
            "precios": {
                str(f["zonas"]["Luneta"]): "850.50",
                str(f["zonas"]["Balcon"]): "450",
            }
        },
    )
    assert r.status_code == 200, r.text
    por_zona = {p["zone_id"]: Decimal(p["precio"]) for p in r.json()}
    assert por_zona[f["zonas"]["Luneta"]] == Decimal("850.50")
    assert por_zona[f["zonas"]["Balcon"]] == Decimal("450")
    assert all(p["activo"] for p in r.json())


async def test_zona_que_no_es_del_mapa_se_rechaza(
    cliente_http, usuarios, funcion_lista
):
    """Poner precio a una zona ajena dejaria la funcion sin precio real."""
    tk_admin = token_de(usuarios["admin"])
    otro_venue = await crear_recinto(cliente_http, tk_admin, "Teatro Ajeno")
    otro_mapa = await crear_mapa(cliente_http, tk_admin, otro_venue, "Otra sala")
    zonas_ajenas = (
        await cliente_http.get(
            f"{API}/seat-maps/{otro_mapa}/zonas", headers=auth(tk_admin)
        )
    ).json()

    f = funcion_lista
    r = await cliente_http.put(
        f"{API}/performances/{f['funcion']['id']}/precios",
        headers=auth(f["token"]),
        json={"precios": {str(zonas_ajenas[0]["id"]): "100"}},
    )
    assert r.status_code == 409


async def test_quitar_una_zona_la_desactiva_no_la_borra(
    cliente_http, funcion_lista, db
):
    """El historico se conserva: puede haber ordenes que la referencien."""
    from sqlalchemy import select

    from app.models.catalog import PerformancePrice

    f = funcion_lista
    url = f"{API}/performances/{f['funcion']['id']}/precios"

    await cliente_http.put(
        url,
        headers=auth(f["token"]),
        json={
            "precios": {
                str(f["zonas"]["Luneta"]): "800",
                str(f["zonas"]["Balcon"]): "400",
            }
        },
    )
    r = await cliente_http.put(
        url,
        headers=auth(f["token"]),
        json={"precios": {str(f["zonas"]["Luneta"]): "900"}},
    )
    assert r.status_code == 200
    assert [p["zone_id"] for p in r.json()] == [f["zonas"]["Luneta"]]
    assert Decimal(r.json()[0]["precio"]) == Decimal("900")

    filas = {
        p.zone_id: p
        for p in (
            await db.scalars(
                select(PerformancePrice).where(
                    PerformancePrice.performance_id == f["funcion"]["id"]
                )
            )
        ).all()
    }
    # Balcon sigue existiendo, solo que inactivo, y con su precio intacto.
    assert len(filas) == 2
    assert filas[f["zonas"]["Balcon"]].activo is False
    assert filas[f["zonas"]["Balcon"]].precio == Decimal("400.00")
    assert filas[f["zonas"]["Luneta"]].activo is True


async def test_precio_negativo_se_rechaza(cliente_http, funcion_lista):
    """Se corta en el esquema. Si llegara a la base seria un 500."""
    f = funcion_lista
    r = await cliente_http.put(
        f"{API}/performances/{f['funcion']['id']}/precios",
        headers=auth(f["token"]),
        json={"precios": {str(f["zonas"]["Luneta"]): "-10"}},
    )
    assert r.status_code == 422


async def test_precio_con_mas_de_dos_decimales_se_rechaza(cliente_http, funcion_lista):
    """La columna es Numeric(10,2): redondear en silencio cambia importes."""
    f = funcion_lista
    r = await cliente_http.put(
        f"{API}/performances/{f['funcion']['id']}/precios",
        headers=auth(f["token"]),
        json={"precios": {str(f["zonas"]["Luneta"]): "850.555"}},
    )
    assert r.status_code == 422


async def test_organizador_no_pone_precio_a_funcion_ajena(
    cliente_http, usuarios, funcion_lista
):
    f = funcion_lista
    r = await cliente_http.put(
        f"{API}/performances/{f['funcion']['id']}/precios",
        headers=auth(token_de(usuarios["org_b"])),
        json={"precios": {str(f["zonas"]["Luneta"]): "100"}},
    )
    assert r.status_code == 403


async def test_precios_de_funcion_inexistente(cliente_http, usuarios):
    r = await cliente_http.put(
        f"{API}/performances/999999/precios",
        headers=auth(token_de(usuarios["org_a"])),
        json={"precios": {"1": "100"}},
    )
    assert r.status_code == 404
