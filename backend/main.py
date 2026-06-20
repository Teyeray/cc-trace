"""FastAPI application entrypoint.

Run with:  uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import api, config, hooks, terminal

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="cc-trace hook server", version="0.1.0")

# The Vite dev server runs on a different origin; allow local dev origins.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(hooks.router)
app.include_router(api.router)
app.include_router(terminal.router)


@app.on_event("startup")
def _ensure_storage() -> None:
    config.RAW_EVENTS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "raw_events_dir": str(config.RAW_EVENTS_DIR)}
