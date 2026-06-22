"""
Image Parser — Azure Document Intelligence
==========================================

Uses the Azure AI Document Intelligence ``prebuilt-read`` model to extract
text and layout from image files.  The model returns content as Markdown
(headings, paragraphs, tables) which is passed directly into RawChunks for
the RAG pipeline.

Configuration
-------------
Set ``AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`` in ``.env`` or environment.
Auth uses ``DefaultAzureCredential`` — no API key required.
Assign the ``Cognitive Services User`` role to the Managed Identity on the
Document Intelligence resource.

Supported formats: JPEG, PNG, BMP, TIFF, HEIF.
File size limit: 500 MB (enforced by the API).
Package required: azure-ai-documentintelligence>=1.0.0
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)

_MIN_CHILD_CHARS = 40


def _get_client():
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set. "
            "Set it in .env to enable image parsing."
        )
    return DocumentIntelligenceClient(endpoint.rstrip("/"), DefaultAzureCredential())


def _split_paragraphs(markdown: str) -> list[str]:
    """Split markdown into non-trivial paragraph groups separated by blank lines."""
    groups: list[str] = []
    current: list[str] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                groups.append(" ".join(current))
                current = []
        else:
            current.append(stripped)

    if current:
        groups.append(" ".join(current))

    return [g for g in groups if len(g) >= _MIN_CHILD_CHARS]


def parse_image(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """Parse an image via Azure Document Intelligence and return RawChunks.

    Args:
        file_bytes: Raw image bytes (JPEG, PNG, BMP, TIFF, HEIF).
        doc_name:   File name, e.g. ``"org_chart.png"``.
        doc_url:    SharePoint URL to the file.
        domain:     Business domain (``"hr"``, ``"ops"``, etc.).
        blob_path:  Path in the raw-documents blob container.

    Returns:
        list[RawChunk] — 1 parent + N child chunks.

    Raises:
        RuntimeError: If AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set.
    """
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

    ingested_at = datetime.now(timezone.utc).isoformat()
    client = _get_client()

    logger.info(
        "Document Intelligence: analysing doc_name=%s size=%dKB",
        doc_name, len(file_bytes) // 1024,
    )

    poller = client.begin_analyze_document(
        "prebuilt-read",
        analyze_request=AnalyzeDocumentRequest(base64_source=file_bytes),
        output_content_format="markdown",
    )
    result = poller.result()

    markdown = (result.content or "").strip()
    if not markdown:
        markdown = "[No content extracted]"

    chunks: list[RawChunk] = []
    parent_id = str(uuid4())

    base = dict(
        domain=domain,
        doc_name=doc_name,
        source=doc_name,
        doc_url=doc_url,
        file_type="image",
        blob_path=blob_path,
        ingested_at=ingested_at,
        title=doc_name,
    )

    # Parent — full markdown, no vector; provides retrieval context.
    chunks.append(RawChunk(
        chunk_id=parent_id,
        parent_id="",
        chunk_type=ChunkType.PARAGRAPH,
        content=markdown,
        **base,
    ))

    # Children — one per paragraph, embedded for similarity search.
    paragraphs = _split_paragraphs(markdown)
    for idx, para in enumerate(paragraphs):
        chunks.append(RawChunk(
            chunk_id=str(uuid4()),
            parent_id=parent_id,
            chunk_type=ChunkType.PARAGRAPH,
            content=para,
            page_number=idx + 1,
            **base,
        ))

    # Guarantee at least one embedded child per parent.
    if len(chunks) == 1:
        chunks.append(RawChunk(
            chunk_id=str(uuid4()),
            parent_id=parent_id,
            chunk_type=ChunkType.PARAGRAPH,
            content=markdown,
            **base,
        ))

    logger.info("Image parsed: %s → %d chunks", doc_name, len(chunks))
    return chunks
