"""The FastAPI application."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from .db import make_engine, make_sessionmaker, migrate
from .settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.auto_migrate:
        migrate(settings.database_url)
    engine = make_engine(settings.database_url)

    app = FastAPI(title="bastion control plane", version="0.1.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from fastapi.staticfiles import StaticFiles

    from .routes import auth, keys, manage, portable, sdk, traces, ui
    from .workspace import ChangeRejected, NotFound

    app.include_router(auth.router)
    app.include_router(manage.router)
    app.include_router(keys.router)
    app.include_router(sdk.router)
    app.include_router(traces.router)
    app.include_router(portable.router)
    app.include_router(ui.router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

    @app.exception_handler(NotFound)
    def _not_found(_req: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ChangeRejected)
    def _rejected(_req: Request, exc: ChangeRejected):
        # The SDK compiler's own message, so the fix is obvious.
        return JSONResponse({"detail": exc.message, "app": exc.app}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
