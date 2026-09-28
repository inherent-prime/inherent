"""Operator-configured per-workspace hybrid search fusion weight (inherent#391).

Same shape and rationale as ``workspace_packs.parse_workspace_vertical_packs``
(read that module's docstring first) -- a hand-onboarded, keyword-heavy
workspace can favour BM25 over vector search without any per-request change,
via one operator setting: ``WORKSPACE_HYBRID_ALPHA``, a comma-separated list
of ``workspace_id=alpha`` pairs (``alpha`` in ``[0.0, 1.0]``), parsed
identically by whichever service reads it (currently just
``inh-public-api-svc``, which alone resolves hybrid search).

Config-based rather than a `vertical.yaml` field: it is a plain workspace ->
number binding with no dependency on a pack being bound at all, so this stays
the simplest fix and needs no vertical-pack-loading code path at all -- most
workspaces that want a keyword-heavy default have no pack bound to begin
with. A request's own explicit ``alpha`` always wins over this binding (see
``SearchService.search``); this only supplies the *default* a request
omitted.

Unset/empty (the default) means "no overrides" -- every workspace keeps the
global default alpha, unchanged from before this setting existed. Any other
malformed value is a hard error raised at settings-construction time (service
startup), matching ``WORKSPACE_VERTICAL_PACKS``'s "fail loudly, not later" contract.
"""

from __future__ import annotations


class WorkspaceHybridAlphaError(ValueError):
    """``WORKSPACE_HYBRID_ALPHA`` is malformed; the message names the entry and why."""


def parse_workspace_hybrid_alpha(raw: str | None) -> dict[str, float]:
    """Parse ``"ws_a=0.3,ws_b=0.5"`` into ``{workspace_id: alpha}``.

    Rules (all violations raise ``WorkspaceHybridAlphaError``):

    - Entries are comma-separated; surrounding whitespace on each entry is
      trimmed. A blank entry (e.g. a trailing comma) is skipped, not an error.
    - Each non-blank entry must contain exactly one ``=``, splitting into a
      non-empty ``workspace_id`` and an ``alpha`` (both trimmed).
    - ``alpha`` must parse as a float in ``[0.0, 1.0]`` (same range as
      ``SearchRequest.alpha``) -- out of range or non-numeric is rejected.
    - A ``workspace_id`` bound twice is rejected outright, even to the same
      value -- a silent "last one wins" would hide a contradictory pilot
      config typo instead of catching it at startup.

    ``None`` or an all-whitespace/empty string returns ``{}`` -- the
    deliberate "unset" case, not an error.
    """
    if not raw or not raw.strip():
        return {}

    bindings: dict[str, float] = {}
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue  # a stray/trailing comma, not a real entry

        if "=" not in entry:
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA entry {entry!r} is missing '=' "
                "(expected workspace_id=alpha)"
            )
        workspace_id, _, alpha_raw = entry.partition("=")
        workspace_id = workspace_id.strip()
        alpha_raw = alpha_raw.strip()

        if not workspace_id or not alpha_raw:
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA entry {entry!r} has an empty workspace_id or alpha"
            )
        if "=" in alpha_raw:
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA entry {entry!r} has more than one '=' "
                "(alpha values cannot contain '=')"
            )
        try:
            alpha = float(alpha_raw)
        except ValueError as exc:
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA entry {entry!r} has a non-numeric alpha {alpha_raw!r}"
            ) from exc
        if not (0.0 <= alpha <= 1.0):
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA entry {entry!r} has alpha {alpha!r} outside [0.0, 1.0]"
            )
        if workspace_id in bindings:
            raise WorkspaceHybridAlphaError(
                f"WORKSPACE_HYBRID_ALPHA: workspace_id {workspace_id!r} is bound twice "
                f"({bindings[workspace_id]!r} and {alpha!r})"
            )
        bindings[workspace_id] = alpha

    return bindings
