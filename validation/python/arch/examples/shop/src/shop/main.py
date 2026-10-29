"""The app factory: settings, routers and the HTTP layer."""

from __future__ import annotations

from fastapi import FastAPI

from shop.config import get_settings
from shop.orders.routes import router as orders_router
from shop.platform.health import router as health_router
from shop.platform.http import install_http_layer
from shop.users.routes import router as users_router


def create_app() -> FastAPI:
    # Reading the settings refuses a shipped placeholder secret before any route is served.
    get_settings()
    app = FastAPI(title="Shop", version="1.0.0")
    for router in (health_router, users_router, orders_router):
        app.include_router(router, prefix="/api/v1")
    install_http_layer(app)
    return app
