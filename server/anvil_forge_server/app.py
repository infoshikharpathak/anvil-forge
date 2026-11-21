"""The FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI

from .db import make_engine, make_sessionmaker, migrate
from .settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.auto_migrate:
        migrate(settings.database_url)
    engine = make_engine(settings.database_url)

    app = FastAPI(title="anvil-forge control plane", version="0.1.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    from .routes import auth

    app.include_router(auth.router)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
