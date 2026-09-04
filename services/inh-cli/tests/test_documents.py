from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

import inh_cli.client as client_mod
from inh_cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _transport(handler):
    return httpx.MockTransport(handler)


def test_docs_list_method_path_headers(api_env, inherent_home, runner, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "id": "doc_1",
                        "name": "README.md",
                        "status": "processed",
                        "chunk_count": 3,
                        "updated_at": "2026-01-01",
                    }
                ],
                "total": 1,
                "page": 1,
                "page_size": 20,
            },
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "list"])
    assert result.exit_code == 0, result.output
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/documents"
    assert seen[0].headers["x-api-key"] == "ink_test"


def test_docs_list_json_only_stdout(api_env, inherent_home, runner, monkeypatch) -> None:
    payload = {"documents": [], "total": 0, "page": 1, "page_size": 20}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["--json", "docs", "list"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload


def test_docs_show_failed_prints_hint(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/documents/doc_bad"
        return httpx.Response(
            200,
            json={"id": "doc_bad", "status": "failed", "error_message": "extract exploded"},
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "show", "doc_bad"])
    assert result.exit_code == 0
    assert "extract exploded" in result.stdout
    assert "inherent logs inh-ingestion-svc" in result.stdout


def test_upload_missing_file_sends_no_request(api_env, inherent_home, runner, monkeypatch) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={}, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "upload", str(inherent_home / "missing.md")])
    assert result.exit_code == 1
    assert seen == []


def test_upload_unsupported_extension_is_local(api_env, inherent_home, runner, monkeypatch) -> None:
    mystery = inherent_home / "notes.xyz"
    mystery.write_text("nope")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={}, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "upload", str(mystery)])
    assert result.exit_code == 1
    assert "Unsupported file type" in result.output
    assert seen == []


def test_upload_sends_multipart(api_env, inherent_home, runner, monkeypatch) -> None:
    readme = inherent_home / "README.md"
    readme.write_text("# hi")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/documents"
        return httpx.Response(201, json={"document_id": "doc_9"}, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "upload", str(readme)])
    assert result.exit_code == 0, result.output
    assert "doc_9" in result.stdout


def test_delete_without_yes_or_tty_does_not_delete(
    api_env, inherent_home, runner, monkeypatch
) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "delete", "doc_1"])
    assert result.exit_code == 1
    assert seen == []


def test_delete_with_yes(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/documents/doc_1"
        return httpx.Response(204, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "delete", "doc_1", "--yes"])
    assert result.exit_code == 0, result.output


def test_refresh_and_lineage_and_chunks(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/v1/documents/doc_1/refresh"
            return httpx.Response(
                200, json={"document_id": "doc_1", "status": "pending"}, request=request
            )
        if request.url.path.endswith("/lineage"):
            return httpx.Response(
                200, json={"source_uri": "s3://x", "is_stale": False}, request=request
            )
        assert request.url.path == "/v1/chunks/doc_1"
        return httpx.Response(
            200,
            json=[{"id": "c1", "chunk_index": 0, "token_count": 4, "content": "hi"}],
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    assert runner.invoke(app, ["docs", "refresh", "doc_1"]).exit_code == 0
    lineage = runner.invoke(app, ["--json", "docs", "lineage", "doc_1"])
    assert json.loads(lineage.stdout)["source_uri"] == "s3://x"
    chunks = runner.invoke(app, ["--json", "chunks", "doc_1"])
    assert json.loads(chunks.stdout)[0]["id"] == "c1"


def test_multiple_workspaces_hint(api_env, inherent_home, runner, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": "Multiple workspaces found. Provide X-Workspace-Id header."},
            request=request,
        )

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["docs", "list"])
    assert result.exit_code == 1
    assert "--workspace" in result.output


def test_upload_explicitly_unsupported_doc(api_env, inherent_home, runner, monkeypatch) -> None:
    legacy = inherent_home / "report.doc"
    legacy.write_bytes(b"legacy")
    seen = []
    monkeypatch.setattr(
        client_mod,
        "_transport",
        httpx.MockTransport(
            lambda req: seen.append(req) or httpx.Response(201, json={}, request=req)
        ),
    )
    result = runner.invoke(app, ["docs", "upload", str(legacy)])
    assert result.exit_code == 1
    assert "docx" in result.output.lower() or "not supported" in result.output.lower()
    assert seen == []


def test_docs_show_json(api_env, inherent_home, runner, monkeypatch) -> None:
    payload = {"id": "doc_1", "status": "processed"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(client_mod, "_transport", _transport(handler))
    result = runner.invoke(app, ["--json", "docs", "show", "doc_1"])
    assert json.loads(result.stdout) == payload


def test_no_config_exits_2(inherent_home, runner) -> None:
    result = runner.invoke(app, ["docs", "list"])
    assert result.exit_code == 2
    assert "No local stack found" in result.output


def test_docs_show_prints_full_values_not_an_elided_row(
    api_env, inherent_home, runner, monkeypatch
) -> None:
    """11 columns on one row elide every value at normal terminal widths."""
    document = {
        "id": "e01b3b18-bcc1-4933-8bda-d4b0e37625c1",
        "name": "review-doc.md",
        "workspace_id": "ws_review",
        "source_type": "s3",
        "mime_type": "application/octet-stream",
        "size_bytes": 87,
        "chunk_count": 1,
        "status": "processed",
        "created_at": "2026-09-04T05:47:12Z",
        "updated_at": "2026-09-04T05:47:19Z",
        "metadata": None,
    }
    monkeypatch.setattr(
        client_mod,
        "_transport",
        _transport(lambda request: httpx.Response(200, json=document, request=request)),
    )

    result = runner.invoke(app, ["docs", "show", document["id"]], env={"COLUMNS": "100"})

    assert result.exit_code == 0, result.output
    for value in (document["id"], "review-doc.md", "ws_review", "processed"):
        assert value in result.stdout, f"{value!r} was truncated away"
