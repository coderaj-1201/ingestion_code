"""
Video Parser — Azure AI Content Understanding
=============================================

Uses the Azure AI Content Understanding SDK (preview) to extract shot-level
transcripts and scene descriptions from video files.

Why Content Understanding (not Video Indexer)
---------------------------------------------
Content Understanding outputs **RAG-ready Markdown** directly — each video shot
becomes a structured section with transcript + visual description.  Video Indexer
outputs a JSON metadata blob that needs significant glue code to produce chunks.
Content Understanding is the correct Azure service for RAG pipelines as of 2025.

Pipeline
--------
1. **Analyzer setup** (one-time per process) — ``_get_or_create_analyzer()``
   creates an Azure backend analyzer from ``resources/video_analyzer_template.json``
   and caches its ID.  Subsequent calls reuse the same analyzer.

2. **File upload** — video bytes are written to a ``NamedTemporaryFile`` so the
   SDK can submit them to the Content Understanding API.

3. **Analysis** — ``client.begin_analyze`` submits the job; the SDK polls until
   the result is available.

4. **Chunk production** — the Markdown result is split on ``# Shot`` headers.
   Each shot becomes a parent + child chunk pair (same pattern as other parsers).

Configuration
-------------
Set ``CONTENT_UNDERSTANDING_ENDPOINT`` in ``.env`` or environment.
Auth uses ``DefaultAzureCredential`` — no API key required.
The container app's Managed Identity needs the ``Cognitive Services User`` role
on the Azure AI Services resource.

Supported formats: MP4, MOV, AVI, MKV, FLV, WMV, MXF.
Free tier: 10 hours/month — enough for dev/test.
Pricing (S1): ~$0.035 per video minute.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)

# Path to the analyzer template shipped with this repo.
_TEMPLATE_PATH = Path(__file__).parent.parent / "resources" / "video_analyzer_template.json"

# Module-level analyzer ID cache — created once per process, reused for all
# subsequent calls.  Protected by a lock so concurrent first-calls don't race.
_ANALYZER_ID: str | None = None
_ANALYZER_LOCK = threading.Lock()

# Minimum characters in a shot section to produce a child chunk.
_MIN_SHOT_CHARS = 60

# Regex that matches the Markdown shot headers emitted by Content Understanding.
# Examples: "# Shot 0:00 - 0:45", "## Shot 1:30 - 2:15"
_SHOT_HEADER_RE = re.compile(r"^#{1,3}\s+Shot\b", re.MULTILINE | re.IGNORECASE)


def _get_client():
    """Construct an AzureContentUnderstandingClient using DefaultAzureCredential.

    Raises RuntimeError if CONTENT_UNDERSTANDING_ENDPOINT is not configured.
    """
    from azure.ai.contentunderstanding import AzureContentUnderstandingClient
    from azure.identity import DefaultAzureCredential

    endpoint = str(settings.CONTENT_UNDERSTANDING_ENDPOINT or "")
    if not endpoint:
        raise RuntimeError(
            "CONTENT_UNDERSTANDING_ENDPOINT is not configured. "
            "Set it in .env to enable video parsing."
        )
    return AzureContentUnderstandingClient(
        endpoint=endpoint.rstrip("/"),
        credential=DefaultAzureCredential(),
    )


def _get_or_create_analyzer() -> str:
    """Return the cached analyzer ID, creating it on the first call.

    The analyzer is an Azure backend resource that must exist before
    ``begin_analyze`` can be called.  Creation is idempotent — if the
    analyzer already exists the API returns it unchanged.

    This function is thread-safe: a lock prevents concurrent creation
    races when multiple files are processed simultaneously.
    """
    global _ANALYZER_ID
    if _ANALYZER_ID:
        return _ANALYZER_ID

    with _ANALYZER_LOCK:
        if _ANALYZER_ID:
            return _ANALYZER_ID

        client = _get_client()
        analyzer_id = "rag-video-analyzer"

        logger.info("Creating Content Understanding video analyzer id=%s", analyzer_id)
        response = client.begin_create_analyzer(
            analyzer_id=analyzer_id,
            analyzer_template_path=str(_TEMPLATE_PATH),
        )
        client.poll_result(response)
        logger.info("Video analyzer ready id=%s", analyzer_id)

        _ANALYZER_ID = analyzer_id
    return _ANALYZER_ID


def _split_shots(markdown: str) -> list[tuple[str, str]]:
    """Split Content Understanding Markdown output into (header, body) pairs.

    Each shot section starts with a ``# Shot`` header.  If no shot headers are
    found the entire Markdown is returned as a single unnamed section.

    Returns:
        List of ``(shot_header, shot_body)`` tuples with stripped whitespace.
    """
    parts = _SHOT_HEADER_RE.split(markdown)
    headers = _SHOT_HEADER_RE.findall(markdown)

    if not headers:
        return [("Shot 0:00", markdown.strip())]

    shots: list[tuple[str, str]] = []
    for header, body in zip(headers, parts[1:]):
        # Re-attach the header prefix that was consumed by the split.
        first_line_end = body.find("\n")
        if first_line_end != -1:
            shot_title = (header + body[:first_line_end]).strip("#").strip()
            shot_body  = body[first_line_end:].strip()
        else:
            shot_title = (header + body).strip("#").strip()
            shot_body  = ""
        shots.append((shot_title, shot_body or shot_title))

    return shots


def parse_video(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """Parse a video file via Azure AI Content Understanding and return RawChunks.

    Each video shot produces a parent chunk (full shot text) and a child chunk
    (same content, embedded for similarity search).  Short shots that produce
    less than ``_MIN_SHOT_CHARS`` characters are merged with the next shot.

    Args:
        file_bytes: Raw video bytes (MP4, MOV, AVI, MKV, FLV, WMV, MXF).
        doc_name:   File name, e.g. ``"onboarding_training.mp4"``.
        doc_url:    SharePoint URL to the file.
        domain:     Business domain (``"hr"``, ``"ops"``, etc.).
        blob_path:  Path in the raw-documents blob container.

    Returns:
        list[RawChunk] — (parent + child) × number_of_shots.

    Raises:
        RuntimeError: If Azure Content Understanding config vars are missing.
    """
    ingested_at = datetime.now(timezone.utc).isoformat()
    suffix      = Path(doc_name).suffix or ".mp4"

    # Write to a temp file so the SDK can read it from disk.
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        analyzer_id = _get_or_create_analyzer()
        client      = _get_client()

        logger.info(
            "Content Understanding: submitting doc_name=%s size=%dMB analyzer=%s",
            doc_name, len(file_bytes) // (1024 * 1024), analyzer_id,
        )

        response = client.begin_analyze(
            analyzer_id=analyzer_id,
            input_source=tmp_path,
        )
        result = client.poll_result(response)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Extract the Markdown from the API result.
    try:
        markdown: str = result["result"]["contents"][0]["markdown"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Unexpected Content Understanding response for doc_name=%s: %s",
            doc_name, exc,
        )
        markdown = str(result)

    if not markdown.strip():
        logger.warning("Content Understanding returned empty Markdown for doc_name=%s", doc_name)
        markdown = "[No content extracted from video]"

    shots  = _split_shots(markdown)
    chunks: list[RawChunk] = []

    title = doc_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")

    base = dict(
        domain=domain,
        doc_name=doc_name,
        source=doc_name,
        doc_url=doc_url,
        file_type="video",
        blob_path=blob_path,
        ingested_at=ingested_at,
        title=title,
    )

    for shot_idx, (shot_header, shot_body) in enumerate(shots):
        if not shot_body.strip():
            continue

        parent_id = str(uuid4())

        # Parent — full shot text, no vector; provides context for retrieval.
        chunks.append(RawChunk(
            chunk_id=parent_id,
            parent_id="",
            chunk_type=ChunkType.PARAGRAPH,
            content=shot_body,
            section_heading=shot_header,
            page_number=shot_idx + 1,
            **base,
        ))

        # Child — same content, embedded for similarity search.
        if len(shot_body) >= _MIN_SHOT_CHARS:
            chunks.append(RawChunk(
                chunk_id=str(uuid4()),
                parent_id=parent_id,
                chunk_type=ChunkType.PARAGRAPH,
                content=shot_body,
                section_heading=shot_header,
                page_number=shot_idx + 1,
                **base,
            ))

    if not chunks:
        # Safety net: emit one parent+child from the full Markdown if shot
        # splitting produced nothing useful.
        parent_id = str(uuid4())
        chunks = [
            RawChunk(chunk_id=parent_id, parent_id="", chunk_type=ChunkType.PARAGRAPH,
                     content=markdown, **base),
            RawChunk(chunk_id=str(uuid4()), parent_id=parent_id, chunk_type=ChunkType.PARAGRAPH,
                     content=markdown, **base),
        ]

    logger.info(
        "Video parsed: %s → %d shots → %d chunks",
        doc_name, len(shots), len(chunks),
    )
    return chunks
