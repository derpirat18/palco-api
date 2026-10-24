"""Checkout, estado de la orden y webhook de Stripe.

Publicos: el comprador no tiene sesion y Stripe tampoco. El checkout se
autoriza con el `hold_id`; la orden, con su `uuid`; el webhook, con la
firma de Stripe.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import UUID4

from app.api.deps import DB
from app.api.v1.apartados import Holds
from app.core.config import settings
from app.models.sales import Order, OrderItem
from app.schemas.boletos import (
    BoletoResponse,
    BoletosResponse,
    EventoDelBoletoResponse,
    FuncionDelBoletoResponse,
    RecintoDelBoletoResponse,
)
from app.schemas.checkout import (
    CheckoutRequest,
    CheckoutResponse,
    OrdenResponse,
    PagoResponse,
    PartidaResponse,
)
from app.services.boletos import PASO, boletos_de_la_orden
from app.services.checkout import (
    Atribucion,
    CheckoutService,
    DatosDelComprador,
    consultar_orden,
)
from app.services.limites import limite, verificar_ordenes_pendientes
from app.services.pasarela import Pasarela, pasarela_de_stripe
from app.services.webhooks import procesar_evento

router = APIRouter(tags=["checkout"])


def get_pasarela() -> Pasarela:
    """Dependencia para que las pruebas usen una pasarela falsa."""
    return pasarela_de_stripe()


PasarelaDep = Annotated[Pasarela, Depends(get_pasarela)]


def _orden(orden: Order, partidas: list[OrderItem]) -> OrdenResponse:
    return OrdenResponse(
        uuid=orden.uuid,
        folio=orden.folio,
        estado=orden.estado,
        moneda=orden.moneda,
        subtotal=orden.subtotal,
        iva=orden.iva,
        iva_pct=orden.iva_pct_snap,
        descuento=orden.descuento,
        total=orden.total,
        expira_en=orden.expira_en,
        pagada_en=orden.pagada_en,
        partidas=[
            PartidaResponse(
                zona=p.zona_snap,
                asiento=p.asiento_snap,
                precio=p.precio_unitario,
                iva=p.iva,
                importe=p.importe,
            )
            for p in partidas
        ],
    )


@router.post(
    "/performances/{performance_id}/apartados/{hold_id}/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[limite("checkout")],
)
async def iniciar_checkout(
    performance_id: int,
    hold_id: UUID4,
    datos: CheckoutRequest,
    db: DB,
    holds: Holds,
    pasarela: PasarelaDep,
) -> CheckoutResponse:
    """Convierte el apartado en una orden y prepara el cobro en Stripe.

    Repetirla con los mismos datos devuelve la misma orden: el doble clic
    no abre dos cobros.
    """
    await verificar_ordenes_pendientes(db, str(datos.email), hold_id.hex)
    iniciada = await CheckoutService(db, holds, pasarela).iniciar(
        performance_id,
        hold_id.hex,
        DatosDelComprador(
            email=str(datos.email),
            nombre=datos.nombre,
            apellidos=datos.apellidos,
            telefono=datos.telefono,
            acepta_aviso=datos.acepta_aviso,
            aviso_version=datos.aviso_version,
            acepta_marketing=datos.acepta_marketing,
        ),
        Atribucion(
            utm_source=datos.utm_source,
            utm_medium=datos.utm_medium,
            utm_campaign=datos.utm_campaign,
            utm_content=datos.utm_content,
            canal=datos.canal,
        ),
    )
    await db.commit()
    return CheckoutResponse(
        orden=_orden(iniciada.orden, iniciada.partidas),
        pago=PagoResponse(
            client_secret=iniciada.client_secret,
            publishable_key=settings.STRIPE_PUBLISHABLE_KEY,
        ),
    )


@router.get(
    "/ordenes/{orden_uuid}",
    response_model=OrdenResponse,
    dependencies=[limite("consulta")],
)
async def consultar(orden_uuid: UUID, db: DB) -> OrdenResponse:
    orden, partidas = await consultar_orden(db, orden_uuid)
    return _orden(orden, partidas)


@router.get(
    "/ordenes/{orden_uuid}/boletos",
    response_model=BoletosResponse,
    dependencies=[limite("consulta")],
)
async def boletos(orden_uuid: UUID, db: DB) -> BoletosResponse:
    """La vista "Tus boletos": la llave del QR de cada lugar, para que el
    celular lo genere (tambien sin internet), y si ya entro."""
    vista = await boletos_de_la_orden(db, orden_uuid)
    return BoletosResponse(
        folio=vista.orden.folio,
        estado=vista.orden.estado,
        evento=EventoDelBoletoResponse(
            nombre=vista.evento.nombre,
            slug=vista.evento.slug,
            imagen_url=vista.evento.imagen_url,
        ),
        funcion=FuncionDelBoletoResponse(
            uuid=vista.funcion.uuid,
            inicia_en=vista.funcion.inicia_en,
            apertura_puertas=vista.funcion.apertura_puertas,
        ),
        recinto=RecintoDelBoletoResponse(
            nombre=vista.recinto.nombre,
            direccion=vista.recinto.direccion,
            ciudad=vista.recinto.ciudad,
            zona_horaria=vista.recinto.zona_horaria,
        ),
        boletos=[BoletoResponse(**b.__dict__) for b in vista.boletos],
        servidor_ahora=vista.servidor_ahora,
        paso_segundos=PASO,
    )


@router.post(
    "/ordenes/{orden_uuid}/confirmar",
    response_model=OrdenResponse,
    dependencies=[limite("confirmar")],
)
async def confirmar(
    orden_uuid: UUID, db: DB, holds: Holds, pasarela: PasarelaDep
) -> OrdenResponse:
    """El front la llama al volver de Stripe para no esperar al webhook.

    Hace lo mismo que el webhook y da igual cual llegue primero.
    """
    orden, _ = await consultar_orden(db, orden_uuid)
    await CheckoutService(db, holds, pasarela).finalizar(orden.id)
    orden, partidas = await consultar_orden(db, orden_uuid)
    return _orden(orden, partidas)


@router.post("/pagos/stripe/webhook", include_in_schema=False)
async def webhook_de_stripe(
    request: Request, db: DB, holds: Holds, pasarela: PasarelaDep
) -> dict:
    # El cuerpo crudo: la firma se calcula sobre los bytes exactos.
    payload = await request.body()
    evento = pasarela.verificar_evento(payload, request.headers.get("stripe-signature"))
    resultado = await procesar_evento(db, evento, CheckoutService(db, holds, pasarela))
    return {"recibido": True, "resultado": resultado}
