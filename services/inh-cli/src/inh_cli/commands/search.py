"""Search client command."""

from __future__ import annotations

import json
from enum import Enum
from typing import Annotated

import typer

from inh_cli.client import call
from inh_cli.output import TableSpec, render


class SearchMode(str, Enum):
    hybrid = "hybrid"
    keyword = "keyword"
    semantic = "semantic"


def _workspace(ctx: typer.Context) -> str | None:
    return ctx.obj.get("workspace") if ctx.obj else None


def _json_mode(ctx: typer.Context, json_flag: bool) -> bool:
    return json_flag or bool(ctx.obj and ctx.obj.get("json"))


def search(
    ctx: typer.Context,
    query: str,
    mode: Annotated[SearchMode, typer.Option("--mode")] = SearchMode.hybrid,
    limit: Annotated[int, typer.Option("--limit")] = 10,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search indexed documents. ``--json`` emits the API response verbatim."""

    response = call(
        "POST",
        "/v1/search",
        workspace_id=_workspace(ctx),
        json={"query": query, "limit": limit, "search_mode": mode.value},
    )
    payload = response.json()
    if _json_mode(ctx, json_flag):
        print(json.dumps(payload, separators=(",", ":")))
        return
    results = payload.get("results") or []
    rows = [
        {
            "score": item.get("score"),
            "document": item.get("document_name") or item.get("document_id"),
            "snippet": item.get("content", ""),
        }
        for item in results
    ]
    render(rows, json_mode=False, table=TableSpec(("score", "document", "snippet")))
