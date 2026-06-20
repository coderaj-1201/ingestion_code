"""
Image Parser — Azure AI Vision
==============================

Uses the Azure AI Vision Image Analysis 4.0 SDK to extract content from image
files. Unlike the GPT-4o Vision path this service is purpose-built for OCR and
image captioning, runs synchronously, and is significantly cheaper per image.

Strategy
--------
1. Submit raw image bytes to ``ImageAnalysisClient.analyze`` requesting the
   ``READ`` (OCR) and ``CAPTION`` visual features.
2. ``READ`` returns the full text content of the image (documents, screenshots,
   printed text, handwriting).  ``CAPTION`` returns a one-sentence description
   of the scene — used as fallback content when no text is found.
3. Produce a two-level chunk hierarchy (same pattern as all other parsers):
   - **Parent chunk** (``parent_id=""``) — full OCR text or caption; no vector.
   - **Child chunks** (``parent_id=<parent_id>``) — one per text line group;
     embedded for similarity search.

Configuration
-------------
Set in ``.env`` or environment:

    AZURE_VISION_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
    AZURE_VISION_KEY=<api-key>

Cost: ~$0.0015 per image (Read + Caption, as of 2025 pricing).

Supported formats: PNG, JPEG, GIF, WEBP, BMP, TIFF.
File size limit: 20 MB (enforced by the API).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)

# Azure AI Vision hard limit.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Group text lines into paragraphs when there is a blank-line gap in the OCR
# output.  Lines shorter than this are joined to the next line.
_MIN_CHILD_CHARS = 40


def _get_vision_client():
    """Construct an ImageAnalysisClient from settings.

    Raises RuntimeError if the required config vars are absent so that the
    error surfaces at call time with a clear message rather than an
    AttributeError deep in the Azure SDK.
    """
    from azure.ai.vision.imageanalysis import ImageAnalysisClient
    from azure.core.credentials import AzureKeyCredential

    endpoint = str(settings.AZURE_VISION_ENDPOINT or "")
    if not endpoint:
        raise RuntimeError(
            "AZURE_VISION_ENDPOINT is not configured. "
            "Set it in .env to enable image parsing."
        )
    raw_key = settings.AZURE_VISION_KEY
    if not raw_key:
        raise RuntimeError(
            "AZURE_VISION_KEY is not configured. "
            "Set it in .env to enable image parsing."
        )
    return ImageAnalysisClient(
        endpoint=endpoint.rstrip("/"),
        credential=AzureKeyCredential(raw_key.get_secret_value()),
    )


def _split_into_groups(text: str) -> list[str]:
    """Split OCR text into logical paragraph groups.

    Lines separated by a blank line form a group.  Short lines (< _MIN_CHILD_CHARS)
    are merged with the following non-blank line to avoid producing trivial
    one-word child chunks.
    """
    groups: list[str] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                groups.append(" ".join(current))
                current = []
        else:
            current.append(line)

    if current:
        groups.append(" ".join(current))

    # Merge groups that are too short into their successor.
    merged: list[str] = []
    carry = ""
    for g in groups:
        combined = (carry + " " + g).strip() if carry else g
        if len(combined) < _MIN_CHILD_CHARS and groups.index(g) < len(groups) - 1:
            carry = combined
        else:
            merged.append(combined)
            carry = ""
    if carry:
        merged.append(carry)

    return [m for m in merged if m]


def parse_image(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """Parse an image file via Azure AI Vision and return a list of RawChunks.

    Args:
        file_bytes: Raw image bytes (PNG, JPEG, GIF, WEBP, BMP, TIFF).
        doc_name:   File name, e.g. ``"org_chart.png"``.
        doc_url:    SharePoint URL to the file.
        domain:     Business domain (``"hr"``, ``"ops"``, etc.).
        blob_path:  Path in the raw-documents blob container.

    Returns:
        list[RawChunk] — 1 parent + N child chunks.

    Raises:
        ValueError:   If the file exceeds 20 MB.
        RuntimeError: If Azure Vision config vars are missing.
    """
    from azure.ai.vision.imageanalysis.models import VisualFeatures

    if len(file_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"{doc_name} is {len(file_bytes) / 1024 / 1024:.1f} MB — "
            "exceeds the 20 MB Azure AI Vision limit."
        )

    ingested_at = datetime.now(timezone.utc).isoformat()
    client = _get_vision_client()

    logger.info(
        "Azure AI Vision: analysing doc_name=%s size=%dKB",
        doc_name, len(file_bytes) // 1024,
    )

    result = client.analyze(
        image_data=file_bytes,
        visual_features=[VisualFeatures.READ, VisualFeatures.CAPTION],
    )

    # Prefer OCR text; fall back to caption for purely visual images.
    ocr_text = result.read.content.strip() if result.read and result.read.content else ""
    caption   = result.caption.text.strip() if result.caption and result.caption.text else ""

    if ocr_text:
        primary_content = ocr_text
        title = doc_name
    elif caption:
        primary_content = caption
        title = doc_name
    else:
        primary_content = "[No text or visual content detected]"
        title = doc_name

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
        title=title,
    )

    # Parent — full text, no vector; used to supply retrieval context.
    chunks.append(RawChunk(
        chunk_id=parent_id,
        parent_id="",
        chunk_type=ChunkType.PARAGRAPH,
        content=primary_content,
        **base,
    ))

    # Children — one per paragraph group, embedded for similarity search.
    groups = _split_into_groups(ocr_text) if ocr_text else ([caption] if caption else [])
    for idx, group in enumerate(groups):
        if len(group) < _MIN_CHILD_CHARS:
            continue
        chunks.append(RawChunk(
            chunk_id=str(uuid4()),
            parent_id=parent_id,
            chunk_type=ChunkType.PARAGRAPH,
            content=group,
            page_number=idx + 1,
            **base,
        ))

    # If no group passed the length gate, emit one child from the full content
    # so every parent always has at least one embedded child.
    if len(chunks) == 1 and primary_content:
        chunks.append(RawChunk(
            chunk_id=str(uuid4()),
            parent_id=parent_id,
            chunk_type=ChunkType.PARAGRAPH,
            content=primary_content,
            **base,
        ))

    logger.info("Image parsed: %s → %d chunks (ocr_len=%d)", doc_name, len(chunks), len(ocr_text))
    return chunks
