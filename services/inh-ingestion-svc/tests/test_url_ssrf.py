"""read_file_from_url must not be an SSRF vector (#34).

It fetched arbitrary URLs with follow_redirects=True and no validation, so an
attacker-influenced storage_url could reach cloud metadata (169.254.169.254) or
internal hosts. The URL is now validated (scheme + non-internal address).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.services.storage import _validate_fetch_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1/secret",
        "http://10.0.0.5/internal",
        "http://192.168.1.10/x",
        "http://localhost:8080/x",
        "http://[::1]/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/x",
    ],
)
def test_blocks_internal_and_bad_schemes(url):
    with pytest.raises(PermissionError):
        _validate_fetch_url(url)


def test_allows_public_https_host():
    # Patch resolution so we don't hit the network; a public IP is allowed.
    with patch(
        "src.services.storage.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],
    ):
        _validate_fetch_url("https://example.com/doc.pdf")  # must not raise
