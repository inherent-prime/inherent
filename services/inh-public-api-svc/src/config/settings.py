"""Application settings using Pydantic Settings for environment variable management."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    version: str = "0.1.0"

    # Database (Read-only access)
    database_url: str = "postgresql://postgres:postgres@localhost:5432/knowledge_base"

    # MongoDB (Read-only — for workspace ownership lookups; control-plane truth)
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017/main",
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
    cloud_sql_database: str = "knowledge_base"
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
    aws_s3_bucket: str = Field(default="inherent-documents", description="S3 bucket for documents")
    aws_s3_region: str = Field(default="eu-central-1", description="S3 region")

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

    # Health Checks
    health_check_timeout_seconds: float = 5.0

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
