"""Text extraction activity for converting document content to plain text.

Fetches file content directly from storage (instead of receiving bytes
via gRPC) and writes extracted text to the staging table.
"""

import io

import structlog
from temporalio import activity

from src.temporal.lineage import track_event
from src.temporal.models import ExtractTextInput, ExtractTextOutput

logger = structlog.get_logger(__name__)


@activity.defn
async def extract_text(input: ExtractTextInput) -> ExtractTextOutput:
    """Extract text from document content based on content type.

    Supports multiple formats:
    - Plain text, Markdown, CSV: Direct UTF-8 decode
    - JSON: Parse and pretty-print
    - PDF: Extract via pypdf/PyPDF2
    - DOCX: Extract via python-docx
    - HTML: Strip tags via BeautifulSoup
    - PNG images: OCR via pytesseract/Pillow (graceful fallback to a
      placeholder when OCR is unavailable, so the pipeline never crashes)

    The activity fetches file content from storage itself (avoiding the
    4MB gRPC limit) and writes extracted text to the staging table.

    Args:
        input: Contains storage refs, content type, filename, and workflow_run_id

    Returns:
        ExtractTextOutput with text_length (text itself is in staging)
    """
    async with track_event(
        workflow_run_id=input.workflow_run_id,
        document_id=input.document_id or "",
        workspace_id=input.workspace_id,
        event_type="text_extracted",
    ):
        return await _extract_text_inner(input)


async def _extract_text_inner(input: ExtractTextInput) -> ExtractTextOutput:
    """Inner implementation for text extraction (wrapped by lineage tracking)."""
    from src.temporal.shared_services import get_staging_service, get_storage_service

    # Fetch file content from storage
    storage_service = get_storage_service()

    if input.storage_backend == "local":
        content = storage_service.read_file(
            path=input.storage_path,
            backend="local",
            bucket=input.storage_bucket,
        )
    elif input.storage_backend == "gcs":
        content = storage_service.read_file(
            path=input.storage_path,
            backend="gcs",
            bucket=input.storage_bucket,
        )
    elif input.storage_backend == "s3":
        content = storage_service.read_file(
            path=input.storage_path,
            backend="s3",
            bucket=input.storage_bucket,
        )
    elif input.storage_backend == "azure":
        if input.storage_url:
            content = storage_service.read_file_from_url(input.storage_url)
        else:
            raise RuntimeError(f"Storage backend '{input.storage_backend}' requires storage_url")
    else:
        raise RuntimeError(f"Unknown storage backend: {input.storage_backend}")

    if content is None:
        raise RuntimeError("Failed to fetch document content from storage")

    content_type = input.content_type.lower()
    filename = input.original_filename.lower()

    logger.info(
        "Extracting text",
        content_type=content_type,
        filename=filename,
        content_size=len(content),
    )

    text = ""

    # Plain text files
    if content_type in ("text/plain", "text/markdown", "text/csv"):
        text = content.decode("utf-8", errors="ignore")

    # JSON files
    elif content_type == "application/json" or filename.endswith(".json"):
        import json

        data = json.loads(content.decode("utf-8"))
        text = json.dumps(data, indent=2)

    # PDF files
    elif content_type == "application/pdf" or filename.endswith(".pdf"):
        text = _extract_pdf_text(content)

    # Word documents
    elif "wordprocessingml" in content_type or filename.endswith((".docx", ".doc")):
        text = _extract_docx_text(content)

    # Spreadsheet documents
    elif "spreadsheetml" in content_type or filename.endswith((".xlsx", ".xls")):
        raise RuntimeError(
            "Unsupported spreadsheet document type. "
            "Add an XLSX extractor before accepting spreadsheet uploads."
        )

    # HTML files
    elif content_type == "text/html" or filename.endswith(".html"):
        text = _extract_html_text(content)

    # PNG images (OCR with graceful fallback)
    elif content_type == "image/png" or filename.endswith(".png"):
        text = _extract_image_text(content, input.original_filename)

    # Default: try to decode as text
    else:
        text = content.decode("utf-8", errors="ignore")

    # Run data quality checks on extracted text
    from src.services.quality import DataQualityService

    quality = DataQualityService()
    quality_results = quality.check_extracted_text(text, input.original_filename)
    quality.log_results(quality_results, document_id="extract:" + input.workflow_run_id)
    if quality.has_critical_failure(quality_results):
        raise RuntimeError(
            f"Text quality check failed for {input.original_filename}: empty extraction"
        )

    # Strip NUL (0x00) bytes before staging (issue #84). Postgres text/varchar
    # columns cannot store the NUL byte at all -- the driver raises before the
    # query reaches the server -- so an unsanitized value fails the staging
    # write permanently. Some documents (e.g. PDFs pypdf decodes imperfectly)
    # produce embedded NUL bytes. We strip *after* the quality check so its
    # `no_binary_content` diagnostic still sees the raw signal, and before the
    # empty-text guard so a text made up entirely of NUL bytes is caught below.
    if "\x00" in text:
        null_count = text.count("\x00")
        text = text.replace("\x00", "")
        logger.warning(
            "Stripped NUL bytes from extracted text before staging",
            filename=input.original_filename,
            null_bytes_removed=null_count,
        )

    if not text.strip():
        raise RuntimeError(
            f"Text extraction produced empty result for {filename} "
            f"(content_type={content_type}, size={len(content)} bytes)"
        )

    logger.info(
        "Text extracted successfully",
        content_type=content_type,
        text_length=len(text),
    )

    # Write extracted text to staging
    staging = get_staging_service()
    staging.write_text(input.workflow_run_id, text)

    return ExtractTextOutput(text_length=len(text))


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from PDF content.

    Raises on failure so Temporal retries the activity instead of
    silently producing an empty document.
    """
    try:
        import pypdf
    except ImportError:
        try:
            import PyPDF2 as pypdf  # type: ignore[no-redef]  # noqa: N813
        except ImportError:
            raise RuntimeError("PDF extraction libraries not available (pypdf or PyPDF2)")

    reader = pypdf.PdfReader(io.BytesIO(content))
    text_parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            text_parts.append(text)
    return "\n\n".join(text_parts)


def _extract_docx_text(content: bytes) -> str:
    """Extract text from DOCX content.

    Raises on failure so Temporal retries the activity.
    """
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx not available for DOCX extraction")

    doc = Document(io.BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def _extract_image_text(content: bytes, original_filename: str) -> str:
    """Extract text from a PNG image via Tesseract OCR with graceful fallback.

    OCR is an optional capability (requires the ``ocr`` extra plus the
    ``tesseract`` system binary). When OCR is unavailable -- the libraries
    are not installed, the tesseract binary is missing, or the image simply
    contains no readable text -- this returns a minimal placeholder instead
    of raising. The placeholder keeps the document flowing through the
    pipeline (0 useful chunks, but not a hard failure) so a missing OCR
    install never crashes ingestion.

    Args:
        content: Raw PNG bytes.
        original_filename: Original filename, used in the fallback placeholder.

    Returns:
        OCR-extracted text, or a placeholder string when OCR yields nothing
        or is unavailable.
    """
    placeholder = f"[image: {original_filename}, no text extracted]"

    try:
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


def _extract_html_text(content: bytes) -> str:
    """Extract text from HTML content.

    Falls back to raw UTF-8 decode if BeautifulSoup is not available.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(content, "html.parser")
        for element in soup(["script", "style"]):
            element.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        logger.warning("beautifulsoup4 not available, falling back to raw decode")
        return content.decode("utf-8", errors="ignore")
