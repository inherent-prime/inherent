"""Raw HTTPExceptions must render as RFC7807 problem+json (#12).

Several routes raise ``fastapi.HTTPException`` (404/403) directly. Without a
handler, FastAPI serves those as ``{"detail": ...}`` with ``application/json``,
inconsistent with the problem+json every other error uses.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.middleware.error_handler import setup_exception_handlers


def _app() -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/notfound")
    async def notfound():
        raise HTTPException(status_code=404, detail="Document not found")

    @app.get("/forbidden")
    async def forbidden():
        raise HTTPException(status_code=403, detail="Nope")

    return app


def test_raw_http_exception_renders_problem_json():
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get("/notfound")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404
    assert body["detail"] == "Document not found"
    # RFC7807 required members present.
    assert "type" in body and "title" in body


def test_forbidden_http_exception_maps_title():
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Authorization Failed"
