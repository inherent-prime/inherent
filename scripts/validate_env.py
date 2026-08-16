#!/usr/bin/env python3
"""Validate local development environment against both service settings.

Loads `.env` (if present) from the repository root, then attempts to
instantiate the Settings classes from `inh-ingestion-svc` and
`inh-public-api-svc`. Reports missing required values and a small set of
cross-service consistency checks.

Run from anywhere; the script resolves the repository root from its own
location:

    uv --project services/inh-ingestion-svc run python scripts/validate_env.py

Exits non-zero when any blocking issue is found.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_SETTINGS = (
    REPO_ROOT / "services" / "inh-ingestion-svc" / "src" / "config" / "settings.py"
)
PUBLIC_API_SETTINGS = (
    REPO_ROOT / "services" / "inh-public-api-svc" / "src" / "config" / "settings.py"
)

CONTAINER_HOSTS = {
    "postgres",
    "mongodb",
    "weaviate",
    "valkey",
    "s3rver",
    "temporal",
    "text-embeddings-inference",
}

# Values ingestion-svc accepts. The current modes are `worker` and `standalone`;
# the others are legacy aliases that `inh-ingestion-svc/src/main.py` still maps
# to `worker` at startup. Keep this in sync with `_MODE_ALIASES` in that module.
ING_SERVICE_MODE_VALID = {
    "worker",
    "standalone",
    "pubsub",
    "temporal_worker",
    "temporal_trigger",
    "temporal_all",
}
PUB_SERVICE_MODE_VALID = {"api", "mcp", "both"}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _load_dotenv(path: Path) -> None:
    """Load repository-root `.env` into os.environ.

    Uses python-dotenv when available (it correctly handles quoted values
    containing `#`, line continuations, export-prefixed lines, etc., which
    is the same parser pydantic-settings uses inside the services). Falls
    back to a minimal parser if python-dotenv is not importable, since the
    script has no install-time guarantee outside a service venv.
    """
    if not path.exists():
        return

    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]
    except ImportError:
        _load_dotenv_fallback(path)
        return

    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        if key and key not in os.environ:
            os.environ[key] = value


def _load_dotenv_fallback(path: Path) -> None:
    """Minimal best-effort parser used only when python-dotenv is unavailable.

    Honors single- and double-quoted values so that `#` inside a quoted
    string is preserved. Does not handle escapes or line continuations.
    """
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def _purge_src_modules() -> dict[str, Any]:
    """Pop and return every cached ``src`` / ``src.*`` module.

    Both services own a top-level ``src`` package, so whichever one is
    imported first would otherwise poison the second's absolute imports
    through ``sys.modules``.
    """
    stale = {k: v for k, v in sys.modules.items() if k == "src" or k.startswith("src.")}
    for key in stale:
        del sys.modules[key]
    return stale


def _import_settings_module(name: str, path: Path) -> Any:
    """Import the service settings module with cwd pinned to REPO_ROOT.

    Some service settings modules (notably inh-public-api-svc) instantiate
    `settings = get_settings()` at import time. Pydantic-settings resolves
    its configured `env_file=".env"` against the *current working directory*
    at that moment. Pinning cwd to REPO_ROOT ensures any module-level
    Settings() call reads the same .env we already loaded, instead of a
    stray .env in the caller's cwd.

    The service root also goes on `sys.path` for the duration of the import.
    Loading by file location does not make the module's own package
    importable, so a settings module doing an absolute `from src.config...`
    import (public-api has since #202, which single-sourced the config
    defaults) fails with ModuleNotFoundError under this validator's project
    venv. `src` is purged from `sys.modules` around each load so the two
    services' identically-named packages cannot shadow each other.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    service_root = path.parents[2]  # <service>/src/config/settings.py -> <service>
    saved_cwd = os.getcwd()
    saved_path = list(sys.path)
    outer_src = _purge_src_modules()
    try:
        sys.path.insert(0, str(service_root))
        os.chdir(REPO_ROOT)
        spec.loader.exec_module(module)
    finally:
        os.chdir(saved_cwd)
        sys.path[:] = saved_path
        _purge_src_modules()
        sys.modules.update(outer_src)
    return module


