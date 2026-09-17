"""RAG-poisoning / prompt-injection risk heuristics for public-api writes (#44 / #133).

Duplicates the live ingestion scorer (``inh-ingestion-svc/src/services/quality.py``
``compute_content_risk``) so REST/MCP chunk Create tags caller-supplied text
with the same signal bulk ingestion uses. Do not import inh-ingestion-svc
from here. Drift is pinned by ``tests/chunk_formula_vectors.py``.
"""

from __future__ import annotations

import re

# Each pattern carries a reason code and a per-match weight. The weights are
# summed across distinct matched patterns and bucketed into a risk level. We
# require somewhat strong phrasing (e.g. "ignore (all )?previous instructions")
# so benign docs that merely mention "instructions" or "override" in normal
# prose do not get flagged high (false-positive guard).
_RISK_PATTERNS: list[tuple[str, str, int]] = [
    # (reason_code, regex, weight)
    (
        "ignore_previous_instructions",
        r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding|earlier)\s+"
        r"(?:instructions?|prompts?|messages?|directions?|context)",
        3,
    ),
    (
        "disregard_instructions",
        r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding|earlier|"
        r"all)\s+(?:instructions?|prompts?|messages?|rules?|directions?)",
        3,
    ),
    (
        "forget_instructions",
        r"forget\s+(?:all\s+|everything\s+)?(?:previous|prior|above|your)?\s*"
        r"(?:instructions?|prompts?|rules?|context|what you were told)",
        3,
    ),
    (
        "override_instructions",
        r"override\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|system\s+)?"
        r"(?:instructions?|prompts?|rules?|settings?|guard)",
        2,
    ),
    (
        "system_prompt_reference",
        r"(?:system\s+prompt|system\s+message|reveal\s+(?:your\s+)?(?:system\s+)?prompt|"
        r"print\s+(?:your\s+)?(?:system\s+)?prompt|what\s+is\s+your\s+system\s+prompt)",
        2,
    ),
    (
        "role_reassignment",
        r"you\s+are\s+now\s+(?:a\s+|an\s+|the\s+)?\w+",
        2,
    ),
    (
        "new_instructions",
        r"(?:new|updated|revised|the\s+following)\s+instructions?\s*[:\-]",
        2,
    ),
    (
        "act_as_jailbreak",
        r"(?:act\s+as|pretend\s+to\s+be|behave\s+as)\s+(?:a\s+|an\s+|if\s+|though\s+)?"
        r"(?:dan|developer\s+mode|jailbroken|unrestricted|an?\s+ai\s+(?:that|without))",
        3,
    ),
    (
        "do_anything_now",
        r"\bdo\s+anything\s+now\b|\bDAN\s+mode\b",
        3,
    ),
    (
        "instruction_override_marker",
        r"###\s*(?:system|instruction|admin|override)|<\s*/?\s*(?:system|instructions?)\s*>",
        2,
    ),
    (
        "exfiltration_request",
        r"(?:reveal|expose|leak|print|repeat)\s+(?:your\s+|the\s+|all\s+)?"
        r"(?:secret|api\s+key|credentials?|password|hidden\s+instructions?)",
        2,
    ),
]

# Compiled once at import. We match case-insensitively across the whole text.
_COMPILED_RISK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (code, re.compile(pattern, re.IGNORECASE)) for code, pattern, _ in _RISK_PATTERNS
]
_RISK_WEIGHTS: dict[str, int] = {code: weight for code, _, weight in _RISK_PATTERNS}

# Score → level buckets. Tuned so a single weak match stays "low", a single
# strong match (weight 3) is "medium", and multiple/strong matches escalate to
# "high".
_RISK_LOW_THRESHOLD = 1
_RISK_MEDIUM_THRESHOLD = 3
_RISK_HIGH_THRESHOLD = 5


def compute_content_risk(text: str) -> tuple[str, list[str]]:
    """Heuristically score text for prompt-injection / RAG-poisoning risk.

    This is a NON-BLOCKING signal only (#44). It never rejects content; it
    returns a coarse risk level plus the reason codes that matched so search
    results and audit logs can surface and weigh suspicious evidence.

    Args:
        text: The chunk (or document) text to score.

    Returns:
        A tuple ``(risk_level, reasons)`` where ``risk_level`` is one of
        ``"none" | "low" | "medium" | "high"`` and ``reasons`` is the sorted,
        de-duplicated list of matched reason codes (empty when level is "none").
    """
    if not text or not text.strip():
        return "none", []

    matched: set[str] = set()
    for code, pattern in _COMPILED_RISK_PATTERNS:
        if pattern.search(text):
            matched.add(code)

    if not matched:
        return "none", []

    score = sum(_RISK_WEIGHTS[code] for code in matched)
    if score >= _RISK_HIGH_THRESHOLD:
        level = "high"
    elif score >= _RISK_MEDIUM_THRESHOLD:
        level = "medium"
    elif score >= _RISK_LOW_THRESHOLD:
        level = "low"
    else:  # pragma: no cover - score is always >= 1 when matched is non-empty
        level = "none"

    return level, sorted(matched)
