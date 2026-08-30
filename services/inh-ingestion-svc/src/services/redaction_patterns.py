"""Credential-redaction pattern registry (#307).

Pattern-based detection for the shapes credentials most commonly take in
pasted conversation text: API-key prefixes, JWTs, PEM private-key blocks,
connection strings with embedded credentials, and a high-entropy-token
catch-all for anything the named patterns miss.

Honest limits (#307 issue body, "Honest limits"): this is BEST-EFFORT
pattern matching, not a guarantee. It will not catch every credential shape
-- a secret with no recognizable prefix and low apparent entropy (a short
passphrase, a credential embedded in unusual formatting) can pass through
unredacted. Any product built on top of this (a consent UI, a compliance
claim) must describe that real limitation to users rather than implying
completeness.

Self-hosters extend the built-in set via ``settings.redaction_patterns_extra``
(see src/config/settings.py's "Redaction (#307)" block) -- a list of raw
regex strings, each applied as its own detector and labelled ``custom`` in
the returned counts/markers.

Design: each detector is a small, independently-callable ``_redact_*``
function taking the running text and returning ``(new_text, match_count)``.
``redact_text`` drives them in a fixed order and wraps each call so a
detector that raises is reported with WHICH detector failed
(``RedactionDetectorError.detector``) without ever embedding the raw input
text in the exception itself -- the caller (src/temporal/activities/redact.py)
relies on that to keep its audit trail credential-free. Keeping every
detector a standalone function (rather than one monolithic regex pass) is
also what makes the activity's failure-path test able to monkeypatch exactly
one detector and assert the rest of the batch is unaffected.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence

# ---------------------------------------------------------------------------
# Redaction marker
# ---------------------------------------------------------------------------


# Replacement is a typed marker, never deletion (#307 requirement) -- the
# surrounding turn stays grammatically/contextually coherent for retrieval
# instead of leaving a hole where the secret was.
def _marker(redaction_type: str) -> str:
    return f"[redacted:{redaction_type}]"


class RedactionDetectorError(Exception):
    """One detector failed while scanning a turn.

    Carries ``detector`` (which named detector raised) and ``cause`` (the
    original exception) so the activity layer can write an audit record
    naming the failure without needing to inspect -- or accidentally log --
    any of the turn's raw text. The exception's own string form intentionally
    contains only the detector name and the cause's TYPE, never `str(cause)`
    or any input text, since a future detector's exception message is not
    something this module controls.
    """

    def __init__(self, detector: str, cause: Exception) -> None:
        super().__init__(f"redaction detector '{detector}' failed: {type(cause).__name__}")
        self.detector = detector
        self.cause = cause


# ---------------------------------------------------------------------------
# Built-in detectors
# ---------------------------------------------------------------------------

# Common API-key prefixes (#307: "common API-key prefixes"). Grouped under
# one "api_key" type -- self-hosters who need a provider this list misses use
# `redaction_patterns_extra` rather than waiting on a code change here.
_API_KEY_PATTERN = re.compile(
    r"""
    sk-proj-[A-Za-z0-9_-]{20,}                 # OpenAI project key
    | sk-ant-[A-Za-z0-9_-]{20,}                # Anthropic key
    | sk-[A-Za-z0-9]{20,}                      # OpenAI legacy / Stripe-shaped sk-*
    | sk_live_[A-Za-z0-9]{20,}                 # Stripe live secret key
    | sk_test_[A-Za-z0-9]{20,}                 # Stripe test secret key
    | AKIA[0-9A-Z]{16}                         # AWS access key ID
    | ghp_[A-Za-z0-9]{36}                      # GitHub personal access token
    | gh[oisur]_[A-Za-z0-9]{36}                # GitHub OAuth/app/server/refresh tokens
    | github_pat_[A-Za-z0-9_]{22,}             # GitHub fine-grained PAT
    | glpat-[A-Za-z0-9_-]{20}                  # GitLab personal access token
    | xox[baprs]-[A-Za-z0-9-]{10,}             # Slack tokens
    | AIza[0-9A-Za-z_-]{35}                    # Google API key
    """,
    re.VERBOSE,
)


def _redact_api_keys(text: str) -> tuple[str, int]:
    """Redact recognizable API-key-shaped tokens by their literal prefix."""
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _marker("api_key")

    return _API_KEY_PATTERN.sub(_sub, text), count


# JWT: three base64url segments joined by '.' (header.payload.signature).
# Real JWTs' base64url-encoded header always starts "eyJ" (the base64 of
# '{"'), which keeps this from firing on arbitrary dotted text.
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")


def _redact_jwts(text: str) -> tuple[str, int]:
    """Redact JSON Web Tokens."""
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _marker("jwt")

    return _JWT_PATTERN.sub(_sub, text), count


# PEM-encoded private key blocks (RSA/EC/DSA/OpenSSH/encrypted/plain
# PKCS8 "PRIVATE KEY"). Matches the full BEGIN..END block, non-greedy, so a
# turn pasting more than one key still redacts each one separately.
_PEM_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"[\s\S]+?"
    r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)


def _redact_private_keys(text: str) -> tuple[str, int]:
    """Redact PEM private-key blocks."""
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _marker("private_key")

    return _PEM_PATTERN.sub(_sub, text), count


# Connection strings with embedded username:password credentials --
# scheme://user:pass@host[...]. Scoped to schemes that legitimately carry
# inline credentials (databases, message brokers) rather than every URL, so
# an ordinary https:// link with no credentials is left untouched.
_CONNECTION_STRING_PATTERN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps|mssql)"
    r"://[^\s:/@'\"]+:[^\s@'\"]+@[^\s'\"]+"
)


def _redact_connection_strings(text: str) -> tuple[str, int]:
    """Redact connection strings that carry inline user:pass credentials."""
    count = 0

    def _sub(_m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _marker("connection_string")

    return _CONNECTION_STRING_PATTERN.sub(_sub, text), count


# High-entropy catch-all (#307: "high-entropy tokens"). Runs LAST, after all
# named detectors above have already replaced their more specific matches
# with markers -- this both avoids double-redacting the same credential and
# means the entropy scanner only ever sees whatever those detectors left
# behind. Candidates are contiguous base64/hex/URL-safe-alphabet runs at
# least `_MIN_ENTROPY_TOKEN_LENGTH` long; each is kept only if its Shannon
# entropy clears `_MIN_ENTROPY_BITS_PER_CHAR`, which is what keeps this from
# firing on ordinary long English words or identifiers (low per-character
# entropy) while still catching an unrecognized provider's opaque secret
# token.
_MIN_ENTROPY_TOKEN_LENGTH = 24
_ENTROPY_CANDIDATE_PATTERN = re.compile(rf"[A-Za-z0-9+/_=-]{{{_MIN_ENTROPY_TOKEN_LENGTH},}}")
# 3.0 bits/char comfortably admits hex-encoded secrets (a 32-hex-char digest
# typically scores ~3.0-4.0, since hex's 16-symbol alphabet caps entropy at
# log2(16)=4) as well as base64/mixed-case random tokens (typically
# ~4.5-5.5). Documented false-positive class (honest limits, module
# docstring): unbroken camelCase/identifier-shaped prose can occasionally
# clear this bar too -- accepted, since ordinary written English almost
# never produces a run this long with no space/punctuation break at all.
_MIN_ENTROPY_BITS_PER_CHAR = 3.0


def _shannon_entropy(token: str) -> float:
    """Shannon entropy of `token`, in bits per character.

    Standard formula: for the observed per-character symbol distribution p,
    entropy = -sum(p * log2(p)). A token drawn from a small alphabet (plain
    English words, "aaaaaaaa...") scores low; a token that looks like random
    base64/hex (real API secrets, almost always generated this way) scores
    high, typically >4 bits/char for base64.
    """
    if not token:
        return 0.0
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(token)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _redact_high_entropy_tokens(text: str) -> tuple[str, int]:
    """Redact long, high-entropy tokens the named detectors above missed."""
    count = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group(0)
        if _shannon_entropy(token) < _MIN_ENTROPY_BITS_PER_CHAR:
            return token  # looks like ordinary text, not a secret -- leave it
        count += 1
        return _marker("high_entropy_token")

    return _ENTROPY_CANDIDATE_PATTERN.sub(_sub, text), count


def _redact_custom_patterns(text: str, patterns: Sequence[str]) -> tuple[str, int]:
    """Apply self-hosted extra patterns (`settings.redaction_patterns_extra`).

    Each raw string is compiled and applied independently; an invalid regex
    raises `re.error`, which `redact_text` below wraps into a
    `RedactionDetectorError(detector="custom", ...)` -- a self-hoster's typo
    in one extra pattern surfaces as a per-turn redaction failure (dropped +
    audited, per #307's non-retryable contract) rather than crashing the
    whole activity or silently skipping validation.
    """
    count = 0
    for raw in patterns:
        compiled = re.compile(raw)

        def _sub(_m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return _marker("custom")

        text = compiled.sub(_sub, text)
    return text, count


# ---------------------------------------------------------------------------
# Registry + entry point
# ---------------------------------------------------------------------------


_DetectorFn = Callable[[str], tuple[str, int]]


def _builtin_detectors() -> tuple[tuple[str, _DetectorFn], ...]:
    """(name, function) pairs for every built-in detector, in FIXED
    application order: more specific patterns before the entropy catch-all
    (see `_redact_high_entropy_tokens`'s docstring for why order matters).

    Deliberately a FUNCTION, not a module-level constant: each call performs
    a fresh global-name lookup of `_redact_api_keys` etc., so a test that
    monkeypatches one of those module-level names (`monkeypatch.setattr
    (redaction_patterns, "_redact_high_entropy_tokens", ...)`, per #307's
    failure-path test requirement) is actually picked up on the NEXT call to
    `redact_text`. A module-level tuple built once at import time would
    instead capture the ORIGINAL function objects permanently, silently
    defeating any such monkeypatch.
    """
    return (
        ("api_key", _redact_api_keys),
        ("jwt", _redact_jwts),
        ("private_key", _redact_private_keys),
        ("connection_string", _redact_connection_strings),
        ("high_entropy_token", _redact_high_entropy_tokens),
    )


def redact_text(text: str, extra_patterns: Sequence[str] = ()) -> tuple[str, dict[str, int]]:
    """Run every detector over `text` in order, returning (redacted, counts).

    `counts` maps redaction_type -> number of matches replaced, omitting any
    type that matched zero times -- used both for the per-turn
    `RedactedTurn.redaction_counts` and for the activity's batch-level metric
    emission (#307: "Emit a metric for redactions by type").

    Raises:
        RedactionDetectorError: a detector raised while scanning `text`. The
            caller (redact.py's `redact_turns` activity) is responsible for
            catching this PER TURN, dropping only that turn, and writing an
            audit record -- this function itself does not know about turns
            or audit, keeping it independently unit-testable.
    """
    counts: dict[str, int] = {}
    for name, fn in _builtin_detectors():
        try:
            text, n = fn(text)
        except RedactionDetectorError:
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: ANY
            # detector failure must convert to a named, catchable error
            # rather than an opaque exception the activity can't attribute.
            raise RedactionDetectorError(name, exc) from exc
        if n:
            counts[name] = n

    if extra_patterns:
        try:
            text, n = _redact_custom_patterns(text, extra_patterns)
        except Exception as exc:  # noqa: BLE001 -- same reasoning as above
            raise RedactionDetectorError("custom", exc) from exc
        if n:
            counts["custom"] = n

    return text, counts
