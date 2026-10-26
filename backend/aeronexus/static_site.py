"""Serve the built React app from the API process (single-service deploys such as Railway).

Enabled only when AERONEXUS_STATIC_DIR points at a Vite build (frontend/dist). API, GraphQL, WebSocket, health and metrics
routes are registered first and keep priority; any other GET falls back to index.html so client-side routes work.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

RESERVED = ("api/", "graphql", "ws/", "health", "metrics", "upload", "docs", "openapi.json", "mcp")


def mount(app: FastAPI) -> bool:
    root = os.environ.get("AERONEXUS_STATIC_DIR")
    if not root or not (Path(root) / "index.html").is_file():
        return False
    base = Path(root).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith(RESERVED):
            raise HTTPException(404)
        f = (base / path).resolve()
        if path and f.is_file() and base in f.parents:  # no path traversal outside the build folder
            return FileResponse(f)
        return FileResponse(base / "index.html")

    return True
