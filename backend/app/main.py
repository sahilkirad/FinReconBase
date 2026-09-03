from fastapi import FastAPI

from app.api import auth, health, invoices, batch, ingestion
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
    app.include_router(batch.router)
    app.include_router(ingestion.razorpay_router)
    app.include_router(ingestion.bank_router)

    return app


app = create_app()