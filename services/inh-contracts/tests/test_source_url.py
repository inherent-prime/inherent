"""source_url sanitization tests (inherent#391)."""

from inh_contracts.source_url import MAX_SOURCE_URL_LENGTH, sanitize_source_url


def test_none_stays_none():
    assert sanitize_source_url(None) is None


def test_empty_string_becomes_none():
    assert sanitize_source_url("") is None


def test_whitespace_only_becomes_none():
    assert sanitize_source_url("   ") is None


def test_valid_https_url_kept():
    url = "https://drive.google.com/file/d/abc123/view"
    assert sanitize_source_url(url) == url


def test_valid_http_url_kept():
    url = "http://example.com/doc"
    assert sanitize_source_url(url) == url


def test_surrounding_whitespace_trimmed():
    assert sanitize_source_url("  https://example.com/doc  ") == "https://example.com/doc"


def test_javascript_scheme_rejected():
    assert sanitize_source_url("javascript:alert(1)") is None


def test_data_scheme_rejected():
    assert sanitize_source_url("data:text/html,<script>alert(1)</script>") is None


def test_file_scheme_rejected():
    assert sanitize_source_url("file:///etc/passwd") is None


def test_relative_path_rejected():
    assert sanitize_source_url("/just/a/path") is None


def test_scheme_without_host_rejected():
    assert sanitize_source_url("https://") is None


def test_bare_string_without_scheme_rejected():
    assert sanitize_source_url("not a url") is None


def test_oversized_value_rejected():
    huge = "https://example.com/" + ("a" * MAX_SOURCE_URL_LENGTH)
    assert sanitize_source_url(huge) is None


def test_value_at_length_limit_kept():
    # Exactly at the limit must still be accepted -- only OVER the limit rejects.
    padding = "a" * (MAX_SOURCE_URL_LENGTH - len("https://example.com/"))
    url = "https://example.com/" + padding
    assert len(url) == MAX_SOURCE_URL_LENGTH
    assert sanitize_source_url(url) == url


def test_unparsable_url_becomes_none():
    # urlsplit raises ValueError on a malformed bracketed IPv6 host.
    assert sanitize_source_url("https://[::1/path") is None


def test_triple_slash_without_host_rejected():
    assert sanitize_source_url("https:///no-host") is None
