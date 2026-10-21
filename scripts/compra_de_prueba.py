"""Compra de prueba de punta a punta contra el sandbox.

Hace lo mismo que hara el front, paso por paso, para probar todo junto
antes de tener el front: catalogo, mapa, apartado con verificacion
humana, cobro con captura manual, webhook de Stripe, boletos y correo.

    python scripts/compra_de_prueba.py preparar
        Una sola vez: organizador, recinto con 60 lugares, evento y funcion
        de prueba, a la venta. Pide la contrasena del admin.

    python scripts/compra_de_prueba.py comprar <slug>
        Cada vez que se quiera probar: aparta 2 lugares y los paga con la
        tarjeta de prueba de Stripe. Sin sesion, como un comprador.

Solo funciona con llaves de prueba: si Stripe o Turnstile tienen llaves
reales, se detiene antes de cobrar. Nunca debe correr contra produccion.
"""

import argparse
import getpass
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

API_SANDBOX = "https://d1hg05h83ra02l.cloudfront.net/api/v1"
ADMIN = "juanmanuel@psdigital.mx"
# SES en modo de pruebas solo entrega a direcciones verificadas.
COMPRADOR = "juanmanuel@psdigital.mx"

# Lo que devuelve el widget de Turnstile con la sitekey de prueba. Con la
# llave secreta de prueba, Cloudflare la acepta; con una real, no.
TOKEN_DE_PRUEBA_TURNSTILE = "XXXX.DUMMY.TOKEN.XXXX"
SITEKEYS_DE_PRUEBA = (
    "1x00000000000000000000",
    "2x00000000000000000000",
    "3x00000000000000000000",
)

# Tarjeta Visa de prueba de Stripe, sin 3D Secure.
TARJETA_DE_PRUEBA = "pm_card_visa"

# La CDMX no cambia de horario desde 2022.
HORA_CDMX = timezone(timedelta(hours=-6))


def falla(r: httpx.Response, paso: str) -> None:
    """Corta con lo que dijo la API, sin traza de Python."""
    try:
        error = r.json().get("error", {})
        detalle = f"{error.get('codigo')}: {error.get('mensaje')}"
        if error.get("detalle"):
            detalle += f" {error['detalle']}"
    except ValueError:
        detalle = r.text[:300]
    sys.exit(f"\n[X] {paso} respondio {r.status_code}. {detalle}")


def pedir(
    api: httpx.Client, metodo: str, ruta: str, paso: str, esperado: int, **kw
) -> dict:
    r = api.request(metodo, ruta, **kw)
    if r.status_code != esperado:
        falla(r, paso)
    print(f"[ok] {paso}")
    return r.json() if r.content else {}


# --------------------------------------------------------------------------
# preparar
# --------------------------------------------------------------------------


