"""Document and chunk REST client commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from inh_cli.client import ClientError, call
from inh_cli.output import TableSpec, render, render_fields

docs_app = typer.Typer(help="Upload, list, inspect, and delete documents.")

_STATUS_GLYPH = {
    "processed": "✓",
    "indexed": "✓",
    "pending": "⟳",
    "processing": "⟳",
    "failed": "✗",
}


def _workspace(ctx: typer.Context) -> str | None:
    return ctx.obj.get("workspace") if ctx.obj else None


def _json_mode(ctx: typer.Context, json_flag: bool) -> bool:
    return json_flag or bool(ctx.obj and ctx.obj.get("json"))


def _unsupported_message(path: Path) -> str | None:
    from inh_contracts.file_types import (
        explicitly_unsupported_message_for_extension,
        get_spec_for_extension,
    )

    rejection = explicitly_unsupported_message_for_extension(path.name)
    if rejection:
        return rejection
    suffix = path.suffix.lower()
    if not suffix:
        return f"Unsupported file type {path.name!r}. Files need a registered extension."
    if get_spec_for_extension(suffix) is None:
        return f"Unsupported file type {suffix!r}."
    return None


@docs_app.command("list")
def docs_list(
    ctx: typer.Context,
    page: Annotated[int, typer.Option("--page")] = 1,
    page_size: Annotated[int, typer.Option("--page-size")] = 20,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List documents on the resolved stack."""

    response = call(
        "GET",
        "/v1/documents",
        workspace_id=_workspace(ctx),
        params={"page": page, "page_size": page_size},
    )
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    documents = payload.get("documents") or []
    rows = []
    for document in documents:
        status = document.get("status", "")
        rows.append(
            {
                "id": document.get("id", ""),
                "name": document.get("name", ""),
                "status": f"{_STATUS_GLYPH.get(status, '?')} {status}",
                "chunks": document.get("chunk_count", 0),
                "updated": document.get("updated_at", ""),
            }
        )
    render(rows, json_mode=False, table=TableSpec(("id", "name", "status", "chunks", "updated")))
    sys.stdout.write(f"{payload.get('total', len(rows))} document(s)\n")


@docs_app.command("show")
def docs_show(
    ctx: typer.Context,
    doc_id: str,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one document. Failed documents print the reason and a logs hint."""

    response = call("GET", f"/v1/documents/{doc_id}", workspace_id=_workspace(ctx))
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    render_fields(payload, json_mode=False)
    if payload.get("status") == "failed":
        reason = payload.get("error_message") or (payload.get("metadata") or {}).get(
            "error_message"
        )
        if reason:
            sys.stdout.write(f"{reason}\n")
        sys.stdout.write("Run: inherent logs inh-ingestion-svc\n")


@docs_app.command("upload")
def docs_upload(
    ctx: typer.Context,
    paths: list[Path],
) -> None:
    """Upload one or more files. Validates existence and type before sending any."""

    if not paths:
        raise ClientError("Pass at least one file path.")
    for path in paths:
        if not path.exists() or not path.is_file():
            raise ClientError(f"File not found: {path}")
        message = _unsupported_message(path)
        if message:
            raise ClientError(message)

    for path in paths:
        with path.open("rb") as handle:
            response = call(
                "POST",
                "/v1/documents",
                workspace_id=_workspace(ctx),
                files={"file": (path.name, handle, "application/octet-stream")},
            )
        payload = response.json()
        document_id = payload.get("document_id") or payload.get("id")
        sys.stdout.write(f"{path.name} → {document_id}\n")


@docs_app.command("delete")
def docs_delete(
    ctx: typer.Context,
    doc_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation.")] = False,
) -> None:
    """Delete a document and all derived data. Irreversible."""

    if not yes:
        if not sys.stdin.isatty():
            raise ClientError("Refusing to delete without --yes (no TTY).")
        if not typer.confirm(f"Delete document {doc_id}? This cannot be undone."):
            raise typer.Exit(1)
    call("DELETE", f"/v1/documents/{doc_id}", workspace_id=_workspace(ctx))
    sys.stdout.write(f"Deleted {doc_id}\n")


@docs_app.command("refresh")
def docs_refresh(ctx: typer.Context, doc_id: str) -> None:
    """Re-ingest an uploaded document."""

    response = call("POST", f"/v1/documents/{doc_id}/refresh", workspace_id=_workspace(ctx))
    payload: Any
    try:
        payload = response.json()
    except ValueError:
        payload = {"document_id": doc_id, "status": "refreshing"}
    sys.stdout.write(
        f"{payload.get('document_id', doc_id)} {payload.get('status', 'refreshing')}\n"
    )


@docs_app.command("lineage")
def docs_lineage(
    ctx: typer.Context,
    doc_id: str,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show provenance and freshness for a document."""

    response = call("GET", f"/v1/documents/{doc_id}/lineage", workspace_id=_workspace(ctx))
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    render(payload, json_mode=False, table=TableSpec(tuple(payload.keys())))


def chunks(
    ctx: typer.Context,
    doc_id: str,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List chunks for a document."""

    response = call("GET", f"/v1/chunks/{doc_id}", workspace_id=_workspace(ctx))
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    rows = payload if isinstance(payload, list) else [payload]
    render(
        rows,
        json_mode=False,
        table=TableSpec(("id", "chunk_index", "token_count", "content")),
    )
