"""Application settings using Pydantic Settings for environment variable management."""

from functools import lru_cache
from typing import Literal

from inh_contracts.defaults import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET, DEFAULT_S3_REGION
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.constants import DEFAULT_DATABASE_NAME


class Settings(BaseSettings):
    """Application configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Tests construct Settings by field name (e.g. eval_capture_disabled_workspaces=...);
        # env loading still resolves via aliases. Without this, extra="ignore"
        # silently drops by-name kwargs for aliased fields instead of erroring.
        populate_by_name=True,
    )

    # Service configuration
    service_name: str = "inh-public-api-svc"
    service_mode: Literal["api", "mcp", "both"] = "both"
    # PORT is Cloud Run's standard env var, API_PORT is a fallback
    port: int = 8080
    api_port: int | None = None

    @property
    def effective_api_port(self) -> int:
        """Get the effective API port (API_PORT overrides PORT)."""
        return self.api_port if self.api_port is not None else self.port

    mcp_port: int = 8001
    log_level: str = "INFO"
    environment: str = "development"
    version: str = "0.2.0"

    # Database (reads + document/eval writes; not a read-only role)
    database_url: str = f"postgresql://postgres:postgres@localhost:5432/{DEFAULT_DATABASE_NAME}"

    # MongoDB (Read-only — for workspace ownership lookups; control-plane truth)
    # Default: see inh_contracts.defaults.DEFAULT_MONGODB_URI (#176) -- the
    # single source of truth shared with ingestion-svc's mongodb_uri field.
    # The URI carries no database path segment on purpose: mongodb_db_name
    # below is what actually selects the database (client[mongodb_db_name],
    # see services/mongo_client.py), so the path is not a second source of
    # truth that needs to independently agree with it.
    mongodb_uri: str = Field(
        default=DEFAULT_MONGODB_URI,
        alias="MONGODB_URI",
        description="MongoDB connection URI; reads workspaces collection for ownership checks",
    )
    mongodb_db_name: str = Field(
        default="main",
        alias="MONGODB_DB_NAME",
        description="MongoDB database containing the workspaces and users collections",
    )

    # Cloud SQL Configuration (for production deployments)
    # When use_cloud_sql_connector=True, the service will use Cloud SQL Python Connector
    # instead of direct DATABASE_URL connection.
    use_cloud_sql_connector: bool = False
    # Format: project:region:instance
    cloud_sql_instance: str | None = None
    cloud_sql_database: str = DEFAULT_DATABASE_NAME
    cloud_sql_user: str = "ingestion_user"
    # Password for Cloud SQL (optional - if not set, uses IAM authentication)
    cloud_sql_password: str | None = None
    cloud_sql_use_iam_auth: bool = True

    # Weaviate (Read-only access)
    weaviate_host: str = "localhost"
    weaviate_port: int = 8080
    weaviate_api_key: str | None = None
    weaviate_url: str | None = Field(
        default=None,
        description="Full Weaviate URL (e.g. http://weaviate:8080). Overrides weaviate_host/weaviate_port when set.",
    )

    @property
    def effective_weaviate_url(self) -> str:
        """Return the effective Weaviate URL.

        Uses ``weaviate_url`` (populated from the WEAVIATE_URL env var) when set,
        otherwise falls back to constructing the URL from ``weaviate_host`` and
        ``weaviate_port``.
        """
        if self.weaviate_url:
            return self.weaviate_url.rstrip("/")
        return f"http://{self.weaviate_host}:{self.weaviate_port}"

    # GCP
    gcp_project_id: str | None = None

    # Rate Limiting
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_default: int = Field(default=100, description="Default rate limit per minute")
    rate_limit_unauthenticated: int = Field(
        default=30,
        description=(
            "Per-client-IP limit for requests with no valid API key. Bounds "
            "brute-force / DB-hammering when auth fails or is absent (#5)."
        ),
    )

    # S3 Storage
    aws_s3_endpoint: str = Field(
        default="",
        description="S3-compatible endpoint URL (e.g. Hetzner Object Storage)",
    )
    aws_access_key_id: str = Field(default="", description="S3 access key ID")
    aws_secret_access_key: str = Field(default="", description="S3 secret access key")
    # Default: see inh_contracts.defaults.DEFAULT_S3_BUCKET (#176) -- the
    # single source of truth shared with ingestion-svc's storage_bucket field.
    aws_s3_bucket: str = Field(default=DEFAULT_S3_BUCKET, description="S3 bucket for documents")
    # Default: see inh_contracts.defaults.DEFAULT_S3_REGION (#132) -- the single
    # source of truth shared with ingestion-svc's s3_region field.
    #
    # Alias: ingestion-svc reads AWS_REGION (#132 blocker 1). Without accepting
    # it here too, an operator who follows docs/deploy/production.md step 3
    # ("set AWS_REGION=<your-region>") configures ingestion but leaves this
    # service on DEFAULT_S3_REGION -- the exact drift #132 exists to prevent,
    # now reintroduced one layer up (env var NAME instead of default VALUE).
    # AWS_S3_REGION is tried first so it still overrides a stray AWS_REGION
    # when an operator deliberately wants this service on a different region.
    aws_s3_region: str = Field(
        default=DEFAULT_S3_REGION,
        validation_alias=AliasChoices("AWS_S3_REGION", "AWS_REGION"),
        description="S3 region. AWS_S3_REGION wins if set; otherwise falls back "
        "to AWS_REGION (the var ingestion-svc reads) so one var configures both.",
    )

    # MQ (Redis / Valkey)
    mq_redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis URL for message queue (document upload notifications)",
    )
    mq_topic_document_uploaded: str = Field(
        default="core.document.uploaded.v1",
        # Must match the ingestion consumer's MQ_UPLOAD_TOPIC (#15) — a separate
        # env var name would let an operator override one side only and silently
        # publish uploads to a stream nobody consumes.
        alias="MQ_UPLOAD_TOPIC",
        description="MQ topic for document upload events",
    )

    # Redis (optional - for distributed rate limiting)
    redis_url: str | None = Field(
        default=None,
        description="Redis URL for distributed rate limiting. Falls back to in-memory if not set.",
    )

    # Trusted reverse proxies whose X-Forwarded-For / X-Real-IP headers may be
    # believed when deriving the client IP for audit/rate-limiting (#16). Empty
    # (default) = trust nobody; the direct peer IP is always used, so a client
    # can't forge its audited IP. Set to your LB/ingress IPs in production.
    trusted_proxies: list[str] = Field(default=[])

    # CORS Configuration
    cors_origins: list[str] = Field(
        default=[
            "https://app.inherent.systems",
            "https://inherent.systems",
            "https://dev-api.inherent.systems",
            "https://api.inherent.systems",
        ],
        description="Allowed CORS origins. Use ['*'] for development only.",
    )
    cors_allow_credentials: bool = True
    cors_allow_methods: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    cors_allow_headers: list[str] = ["*"]

    # Metrics
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"

    # Security
    enable_hsts: bool = Field(
        default=True,
        description="Enable HSTS header in production",
    )
    api_key_header_name: str = "X-API-Key"

    # Embedding service (TEI sidecar; same one ingestion-svc uses)
    embedding_service_url: str = Field(
        "http://text-embeddings-inference:80",
        alias="EMBEDDING_SERVICE_URL",
    )
    embedding_dim: int = Field(384, alias="EMBEDDING_DIM")

    # Search (#13 — multi-workspace retrieval)
    search_max_workspace_concurrency: int = Field(
        default=8,
        ge=1,
        description=(
            "Maximum number of workspaces searched concurrently for a single "
            "multi-workspace search request. Bounds in-flight Weaviate queries "
            "so a user with many workspaces cannot exhaust the connection pool."
        ),
    )

    # Freshness (#42) — stale-evidence policy
    freshness_max_age_days: int = Field(
        default=90,
        ge=1,
        description=(
            "Evidence older than this many days is flagged is_stale=true on each "
            "SearchResult. Stale evidence is NOT filtered out — it is returned with "
            "the flag so callers can decide how to treat it (and can trigger a "
            "refresh/re-ingestion). Compared against the chunk's ingested_at."
        ),
    )

    # Advanced retrieval methods (#47) — EXPERIMENTAL, OFF BY DEFAULT.
    #
    # Each flag gates an advanced retrieval method that is NOT yet implemented
    # (scaffolding only). They are opt-in and default to False so the production
    # default stays the measured hybrid baseline (#45). Per the eval-gate policy
    # (see docs/advanced-indexes.md), NO method may be turned on by default until
    # it shows a documented eval improvement over the hybrid baseline on the M4
    # retrieval evals (tests/evals/) AND has maintainer approval. Enable in dev
    # only, to experiment.
    enable_reranker: bool = Field(
        default=False,
        description=(
            "EXPERIMENTAL (#47), off by default. Opt-in cross-encoder reranking of "
            "assembled results. NOT implemented (scaffolding). Requires a documented "
            "eval improvement vs the hybrid baseline (#45) + maintainer approval "
            "before it may default on. See docs/advanced-indexes.md."
        ),
    )
    enable_graphrag_index: bool = Field(
        default=False,
        description=(
            "EXPERIMENTAL (#47), off by default. Opt-in GraphRAG-style graph index "
            "retrieval. NOT implemented (scaffolding). Requires a documented eval "
            "improvement vs the hybrid baseline (#45) + maintainer approval before "
            "it may default on. See docs/advanced-indexes.md."
        ),
    )
    enable_hierarchy_index: bool = Field(
        default=False,
        description=(
            "EXPERIMENTAL (#47), off by default. Opt-in hierarchical (parent/child) "
            "index retrieval. NOT implemented (scaffolding). Requires a documented "
            "eval improvement vs the hybrid baseline (#45) + maintainer approval "
            "before it may default on. See docs/advanced-indexes.md."
        ),
    )

    # Per-document diversification (#146) — ON BY DEFAULT since 2026-08-06.
    #
    # Unlike the #47 scaffolding above, this method IS implemented (it's a
    # deterministic round-robin over already-fetched candidates, not a new
    # model or index). It was gated behind the same eval-gate policy as the
    # #47 methods (no default-on without a documented eval improvement vs the
    # hybrid baseline + maintainer approval) because it changes ranking
    # order; both conditions are now met (recall@5 0.5->1.0 on the
    # multi_doc_crowding golden-corpus category, maintainer approval granted
    # 2026-08-06) so the default flipped. Set ENABLE_DIVERSIFICATION=false to
    # restore the pre-2026-08-06 (pre-#146-default-flip) behavior. See
    # docs/advanced-indexes.md and ADR 0004 (including its 2026-08-06
    # amendment).
    enable_diversification: bool = Field(
        default=True,
        description=(
            "On by default since 2026-08-06 (#146). Per-document result "
            "diversification: round-robins candidates across document_id before "
            "truncating to the page size, so one highly-relevant document can't "
            "crowd out every other result. Cleared the eval-gate policy "
            "(documented eval improvement vs the hybrid baseline + maintainer "
            "approval, see ADR 0004) before defaulting on. Set to false to "
            "restore the pre-#146-default-flip ranking behavior."
        ),
    )
    diversification_over_fetch_multiplier: int = Field(
        default=5,
        ge=1,
        description=(
            "When enable_diversification is on, fetch up to "
            "min(100, limit * this) candidates from Weaviate before "
            "diversifying and truncating back to limit, so there are enough "
            "distinct documents in the pool to diversify across. Ignored "
            "when enable_diversification is off. Must be >= 1 -- a value of "
            "0 makes min(100, limit * 0) == 0, so the max() against the "
            "base fetch_limit in _build_graphql never widens it, silently "
            "defeating diversification's over-fetch even while the flag "
            "reads as on."
        ),
    )

    # Evals v1 — traffic-mined retrieval evals (design spec: evals-v1).
    # Capture is ON by default (opt-out model): every search is recorded to
    # eval_query_events by a fire-and-forget background task. Raw events are
    # purged after eval_retention_days; promoted eval_cases persist.
    eval_capture_enabled: bool = Field(
        default=True,
        alias="EVAL_CAPTURE_ENABLED",
        description="Record search query events for evals (opt-out).",
    )
    eval_retention_days: int = Field(
        default=30,
        alias="EVAL_RETENTION_DAYS",
        description="Days to keep raw eval_query_events rows before purge.",
    )
    eval_min_sample_size: int = Field(
        default=50,
        alias="EVAL_MIN_SAMPLE_SIZE",
        description="Labeled-case count under which the scorecard flags low confidence.",
    )
    eval_run_concurrency: int = Field(
        default=4,
        alias="EVAL_RUN_CONCURRENCY",
        description="Max concurrent replay searches during an eval run.",
    )
    eval_run_k: int = Field(
        default=5,
        alias="EVAL_RUN_K",
        description="Ranking-metric cutoff k for eval runs (recall@k, nDCG@k).",
    )
    eval_capture_disabled_workspaces: str = Field(
        default="",
        alias="EVAL_CAPTURE_DISABLED_WORKSPACES",
        description="Comma-separated workspace ids excluded from eval capture.",
    )

    def eval_capture_optout_set(self) -> set[str]:
        """Parse the opt-out CSV into a set (whitespace/empty entries dropped)."""
        return {w.strip() for w in self.eval_capture_disabled_workspaces.split(",") if w.strip()}

    # Health Checks (#203)
    # Two independent knobs, not one: the readiness probe already treats
    # Postgres and Weaviate as having different tolerances (see
    # api/v1/health.py's 100ms-vs-500ms "high latency" thresholds -- Weaviate
    # vector search is expected to be slower than a Postgres round-trip), so
    # a shared single timeout would force one dependency's probe to inherit
    # the other's budget. The previous single `health_check_timeout_seconds`
    # knob was declared but had ZERO call sites -- health.py read hardcoded
    # DATABASE_HEALTH_CHECK_TIMEOUT / WEAVIATE_HEALTH_CHECK_TIMEOUT constants
    # instead, so setting it silently did nothing (#203). Deleted rather than
    # kept alongside these two, to avoid leaving a second unread knob.
    database_health_check_timeout_seconds: float = Field(
        default=5.0,
        alias="DATABASE_HEALTH_CHECK_TIMEOUT_SECONDS",
        description="Timeout for the Postgres health-check query used by GET /health/ready",
    )
    weaviate_health_check_timeout_seconds: float = Field(
        default=5.0,
        alias="WEAVIATE_HEALTH_CHECK_TIMEOUT_SECONDS",
        description="Timeout for the Weaviate health-check call used by GET /health/ready",
    )

    # Audit Logging
    audit_log_enabled: bool = True
    audit_log_topic: str = "audit.log.write"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def cors_origins_list(self) -> list[str]:
        """Get CORS origins, allowing all in development if not explicitly set."""
        if self.is_development and self.cors_origins == [
            "https://app.inherent.systems",
            "https://inherent.systems",
            "https://dev-api.inherent.systems",
            "https://api.inherent.systems",
        ]:
            return ["*"]
        return self.cors_origins

    @property
    def cors_allow_credentials_effective(self) -> bool:
        """Never advertise credentials alongside a wildcard origin (#36).

        allow_origins=["*"] with allow_credentials=True lets any site make
        credentialed cross-origin calls (and is spec-invalid). When the origin
        list is a wildcard, force credentials off regardless of config.
        """
        if "*" in self.cors_origins_list:
            return False
        return self.cors_allow_credentials


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
