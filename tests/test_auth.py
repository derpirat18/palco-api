"""Pruebas de autenticacion y autorizacion.

Las de aislamiento entre organizadores son tan intocables como las de
concurrencia: si una falla, un cliente puede leer datos de otro.
"""

import pytest

from app.core.errors import EVENTO_NO_EXISTE, RecursoNoEncontrado
from app.core.security import (
    CredencialesInvalidas,
    PasswordDemasiadoLarga,
    PermisoInsuficiente,
    TokenInvalido,
    crear_access_token,
    decodificar_token,
    hash_password,
    verificar_password,
)
from app.models.base import RolUsuario

pytestmark = pytest.mark.asyncio

PASSWORD = "Secreta-12345"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def token_de(usuario) -> str:
    return crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )


# --------------------------------------------------------------------------
# Contraseñas
# --------------------------------------------------------------------------


async def test_hash_no_guarda_la_password_en_claro():
    h = hash_password(PASSWORD)
    assert PASSWORD not in h
    assert verificar_password(PASSWORD, h)
    assert not verificar_password("otra-cosa-12345", h)


async def test_dos_hashes_de_la_misma_password_son_distintos():
    """Salt aleatorio: dos cuentas con la misma contraseña no se delatan."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


async def test_rechaza_password_de_mas_de_72_bytes():
    """bcrypt trunca en silencio pasando 72 bytes. Truncar sin avisar
    haria equivalentes dos contraseñas distintas."""
    with pytest.raises(PasswordDemasiadoLarga):
        hash_password("a" * 73)


async def test_hash_corrupto_no_valida():
    assert not verificar_password(PASSWORD, "esto-no-es-un-hash")


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


async def test_token_lleva_rol_y_cliente():
    t = crear_access_token(subject=7, rol="organizador", client_id=3)
    p = decodificar_token(t)
    assert p["sub"] == "7"
    assert p["rol"] == "organizador"
    assert p["client_id"] == 3


async def test_token_firmado_con_otra_llave_se_rechaza():
    from jose import jwt

    falso = jwt.encode({"sub": "1", "tipo": "access"}, "llave-ajena", algorithm="HS256")
    with pytest.raises(TokenInvalido):
        decodificar_token(falso)


async def test_token_expirado_se_rechaza():
    from datetime import timedelta

    t = crear_access_token(subject=1, rol="admin_psd", expira_en=timedelta(seconds=-10))
    with pytest.raises(TokenInvalido):
        decodificar_token(t)


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


async def test_login_correcto(cliente_http, usuarios):
    r = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "admin@psdigital.mx", "password": PASSWORD},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


async def test_login_normaliza_mayusculas_del_correo(cliente_http, usuarios):
    r = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "ADMIN@PSDigital.MX", "password": PASSWORD},
    )
    assert r.status_code == 200


async def test_password_incorrecta(cliente_http, usuarios):
    r = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "admin@psdigital.mx", "password": "incorrecta-123"},
    )
    assert r.status_code == 401


async def test_mismo_mensaje_para_correo_inexistente_y_password_mala(
    cliente_http, usuarios
):
    """Mensajes distintos permitirian enumerar los correos validos."""
    a = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "nadie@ejemplo.mx", "password": PASSWORD},
    )
    b = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "admin@psdigital.mx", "password": "incorrecta-123"},
    )
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["mensaje"] == b.json()["error"]["mensaje"]


async def test_usuario_inactivo_no_entra(cliente_http, usuarios):
    r = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "exempleado@circo.mx", "password": PASSWORD},
    )
    assert r.status_code == 401


async def test_autenticar_con_cuenta_inactiva_levanta_error(db, usuarios):
    from app.services.auth import AuthService

    with pytest.raises(CredencialesInvalidas):
        await AuthService(db).autenticar("exempleado@circo.mx", PASSWORD)


# --------------------------------------------------------------------------
# Sesion
# --------------------------------------------------------------------------


async def test_me_devuelve_al_usuario(cliente_http, usuarios):
    t = await token_de(usuarios["org_a"])
    r = await cliente_http.get("/api/v1/auth/me", headers=auth(t))
    assert r.status_code == 200
    assert r.json()["email"] == "org.a@circo.mx"
    assert r.json()["rol"] == "organizador"


async def test_me_sin_token(cliente_http):
    assert (await cliente_http.get("/api/v1/auth/me")).status_code == 401


async def test_me_con_token_basura(cliente_http):
    r = await cliente_http.get("/api/v1/auth/me", headers=auth("no-es-un-token"))
    assert r.status_code == 401


async def test_desactivar_usuario_revoca_su_token_al_instante(
    cliente_http, usuarios, db
):
    """El token sigue siendo criptograficamente valido, pero la cuenta ya
    no existe para el sistema. Por eso se relee el usuario en cada
    peticion en lugar de confiar solo en el token."""
    usuario = usuarios["org_a"]
    t = await token_de(usuario)
    assert (
        await cliente_http.get("/api/v1/auth/me", headers=auth(t))
    ).status_code == 200

    usuario.activo = False
    await db.flush()

    assert (
        await cliente_http.get("/api/v1/auth/me", headers=auth(t))
    ).status_code == 401


async def test_cambio_de_password(cliente_http, usuarios):
    t = await token_de(usuarios["org_a"])
    r = await cliente_http.post(
        "/api/v1/auth/password",
        headers=auth(t),
        json={"password_actual": PASSWORD, "password_nueva": "NuevaSecreta-123"},
    )
    assert r.status_code == 204

    login = await cliente_http.post(
        "/api/v1/auth/login",
        json={"email": "org.a@circo.mx", "password": "NuevaSecreta-123"},
    )
    assert login.status_code == 200


async def test_cambio_de_password_exige_la_actual(cliente_http, usuarios):
    t = await token_de(usuarios["org_a"])
    r = await cliente_http.post(
        "/api/v1/auth/password",
        headers=auth(t),
        json={"password_actual": "no-es-la-mia", "password_nueva": "NuevaSecreta-123"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Aislamiento entre organizadores
# --------------------------------------------------------------------------


async def test_organizador_no_toca_datos_de_otro_cliente(usuarios, clientes):
    """La garantia critica de este modulo."""
    from app.api.deps import verificar_acceso_cliente

    _, cliente_b = clientes
    with pytest.raises(RecursoNoEncontrado):
        verificar_acceso_cliente(
            usuarios["org_a"], cliente_b.id, no_encontrado=EVENTO_NO_EXISTE
        )


async def test_organizador_si_toca_lo_suyo(usuarios, clientes):
    from app.api.deps import verificar_acceso_cliente

    cliente_a, _ = clientes
    verificar_acceso_cliente(
        usuarios["org_a"], cliente_a.id, no_encontrado=EVENTO_NO_EXISTE
    )


async def test_admin_psd_toca_cualquier_cliente(usuarios, clientes):
    from app.api.deps import verificar_acceso_cliente

    cliente_a, cliente_b = clientes
    verificar_acceso_cliente(
        usuarios["admin"], cliente_a.id, no_encontrado=EVENTO_NO_EXISTE
    )
    verificar_acceso_cliente(
        usuarios["admin"], cliente_b.id, no_encontrado=EVENTO_NO_EXISTE
    )


async def test_el_error_no_revela_si_el_recurso_existe(usuarios):
    """Mensajes distintos para 'no existe' y 'no es tuyo' permitirian
    descubrir que clientes hay sondeando identificadores."""
    from app.api.deps import verificar_acceso_cliente

    with pytest.raises(RecursoNoEncontrado) as a:
        verificar_acceso_cliente(
            usuarios["org_a"], 99999, no_encontrado=EVENTO_NO_EXISTE
        )
    with pytest.raises(RecursoNoEncontrado) as b:
        verificar_acceso_cliente(usuarios["org_a"], 2, no_encontrado=EVENTO_NO_EXISTE)
    assert a.value.mensaje == b.value.mensaje == EVENTO_NO_EXISTE
    # Y es el mismo error que da el servicio cuando el evento no existe.
    assert a.value.http_status == RecursoNoEncontrado.http_status == 404


async def test_alcance_de_admin_es_global(usuarios):
    from app.api.deps import alcance_cliente

    assert await alcance_cliente(usuarios["admin"]) is None


async def test_alcance_de_organizador_es_su_cliente(usuarios, clientes):
    from app.api.deps import alcance_cliente

    cliente_a, _ = clientes
    assert await alcance_cliente(usuarios["org_a"]) == cliente_a.id


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------


async def test_rol_insuficiente_se_rechaza(usuarios):
    from app.api.deps import requiere_rol

    verificar = requiere_rol(RolUsuario.ADMIN_PSD)
    with pytest.raises(PermisoInsuficiente):
        await verificar(usuarios["acceso"])


async def test_rol_permitido_pasa(usuarios):
    from app.api.deps import requiere_rol

    verificar = requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR)
    assert await verificar(usuarios["org_a"]) is usuarios["org_a"]


async def test_personal_de_acceso_no_es_organizador(usuarios):
    from app.api.deps import requiere_rol

    verificar = requiere_rol(RolUsuario.ORGANIZADOR)
    with pytest.raises(PermisoInsuficiente):
        await verificar(usuarios["acceso"])