def preparar(base: str) -> None:
    with httpx.Client(base_url=base, timeout=30) as api:
        password = getpass.getpass(f"Contraseña de {ADMIN}: ")
        token = pedir(
            api,
            "POST",
            "/auth/login",
            "Inicio de sesion",
            200,
            json={"email": ADMIN, "password": password},
        )["access_token"]
        del password
        api.headers["Authorization"] = f"Bearer {token}"

        cliente = pedir(
            api,
            "POST",
            "/clients",
            "Organizador de prueba",
            201,
            json={"nombre": "Organizador de prueba", "email": ADMIN},
        )
        recinto = pedir(
            api,
            "POST",
            "/venues",
            "Recinto de prueba",
            201,
            json={
                "nombre": "Recinto de prueba",
                "ciudad": "Ciudad de México",
                "estado_geo": "CDMX",
                "direccion": "Dirección de prueba 123",
                "capacidad": 60,
            },
        )
        mapa = pedir(
            api,
            "POST",
            f"/venues/{recinto['id']}/seat-maps/generar",
            "Mapa de 60 lugares",
            201,
            json={
                "nombre": "Mapa de prueba",
                "bloques": [
                    {
                        "zona": "Preferente",
                        "color": "#C0392B",
                        "filas": {"desde": "A", "hasta": "C"},
                        "butacas_por_fila": 10,
                        "origen_x": 100,
                        "origen_y": 100,
                    },
                    {
                        "zona": "General",
                        "color": "#2E86C1",
                        "filas": {"desde": "D", "hasta": "F"},
                        "butacas_por_fila": 10,
                        "origen_x": 100,
                        "origen_y": 220,
                    },
                ],
            },
        )
        zonas = pedir(api, "GET", f"/seat-maps/{mapa['id']}/zonas", "Zonas", 200)
        evento = pedir(
            api,
            "POST",
            "/events",
            "Evento de prueba",
            201,
            json={
                "client_id": cliente["id"],
                "venue_id": recinto["id"],
                "nombre": "Función de prueba Palco",
                "categoria": "Prueba",
                "descripcion": "Evento de prueba del sandbox. No es real.",
                "iva_pct": "16.00",
                "precios_incluyen_iva": True,
            },
        )

        dia = (datetime.now(HORA_CDMX) + timedelta(days=30)).date()
        inicia = datetime(dia.year, dia.month, dia.day, 20, 0, tzinfo=HORA_CDMX)
        funcion = pedir(
            api,
            "POST",
            f"/events/{evento['id']}/performances",
            "Funcion",
            201,
            json={
                "seat_map_id": mapa["id"],
                "inicia_en": inicia.isoformat(),
                "apertura_puertas": (inicia - timedelta(hours=1)).isoformat(),
            },
        )
        precios = {"Preferente": "350.00", "General": "250.00"}
        pedir(
            api,
            "PUT",
            f"/performances/{funcion['id']}/precios",
            "Precios",
            200,
            json={"precios": {str(z["id"]): precios[z["nombre"]] for z in zonas}},
        )
        inventario = pedir(
            api, "POST", f"/performances/{funcion['id']}/inventario", "Inventario", 201
        )
        pedir(
            api,
            "POST",
            f"/performances/{funcion['id']}/abrir-venta",
            "Venta abierta",
            200,
        )
        pedir(
            api,
            "PATCH",
            f"/events/{evento['id']}",
            "Evento publicado",
            200,
            json={"estado": "publicado"},
        )

    print(
        f"\nListo. Evento '{evento['nombre']}', "
        f"{inventario['vendibles']} lugares a la venta,"
        f" funcion el {inicia:%d/%m/%Y %H:%M} (CDMX).\n"
        f"Para comprar:  python scripts/compra_de_prueba.py comprar {evento['slug']}"
    )


# --------------------------------------------------------------------------
# comprar
# --------------------------------------------------------------------------


def elegir_dos(butacas: list[dict]) -> list[dict]:
    """Las dos primeras de una fila que este completa: no deja nadie solo."""
    filas: dict[tuple[int, str], list[dict]] = {}
    for b in butacas:
        filas.setdefault((b["zona_id"], b["fila"]), []).append(b)
    for fila in filas.values():
        if len(fila) >= 4 and all(b["estado"] == "libre" for b in fila):
            fila.sort(key=lambda b: float(b["x"]))
            return fila[:2]
    sys.exit("\n[X] Ya no hay filas completas libres. Corre 'preparar' otra vez.")


