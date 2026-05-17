"""FastAPI entrypoint for the Goals dashboard."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dashboard.routes import router


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Purcival Goals Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(router)
