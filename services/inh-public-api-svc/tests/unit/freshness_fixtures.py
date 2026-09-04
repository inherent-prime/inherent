"""Wall-clock-relative `ingested_at` values for freshness tests (#351).

`SearchService._compute_is_stale` is the one piece of the lineage/search
projection that reads the clock:

    ingested_at < datetime.now(UTC) - timedelta(days=freshness_max_age_days)

So a fixture written as a literal date does not describe "a fresh document" --
it describes "a document ingested on that date", which *becomes* stale the
moment the window elapses. That is not hypothetical: both lineage tests pinned
`ingested_at` to `2026-06-01T00:00:00Z` and asserted `is_stale is False`, and
they began failing on 2026-08-30 -- exactly 90 days later, the configured
default -- on every branch at once, with `main` still showing green only
because it had not been re-run since.

Express age relative to now AND relative to the configured window, so the
fixtures stay meaningful whatever the date is and whatever
`freshness_max_age_days` is set to.
"""

from __future__ import annotations

import datetime as _dt

from src.config import settings


def ingested_days_ago(days: int) -> str:
    """An RFC-3339 `ingested_at` string, `days` before now."""
    moment = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)
    return moment.isoformat().replace("+00:00", "Z")


def as_response_iso(ingested_at: str) -> str:
    """The form `build_lineage` echoes back for `ingested_at`.

    The builder parses the string and re-serialises with `.isoformat()`, which
    renders UTC as `+00:00` rather than the `Z` the fixtures are written with.
    """
    return ingested_at.replace("Z", "+00:00")


# One day inside the window, and one day past it. Both sides are pinned so the
# boundary is asserted deliberately rather than by accident of the calendar.
FRESH_INGESTED_AT = ingested_days_ago(1)
STALE_INGESTED_AT = ingested_days_ago(settings.freshness_max_age_days + 1)
