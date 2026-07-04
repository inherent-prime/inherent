"""A wildcard CORS origin must never be advertised with credentials (#36).

allow_origins=["*"] together with allow_credentials=True lets any site make
credentialed cross-origin calls (and is spec-invalid). In development
cors_origins_list returns ["*"], so credentials must be forced off.
"""

from __future__ import annotations

from src.config.settings import Settings

_DEV_DEFAULT_ORIGINS = [
    "https://app.inherent.systems",
    "https://inherent.systems",
    "https://dev-api.inherent.systems",
    "https://api.inherent.systems",
]


def test_wildcard_origin_forces_credentials_off():
    s = Settings.model_construct(
        environment="development",
        cors_origins=_DEV_DEFAULT_ORIGINS,
        cors_allow_credentials=True,
    )
    assert s.cors_origins_list == ["*"]  # dev wildcard
    assert s.cors_allow_credentials_effective is False


def test_explicit_origins_keep_credentials():
    s = Settings.model_construct(
        environment="production",
        cors_origins=["https://app.inherent.systems"],
        cors_allow_credentials=True,
    )
    assert "*" not in s.cors_origins_list
    assert s.cors_allow_credentials_effective is True
