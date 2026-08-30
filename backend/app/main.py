from fastapi import FastAPI

from app.api import auth, health, invoices
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(invoices.router)

    return app


app = create_app()