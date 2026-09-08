"""Application settings and configuration."""

from functools import lru_cache
from typing import Literal

from inh_contracts.defaults import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET, DEFAULT_S3_REGION
from inh_contracts.events import StorageBackend
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database Configuration
    database_url: str = Field(..., alias="DATABASE_URL")

    # Weaviate Configuration
    weaviate_url: str = Field(..., alias="WEAVIATE_URL")
    weaviate_api_key: str | None = Field(None, alias="WEAVIATE_API_KEY")

    # GCP Configuration (only needed when using GCS storage or Pub/Sub MQ)
    gcp_project_id: str = Field("", alias="GCP_PROJECT_ID")
    # Default: see inh_contracts.defaults.DEFAULT_S3_BUCKET (#176) -- the single
    # source of truth shared with public-api-svc's aws_s3_bucket field. Most
    # uploads carry their own bucket in the event payload
    # (message.storage_bucket), so this default is mainly a fallback
    # (services/storage.py's default_bucket) -- but it must still agree with
    # public-api's default so a deployment that sets neither env var doesn't
    # write to one bucket while public-api reads from another.
    storage_bucket: str = Field(DEFAULT_S3_BUCKET, alias="STORAGE_BUCKET")

    # Storage Configuration
    # Reuse the shared contract type so this can't drift from the wire/DB enum
    # (which already accept 'azure') (#27).
    storage_backend: StorageBackend = Field("s3", alias="STORAGE_BACKEND")

    # S3-compatible storage (Hetzner Object Storage, AWS S3, MinIO, etc.)
    s3_access_key_id: str | None = Field(None, alias="AWS_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(None, alias="AWS_SECRET_ACCESS_KEY")
    # Default: see inh_contracts.defaults.DEFAULT_S3_REGION (#132) -- the single
    # source of truth shared with public-api-svc's aws_s3_region field, which
    # also accepts this same AWS_REGION var (see its AliasChoices, #132 blocker 1).
    s3_region: str = Field(DEFAULT_S3_REGION, alias="AWS_REGION")
    s3_endpoint: str | None = Field(None, alias="AWS_S3_ENDPOINT")

    # Local Storage (for fetching files from integration service)
    local_storage_path: str = Field("", alias="LOCAL_STORAGE_PATH")
    intg_service_url: str = Field("http://localhost:4000", alias="INTG_SERVICE_URL")

    # #214: storage_backend="azure" has no real Azure Blob client anywhere in
    # this codebase (no AzureStorageBackend class, no azure-* SDK dependency)
    # -- fetch.py/extract.py instead treat it as "fetch storage_url directly"
    # (StorageService.read_file_from_url), completely bypassing the #210
    # storage_path/workspace_id prefix check (there is no workspace-prefixed
    # invariant to check a caller-supplied URL against). No in-repo caller
    # ever sets storage_backend="azure" (public-api-svc's document_intake.py
    # always emits "s3"), so this is dead weight from every legitimate path
    # and reachable only as an attacker-controlled bypass via POST /ingest's
    # caller-supplied storage_backend field. Off by default: an operator who
    # actually needs direct-URL ingestion must opt in explicitly, at which
    # point they are accepting that #34's SSRF guard (blocks
    # internal/metadata/loopback targets) is the ONLY remaining check on
    # what gets fetched into a tenant. See fetch.py/extract.py's azure
    # branch and CHANGELOG.md for the full story.
    allow_url_based_ingestion: bool = Field(False, alias="ALLOW_URL_BASED_INGESTION")

    # =========================================================================
    # Message Queue Configuration
    # =========================================================================

    # MQ backend: "redis" (default, recommended), "pubsub" (GCP), "memory" (tests only)
    mq_backend: Literal["redis", "pubsub", "memory"] = Field("redis", alias="MQ_BACKEND")

    # Valkey / Redis URL (used when MQ_BACKEND=redis)
    redis_url: str = Field("redis://localhost:6379", alias="REDIS_URL")

    # Topic names (same across all backends)
    mq_upload_topic: str = Field("core.document.uploaded.v1", alias="MQ_UPLOAD_TOPIC")
    mq_completion_topic: str = Field("core.document.processed.v1", alias="MQ_COMPLETION_TOPIC")

    # Consumer group for this service
    mq_consumer_group: str = Field("ingestion-workers", alias="MQ_CONSUMER_GROUP")

    # Legacy Pub/Sub settings (only used when MQ_BACKEND=pubsub)
    pubsub_subscription: str = Field("unused", alias="PUBSUB_SUBSCRIPTION")

    # Processing Configuration
    chunking_strategy: Literal["tokens", "sentences", "paragraphs"] = Field(
        "sentences", alias="CHUNKING_STRATEGY"
    )
    max_chunk_size: int = Field(1000, alias="MAX_CHUNK_SIZE")
    chunk_overlap: int = Field(200, alias="CHUNK_OVERLAP")

    # Embedding Configuration
    # The model itself runs in a separate text-embeddings-inference (TEI) sidecar.
    # To upgrade the model, change MODEL_ID on the sidecar; this URL stays the same.
    embedding_enabled: bool = Field(True, alias="EMBEDDING_ENABLED")
    embedding_service_url: str = Field(
        "http://text-embeddings-inference:80",
        alias="EMBEDDING_SERVICE_URL",
    )
    embedding_dim: int = Field(384, alias="EMBEDDING_DIM")
    # Hard token limit of the embedding model's context window.
    # Default 512 matches bge-small-en-v1.5 (the default TEI model).
    # Chunking keeps each chunk's ESTIMATED token count under this budget so
    # we don't silently rely on TEI's server-side truncation (which would drop
    # the tail of an oversized chunk and degrade retrieval quality).
    embedding_max_tokens: int = Field(512, alias="EMBEDDING_MAX_TOKENS")

    # Performance Configuration
    max_workers: int = Field(4, alias="MAX_WORKERS")
    max_retries: int = Field(3, alias="MAX_RETRIES")
    retry_delay_seconds: int = Field(5, alias="RETRY_DELAY_SECONDS")

    # Backpressure: max number of MQ messages whose Temporal workflow-starts
    # may be in flight concurrently in the consume loop. Defaults to max_workers.
    # See #18: this bounds async workflow starts so the consumer doesn't fan out
    # unbounded; actual processing concurrency is bounded by the Temporal worker.
    mq_max_concurrent: int = Field(0, alias="MQ_MAX_CONCURRENT")

    # Configuration Path
    config_path: str = Field("config/sources.yaml", alias="CONFIG_PATH")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # Service Mode:
    # - worker: Temporal worker + MQ subscriber (recommended for all environments)
    # - standalone: HTTP API + Temporal worker (for manual triggers, health checks)
    # Legacy modes (pubsub, temporal_worker, temporal_trigger, temporal_all) are
    # mapped to 'worker' for backward compatibility.
    service_mode: str = Field("worker", alias="SERVICE_MODE")

    # =========================================================================
    # Temporal Configuration
    # =========================================================================

    # Whether Temporal workflow orchestration is enabled
    temporal_enabled: bool = Field(False, alias="TEMPORAL_ENABLED")

    # Temporal server host (e.g., localhost:7233 for local, or Temporal Cloud endpoint)
    temporal_host: str = Field("localhost:7233", alias="TEMPORAL_HOST")

    # Temporal namespace
    temporal_namespace: str = Field("default", alias="TEMPORAL_NAMESPACE")

    # Task queue name for document ingestion workflows
    temporal_task_queue: str = Field("document-ingestion", alias="TEMPORAL_TASK_QUEUE")

    # Temporal worker concurrency limits (#18 backpressure). These cap how many
    # activities / workflow tasks the worker will execute concurrently, which is
    # the real bound on processing throughput once MQ consumption is non-blocking.
    temporal_max_concurrent_activities: int = Field(10, alias="TEMPORAL_MAX_CONCURRENT_ACTIVITIES")
    temporal_max_concurrent_workflow_tasks: int = Field(
        10, alias="TEMPORAL_MAX_CONCURRENT_WORKFLOW_TASKS"
    )

    # =========================================================================
    # Multi-Tenancy Configuration
    # =========================================================================

    # Number of days of inactivity before a tenant is considered idle
    # Idle tenants can be deactivated in Weaviate to save resources
    tenant_idle_days: int = Field(30, alias="TENANT_IDLE_DAYS")

    # Whether to automatically create tenants on first document upload
    # If False, tenants must be created explicitly
    auto_create_tenants: bool = Field(True, alias="AUTO_CREATE_TENANTS")

    # =========================================================================
    # Audit Log Configuration
    # =========================================================================

    audit_log_topic: str = Field("audit.log.write", alias="AUDIT_LOG_TOPIC")
    audit_consumer_group: str = Field("ingestion-audit-writers", alias="AUDIT_CONSUMER_GROUP")
    temporal_audit_namespace: str = Field("audit", alias="TEMPORAL_AUDIT_NAMESPACE")
    temporal_audit_task_queue: str = Field("audit-writer-queue", alias="TEMPORAL_AUDIT_TASK_QUEUE")

    # =========================================================================
    # MongoDB Configuration (for audit log writes)
    # =========================================================================

    # Default: see inh_contracts.defaults.DEFAULT_MONGODB_URI (#176) -- the
    # single source of truth shared with public-api-svc's mongodb_uri field.
    # The URI carries no database path segment on purpose: mongodb_db_name
    # below is what actually selects the database (client[mongodb_db_name]),
    # so the path is not a second source of truth to keep in sync.
    mongodb_uri: str = Field(DEFAULT_MONGODB_URI, alias="MONGODB_URI")
    mongodb_db_name: str = Field("main", alias="MONGODB_DB_NAME")

    # One-shot release-stack bootstrap. Required identity values stay optional
    # in the shared model because worker/migrate modes do not consume them;
    # bootstrap mode validates them before opening either database.
    bootstrap_api_key: str | None = Field(None, alias="BOOTSTRAP_API_KEY")
    bootstrap_workspace_id: str | None = Field(None, alias="BOOTSTRAP_WORKSPACE_ID")
    bootstrap_user_id: str | None = Field(None, alias="BOOTSTRAP_USER_ID")
    bootstrap_key_name: str = Field("Local CLI Key", alias="BOOTSTRAP_KEY_NAME")
    bootstrap_workspace_name: str = Field("Default Workspace", alias="BOOTSTRAP_WORKSPACE_NAME")
    # `seed` (compose start-up), `create` (mint one more key), `revoke`.
    bootstrap_action: str = Field("seed", alias="BOOTSTRAP_ACTION")
    bootstrap_key_prefix: str | None = Field(None, alias="BOOTSTRAP_KEY_PREFIX")

    # =========================================================================
    # Standalone API Configuration
    # =========================================================================

    # Secret key for authenticating HTTP API requests (required for standalone mode)
    ingestion_api_key: str | None = Field(None, alias="INGESTION_API_KEY")

    # Host and port for the standalone HTTP API server. Binding all interfaces
    # is required inside a container -- the process cannot know the container
    # IP, and the orchestrator, not this default, decides what is published.
    # Override with API_HOST for a bare-metal run.
    api_host: str = Field("0.0.0.0", alias="API_HOST")  # nosec B104 -- see above
    api_port: int = Field(8000, alias="API_PORT")

    # Port for Prometheus metrics server (worker mode only; standalone uses /metrics route)
    metrics_port: int = Field(9090, alias="METRICS_PORT")

    @property
    def resolved_mq_max_concurrent(self) -> int:
        """Effective MQ consume-loop concurrency bound.

        Falls back to ``max_workers`` when ``mq_max_concurrent`` is unset
        (<= 0), and never returns less than 1.
        """
        value = self.mq_max_concurrent if self.mq_max_concurrent > 0 else self.max_workers
        return max(1, value)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()  # type: ignore[call-arg]
