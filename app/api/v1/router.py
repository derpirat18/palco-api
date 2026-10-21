from fastapi import APIRouter

from app.api.v1 import (
    apartados,
    auth,
    catalog,
    checkout,
    clients,
    cortesias,
    health,
    inventory,
    publico,
    usuarios,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["salud"])
api_router.include_router(auth.router, tags=["autenticacion"])
api_router.include_router(clients.router)
api_router.include_router(catalog.router)
api_router.include_router(inventory.router)
api_router.include_router(apartados.router)
api_router.include_router(checkout.router)
api_router.include_router(cortesias.router)
api_router.include_router(publico.router)
api_router.include_router(usuarios.router)
