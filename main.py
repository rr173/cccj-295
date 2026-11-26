"""Convenience entry point: ``python main.py``.

The canonical ASGI application remains ``app.main:app``.
"""

from __future__ import annotations

import os

import uvicorn

from app.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
