"""
Image Parser
============

Uses Azure OpenAI GPT-4o Vision to extract content from image files.

Strategy
--------
1. Base64-encode the image and send it to the chat completions API with a
   structured prompt that requests OCR, diagram descriptions, and table content.
2. Split the response into paragraphs and produce a two-level chunk hierarchy:
   - One parent chunk  (``parent_id=""``) holding the full extracted text
   - Child chunks      (``parent_id=<parent_chunk_id>``) per paragraph

Supported formats
-----------------
PNG, JPEG, GIF, WEBP, BMP, TIFF — anything the Vision API accepts.
Files larger than 20 MB are rejected by the API; this parser raises
``ValueError`` for oversized inputs so the caller can skip gracefully.

Cost note
---------
GPT-4o processes images in 512-px tiles. ``detail="auto"`` lets the model
choose resolution. For most document scans ``detail="low"`` (~85 tokens,
$0.0002) is sufficient; leave ``detail="auto"`` for diagrams/charts.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from datetime import datetime, timezone
from uuid import uuid4

from shared.azure_clients import get_openai_client
from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)

# GPT-4o Vision API enforces a 20 MB per-image limit.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

_EXTRACT_PROMPT = """\
You are a document digitisation assistant. Analyse this image and extract ALL content.

Return the content as plain text, structured as follows:
- If there is a visible document title, output it on the first line prefixed with "TITLE: "
- Reproduce any printed or handwritten text verbatim, preserving paragraph breaks
- For tables: output a Markdown table followed by a plain-English summary
- For charts/diagrams/graphs: write a clear description of what the visual shows,
  including axis labels, data ranges, trends, and any annotations
- For forms: output each field label and its filled value on a separate line
- Omit decorative elements (borders, logos, background patterns) unless they carry meaning

Do not add commentary, introductions, or apologies. Output only the extracted content.\
"""

# Minimum text length (chars) to produce child chunks; shorter content → parent only.
_MIN_CHILD_CONTENT = 200
# Minimum paragraph length to become its own child chunk.
_MIN_PARAGRAPH_CHARS = 40


def _detect_mime(doc_name: str, file_bytes: bytes) -> str:
    """Infer MIME type from filename extension, with fallback to image/png."""
    ext = "." + doc_name.lower().rsplit(".", 1)[-1] if "." in doc_name else ""
    mime_map = {
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".webp": "image/webp",
        ".bmp":  "image/bmp",
        ".tiff": "image/tiff",
        ".tif":  "image/tiff",
    }
    return mime_map.get(ext, "image/png")


def _call_vision(image_b64: str, mime: str) -> str:
    """Send the image to GPT-4o Vision and return the raw text response."""
    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_VISION_DEPLOYMENT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                            "detail": "auto",
                        },
                    },
                    {"type": "text", "text": _EXTRACT_PROMPT},
                ],
            }
        ],
        max_tokens=4096,
        temperature=0,
    )
    return resp.choices[0].message.content or ""


def _split_paragraphs(text: str) -> list[str]:
    """Split extracted text into non-empty paragraphs."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def parse_image(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """Parse an image file via GPT-4o Vision and return a list of RawChunks.

    Args:
        file_bytes: Raw bytes of the image file.
        doc_name:   File name (e.g. ``"org_chart.png"``).
        doc_url:    SharePoint URL to the file.
        domain:     Business domain (``"hr"``, ``"ops"``, etc.).
        blob_path:  Path in the raw-documents blob container.

    Returns:
        List of :class:`~shared.models.RawChunk` objects (1 parent + N children).

    Raises:
        ValueError: If the file exceeds the 20 MB API limit.
    """
    if len(file_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image {doc_name} is {len(file_bytes) / 1024 / 1024:.1f} MB — "
            f"exceeds the 20 MB GPT-4o Vision limit."
        )

    ingested_at = datetime.now(timezone.utc).isoformat()
    mime = _detect_mime(doc_name, file_bytes)
    image_b64 = base64.b64encode(file_bytes).decode("ascii")

    logger.info(
        "Calling GPT-4o Vision for image doc_name=%s size=%dKB mime=%s",
        doc_name, len(file_bytes) // 1024, mime,
    )
    raw_text = _call_vision(image_b64, mime)

    if not raw_text.strip():
        logger.warning("GPT-4o Vision returned empty content for doc_name=%s", doc_name)
        raw_text = "[No extractable content]"

    # Extract title line if present.
    title = doc_name
    lines = raw_text.split("\n")
    if lines and lines[0].startswith("TITLE:"):
        title = lines[0].removeprefix("TITLE:").strip()
        raw_text = "\n".join(lines[1:]).strip()

    chunks: list[RawChunk] = []
    parent_id = str(uuid4())

    # Parent chunk — full extracted text, no vector (used for retrieval context).
    parent = RawChunk(
        chunk_id=parent_id,
        parent_id="",
        chunk_type=ChunkType.PARAGRAPH,
        domain=domain,
        doc_name=doc_name,
        source=doc_name,
        doc_url=doc_url,
        file_type="image",
        blob_path=blob_path,
        ingested_at=ingested_at,
        title=title,
        content=raw_text,
    )
    chunks.append(parent)

    # Child chunks — per paragraph, embedded for similarity search.
    if len(raw_text) >= _MIN_CHILD_CONTENT:
        paragraphs = _split_paragraphs(raw_text)
        for idx, para in enumerate(paragraphs):
            if len(para) < _MIN_PARAGRAPH_CHARS:
                continue
            child = RawChunk(
                chunk_id=str(uuid4()),
                parent_id=parent_id,
                chunk_type=ChunkType.PARAGRAPH,
                domain=domain,
                doc_name=doc_name,
                source=doc_name,
                doc_url=doc_url,
                file_type="image",
                blob_path=blob_path,
                ingested_at=ingested_at,
                title=title,
                content=para,
                page_number=idx + 1,  # paragraph index as page proxy
            )
            chunks.append(child)

        # If no paragraph was long enough, produce one child from full text.
        if len(chunks) == 1:
            chunks.append(RawChunk(
                chunk_id=str(uuid4()),
                parent_id=parent_id,
                chunk_type=ChunkType.PARAGRAPH,
                domain=domain,
                doc_name=doc_name,
                source=doc_name,
                doc_url=doc_url,
                file_type="image",
                blob_path=blob_path,
                ingested_at=ingested_at,
                title=title,
                content=raw_text,
            ))

    logger.info("Image parsed: %s → %d chunks", doc_name, len(chunks))
    return chunks
