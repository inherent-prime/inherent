"""Operator-configured per-workspace opt-in for chunk reuse detection (inherent#394).

Same shape and rationale as ``workspace_packs.parse_workspace_vertical_packs``
and ``workspace_hybrid_alpha.parse_workspace_hybrid_alpha`` (read either
module's docstring first) -- reuse detection at ingest (near-duplicate chunks
from OTHER, already-ingested documents in the same workspace get their
``reuse_count`` bumped, feeding the public API's usage-based ranking boost)
is extra write-path work on every ingest, so it stays an explicit per-
workspace opt-in rather than an engine-wide default: ``WORKSPACE_REUSE_DETECTION``,
a comma-separated list of workspace ids, parsed identically by whichever
service reads it (currently just ``inh-ingestion-svc``, which alone runs the
ingest-time detection step).

Unset/empty (the default) means "no workspace opts in" -- ingestion behaves
exactly as it did before this feature existed. Any other malformed value is
a hard error raised at settings-construction time (service startup),
matching the other workspace knobs' "fail loudly, not later" contract.
"""

from __future__ import annotations


class WorkspaceReuseDetectionError(ValueError):
    """``WORKSPACE_REUSE_DETECTION`` is malformed; the message names the entry and why."""


def parse_workspace_reuse_detection(raw: str | None) -> set[str]:
    """Parse ``"ws_a,ws_b"`` into ``{"ws_a", "ws_b"}``.

    Rules (all violations raise ``WorkspaceReuseDetectionError``):

    - Entries are comma-separated; surrounding whitespace on each entry is
      trimmed. A blank entry (e.g. a trailing comma) is skipped, not an error.
    - Each non-blank entry must be a bare workspace id -- no ``=`` (unlike
      ``WORKSPACE_HYBRID_ALPHA``/``WORKSPACE_VERTICAL_PACKS``, this setting
      carries no value beyond membership).
    - A ``workspace_id`` listed twice is rejected outright -- a silent
      dedupe would hide a copy-paste mistake in a hand-edited pilot config
      instead of catching it at startup.

    ``None`` or an all-whitespace/empty string returns ``set()`` -- the
    deliberate "unset" case, not an error.
    """
    if not raw or not raw.strip():
        return set()

    workspace_ids: set[str] = set()
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue  # a stray/trailing comma, not a real entry

        if "=" in entry:
            raise WorkspaceReuseDetectionError(
                f"WORKSPACE_REUSE_DETECTION entry {entry!r} contains '=' "
                "(expected a bare workspace_id, not workspace_id=value)"
            )
        if entry in workspace_ids:
            raise WorkspaceReuseDetectionError(
                f"WORKSPACE_REUSE_DETECTION: workspace_id {entry!r} is listed twice"
            )
        workspace_ids.add(entry)

    return workspace_ids
