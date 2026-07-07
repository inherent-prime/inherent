#!/usr/bin/env python3
"""LangGraph agent that searches Inherent and auto-reports eval feedback.

Illustrates the evals flywheel from the consumer side: the agent's tool wrapper
calls POST /v1/search, uses the results, then files feedback on the event_id —
exactly what the report_feedback MCP tool description asks agents to do.

Requires (your venv, not this repo): pip install langgraph langchain-core
Env: API_BASE, API_KEY, WORKSPACE_ID, plus your LLM provider key.
"""

import json
import os
import urllib.request

from langchain_core.tools import tool

API_BASE = os.environ.get("API_BASE", "http://localhost:18000")
_HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": os.environ.get("API_KEY", ""),
    "X-Workspace-Id": os.environ.get("WORKSPACE_ID", ""),
}


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(API_BASE + path, data=json.dumps(body).encode(),
                                 headers=_HEADERS, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


@tool
def search_company_knowledge(query: str) -> str:
    """Search the company knowledge base. Returns evidence chunks with ids."""
    resp = _post("/v1/search", {"query": query, "limit": 5, "search_mode": "hybrid"})
    # Keep the event_id in the payload so the agent can report feedback on it.
    return json.dumps({
        "event_id": resp.get("event_id"),
        "results": [
            {"chunk_id": r["chunk_id"], "document": r["document_name"], "content": r["content"]}
            for r in resp.get("results", [])
        ],
    })


@tool
def report_search_feedback(event_id: str, verdict: str, useful_chunk_ids: list[str]) -> str:
    """ALWAYS call after using search results. verdict: answered|partial|not_relevant."""
    resp = _post("/v1/evals/feedback", {
        "event_id": event_id, "verdict": verdict, "useful_chunk_ids": useful_chunk_ids,
    })
    return json.dumps(resp)


if __name__ == "__main__":
    # Wire the two tools into your LangGraph agent of choice, e.g.:
    #   from langgraph.prebuilt import create_react_agent
    #   agent = create_react_agent(model, [search_company_knowledge, report_search_feedback])
    #   agent.invoke({"messages": [("user", "What is our refund policy?")]})
    # The system prompt should instruct: after answering from search results,
    # call report_search_feedback with the event_id and the chunk ids you used.
    print("Import the two tools into your LangGraph agent; see __main__ comments.")
