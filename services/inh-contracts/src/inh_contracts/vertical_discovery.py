"""Vertical pack discovery (inherent#390 item 2).

Two ways core can find a pack, both OFF by default so an existing
deployment that never mounts/installs a pack behaves exactly as before:

1. **Directory discovery**: a directory (typically set via a
   ``VERTICAL_PACKS_DIR`` environment variable read by the calling service's
   own settings -- this module takes the path explicitly, it does not read
   env vars itself) containing one subdirectory per pack, each with its own
   ``vertical.yaml`` at the subdirectory root. See ``discover_packs``.
2. **Entry-point discovery**: a pack ships as an installed Python package
   that registers itself under the ``inherent.verticals`` entry-point group,
   value = the pack's root directory (a string path, or a zero-argument
   callable returning one -- e.g. ``importlib.resources.files(...)``). See
   ``discover_entry_point_packs``.

Both return ``{pack_name: Vertical}`` keyed by the manifest's own ``name``
field (not the directory name), so a badly-named directory can't silently
shadow the pack's declared identity. A pack that fails to load is skipped
with a warning-worthy `VerticalError` collected in the result rather than
raising -- one broken pack must never prevent the others (or a
feature-off/no-packs deployment) from working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

from inh_contracts.vertical_pack import Vertical, VerticalError, load_vertical

# The single entry-point group name every pack registers under (inherent#390).
ENTRY_POINT_GROUP = "inherent.verticals"


@dataclass
class DiscoveryResult:
    """Packs that loaded, plus any that were found but failed to load.

    `errors` is keyed by pack source (directory name or entry-point name) so
    a caller can log/report exactly which pack is broken without that
    failure hiding the packs that loaded fine.
    """

    packs: dict[str, Vertical] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def discover_packs(packs_dir: Path | str | None) -> DiscoveryResult:
    """Load every pack under `packs_dir` (one subdirectory per pack).

    `packs_dir` is `None` (the unset-by-default case) or a path that does not
    exist -> empty result, no error: directory discovery is simply off, which
    is the required legacy behaviour (no VERTICAL_PACKS_DIR = today exactly).
    """
    result = DiscoveryResult()
    if packs_dir is None:
        return result
    root = Path(packs_dir)
    if not root.is_dir():
        return result

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "vertical.yaml").is_file():
            continue  # not a pack directory (e.g. stray file/dir); skip quietly
        try:
            vertical = load_vertical(child)
        except VerticalError as exc:
            result.errors[child.name] = str(exc)
            continue
        result.packs[vertical.manifest.name] = vertical
    return result


def discover_entry_point_packs() -> DiscoveryResult:
    """Load every pack registered under the `inherent.verticals` entry-point group.

    No packages register under the group -> empty result (feature off by
    default; nothing to install for legacy deployments).
    """
    result = DiscoveryResult()
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - defensive; importlib.metadata is stable
        result.errors["<entry_points>"] = str(exc)
        return result

    for ep in eps:
        try:
            loaded = ep.load()
            root = Path(loaded() if callable(loaded) else loaded)
            vertical = load_vertical(root)
        except (
            VerticalError,
            Exception,
        ) as exc:  # noqa: BLE001 - one bad pack must not break the rest
            result.errors[ep.name] = str(exc)
            continue
        result.packs[vertical.manifest.name] = vertical
    return result


def discover_all_packs(packs_dir: Path | str | None) -> DiscoveryResult:
    """Combine directory + entry-point discovery into one lookup table.

    Directory packs win a name collision (mounted packs are the more
    explicit, locally-controlled source) -- entry-point results are applied
    first, then overwritten by directory results for any shared name.
    """
    combined = discover_entry_point_packs()
    directory = discover_packs(packs_dir)
    combined.packs.update(directory.packs)
    combined.errors.update(directory.errors)
    return combined