def _load_settings(
    name: str,
    path: Path,
    report: Report,
    env_overrides: dict[str, str | None] | None = None,
) -> Any | None:
    saved = os.environ.copy()
    try:
        for k, v in (env_overrides or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            module = _import_settings_module(name, path)
        except ModuleNotFoundError as exc:
            report.error(
                f"{name}: cannot import (missing dependency '{exc.name}'). "
                "Install service deps first: cd services/<svc> && uv sync"
            )
            return None
        except Exception as exc:  # noqa: BLE001
            report.error(f"{name}: import failed: {exc}")
            return None

        try:
            # `_env_file=None` prevents pydantic-settings from layering a
            # cwd-relative `.env` on top of the REPO_ROOT/.env we already
            # loaded into os.environ.
            return module.Settings(_env_file=None)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            report.error(f"{name}: settings load failed:\n    {exc}")
            return None
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _check_host_reachability(name: str, url: str, report: Report) -> None:
    """Warn when URL points at a Compose-internal hostname while running on host."""
    if not url:
        return
    parsed = urlparse(url if "://" in url else f"//{url}", scheme="")
    host = (parsed.hostname or "").lower()
    if host in CONTAINER_HOSTS:
        report.warn(
            f"{name}={url} uses Compose-internal hostname '{host}'. "
            "Only valid inside the docker-compose network. "
            "Use the published host port (see .env.example) when running outside Compose."
        )


def _check_consistency(ing: Any, pub: Any, report: Report) -> None:
    if ing is None or pub is None:
        return

    if ing.database_url and pub.database_url and ing.database_url != pub.database_url:
        report.warn(
            "DATABASE_URL differs between ingestion-svc and public-api-svc; both should "
            "point at the same Postgres instance for local dev."
        )

    if ing.mongodb_uri and pub.mongodb_uri:
        # Parse host with urlparse so we ignore auth, path, and query — a
        # split("/")[2:3] approach would treat `mongodb://user:pass@host` and
        # `mongodb://host` as different even when the underlying host matches.
        ing_host = urlparse(ing.mongodb_uri).hostname
        pub_host = urlparse(pub.mongodb_uri).hostname
        if ing_host and pub_host and ing_host != pub_host:
            report.warn(
                f"MONGODB_URI host differs: ingestion={ing_host}, public-api={pub_host}."
            )

    pub_weaviate = pub.effective_weaviate_url
    if ing.weaviate_url and pub_weaviate and ing.weaviate_url.rstrip("/") != pub_weaviate.rstrip("/"):
        report.warn(
            f"WEAVIATE_URL differs: ingestion={ing.weaviate_url}, public-api={pub_weaviate}."
        )

    # (#132 item 7) Promoted from warn to error: this is the exact defect #132
    # exists to prevent -- ingestion and public-api targeting different S3
    # regions means uploads land in one bucket/region while reads target
    # another (PermanentRedirect / IllegalLocationConstraintException at
    # request time, not at startup). Same severity class as the EMBEDDING_DIM
    # check below: a resolved cross-service physical-target mismatch, not a
    # cosmetic naming difference. public-api now falls back to AWS_REGION via
    # AliasChoices (#132 blocker 1), so this only fires when an operator sets
    # AWS_S3_REGION to deliberately diverge from AWS_REGION -- which is a real
    # disagreement, not the accidental-default case the alias already closed.
    if ing.s3_region and pub.aws_s3_region and ing.s3_region != pub.aws_s3_region:
        report.error(
            f"AWS_REGION ({ing.s3_region}) and AWS_S3_REGION ({pub.aws_s3_region}) disagree. "
            "Set both to the same value (or unset AWS_S3_REGION so public-api falls back to "
            "AWS_REGION)."
        )

    # Ingestion reads STORAGE_BUCKET; public-api reads AWS_S3_BUCKET. They are
    # two different env vars pointing at the same physical bucket — drift means
    # ingestion writes objects public-api will never find.
    if ing.storage_bucket and pub.aws_s3_bucket and ing.storage_bucket != pub.aws_s3_bucket:
        report.warn(
            f"STORAGE_BUCKET ({ing.storage_bucket}) and AWS_S3_BUCKET ({pub.aws_s3_bucket}) "
            "disagree. Ingestion would write to one bucket while public-api reads from another."
        )

    if ing.mq_upload_topic != pub.mq_topic_document_uploaded:
        report.warn(
            f"Upload topic mismatch: ingestion MQ_UPLOAD_TOPIC={ing.mq_upload_topic}, "
            f"public-api MQ_TOPIC_DOCUMENT_UPLOADED={pub.mq_topic_document_uploaded}."
        )

    if ing.embedding_dim != pub.embedding_dim:
        report.error(
            f"EMBEDDING_DIM mismatch: ingestion={ing.embedding_dim}, public-api={pub.embedding_dim}. "
            "Vectors written by ingestion will be unreadable by public-api search."
        )


def _resolve_public_api_overrides(report: Report) -> dict[str, str | None]:
    """Decide which env values to override before loading public-api Settings.

    Only the documented `SERVICE_MODE` collision warrants an override:
    a value that is valid for ingestion-svc but not for public-api-svc.
    Any other value (including invalid garbage like 'not-a-mode') is left
    alone so public-api's own Literal validation surfaces the real error
    instead of being masked by a blanket override.
    """
    overrides: dict[str, str | None] = {}
    sm = os.environ.get("SERVICE_MODE")
    if sm and sm in ING_SERVICE_MODE_VALID and sm not in PUB_SERVICE_MODE_VALID:
        overrides["SERVICE_MODE"] = "both"
        report.warn(
            f"SERVICE_MODE='{sm}' is valid for ingestion-svc but not public-api-svc "
            f"(expects one of {sorted(PUB_SERVICE_MODE_VALID)}). The two services share "
            "this env var name — in Compose they get separate values via per-service "
            "`environment:` blocks. Validator will override SERVICE_MODE='both' when "
            "loading public-api-svc."
        )
    return overrides


def main() -> int:
    report = Report()

    _load_dotenv(REPO_ROOT / ".env")

    ing = _load_settings("inh_ingestion_settings", INGESTION_SETTINGS, report)
    pub = _load_settings(
        "inh_public_api_settings",
        PUBLIC_API_SETTINGS,
        report,
        env_overrides=_resolve_public_api_overrides(report),
    )

    if ing is not None:
        _check_host_reachability("ingestion DATABASE_URL", ing.database_url, report)
        _check_host_reachability("ingestion WEAVIATE_URL", ing.weaviate_url, report)
        _check_host_reachability("ingestion REDIS_URL", ing.redis_url, report)
        _check_host_reachability("ingestion MONGODB_URI", ing.mongodb_uri, report)
        _check_host_reachability(
            "ingestion EMBEDDING_SERVICE_URL", ing.embedding_service_url, report
        )
        # TEMPORAL_HOST is a bare host:port (no scheme); _check_host_reachability
        # handles that by prepending `//` when no scheme is present.
        if ing.temporal_enabled:
            _check_host_reachability("ingestion TEMPORAL_HOST", ing.temporal_host, report)
        if ing.s3_endpoint:
            _check_host_reachability("ingestion AWS_S3_ENDPOINT", ing.s3_endpoint, report)

    if pub is not None:
        _check_host_reachability(
            "public-api WEAVIATE_URL (effective)", pub.effective_weaviate_url, report
        )
        _check_host_reachability("public-api MQ_REDIS_URL", pub.mq_redis_url, report)
        _check_host_reachability("public-api MONGODB_URI", pub.mongodb_uri, report)
        _check_host_reachability(
            "public-api EMBEDDING_SERVICE_URL", pub.embedding_service_url, report
        )
        if pub.aws_s3_endpoint:
            _check_host_reachability(
                "public-api AWS_S3_ENDPOINT", pub.aws_s3_endpoint, report
            )
        if pub.redis_url:
            _check_host_reachability(
                "public-api REDIS_URL (rate-limit)", pub.redis_url, report
            )

    _check_consistency(ing, pub, report)

    if report.warnings:
        print("WARNINGS:")
        for w in report.warnings:
            print(f"  - {w}")
        print()

    if report.errors:
        print("ERRORS:")
        for e in report.errors:
            print(f"  - {e}")
        print()
        print(f"FAIL: {len(report.errors)} error(s), {len(report.warnings)} warning(s).")
        return 1

    print(f"OK: settings loaded. {len(report.warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
