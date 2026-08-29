"""inh-contracts: shared contracts consumed by both Inherent services.

Single source of truth for Weaviate naming (#12), the versioned cross-service
event schemas (#17), shared configuration defaults (#132), the file-type
support registry (#117), and the embedding provider abstraction (#311). See
``inh_contracts.naming``, ``inh_contracts.events``, ``inh_contracts.defaults``,
``inh_contracts.file_types`` and ``inh_contracts.embedding``.
"""

from inh_contracts.defaults import DEFAULT_MONGODB_URI, DEFAULT_S3_BUCKET, DEFAULT_S3_REGION
from inh_contracts.embedding import (
    DEFAULT_EMBEDDING_PROVIDER,
    EmbeddingIdentity,
    EmbeddingIdentityAdoptionRequiredError,
    EmbeddingIdentityMismatchError,
    EmbeddingProvider,
    OpenAICompatibleProvider,
    TEIProvider,
    create_embedding_provider,
    decode_identity,
    embed_batch_with_retry,
    embed_single,
    embed_texts_batched,
    encode_identity,
    is_transient_embed_error,
    redact_url,
    resolve_identity,
)
from inh_contracts.events import (
    CONTRACT_VERSION,
    DocumentCompletionMessage,
    DocumentUploadMessage,
    StorageBackend,
)
from inh_contracts.file_types import (
    FILE_TYPE_REGISTRY,
    ContentTypeMismatchError,
    ExtensionMismatchError,
    FileTypeSpec,
    UnknownContentTypeError,
    all_mime_types,
    check_extension_consistency,
    get_spec_by_key,
    get_spec_for_extension,
    get_spec_for_mime,
    mcp_mime_types,
    render_markdown_table,
    sniff_content_type,
)
from inh_contracts.naming import (
    USER_TENANT_PREFIX,
    WORKSPACE_COLLECTION_PREFIX,
    get_user_tenant_name,
    get_workspace_collection_name,
)

__all__ = [
    "CONTRACT_VERSION",
    "DocumentUploadMessage",
    "DocumentCompletionMessage",
    "StorageBackend",
    "get_workspace_collection_name",
    "get_user_tenant_name",
    "WORKSPACE_COLLECTION_PREFIX",
    "USER_TENANT_PREFIX",
    "DEFAULT_S3_REGION",
    "DEFAULT_S3_BUCKET",
    "DEFAULT_MONGODB_URI",
    "FILE_TYPE_REGISTRY",
    "FileTypeSpec",
    "UnknownContentTypeError",
    "ContentTypeMismatchError",
    "ExtensionMismatchError",
    "all_mime_types",
    "mcp_mime_types",
    "get_spec_for_mime",
    "get_spec_for_extension",
    "get_spec_by_key",
    "sniff_content_type",
    "check_extension_consistency",
    "render_markdown_table",
    "EmbeddingProvider",
    "EmbeddingIdentity",
    "EmbeddingIdentityMismatchError",
    "EmbeddingIdentityAdoptionRequiredError",
    "TEIProvider",
    "OpenAICompatibleProvider",
    "create_embedding_provider",
    "redact_url",
    "encode_identity",
    "decode_identity",
    "resolve_identity",
    "embed_single",
    "embed_texts_batched",
    "embed_batch_with_retry",
    "is_transient_embed_error",
    "DEFAULT_EMBEDDING_PROVIDER",
]
