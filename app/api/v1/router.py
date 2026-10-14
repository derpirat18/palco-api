from fastapi import APIRouter

from app.api.v1 import auth, catalog, clients, health, inventory

api_router = APIRouter()
api_router.include_router(health.router, tags=["salud"])
api_router.include_router(auth.router, tags=["autenticacion"])
api_router.include_router(clients.router)
api_router.include_router(catalog.router)
api_router.include_router(inventory.router)
