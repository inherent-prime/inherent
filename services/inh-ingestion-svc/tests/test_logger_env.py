"""Production worker logs must be JSON, not console (#40).

is_production keyed off SERVICE_MODE in ('standalone','api'), but the prod
default is SERVICE_MODE=worker, so worker pods emitted human ConsoleRenderer
output — breaking Loki/Promtail field queries.
"""

from __future__ import annotations

from src.utils.logger import _is_production_env


def test_worker_mode_is_production(monkeypatch):
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("SERVICE_MODE", "worker")
    assert _is_production_env() is True


def test_migrate_mode_is_production(monkeypatch):
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("SERVICE_MODE", "migrate")
    assert _is_production_env() is True


def test_unset_service_mode_is_development(monkeypatch):
    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.delenv("SERVICE_MODE", raising=False)
    assert _is_production_env() is False


def test_node_env_production_overrides(monkeypatch):
    monkeypatch.setenv("NODE_ENV", "production")
    monkeypatch.delenv("SERVICE_MODE", raising=False)
    assert _is_production_env() is True
