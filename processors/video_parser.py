"""
Video Parser — Azure Video Indexer
====================================

Uses the Azure Video Indexer REST API to extract transcripts from video files.
Transcript segments are grouped into ~60-second windows and formatted as text
sections for the RAG pipeline.

Configuration (environment variables)
--------------------------------------
AZURE_VIDEO_INDEXER_SUBSCRIPTION_ID  : Azure subscription ID
AZURE_VIDEO_INDEXER_RESOURCE_GROUP   : Resource group containing the VI account
AZURE_VIDEO_INDEXER_ACCOUNT_NAME     : ARM resource name of the VI account
AZURE_VIDEO_INDEXER_ACCOUNT_ID       : Video Indexer account GUID (from VI portal)
AZURE_VIDEO_INDEXER_LOCATION         : Azure region, e.g. ``eastus``

Auth uses ``DefaultAzureCredential`` → ARM token → Video Indexer access token.
Assign ``Contributor`` to the Managed Identity on the Video Indexer account.

Supported formats: MP4, MOV, AVI, MKV, FLV, WMV, MXF.
Package required: httpx>=0.27.0 (already in requirements.txt)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from azure.identity import DefaultAzureCredential

from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)

_VI_BASE = "https://api.videoindexer.ai"
_POLL_INTERVAL = 15        # seconds between index-state polls
_SEGMENT_WINDOW = 60.0     # seconds — group transcript lines into ~1-minute chunks


# ── Auth & API helpers ────────────────────────────────────────────────────────

def _get_access_token() -> tuple[str, str, str]:
    """Exchange ARM credentials for a Video Indexer access token.

    Returns:
        (location, account_id, access_token)
    """
    sub_id      = os.environ["AZURE_VIDEO_INDEXER_SUBSCRIPTION_ID"]
    rg          = os.environ["AZURE_VIDEO_INDEXER_RESOURCE_GROUP"]
    account     = os.environ["AZURE_VIDEO_INDEXER_ACCOUNT_NAME"]
    location    = os.environ["AZURE_VIDEO_INDEXER_LOCATION"]
    account_id  = os.environ["AZURE_VIDEO_INDEXER_ACCOUNT_ID"]

    arm_token = DefaultAzureCredential().get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.VideoIndexer/accounts/{account}"
        f"/generateAccessToken?api-version=2024-01-01"
    )
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {arm_token}"},
        json={"permissionType": "Contributor", "scope": "Account"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return location, account_id, resp.json()["accessToken"]


def _upload_video(
    location: str, account_id: str, token: str, file_bytes: bytes, doc_name: str
) -> str:
    """Upload video bytes to Video Indexer; return the assigned video ID."""
    url = f"{_VI_BASE}/{location}/Accounts/{account_id}/Videos"
    resp = httpx.post(
        url,
        params={"accessToken": token, "name": doc_name, "privacy": "Private"},
        files={"file": (doc_name, file_bytes, "application/octet-stream")},
        timeout=600.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _wait_for_index(
    location: str, account_id: str, video_id: str, token: str
) -> dict:
    """Poll the Video Indexer index endpoint until the video is processed."""
    url = f"{_VI_BASE}/{location}/Accounts/{account_id}/Videos/{video_id}/Index"
    while True:
        resp = httpx.get(url, params={"accessToken": token}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state", "")
        if state == "Processed":
            return data
        if state in ("Failed", "Quarantined"):
            raise RuntimeError(f"Video Indexer: video_id={video_id} ended with state={state}")
        logger.debug("Video Indexer: video_id=%s state=%s — waiting %ds", video_id, state, _POLL_INTERVAL)
        time.sleep(_POLL_INTERVAL)


def _delete_video(location: str, account_id: str, video_id: str, token: str) -> None:
    """Remove the video from Video Indexer storage after processing."""
    url = f"{_VI_BASE}/{location}/Accounts/{account_id}/Videos/{video_id}"
    try:
        httpx.delete(url, params={"accessToken": token}, timeout=30.0).raise_for_status()
        logger.debug("Video Indexer: deleted video_id=%s", video_id)
    except Exception as exc:
        logger.warning("Video Indexer: could not delete video_id=%s — %s", video_id, exc)


# ── Transcript → sections ─────────────────────────────────────────────────────

def _fmt_ts(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_secs(ts: str) -> float:
    """Parse ``H:MM:SS`` or ``H:MM:SS.mmm`` to seconds."""
    try:
        parts = ts.split(":")
        secs = float(parts[-1]) + int(parts[-2]) * 60
        if len(parts) == 3:
            secs += int(parts[0]) * 3600
        return secs
    except (ValueError, IndexError):
        return 0.0


def _transcript_to_sections(index: dict) -> list[tuple[str, str]]:
    """Group transcript segments into ~_SEGMENT_WINDOW second windows.

    Returns:
        List of ``(section_heading, transcript_text)`` pairs.
        Heading format: ``"0:00 - 1:00"``.
    """
    try:
        transcript: list[dict] = index["videos"][0]["insights"]["transcript"]
    except (KeyError, IndexError, TypeError):
        return [("0:00", "[No transcript available]")]

    sections: list[tuple[str, str]] = []
    window_start = 0.0
    window_end   = 0.0
    window_lines: list[str] = []

    for seg in transcript:
        text = seg.get("text", "").strip()
        if not text:
            continue
        instances = seg.get("instances", [{}])
        start_secs = _parse_secs(instances[0].get("adjustedStart", "0:00:00"))
        end_secs   = _parse_secs(instances[0].get("adjustedEnd",   "0:00:00"))

        if start_secs - window_start >= _SEGMENT_WINDOW and window_lines:
            heading = f"{_fmt_ts(window_start)} - {_fmt_ts(window_end)}"
            sections.append((heading, " ".join(window_lines)))
            window_lines = []
            window_start = start_secs

        window_lines.append(text)
        window_end = max(window_end, end_secs)

    if window_lines:
        heading = f"{_fmt_ts(window_start)} - {_fmt_ts(window_end)}"
        sections.append((heading, " ".join(window_lines)))

    return sections or [("0:00", "[No transcript available]")]


# ── Public parser ─────────────────────────────────────────────────────────────

def parse_video(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """Parse a video via Azure Video Indexer and return RawChunks.

    Each ~60-second transcript window produces a parent + child chunk pair.

    Args:
        file_bytes: Raw video bytes (MP4, MOV, AVI, MKV, FLV, WMV, MXF).
        doc_name:   File name, e.g. ``"onboarding_training.mp4"``.
        doc_url:    SharePoint URL to the file.
        domain:     Business domain (``"hr"``, ``"ops"``, etc.).
        blob_path:  Path in the raw-documents blob container.

    Returns:
        list[RawChunk] — (parent + child) × number_of_sections.

    Raises:
        RuntimeError: If any required AZURE_VIDEO_INDEXER_* env vars are missing,
                      or if Video Indexer reports a failure state.
    """
    ingested_at = datetime.now(timezone.utc).isoformat()
    title       = doc_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")

    location, account_id, token = _get_access_token()

    logger.info(
        "Video Indexer: uploading doc_name=%s size=%dMB",
        doc_name, len(file_bytes) // (1024 * 1024),
    )
    video_id = _upload_video(location, account_id, token, file_bytes, doc_name)

    try:
        logger.info("Video Indexer: polling video_id=%s", video_id)
        index = _wait_for_index(location, account_id, video_id, token)
    finally:
        _delete_video(location, account_id, video_id, token)

    sections = _transcript_to_sections(index)

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

    chunks: list[RawChunk] = []
    for idx, (heading, body) in enumerate(sections):
        if not body.strip():
            continue
        parent_id = str(uuid4())
        # Parent — full section text, no vector; provides retrieval context.
        chunks.append(RawChunk(
            chunk_id=parent_id,
            parent_id="",
            chunk_type=ChunkType.PARAGRAPH,
            content=body,
            section_heading=heading,
            page_number=idx + 1,
            **base,
        ))
        # Child — same content, embedded for similarity search.
        chunks.append(RawChunk(
            chunk_id=str(uuid4()),
            parent_id=parent_id,
            chunk_type=ChunkType.PARAGRAPH,
            content=body,
            section_heading=heading,
            page_number=idx + 1,
            **base,
        ))

    if not chunks:
        parent_id = str(uuid4())
        fallback  = "[No content extracted from video]"
        chunks = [
            RawChunk(chunk_id=parent_id, parent_id="", chunk_type=ChunkType.PARAGRAPH,
                     content=fallback, **base),
            RawChunk(chunk_id=str(uuid4()), parent_id=parent_id, chunk_type=ChunkType.PARAGRAPH,
                     content=fallback, **base),
        ]

    logger.info(
        "Video parsed: %s → %d sections → %d chunks",
        doc_name, len(sections), len(chunks),
    )
    return chunks
