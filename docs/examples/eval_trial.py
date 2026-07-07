#!/usr/bin/env python3
"""Interactive trial labeling for Inherent evals (stdlib only).

You play the agent's role: ask questions about your corpus, judge the results,
and your y/n answers are filed as REAL feedback through the same API an agent
would use (POST /v1/evals/feedback). Twenty questions in ~10 minutes gives you
enough labeled eval cases for a first mode-comparison run.

Usage:
    export API_BASE=http://localhost:18000
    export API_KEY=ink_dev_local_key_001
    export WORKSPACE_ID=ws_local_001
    python3 docs/examples/eval_trial.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:18000")
API_KEY = os.environ.get("API_KEY", "")
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "")


# This is an interactive human session: one transient failure (stale key 401,
# expired event 404, rate limit 429, backend 503) should log a single line and
# let the evaluator keep labeling — never crash mid-session with a traceback.
# So _post returns None on HTTP error (the loop skips that query) and _get
# returns {} (the final scorecard line degrades to "unavailable"). No retries.
def _post(path: str, body: dict) -> dict | None:
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": API_KEY,
            "X-Workspace-Id": WORKSPACE_ID,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"Request to {path} failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        return None


def _get(path: str) -> dict:
    req = urllib.request.Request(
        API_BASE + path,
        headers={"X-API-Key": API_KEY, "X-Workspace-Id": WORKSPACE_ID},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"Request to {path} failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        return {}


def main() -> int:
    if not API_KEY or not WORKSPACE_ID:
        print("Set API_KEY and WORKSPACE_ID env vars first (see docstring).")
        return 1
    labeled = 0
    print("Ask questions about your corpus. Empty question quits.\n")
    while True:
        query = input("question> ").strip()
        if not query:
            break
        search = _post("/v1/search", {"query": query, "limit": 5, "search_mode": "hybrid"})
        if search is None:
            print("  skipped (request failed)\n")
            continue
        event_id = search.get("event_id")
        results = search.get("results", [])
        if not results:
            print("  (no results)")
            if event_id:
                fb = _post("/v1/evals/feedback", {"event_id": event_id, "verdict": "not_relevant"})
                if fb is not None:
                    labeled += 1
            continue
        useful = []
        for i, r in enumerate(results, 1):
            snippet = r["content"][:160].replace("\n", " ")
            print(f"  [{i}] {r['document_name']} (score {r['score']:.2f}): {snippet}")
            if input("      relevant? [y/N] ").strip().lower() == "y":
                useful.append(r["chunk_id"])
        if event_id is None:
            print("  (capture disabled on this workspace — no event to label)")
            continue
        verdict = "answered" if useful else "not_relevant"
        fb = _post("/v1/evals/feedback",
                   {"event_id": event_id, "verdict": verdict, "useful_chunk_ids": useful})
        if fb is None:
            print("  skipped (feedback request failed)\n")
            continue
        labeled += 1
        print(f"  -> feedback filed ({verdict}); promoted={fb.get('promoted')}\n")

    print(f"\nLabeled {labeled} queries.")
    scorecard = _get("/v1/evals/scorecard")
    print("Scorecard:", scorecard.get("summary") or "unavailable")
    print("\nNext: trigger a mode-comparison run:")
    print(f'  curl -s -X POST "{API_BASE}/v1/evals/runs" -H "X-API-Key: $API_KEY" '
          f'-H "X-Workspace-Id: $WORKSPACE_ID" | jq .')
    return 0


if __name__ == "__main__":
    sys.exit(main())
