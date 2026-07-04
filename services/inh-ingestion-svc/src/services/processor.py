"""Document processor service with full extraction and storage logic.

.. deprecated::
    LEGACY / NOT ON THE LIVE PATH (#23). ``DocumentProcessor`` is the pre-Temporal
    synchronous ingestion pipeline. The live ingestion path is the Temporal
    workflow (``temporal/workflows/document_ingestion.py``) driving the
    ``temporal/activities/*`` activities. No runtime entrypoint imports this
    module — it is referenced only by tests. Do NOT build on it; its behaviour
    (e.g. it reports success even when a store fails, and does not delete-before-
    reindex) diverges from the live activities. Scheduled for removal — see the
    defect register. Kept for now only to preserve its extraction/OCR test
    coverage until that coverage is confirmed duplicated by the activity tests.

This processor integrates with the TenantManager for multi-tenancy support,
ensuring proper tenant isolation in both PostgreSQL and Weaviate.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError as PydanticValidationError

from src.config.settings import Settings
from src.models.document import DocumentChunk, DocumentUploadMessage, ProcessingResult
from src.services.database import DatabaseService
from src.services.storage import StorageService
from src.services.tenant_manager import TenantManager
from src.services.weaviate import WeaviateService

if TYPE_CHECKING:
    from src.services.mq import BaseMQService

logger = structlog.get_logger(__name__)


class DocumentProcessor:
    """Document processor for handling ingestion tasks with multi-tenancy support."""

    def __init__(self, settings: Settings, mq_service: BaseMQService | None = None):
        """Initialize document processor."""
        self.settings = settings
        self.mq_service = mq_service
        self.db_service: DatabaseService | None = None
        self.weaviate_service: WeaviateService | None = None
        self.storage_service: StorageService | None = None
        self.tenant_manager: TenantManager | None = None
        self._initialized = False

    def initialize(self, require_postgres: bool = True) -> None:
        """Initialize all services including TenantManager.

        Args:
            require_postgres: If True, raise exception if PostgreSQL connection fails.
                            If False, log warning and continue without it.
        """
        if self._initialized:
            return

        # Initialize Database Service (critical)
        try:
            self.db_service = DatabaseService(self.settings)
            self.db_service.connect()
            logger.info("PostgreSQL service initialized")
        except Exception as e:
            if require_postgres:
                logger.error("PostgreSQL connection failed (required)", error=str(e), exc_info=True)
                raise RuntimeError(f"Failed to connect to PostgreSQL: {e}") from e
            else:
                logger.warning("Database service not available", error=str(e))
                self.db_service = None

        # Initialize Weaviate Service (optional)
        try:
            self.weaviate_service = WeaviateService(self.settings)
            self.weaviate_service.connect()
            logger.info("Weaviate service initialized")
        except Exception as e:
            logger.warning("Weaviate service not available", error=str(e))
            self.weaviate_service = None

        # Initialize Storage Service
        self.storage_service = StorageService(self.settings)
        self.storage_service.connect()
        logger.info("Storage service initialized")

        # Initialize TenantManager with the services
        self.tenant_manager = TenantManager(
            settings=self.settings,
            db_service=self.db_service,
            weaviate_service=self.weaviate_service,
        )

        self._initialized = True
        logger.info("Document processor initialized with multi-tenancy support")

    async def process_message(self, message: dict) -> ProcessingResult:
        """Process a document upload message with multi-tenancy support.

        Args:
            message: Raw message dictionary from Pub/Sub

        Returns:
            ProcessingResult with success status and details
        """
        start_time = time.time()

        if not self._initialized:
            self.initialize()

        document_id = message.get("document_id", "unknown")
        tenant_id: int | None = None

        try:
            # Validate message schema
            try:
                upload_message = DocumentUploadMessage(**message)
                document_id = upload_message.document_id
            except PydanticValidationError as e:
                logger.error(
                    "Message validation failed",
                    error=str(e),
                    message=message,
                    validation_errors=e.errors(),
                )
                return ProcessingResult(
                    document_id=document_id,
                    success=False,
                    error=f"Invalid message format: {e}",
                )

            logger.info(
                "Processing document",
                document_id=upload_message.document_id,
                workspace_id=upload_message.workspace_id,
                user_id=upload_message.user_id,
                filename=upload_message.original_filename,
                content_type=upload_message.content_type,
                storage_backend=upload_message.storage_backend,
            )

            # Step 0: Ensure tenant infrastructure is ready (NEW)
            if self.tenant_manager:
                try:
                    tenant_id = await self.tenant_manager.ensure_workspace_ready(
                        workspace_id=upload_message.workspace_id,
                        user_id=upload_message.user_id,
                    )
                    logger.info(
                        "Tenant infrastructure ready",
                        tenant_id=tenant_id,
                        workspace_id=upload_message.workspace_id,
                        user_id=upload_message.user_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to setup tenant infrastructure, continuing without tenant_id",
                        error=str(e),
                    )
                    tenant_id = None

            # Step 1: Fetch document content based on storage backend
            content = await self._fetch_document(upload_message)
            if content is None:
                return ProcessingResult(
                    document_id=document_id,
                    success=False,
                    error="Failed to fetch document content",
                )

            logger.info(
                "Document fetched",
                document_id=document_id,
                size=len(content),
            )

            # Step 2: Extract text from document
            text = await self._extract_text(content, upload_message)
            if not text:
                logger.warning("No text extracted from document", document_id=document_id)
                text = ""

            logger.info(
                "Text extracted",
                document_id=document_id,
                text_length=len(text),
            )

            # Step 3: Chunk the text
            chunks = self._chunk_text(text, upload_message)
            logger.info(
                "Text chunked",
                document_id=document_id,
                chunk_count=len(chunks),
            )

            processing_time_ms = int((time.time() - start_time) * 1000)

            # Step 4: Store in databases with tenant context
            await self._store_document(
                upload_message,
                chunks,
                text_length=len(text),
                processing_time_ms=processing_time_ms,
                tenant_id=tenant_id,
            )

            # Step 5: Update workspace statistics
            if self.tenant_manager:
                try:
                    await self.tenant_manager.update_workspace_stats(
                        workspace_id=upload_message.workspace_id,
                        document_delta=1,
                        chunk_delta=len(chunks),
                        size_delta=upload_message.size_bytes,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to update workspace stats",
                        error=str(e),
                    )

            logger.info(
                "Document processed successfully",
                document_id=document_id,
                workspace_id=upload_message.workspace_id,
                user_id=upload_message.user_id,
                tenant_id=tenant_id,
                chunks_created=len(chunks),
                processing_time_ms=processing_time_ms,
            )

            result = ProcessingResult(
                document_id=document_id,
                success=True,
                chunks_created=len(chunks),
                processing_time_ms=processing_time_ms,
            )

            # Publish completion notification
            if self.mq_service:
                try:
                    await self.mq_service.publish_completion(result, upload_message)
                except Exception as e:
                    logger.error(
                        "Failed to publish completion", document_id=document_id, error=str(e)
                    )

            return result

        except Exception as e:
            processing_time_ms = int((time.time() - start_time) * 1000)
            logger.error(
                "Error processing document",
                document_id=document_id,
                error=str(e),
                exc_info=True,
            )

            result = ProcessingResult(
                document_id=document_id,
                success=False,
                error=str(e),
                processing_time_ms=processing_time_ms,
            )

            # Publish completion notification
            if self.mq_service:
                try:
                    await self.mq_service.publish_completion(result, upload_message)
                except Exception as pub_e:
                    logger.error(
                        "Failed to publish completion", document_id=document_id, error=str(pub_e)
                    )

            return result

    async def _fetch_document(self, message: DocumentUploadMessage) -> bytes | None:
        """Fetch document content based on storage backend."""
        if not self.storage_service:
            raise RuntimeError("Storage service not initialized")

        storage_backend = message.storage_backend
        storage_path = message.storage_path
        storage_bucket = message.storage_bucket
        storage_url = message.storage_url

        logger.info(
            "Fetching document",
            backend=storage_backend,
            path=storage_path,
            bucket=storage_bucket,
        )

        try:
            if storage_backend == "local":
                if storage_url:
                    return self.storage_service.read_file_from_url(storage_url)
                else:
                    return self.storage_service.read_file(
                        path=storage_path,
                        backend="local",
                        bucket=storage_bucket,
                    )

            elif storage_backend == "gcs":
                return self.storage_service.read_file(
                    path=storage_path,
                    backend="gcs",
                    bucket=storage_bucket,
                )

            elif storage_backend == "s3":
                logger.warning("S3 storage backend not yet implemented")
                if storage_url:
                    return self.storage_service.read_file_from_url(storage_url)
                return None

            elif storage_backend == "azure":
                logger.warning("Azure storage backend not yet implemented")
                if storage_url:
                    return self.storage_service.read_file_from_url(storage_url)
                return None

            else:
                logger.error("Unknown storage backend", backend=storage_backend)
                return None

        except Exception as e:
            logger.error(
                "Failed to fetch document",
                backend=storage_backend,
                path=storage_path,
                error=str(e),
                exc_info=True,
            )
            return None

    async def _extract_text(self, content: bytes, message: DocumentUploadMessage) -> str:
        """Extract text from document content based on content type."""
        content_type = message.content_type.lower()
        filename = message.original_filename.lower()

        try:
            # Plain text files
            if content_type in ["text/plain", "text/markdown", "text/csv"]:
                return content.decode("utf-8", errors="ignore")

            # JSON files
            if content_type == "application/json" or filename.endswith(".json"):
                import json

                data = json.loads(content.decode("utf-8"))
                return json.dumps(data, indent=2)

            # PDF files
            if content_type == "application/pdf" or filename.endswith(".pdf"):
                return self._extract_pdf_text(content)

            # Word documents
            if "wordprocessingml" in content_type or filename.endswith((".docx", ".doc")):
                return self._extract_docx_text(content)

            # Spreadsheet documents
            if "spreadsheetml" in content_type or filename.endswith((".xlsx", ".xls")):
                logger.warning(
                    "Spreadsheet extraction is unsupported",
                    content_type=content_type,
                    filename=filename,
                )
                return ""

            # HTML files
            if content_type == "text/html" or filename.endswith(".html"):
                return self._extract_html_text(content)

            # PNG images (OCR with graceful fallback)
            if content_type == "image/png" or filename.endswith(".png"):
                return self._extract_image_text(content, message.original_filename)

            # Default: try to decode as text
            try:
                return content.decode("utf-8", errors="ignore")
            except Exception:
                logger.warning(
                    "Could not extract text, unknown content type",
                    content_type=content_type,
                    filename=filename,
                )
                return ""

        except Exception as e:
            logger.error(
                "Error extracting text",
                content_type=content_type,
                error=str(e),
                exc_info=True,
            )
            return ""

    def _extract_pdf_text(self, content: bytes) -> str:
        """Extract text from PDF content."""
        try:
            import io

            try:
                import pypdf

                reader = pypdf.PdfReader(io.BytesIO(content))
                text_parts = []
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
                return "\n\n".join(text_parts)
            except ImportError:
                import PyPDF2

                pdf_reader: PyPDF2.PdfReader = PyPDF2.PdfReader(io.BytesIO(content))  # type: ignore[assignment]
                text_parts = []
                for page in pdf_reader.pages:  # type: ignore[assignment]
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
                return "\n\n".join(text_parts)
        except ImportError:
            logger.warning("PDF extraction libraries not available (pypdf or PyPDF2)")
            return ""
        except Exception as e:
            logger.error("PDF extraction failed", error=str(e))
            return ""

    def _extract_docx_text(self, content: bytes) -> str:
        """Extract text from DOCX content."""
        try:
            import io

            from docx import Document

            doc = Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)
        except ImportError:
            logger.warning("python-docx not available for DOCX extraction")
            return ""
        except Exception as e:
            logger.error("DOCX extraction failed", error=str(e))
            return ""

    def _extract_image_text(self, content: bytes, original_filename: str) -> str:
        """Extract text from a PNG image via Tesseract OCR with graceful fallback.

        OCR is optional (requires the ``ocr`` extra plus the ``tesseract``
        system binary). When OCR is unavailable -- libraries not installed,
        the tesseract binary is missing, or no readable text in the image --
        this returns a minimal placeholder instead of raising, so a missing
        OCR install never crashes ingestion (0 useful chunks, not a hard
        failure).
        """
        placeholder = f"[image: {original_filename}, no text extracted]"

        try:
            import io

            import pytesseract
            from PIL import Image
        except ImportError:
            logger.warning(
                "OCR libraries not available (install the 'ocr' extra: pytesseract, pillow); "
                "returning placeholder for image",
                filename=original_filename,
            )
            return placeholder

        try:
            image = Image.open(io.BytesIO(content))
            text = pytesseract.image_to_string(image)
        except pytesseract.TesseractNotFoundError:
            logger.warning(
                "Tesseract binary not found; install the 'tesseract-ocr' system package. "
                "Returning placeholder for image",
                filename=original_filename,
            )
            return placeholder
        except Exception as e:
            logger.warning(
                "OCR failed for image; returning placeholder",
                filename=original_filename,
                error=str(e),
            )
            return placeholder

        if not text.strip():
            logger.warning(
                "OCR produced no text for image; returning placeholder",
                filename=original_filename,
            )
            return placeholder

        return text

    def _extract_html_text(self, content: bytes) -> str:
        """Extract text from HTML content."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style"]):
                element.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            logger.warning("beautifulsoup4 not available for HTML extraction")
            return content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error("HTML extraction failed", error=str(e))
            return content.decode("utf-8", errors="ignore")

    def _chunk_text(self, text: str, message: DocumentUploadMessage) -> list[DocumentChunk]:
        """Split text into chunks based on chunking strategy."""
        if not text:
            return []

        chunks: list[DocumentChunk] = []
        max_size = self.settings.max_chunk_size
        overlap = self.settings.chunk_overlap
        strategy = self.settings.chunking_strategy

        if strategy == "sentences":
            chunks = self._chunk_by_sentences(text, message.document_id, max_size, overlap)
        elif strategy == "paragraphs":
            chunks = self._chunk_by_paragraphs(text, message.document_id, max_size)
        else:
            chunks = self._chunk_by_size(text, message.document_id, max_size, overlap)

        return chunks

    def _chunk_by_size(
        self, text: str, document_id: str, max_size: int, overlap: int
    ) -> list[DocumentChunk]:
        """Split text into fixed-size chunks with overlap."""
        chunks = []
        start = 0
        chunk_index = 0

        while start < len(text):
            end = min(start + max_size, len(text))

            if end < len(text):
                last_space = text.rfind(" ", start, end)
                if last_space > start:
                    end = last_space

            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(
                    DocumentChunk(
                        document_id=document_id,
                        content=chunk_text,
                        chunk_index=chunk_index,
                        start_char=start,
                        end_char=end,
                    )
                )
                chunk_index += 1

            start = end - overlap if end - overlap > start else end

        return chunks

    def _chunk_by_sentences(
        self, text: str, document_id: str, max_size: int, overlap: int
    ) -> list[DocumentChunk]:
        """Split text into chunks by sentences."""
        import re

        sentences = re.split(r"(?<=[.!?])\s+", text)

        chunks = []
        current_chunk: list[str] = []
        current_size = 0
        chunk_index = 0
        start_char = 0

        for sentence in sentences:
            sentence_len = len(sentence)

            if current_size + sentence_len > max_size and current_chunk:
                chunk_text = " ".join(current_chunk).strip()
                if chunk_text:
                    chunks.append(
                        DocumentChunk(
                            document_id=document_id,
                            content=chunk_text,
                            chunk_index=chunk_index,
                            start_char=start_char,
                            end_char=start_char + len(chunk_text),
                        )
                    )
                    chunk_index += 1
                    start_char += len(chunk_text) + 1

                overlap_sentences: list[str] = []
                overlap_size = 0
                for s in reversed(current_chunk):
                    if overlap_size + len(s) <= overlap:
                        overlap_sentences.insert(0, s)
                        overlap_size += len(s)
                    else:
                        break

                current_chunk = overlap_sentences
                current_size = overlap_size

            current_chunk.append(sentence)
            current_size += sentence_len

        if current_chunk:
            chunk_text = " ".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    DocumentChunk(
                        document_id=document_id,
                        content=chunk_text,
                        chunk_index=chunk_index,
                        start_char=start_char,
                        end_char=start_char + len(chunk_text),
                    )
                )

        return chunks

    def _chunk_by_paragraphs(
        self, text: str, document_id: str, max_size: int
    ) -> list[DocumentChunk]:
        """Split text into chunks by paragraphs."""
        paragraphs = text.split("\n\n")

        chunks = []
        current_chunk: list[str] = []
        current_size = 0
        chunk_index = 0
        start_char = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            para_len = len(para)

            if current_size + para_len > max_size and current_chunk:
                chunk_text = "\n\n".join(current_chunk).strip()
                if chunk_text:
                    chunks.append(
                        DocumentChunk(
                            document_id=document_id,
                            content=chunk_text,
                            chunk_index=chunk_index,
                            start_char=start_char,
                            end_char=start_char + len(chunk_text),
                        )
                    )
                    chunk_index += 1
                    start_char += len(chunk_text) + 2

                current_chunk = []
                current_size = 0

            current_chunk.append(para)
            current_size += para_len

        if current_chunk:
            chunk_text = "\n\n".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    DocumentChunk(
                        document_id=document_id,
                        content=chunk_text,
                        chunk_index=chunk_index,
                        start_char=start_char,
                        end_char=start_char + len(chunk_text),
                    )
                )

        return chunks

    async def _store_document(
        self,
        message: DocumentUploadMessage,
        chunks: list[DocumentChunk],
        text_length: int,
        processing_time_ms: int,
        tenant_id: int | None = None,
    ) -> None:
        """Store document and chunks in databases with tenant context."""
        document_id = message.document_id

        # Store in PostgreSQL if available
        if self.db_service:
            try:
                await self.db_service.store_processed_document(
                    message=message,
                    chunks=chunks,
                    text_length=text_length,
                    processing_time_ms=processing_time_ms,
                    tenant_id=tenant_id,
                )
                logger.info(
                    "Stored in PostgreSQL",
                    document_id=document_id,
                    chunks=len(chunks),
                    tenant_id=tenant_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to store in PostgreSQL",
                    document_id=document_id,
                    error=str(e),
                    exc_info=True,
                )

        # Store in Weaviate with multi-tenant support
        if self.weaviate_service:
            try:
                # Use the new multi-tenant store method
                await self.weaviate_service.store_chunks_with_tenant(
                    chunks=chunks,
                    document_id=message.document_id,
                    workspace_id=message.workspace_id,
                    user_id=message.user_id,
                    original_filename=message.original_filename,
                    content_type=message.content_type,
                    # Provenance (#41): record where the source bytes live.
                    source_uri=message.storage_path or message.storage_url,
                )
                logger.info(
                    "Stored in Weaviate with multi-tenancy",
                    document_id=document_id,
                    workspace_id=message.workspace_id,
                    user_id=message.user_id,
                    chunks=len(chunks),
                )
            except Exception as e:
                logger.error(
                    "Failed to store in Weaviate",
                    document_id=document_id,
                    error=str(e),
                    exc_info=True,
                )

    def shutdown(self) -> None:
        """Shutdown all services."""
        if self.tenant_manager:
            self.tenant_manager.clear_cache()

        if self.storage_service:
            self.storage_service.disconnect()

        if self.weaviate_service:
            self.weaviate_service.disconnect()

        if self.db_service:
            self.db_service.disconnect()

        self._initialized = False
        logger.info("Document processor shut down")
