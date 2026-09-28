"""Sanitize a connector-supplied ``source_url`` (inherent#391).

``source_url`` is the link back to a document's ORIGINAL file in whatever
system uploaded it (e.g. a Drive ``webViewLink``) -- distinct from
``storage_uri``/``storage_url``, which point at the copy this engine stores
internally. It travels from the upload boundary (REST + the
``document.uploaded`` MQ event) all the way to search results and citations,
so a hostile or malformed value must never propagate as-is.

Policy (shared by every entry point so they can't disagree): a value that
does not look like a safe, absolute http(s) link is dropped to ``None``
rather than rejected -- this is a display/citation field, not something the
rest of ingestion depends on, so a bad value degrading to "no link" is far
better than failing an otherwise-valid upload over it (see callers).
"""

from __future__ import annotations

from urllib.parse import urlsplit

# A citation link has no business being longer than this; also caps the
# amount of attacker-controlled text a bad value could otherwise carry all
# the way to a stored column / JSON response.
MAX_SOURCE_URL_LENGTH = 2000

# Only these two schemes are ever safe to render as a clickable link back to
# a source system. Everything else -- "javascript:", "data:", "file:", a
# bare scheme-less path, etc. -- is exactly the class of value this exists
# to keep out of a field clients may render as an href.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def sanitize_source_url(value: str | None) -> str | None:
    """Return ``value`` unchanged if it is a safe absolute http(s) URL, else ``None``.

    Never raises: a missing or malformed value degrades to ``None`` (see
    module docstring) so one bad connector-supplied link can't fail an
    otherwise-valid upload.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_SOURCE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        # urlsplit raises on some pathological inputs (e.g. an unparsable
        # IPv6-looking host) -- treat exactly like any other bad value.
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        return None
    return candidate
