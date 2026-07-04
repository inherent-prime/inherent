"""Audit/trace fields must not be attacker-spoofable (#16).

client_ip / request_id feed every audit event. X-Forwarded-For / X-Real-IP must
only be trusted from a configured proxy, and a client-supplied request id must
be sanitized (no control chars / unbounded length) before it's logged or echoed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.middleware.request_context import _get_client_ip, _sanitize_request_id


def _req(peer: str | None, headers: dict):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer) if peer else None,
        headers=headers,
    )


def test_forwarded_header_ignored_from_untrusted_peer():
    req = _req("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
    with patch("src.middleware.request_context.settings") as s:
        s.trusted_proxies = []
        assert _get_client_ip(req) == "203.0.113.9"  # the real peer, not the spoof


def test_forwarded_header_honored_from_trusted_peer():
    req = _req("10.0.0.1", {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})
    with patch("src.middleware.request_context.settings") as s:
        s.trusted_proxies = ["10.0.0.1"]
        assert _get_client_ip(req) == "1.2.3.4"


def test_real_ip_ignored_from_untrusted_peer():
    req = _req("203.0.113.9", {"X-Real-IP": "9.9.9.9"})
    with patch("src.middleware.request_context.settings") as s:
        s.trusted_proxies = []
        assert _get_client_ip(req) == "203.0.113.9"


def test_sanitize_request_id_strips_control_chars_and_markup():
    assert _sanitize_request_id("abc\n\r<script>") == "abcscript"


def test_sanitize_request_id_caps_length():
    assert len(_sanitize_request_id("x" * 500)) == 128
