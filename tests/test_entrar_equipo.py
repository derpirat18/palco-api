"""Una sola puerta para entrar: el equipo con contrasena, el comprador con codigo.

Lo que importa:

- Recuperar la contrasena responde igual sea o no del equipo el correo; solo
  al equipo le llega el codigo. El codigo sirve una vez y no sirve para
  entrar como comprador (ni el de entrar sirve para la contrasena).
- Al cambiarla se cierran sus sesiones de antes y queda probado su correo.
- Quien entra con contrasena ve tambien sus boletos solo si ya probo que el
  correo es suyo: su cuenta la creo alguien mas con el correo que escribio.
- Una contrasena temporal nueva (la pone PSD o el organizador) tambien
  cierra sus sesiones.
"""

import pytest
from sqlalchemy import select

from app.models.users import User
from app.services.usuarios import UsuariosService
from tests.test_catalog import API, auth
from tests.test_correo import CarteroFalso
from tests.test_cuenta import CTA, entrar, ultimo_codigo

pytestmark = pytest.mark.asyncio

PW = "Secreta-12345"


@pytest.fixture
def cartero(redis_test):
    from app.api.v1.cuenta import get_cartero_cuenta, get_redis_cuenta
    from app.main import app

    falso = CarteroFalso()
    app.dependency_overrides[get_cartero_cuenta] = lambda: falso
    app.dependency_overrides[get_redis_cuenta] = lambda: redis_test
    return falso


async def login(cliente_http, email: str, password: str = PW):
    return await cliente_http.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )


async def token_del_equipo(cliente_http, email: str, password: str = PW) -> dict:
    r = await login(cliente_http, email, password)
    assert r.status_code == 200, r.text
    return auth(r.json()["access_token"])


async def test_recuperar_no_dice_quien_es_del_equipo(cliente_http, cartero, usuarios):
    for correo in ("org.a@circo.mx", "nadie@correo.mx"):
        r = await cliente_http.post(f"{API}/auth/recuperar", json={"email": correo})
        assert r.status_code == 202, r.text
        assert set(r.json()) == {"enviado_a", "minutos"}
    # Solo al equipo le llega.
    assert [m.para for m in cartero.enviados] == ["org.a@circo.mx"]
    assert "contraseña" in cartero.enviados[0].asunto


async def test_cambiar_la_contrasena_con_el_codigo(cliente_http, db, cartero, usuarios):
    viejo = await token_del_equipo(cliente_http, "org.a@circo.mx")
    await cliente_http.post(f"{API}/auth/recuperar", json={"email": "Org.A@circo.mx"})
    codigo = ultimo_codigo(cartero, "org.a@circo.mx")

    # El codigo de la contrasena no sirve para entrar como comprador.
    r = await cliente_http.post(
        f"{CTA}/entrar", json={"correo": "org.a@circo.mx", "codigo": codigo}
    )
    assert r.status_code == 422

    nueva = "Una-frase-nueva-2026"
    r = await cliente_http.post(
        f"{API}/auth/recuperar/confirmar",
        json={"email": "org.a@circo.mx", "codigo": codigo, "password_nueva": nueva},
    )
    assert r.status_code == 204, r.text
    # La sesion de antes termino; la contrasena vieja ya no entra.
    r = await cliente_http.get(f"{API}/auth/me", headers=viejo)
    assert r.status_code == 401
    assert (await login(cliente_http, "org.a@circo.mx")).status_code == 401
    nuevo = await token_del_equipo(cliente_http, "org.a@circo.mx", nueva)
    r = await cliente_http.get(f"{API}/auth/me", headers=nuevo)
    assert r.status_code == 200
    # Sirve una sola vez.
    r = await cliente_http.post(
        f"{API}/auth/recuperar/confirmar",
        json={"email": "org.a@circo.mx", "codigo": codigo, "password_nueva": nueva},
    )
    assert r.status_code == 422
    u = await db.scalar(select(User).where(User.email == "org.a@circo.mx"))
    await db.refresh(u)
    assert u.email_verificado_en is not None


async def test_el_codigo_de_entrar_no_cambia_la_contrasena(
    cliente_http, cartero, usuarios
):
    await cliente_http.post(f"{CTA}/codigo", json={"correo": "org.a@circo.mx"})
    codigo = ultimo_codigo(cartero, "org.a@circo.mx")
    r = await cliente_http.post(
        f"{API}/auth/recuperar/confirmar",
        json={
            "email": "org.a@circo.mx",
            "codigo": codigo,
            "password_nueva": "Otra-frase-larga-1",
        },
    )
    assert r.status_code == 422


async def test_correo_que_no_es_del_equipo_no_cambia_nada(
    cliente_http, cartero, usuarios
):
    await cliente_http.post(f"{API}/auth/recuperar", json={"email": "nadie@correo.mx"})
    # No le llego, pero aunque lo adivinara: no hay cuenta que cambiar.
    r = await cliente_http.post(
        f"{API}/auth/recuperar/confirmar",
        json={
            "email": "nadie@correo.mx",
            "codigo": "123456",
            "password_nueva": "Otra-frase-larga-1",
        },
    )
    assert r.status_code == 422


async def test_ve_sus_boletos_solo_con_el_correo_probado(
    cliente_http, cartero, usuarios
):
    equipo = await token_del_equipo(cliente_http, "org.a@circo.mx")
    r = await cliente_http.post(f"{CTA}/desde-equipo", headers=equipo)
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "correo_sin_confirmar"

    # Prueba que el correo es suyo con el codigo de entrar (una vez).
    await entrar(cliente_http, cartero, "org.a@circo.mx")
    r = await cliente_http.post(f"{CTA}/desde-equipo", headers=equipo)
    assert r.status_code == 200, r.text
    # Dura lo que la sesion del equipo (8 h), no 30 dias: puede ser la taquilla.
    assert r.json()["expira_en_segundos"] == 8 * 3600
    r = await cliente_http.get(CTA, headers=auth(r.json()["token"]))
    assert r.json()["correo"] == "org.a@circo.mx"

    # Sin sesion del equipo, no.
    r = await cliente_http.post(f"{CTA}/desde-equipo")
    assert r.status_code == 401


async def test_temporal_nueva_cierra_sus_sesiones(cliente_http, db, usuarios):
    h = await token_del_equipo(cliente_http, "puerta@circo.mx")
    await UsuariosService(db).restablecer(usuarios["acceso"].id)
    await db.commit()
    r = await cliente_http.get(f"{API}/auth/me", headers=h)
    assert r.status_code == 401
