"""Aeternix Claims-to-Cash API. Run: ``uvicorn app.main:app``."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import routers
from .config import settings
from .db import init_db

@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.demo_mode and settings.secret_key == "dev-insecure-change-me":
        raise RuntimeError("Set SECRET_KEY before running with DEMO_MODE off: sign-in tokens are signed with it.")
    init_db()
    yield


app = FastAPI(title="Aeternix Claims-to-Cash", version="1.0.0", lifespan=lifespan,
              description="Government-scheme-first claims ledger: case to claim to query to payer to bank.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"],
                   # the Chrome extension calls the API from its own origin
                   allow_origin_regex=r"chrome-extension://.*")

for r in routers:
    app.include_router(r)


@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version}


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


STATIC = Path(__file__).parent / "static"
if settings.demo_mode:
    app.mount("/mock-portal", StaticFiles(directory=STATIC / "mock-portal", html=True), name="mock-portal")

if settings.web_dist.exists():
    app.mount("/assets", StaticFiles(directory=settings.web_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = settings.web_dist / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(settings.web_dist / "index.html")
