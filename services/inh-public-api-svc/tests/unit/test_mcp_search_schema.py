"""MCP search tool must not advertise context params it doesn't honor (#29).

_run_search never expands context, so include_context/context_window were a
silent no-op on the MCP surface. MCP exposes a dedicated get_document_context
tool for that, so the params are removed from the search schema.
"""

from __future__ import annotations

from src.mcp_server.server import _SEARCH_INPUT_SCHEMA


def test_search_schema_omits_noop_context_params():
    props = _SEARCH_INPUT_SCHEMA["properties"]
    assert "include_context" not in props
    assert "context_window" not in props
