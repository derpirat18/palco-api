"""La pasarela de pago, detras de una interfaz chica.

El checkout no habla con Stripe directamente: habla con `Pasarela`. Asi
las pruebas usan una pasarela falsa que puede fallar a voluntad (tarjeta
autorizada, Stripe caido, captura rechazada) sin red ni llaves, y el dia
que se agregue Connect u otro proveedor el cambio vive aqui.

Dos clases de error, porque piden respuestas opuestas:

- `PasarelaNoDisponible`: transitorio (red, Stripe con 5xx, limite de
  peticiones). Se reintenta. Nunca se toma como "el cobro no procede".
- `CobroRechazado`: definitivo. Stripe dice que ese intento ya no se
  puede capturar. Ahi si se deshace la venta.

Confundirlas es caro: tratar un tropiezo de red como rechazo cancela
boletos ya emitidos de alguien que si pago.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import stripe

from app.core.config import settings
from app.core.errors import ErrorDeDominio

# Estados de un PaymentIntent que importan aqui.
AUTORIZADO = "requires_capture"
COBRADO = "succeeded"
CANCELADO = "canceled"


class PasarelaNoDisponible(ErrorDeDominio):
    """Transitorio: vuelve a intentar mas tarde."""

    codigo = "pasarela_no_disponible"
    http_status = 502


class CobroRechazado(ErrorDeDominio):
    """Definitivo: ese intento ya no se puede capturar."""

    codigo = "cobro_rechazado"
    http_status = 409


class FirmaInvalida(ErrorDeDominio):
    codigo = "firma_invalida"
    http_status = 400


class PasarelaSinConfigurar(ErrorDeDominio):
    """Faltan llaves. Nunca se acepta un webhook sin poder verificarlo."""

    codigo = "pasarela_sin_configurar"
    http_status = 503


@dataclass(frozen=True)
class IntentoDePago:
    id: str
    estado: str
    monto_centavos: int
    moneda: str
    client_secret: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class Pasarela(Protocol):
    async def crear_intento(
        self,
        *,
        monto_centavos: int,
        moneda: str,
        descripcion: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> IntentoDePago: ...

    async def obtener(self, intento_id: str) -> IntentoDePago: ...

    async def capturar(
        self, intento_id: str, *, idempotency_key: str
    ) -> IntentoDePago: ...

    async def cancelar(
        self, intento_id: str, *, idempotency_key: str
    ) -> IntentoDePago: ...

    def verificar_evento(self, payload: bytes, firma: str | None) -> dict: ...


def _transitorio(exc: stripe.StripeError) -> bool:
    return isinstance(
        exc,
        stripe.APIConnectionError
        | stripe.RateLimitError
        | stripe.APIError
        | stripe.AuthenticationError,
    )


def _intento(pi) -> IntentoDePago:
    return IntentoDePago(
        id=pi.id,
        estado=pi.status,
        monto_centavos=int(pi.amount),
        moneda=str(pi.currency).upper(),
        client_secret=pi.client_secret,
        # En el SDK v16 los objetos de Stripe ya no se comportan como dict.
        metadata=pi.metadata.to_dict() if pi.metadata else {},
    )


class StripePasarela:
    """Stripe con captura manual: primero se autoriza, despues se cobra."""

    def __init__(
        self,
        secret_key: str | None = None,
        webhook_secret: str | None = None,
        http_client: stripe.HTTPClient | None = None,
    ) -> None:
        self._secret_key = (
            secret_key if secret_key is not None else (settings.STRIPE_SECRET_KEY)
        )
        self._webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else (settings.STRIPE_WEBHOOK_SECRET)
        )
        # Inyectable para probar las peticiones que arma el SDK sin red.
        self._http = http_client
        self._cliente: stripe.StripeClient | None = None

    def _stripe(self) -> stripe.StripeClient:
        if not self._secret_key:
            raise PasarelaSinConfigurar("Falta STRIPE_SECRET_KEY en el .env.")
        if self._cliente is None:
            self._cliente = stripe.StripeClient(
                self._secret_key, http_client=self._http or stripe.HTTPXClient()
            )
        return self._cliente

    def _intentos(self):
        # Desde el SDK v12 los servicios viven bajo `v1`; el atajo viejo
        # avisa que va a desaparecer.
        cliente = self._stripe()
        return getattr(cliente, "v1", cliente).payment_intents

    async def crear_intento(
        self,
        *,
        monto_centavos: int,
        moneda: str,
        descripcion: str,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> IntentoDePago:
        try:
            pi = await self._intentos().create_async(
                {
                    "amount": monto_centavos,
                    "currency": moneda.lower(),
                    # Captura manual: si al final la butaca ya no es suya,
                    # se cancela la autorizacion y nunca ve un cargo.
                    "capture_method": "manual",
                    # Solo tarjeta. OXXO y SPEI se pagan horas o dias
                    # despues y no admiten captura manual: no caben en un
                    # apartado de minutos.
                    "payment_method_types": ["card"],
                    "description": descripcion,
                    "metadata": metadata,
                },
                {"idempotency_key": idempotency_key},
            )
        except stripe.StripeError as exc:
            raise PasarelaNoDisponible(
                "No pudimos iniciar el pago. Intenta de nuevo en un momento."
            ) from exc
        return _intento(pi)

    async def obtener(self, intento_id: str) -> IntentoDePago:
        try:
            pi = await self._intentos().retrieve_async(intento_id)
        except stripe.StripeError as exc:
            raise PasarelaNoDisponible("No pudimos consultar el pago.") from exc
        return _intento(pi)

    async def capturar(self, intento_id: str, *, idempotency_key: str) -> IntentoDePago:
        try:
            pi = await self._intentos().capture_async(
                intento_id, options={"idempotency_key": idempotency_key}
            )
        except stripe.StripeError as exc:
            if _transitorio(exc):
                raise PasarelaNoDisponible("No pudimos capturar el pago.") from exc
            # Stripe se nego. Puede ser que ya estaba capturado (reintento
            # de un reintento): eso no es rechazo.
            actual = await self.obtener(intento_id)
            if actual.estado == COBRADO:
                return actual
            raise CobroRechazado(
                "El pago ya no se puede cobrar.",
                {"estado": actual.estado},
            ) from exc
        return _intento(pi)

    async def cancelar(self, intento_id: str, *, idempotency_key: str) -> IntentoDePago:
        try:
            pi = await self._intentos().cancel_async(
                intento_id, options={"idempotency_key": idempotency_key}
            )
        except stripe.StripeError as exc:
            if _transitorio(exc):
                raise PasarelaNoDisponible("No pudimos cancelar el pago.") from exc
            # Ya cancelado, ya cobrado, o en proceso: quien llama decide
            # con el estado real.
            return await self.obtener(intento_id)
        return _intento(pi)

    def verificar_evento(self, payload: bytes, firma: str | None) -> dict:
        """Comprueba que el webhook lo mando Stripe y lo devuelve como dict.

        Sin secreto configurado no se acepta nada: un webhook sin verificar
        es una puerta para que cualquiera marque ordenes como pagadas.
        """
        if not self._webhook_secret:
            raise PasarelaSinConfigurar("Falta STRIPE_WEBHOOK_SECRET en el .env.")
        try:
            stripe.Webhook.construct_event(payload, firma, self._webhook_secret)
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise FirmaInvalida("La firma del webhook no es valida.") from exc
        return json.loads(payload)


_pasarela: StripePasarela | None = None


def pasarela_de_stripe() -> StripePasarela:
    global _pasarela
    if _pasarela is None:
        _pasarela = StripePasarela()
    return _pasarela
