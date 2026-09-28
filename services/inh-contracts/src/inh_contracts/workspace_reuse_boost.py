"""Operator-configured per-workspace usage-based ranking boost weight (inherent#394).

Same shape and rationale as ``workspace_hybrid_alpha.parse_workspace_hybrid_alpha``
(read that module's docstring first): ``WORKSPACE_REUSE_BOOST``, a comma-
separated list of ``workspace_id=weight`` pairs (``weight`` in ``[0.0,
1.0]``), parsed identically by whichever service reads it (currently just
``inh-public-api-svc``, which alone resolves ranking). The weight feeds
``reuse_boost.apply_reuse_boost`` -- see that module for the boost formula
and its hard cap.

Unset/empty (the default) means "no workspace boosts" -- every workspace's
ranking stays byte-for-byte identical to before this feature existed. Any
other malformed value is a hard error raised at settings-construction time
(service startup), matching ``WORKSPACE_HYBRID_ALPHA``'s "fail loudly, not
later" contract.
"""

from __future__ import annotations


class WorkspaceReuseBoostError(ValueError):
    """``WORKSPACE_REUSE_BOOST`` is malformed; the message names the entry and why."""


def parse_workspace_reuse_boost(raw: str | None) -> dict[str, float]:
    """Parse ``"ws_a=0.1,ws_b=0.3"`` into ``{workspace_id: weight}``.

    Rules (all violations raise ``WorkspaceReuseBoostError``) mirror
    ``parse_workspace_hybrid_alpha`` exactly, with ``weight`` in place of
    ``alpha``:

    - Entries are comma-separated; surrounding whitespace on each entry is
      trimmed. A blank entry (e.g. a trailing comma) is skipped, not an error.
    - Each non-blank entry must contain exactly one ``=``, splitting into a
      non-empty ``workspace_id`` and a ``weight`` (both trimmed).
    - ``weight`` must parse as a float in ``[0.0, 1.0]`` -- out of range or
      non-numeric is rejected.
    - A ``workspace_id`` bound twice is rejected outright, even to the same
      value.

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
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST entry {entry!r} is missing '=' "
                "(expected workspace_id=weight)"
            )
        workspace_id, _, weight_raw = entry.partition("=")
        workspace_id = workspace_id.strip()
        weight_raw = weight_raw.strip()

        if not workspace_id or not weight_raw:
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST entry {entry!r} has an empty workspace_id or weight"
            )
        if "=" in weight_raw:
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST entry {entry!r} has more than one '=' "
                "(weight values cannot contain '=')"
            )
        try:
            weight = float(weight_raw)
        except ValueError as exc:
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST entry {entry!r} has a non-numeric weight {weight_raw!r}"
            ) from exc
        if not (0.0 <= weight <= 1.0):
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST entry {entry!r} has weight {weight!r} outside [0.0, 1.0]"
            )
        if workspace_id in bindings:
            raise WorkspaceReuseBoostError(
                f"WORKSPACE_REUSE_BOOST: workspace_id {workspace_id!r} is bound twice "
                f"({bindings[workspace_id]!r} and {weight!r})"
            )
        bindings[workspace_id] = weight

    return bindings
