"""Cuentas del personal que PSD da de alta desde el panel.

Lo que importa: solo PSD crea cuentas, nunca de admin; la contrasena
temporal sale una sola vez y sirve para entrar; desactivar corta el
acceso en la siguiente peticion.
"""

import pytest

from tests.test_catalog import API, auth, token_de

pytestmark = pytest.mark.asyncio


async def crear(cliente_http, tk, **datos):
    cuerpo = {
        "email": "Taquilla@Circo.mx ",
        "nombre": "Lupita Puerta",
        "rol": "acceso",
        **datos,
    }
    return await cliente_http.post(f"{API}/usuarios", headers=auth(tk), json=cuerpo)


async def entrar(cliente_http, email, password):
    return await cliente_http.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )


async def test_psd_crea_cuenta_y_la_contrasena_temporal_sirve(
    cliente_http, usuarios, clientes
):
    tk = token_de(usuarios["admin"])
    r = await crear(cliente_http, tk, client_id=clientes[0].id)

    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"cuenta", "password_temporal"}
    assert set(cuerpo["cuenta"]) == {
        "id",
        "email",
        "nombre",
        "rol",
        "client_id",
        "activo",
        "ultimo_acceso",
    }
    assert cuerpo["cuenta"]["email"] == "taquilla@circo.mx"
    assert cuerpo["cuenta"]["rol"] == "acceso"
    assert len(cuerpo["password_temporal"]) >= 14

    login = await entrar(cliente_http, "taquilla@circo.mx", cuerpo["password_temporal"])
    assert login.status_code == 200, login.text

    # La contrasena no vuelve a salir en ningun listado.
    lista = await cliente_http.get(f"{API}/usuarios", headers=auth(tk))
    assert cuerpo["password_temporal"] not in lista.text
    assert "password" not in lista.text


async def test_el_listado_filtra_por_organizador(cliente_http, usuarios, clientes):
    tk = token_de(usuarios["admin"])
    await crear(cliente_http, tk, client_id=clientes[0].id)
    await crear(cliente_http, tk, client_id=clientes[1].id, email="otra@circo.mx")

    r = await cliente_http.get(
        f"{API}/usuarios",
        headers=auth(tk),
        params={"client_id": clientes[1].id},
    )
    assert {c["email"] for c in r.json()} >= {"otra@circo.mx"}
    assert all(c["client_id"] == clientes[1].id for c in r.json())


async def test_solo_psd_administra_cuentas(cliente_http, usuarios, clientes):
    for quien in ("org_a", "org_b"):
        tk = token_de(usuarios[quien])
        assert (
            await crear(cliente_http, tk, client_id=clientes[0].id)
        ).status_code == 403
        assert (
            await cliente_http.get(f"{API}/usuarios", headers=auth(tk))
        ).status_code == 403
    assert (await cliente_http.get(f"{API}/usuarios")).status_code == 401


async def test_no_se_crean_admins_desde_el_panel(cliente_http, usuarios, clientes):
    r = await crear(
        cliente_http,
        token_de(usuarios["admin"]),
        rol="admin_psd",
        client_id=clientes[0].id,
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "datos_invalidos"


async def test_correo_repetido_y_organizador_inexistente(
    cliente_http, usuarios, clientes
):
    tk = token_de(usuarios["admin"])
    assert (await crear(cliente_http, tk, client_id=clientes[0].id)).status_code == 201

    repetido = await crear(
        cliente_http, tk, client_id=clientes[1].id, email="TAQUILLA@circo.mx"
    )
    assert repetido.status_code == 409
    assert repetido.json()["error"]["codigo"] == "conflicto_de_cuenta"

    sin_cliente = await crear(
        cliente_http, tk, client_id=999999, email="nadie@circo.mx"
    )
    assert sin_cliente.status_code == 404


async def test_desactivar_corta_el_acceso_y_reactivar_lo_devuelve(
    cliente_http, usuarios, clientes
):
    tk = token_de(usuarios["admin"])
    creada = (await crear(cliente_http, tk, client_id=clientes[0].id)).json()
    password = creada["password_temporal"]
    token_viejo = (await entrar(cliente_http, "taquilla@circo.mx", password)).json()[
        "access_token"
    ]

    r = await cliente_http.patch(
        f"{API}/usuarios/{creada['cuenta']['id']}",
        headers=auth(tk),
        json={"activo": False},
    )
    assert r.status_code == 200 and r.json()["activo"] is False
    # El token que ya tenia deja de servir, y no puede volver a entrar.
    assert (
        await cliente_http.get(f"{API}/auth/me", headers=auth(token_viejo))
    ).status_code == 401
    otra_vez = await entrar(cliente_http, "taquilla@circo.mx", password)
    assert otra_vez.status_code == 401

    await cliente_http.patch(
        f"{API}/usuarios/{creada['cuenta']['id']}",
        headers=auth(tk),
        json={"activo": True},
    )
    de_vuelta = await entrar(cliente_http, "taquilla@circo.mx", password)
    assert de_vuelta.status_code == 200


async def test_restablecer_da_otra_contrasena_y_la_vieja_ya_no_sirve(
    cliente_http, usuarios, clientes
):
    tk = token_de(usuarios["admin"])
    creada = (await crear(cliente_http, tk, client_id=clientes[0].id)).json()

    r = await cliente_http.post(
        f"{API}/usuarios/{creada['cuenta']['id']}/restablecer", headers=auth(tk)
    )
    assert r.status_code == 200
    nueva = r.json()["password_temporal"]
    assert nueva != creada["password_temporal"]
    assert (
        await entrar(cliente_http, "taquilla@circo.mx", creada["password_temporal"])
    ).status_code == 401
    assert (await entrar(cliente_http, "taquilla@circo.mx", nueva)).status_code == 200


async def test_las_cuentas_de_psd_no_se_tocan_desde_el_panel(cliente_http, usuarios):
    """Para el panel no existen: mismo 404 que una cuenta inexistente."""
    tk = token_de(usuarios["admin"])
    admin_id = usuarios["admin"].id
    for metodo, sufijo, cuerpo in (
        ("patch", "", {"activo": False}),
        ("post", "/restablecer", None),
    ):
        propia = await getattr(cliente_http, metodo)(
            f"{API}/usuarios/{admin_id}{sufijo}",
            headers=auth(tk),
            **({"json": cuerpo} if cuerpo else {}),
        )
        inexistente = await getattr(cliente_http, metodo)(
            f"{API}/usuarios/999999{sufijo}",
            headers=auth(tk),
            **({"json": cuerpo} if cuerpo else {}),
        )
        assert propia.status_code == 404
        assert propia.json() == inexistente.json()
    # Y la cuenta de admin sigue activa.
    assert (
        await cliente_http.get(f"{API}/auth/me", headers=auth(tk))
    ).status_code == 200
