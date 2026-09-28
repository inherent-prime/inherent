"""Retrieval eval harness: ``inherent eval run`` (inherent#391).

A reusable, CI-runnable harness for measuring retrieval quality against a
real deployment. It takes a dataset file (queries + the ids each one SHOULD
retrieve), runs each query through ``POST /v1/search`` on the resolved stack,
and reports hit@k and MRR -- so any vertical pack (or the core engine itself)
can pin a retrieval regression bar with one command:

    inherent eval run --dataset evals/support.yaml --k 5 --min-hit-rate 0.7

Design decisions (see inherent#391's issue for the alternatives considered):

- **Dataset format is YAML, not JSONL.** Both were on the table; YAML wins on
  simplicity for a hand-written, hand-reviewed eval set (comments, multi-line
  queries, and the ``filters`` mapping all read more naturally than escaped
  JSON on one line), and this repo's other hand-authored config file (a
  vertical pack's ``vertical.yaml``, #390) already sets that precedent. A
  second, JSONL-specific parser would only pay for itself if datasets were
  generated/streamed rather than curated by a human -- not the common case
  here.
- **``expected_ids`` matches against BOTH document ids and chunk ids.** The
  issue's fallback for "otherwise document ids + chunk ids" is exactly this:
  cheap (``SearchResult`` already carries both), needs no new server-side
  concept, and covers "the right chunk matched" and "the right document
  matched" with one field instead of inventing a
  ``document_id#section``-style compound key.
- **This lives in ``inh-cli``, not ``inh-public-api-svc``.** It calls the
  search API over HTTP (this module's only dependency beyond the standard
  library is ``inh_cli.client``), so it exercises the SAME contract a real
  agent/integration does and works against any deployment, not just an
  in-process test harness. The metrics themselves are NOT reimplemented here:
  ``hit_at_k``/``mrr`` are imported from ``inh_contracts.ranking_metrics``,
  the same functions ``inh-public-api-svc``'s own eval runner uses (see that
  module's docstring for why it lives in the shared contracts package).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from inh_contracts.ranking_metrics import hit_at_k, mrr

from inh_cli.client import ClientError, call

eval_app = typer.Typer(help="Run retrieval quality evals against a dataset file.")

# Fallback cutoff when neither --k nor the dataset file specifies one.
DEFAULT_K = 5


class DatasetError(ClientError):
    """The dataset file is missing, malformed, or has no queries to run."""


def _workspace(ctx: typer.Context) -> str | None:
    return ctx.obj.get("workspace") if ctx.obj else None


def _json_mode(ctx: typer.Context, json_flag: bool) -> bool:
    return json_flag or bool(ctx.obj and ctx.obj.get("json"))


def load_dataset(path: Path) -> dict[str, Any]:
    """Parse and validate a dataset file into ``{name, k, queries}``.

    Format (YAML)::

        name: support-eval-v1        # optional, defaults to the file's stem
        k: 5                         # optional, overridden by --k
        queries:
          - id: q1                   # optional, defaults to its 1-based position
            query: "how do I reset my password"
            expected_ids: ["doc_123", "chunk_456"]   # document ids AND/OR chunk ids
            filters: {category: "billing"}           # optional, passed through as-is

    Raises ``DatasetError`` (a ``ClickException``, so the CLI exits cleanly
    with a message) for anything that would otherwise surface as a raw
    traceback: a missing file, invalid YAML, a non-mapping document, or a
    query missing ``query``/``expected_ids``.
    """
    if not path.exists() or not path.is_file():
        raise DatasetError(f"Dataset file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise DatasetError(f"Invalid YAML in {path}: {error}") from error

    if not isinstance(raw, dict):
        raise DatasetError(f"{path}: top level must be a mapping (name/k/queries)")

    queries_raw = raw.get("queries")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise DatasetError(f"{path}: 'queries' must be a non-empty list")

    queries: list[dict[str, Any]] = []
    for i, entry in enumerate(queries_raw, start=1):
        if not isinstance(entry, dict):
            raise DatasetError(f"{path}: queries[{i}] must be a mapping")
        query_text = entry.get("query")
        if not isinstance(query_text, str) or not query_text.strip():
            raise DatasetError(f"{path}: queries[{i}] is missing a non-empty 'query'")
        expected_ids = entry.get("expected_ids")
        if not isinstance(expected_ids, list) or not expected_ids:
            raise DatasetError(f"{path}: queries[{i}] is missing a non-empty 'expected_ids'")
        queries.append(
            {
                "id": str(entry.get("id") or i),
                "query": query_text,
                "expected_ids": [str(x) for x in expected_ids],
                "filters": entry.get("filters"),
            }
        )

    return {
        "name": raw.get("name") or path.stem,
        "k": raw.get("k"),
        "queries": queries,
    }


def _ranked_ids(results: list[dict[str, Any]], expected: set[str]) -> list[str]:
    """One id per result, best-match-first, for hit@k/MRR to score.

    Each result contributes whichever of its document_id / chunk_id is in
    ``expected`` (document ids AND chunk ids both count, per this harness's
    matching policy — see module docstring). When NEITHER is expected, the
    chunk_id is used as a harmless, guaranteed-non-matching filler so the
    ranking's length/order is preserved without ever producing a false hit.
    """
    ranked: list[str] = []
    for result in results:
        document_id = str(result.get("document_id") or "")
        chunk_id = str(result.get("chunk_id") or "")
        if document_id in expected:
            ranked.append(document_id)
        elif chunk_id in expected:
            ranked.append(chunk_id)
        else:
            ranked.append(chunk_id)
    return ranked


def _run_one_query(*, workspace_id: str | None, query: dict[str, Any], k: int) -> dict[str, Any]:
    """POST /v1/search for one dataset query and score its ranked results."""
    body: dict[str, Any] = {"query": query["query"], "limit": k, "search_mode": "hybrid"}
    if query["filters"]:
        body["filters"] = query["filters"]

    response = call("POST", "/v1/search", workspace_id=workspace_id, json=body)
    results = response.json().get("results") or []

    expected = set(query["expected_ids"])
    ranked = _ranked_ids(results, expected)

    return {
        "id": query["id"],
        "query": query["query"],
        "hit": hit_at_k(ranked, expected, k),
        "mrr": mrr(ranked, expected),
        "returned": len(results),
    }


@eval_app.command("run")
def eval_run(
    ctx: typer.Context,
    dataset: Annotated[Path, typer.Option("--dataset", help="Path to the dataset YAML file.")],
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Workspace to search (overrides the global --workspace)."),
    ] = None,
    k: Annotated[
        int | None, typer.Option("--k", help="Top-k cutoff (default: dataset's own, else 5).")
    ] = None,
    min_hit_rate: Annotated[
        float | None,
        typer.Option(
            "--min-hit-rate",
            help="Exit non-zero if the mean hit@k falls below this threshold.",
        ),
    ] = None,
    json_flag: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run a retrieval eval dataset against POST /v1/search and report hit@k + MRR.

    Works against any deployment reachable from the resolved stack (the same
    connection ``inherent search`` uses) — it is a thin, dependency-light
    client over the real search API, so it can run in CI against a staging
    or Compose deployment as easily as a laptop.
    """
    data = load_dataset(dataset)
    effective_k = k or data["k"] or DEFAULT_K
    if effective_k <= 0:
        raise DatasetError("k must be a positive integer")

    effective_workspace = workspace or _workspace(ctx)

    per_query = [
        _run_one_query(workspace_id=effective_workspace, query=query, k=effective_k)
        for query in data["queries"]
    ]

    n = len(per_query)
    hit_rate = sum(row["hit"] for row in per_query) / n
    mean_mrr = sum(row["mrr"] for row in per_query) / n

    report = {
        "dataset": data["name"],
        "k": effective_k,
        "query_count": n,
        "hit_rate": round(hit_rate, 4),
        "mrr": round(mean_mrr, 4),
        "queries": per_query,
    }

    if _json_mode(ctx, json_flag):
        print(json.dumps(report, separators=(",", ":")))
    else:
        sys.stdout.write(
            f"{data['name']}: {n} queries, k={effective_k} -- "
            f"hit@{effective_k}={hit_rate:.2f} mrr={mean_mrr:.2f}\n"
        )
        for row in per_query:
            mark = "✓" if row["hit"] else "✗"
            sys.stdout.write(f"  {mark} {row['id']}: {row['query']!r} (mrr={row['mrr']:.2f})\n")

    if min_hit_rate is not None and hit_rate < min_hit_rate:
        sys.stdout.write(
            f"FAIL: hit_rate {hit_rate:.2f} is below --min-hit-rate {min_hit_rate:.2f}\n"
        )
        raise typer.Exit(1)