def comprar(base: str, slug: str) -> None:
    with httpx.Client(base_url=base, timeout=30) as api:
        config = pedir(
            api, "GET", "/publico/configuracion", "Configuracion publica", 200
        )
        # Sin sitekey la verificacion esta apagada (solo pasa en local).
        site_key = config["verificacion_humana"]["site_key"]
        if site_key and not site_key.startswith(SITEKEYS_DE_PRUEBA):
            sys.exit(
                "\n[X] Turnstile tiene llaves reales: esta prueba es solo para sandbox."
            )

        evento = pedir(api, "GET", f"/publico/eventos/{slug}", "Pagina del evento", 200)
        funcion = next(
            (f for f in evento["funciones"] if f["estado_venta"] == "en_venta"), None
        )
        if funcion is None:
            sys.exit("\n[X] El evento no tiene funciones a la venta.")

        mapa = pedir(
            api, "GET", f"/publico/funciones/{funcion['id']}/mapa", "Mapa", 200
        )
        lugares = elegir_dos(mapa["butacas"])
        zonas = {z["id"]: z["nombre"] for z in mapa["zonas"]}
        print(
            "     Lugares: "
            + ", ".join(
                f"{zonas[b['zona_id']]} {b['fila']}-{b['numero']}" for b in lugares
            )
        )

        hold_id = str(uuid.uuid4())
        apartado = pedir(
            api,
            "POST",
            f"/performances/{funcion['id']}/apartados",
            "Apartado (con verificacion humana)",
            201,
            json={"hold_id": hold_id, "seat_ids": [b["id"] for b in lugares]},
            headers={"X-Palco-Verificacion": TOKEN_DE_PRUEBA_TURNSTILE},
        )
        print(f"     Apartados por {apartado['segundos_restantes']} s")

        checkout = pedir(
            api,
            "POST",
            f"/performances/{funcion['id']}/apartados/{hold_id}/checkout",
            "Orden y cobro preparados",
            201,
            json={
                "email": COMPRADOR,
                "nombre": "Comprador",
                "apellidos": "De Prueba",
                "telefono": "5512345678",
                "acepta_aviso": True,
                "aviso_version": "2026-10",
                "acepta_marketing": False,
                "canal": "prueba",
            },
        )
        orden = checkout["orden"]
        pago = checkout["pago"]
        print(f"     Folio {orden['folio']}, total ${orden['total']} {orden['moneda']}")

        # Antes de tocar Stripe: nunca con una llave real.
        if not pago["publishable_key"].startswith("pk_test_"):
            sys.exit("\n[X] La llave de Stripe no es de prueba. No se cobra nada.")

        # Lo que hace el formulario de Stripe en la pagina: confirmar el pago
        # con la llave publica y el client_secret de esta orden.
        intento = pago["client_secret"].split("_secret_")[0]
        r = httpx.post(
            f"https://api.stripe.com/v1/payment_intents/{intento}/confirm",
            auth=(pago["publishable_key"], ""),
            data={
                "client_secret": pago["client_secret"],
                "payment_method": TARJETA_DE_PRUEBA,
            },
            timeout=30,
        )
        if r.status_code != 200:
            sys.exit(
                "\n[X] Stripe rechazo la tarjeta de prueba: "
                f"{r.json().get('error', {}).get('message')}"
            )
        print(f"[ok] Tarjeta autorizada en Stripe (estado: {r.json()['status']})")

        # No se llama a /confirmar: se espera a que el webhook de Stripe
        # cierre la orden solo. Asi se prueba el webhook de verdad.
        print("     Esperando el aviso de Stripe (webhook)...", end="", flush=True)
        limite = time.monotonic() + 90
        estado = orden["estado"]
        while time.monotonic() < limite:
            time.sleep(3)
            print(".", end="", flush=True)
            estado = api.get(f"/ordenes/{orden['uuid']}").json()["estado"]
            if estado != "pendiente":
                break
        print()
        if estado == "pagada":
            print("[ok] El webhook llego y la orden quedo pagada")
        else:
            print(
                f"[!] En 90 s el webhook no la cerro (estado: {estado}). "
                "Se confirma a mano."
            )
            estado = pedir(
                api,
                "POST",
                f"/ordenes/{orden['uuid']}/confirmar",
                "Confirmacion manual",
                200,
            )["estado"]
            if estado != "pagada":
                sys.exit(f"\n[X] La orden quedo en '{estado}'.")

        boletos = pedir(api, "GET", f"/ordenes/{orden['uuid']}/boletos", "Boletos", 200)
        for b in boletos["boletos"]:
            print(
                f"     {b['zona']} {b['asiento']}  codigo {b['codigo']}  "
                f"QR: {'si' if b['qr'] else 'no'}"
            )

    print(
        f"\nCompra completa a las {datetime.now(HORA_CDMX):%H:%M} (CDMX).\n"
        f"En 1 o 2 minutos debe llegar el correo con los boletos a {COMPRADOR}.\n"
        f"Orden: {orden['uuid']}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Compra de prueba contra el sandbox")
    p.add_argument("--api", default=API_SANDBOX, help="URL base de la API (…/api/v1)")
    sub = p.add_subparsers(dest="accion", required=True)
    sub.add_parser("preparar", help="Crea el evento de prueba (una vez)")
    c = sub.add_parser("comprar", help="Compra 2 lugares con la tarjeta de prueba")
    c.add_argument("slug")
    a = p.parse_args()
    if a.accion == "preparar":
        preparar(a.api)
    else:
        comprar(a.api, a.slug)


if __name__ == "__main__":
    main()
