"""
Processing pipeline steps and workflow for the Processing Agent.

Each public function is either a MAF ``@step`` (atomic, retryable unit of work)
or a plain async helper called directly by the Service Bus listener when
concurrent execution is required (the MAF workflow wrapper is not safe to call
concurrently on the same task).

Pipeline order
--------------
1. :func:`download_raw_file`       — fetch bytes from raw-documents Blob
2. :func:`run_parser`              — route to correct parser, get list[RawChunk]
3. :func:`upload_processed_chunks` — write chunk JSON to processed-chunks Blob
4. :func:`queue_embedding_task`    — push task to embedding-queue Service Bus
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict

from agent_framework import step, workflow

from agents.processing.blob_ops import (
    delete_blobs,
    download_blob,
    sha256_already_indexed,
    upload_blob,
)
from processors.dispatcher import parse_document
from shared.config import settings
from shared.models import ProcessingTask, RawChunk
from shared.service_bus import send_to_queue

logger = logging.getLogger(__name__)

# 5-minute hard cap on parser execution — corrupted files can hang LLM retries
# indefinitely without this guard.
_PARSER_TIMEOUT_SECONDS = 300


@step
async def download_raw_file(task: ProcessingTask) -> bytes:
    """Download the raw file from Blob Storage.

    Args:
        task: Contains ``domain`` and ``doc_name`` to build the blob path.

    Returns:
        Raw file bytes.
    """
    logger.info(
        "Downloading raw file: %s",
        task.doc_name,
        extra={"task_id": task.task_id, "doc_name": task.doc_name},
    )
    return await download_blob(
        settings.AZURE_STORAGE_CONTAINER_RAW,
        f"{task.domain}/{task.doc_name}",
    )


@step
async def run_parser(file_bytes: bytes, task: ProcessingTask) -> list[RawChunk]:
    """Route ``file_bytes`` to the correct parser and return a list of chunks.

    Runs the parser in a thread (via ``asyncio.to_thread``) because all parsers
    are synchronous and may be CPU-bound. Enforces a hard timeout to prevent a
    single corrupted file from stalling the listener indefinitely.

    Also stamps ``file_sha256`` onto every chunk so it is searchable in the index.

    Args:
        file_bytes: Raw bytes of the file to parse.
        task:       Provides ``doc_name``, ``doc_url``, ``domain``, and ``file_sha256``.

    Returns:
        List of :class:`~shared.models.RawChunk` objects ready for embedding.

    Raises:
        RuntimeError: If the parser exceeds ``_PARSER_TIMEOUT_SECONDS``.
    """
    logger.info(
        "Parsing %s (%s)",
        task.doc_name, task.file_type,
        extra={"task_id": task.task_id, "doc_name": task.doc_name},
    )
    try:
        chunks = await asyncio.wait_for(
            asyncio.to_thread(
                parse_document,
                file_bytes,
                task.doc_name,
                task.doc_url,
                task.domain,
                f"{task.domain}/{task.doc_name}",
            ),
            timeout=_PARSER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Parser timed out after {_PARSER_TIMEOUT_SECONDS}s for doc_name={task.doc_name} "
            f"— file may be corrupted or contain content that causes LLM retries to stall"
        )

    if task.file_sha256:
        for chunk in chunks:
            chunk.file_sha256 = task.file_sha256

    return chunks


@step
async def upload_processed_chunks(chunks: list[RawChunk], task: ProcessingTask) -> str:
    """Serialise ``chunks`` to JSON and upload to the processed-chunks Blob container.

    Args:
        chunks: Parsed chunks to store.
        task:   Provides ``domain`` and ``doc_name`` to build the blob path.

    Returns:
        Blob path of the uploaded JSON (e.g. ``hr/Leave Policy 2024.pdf.json``).
    """
    blob_path = f"{task.domain}/{task.doc_name}.json"
    data = json.dumps([asdict(c) for c in chunks], ensure_ascii=False).encode("utf-8")
    await upload_blob(settings.AZURE_STORAGE_CONTAINER_PROCESSED, blob_path, data)
    logger.info(
        "Stored %d chunks to blob: %s",
        len(chunks), blob_path,
        extra={"task_id": task.task_id, "chunk_count": len(chunks)},
    )
    return blob_path


@step
async def queue_embedding_task(
    task: ProcessingTask,
    processed_blob_path: str,
    chunk_count: int,
) -> None:
    """Send an embedding task to the Service Bus embedding queue.

    Args:
        task:                 Source processing task (provides IDs and metadata).
        processed_blob_path:  Blob path of the uploaded chunk JSON.
        chunk_count:          Number of chunks in the processed blob.
    """
    embedding_task = {
        "task_id":             task.task_id,
        "domain":              task.domain,
        "doc_name":            task.doc_name,
        "doc_url":             task.doc_url,
        "file_type":           task.file_type,
        "processed_blob_path": processed_blob_path,
        "chunk_count":         chunk_count,
        "is_delete":           task.is_delete,
    }
    await send_to_queue(
        settings.SB_QUEUE_EMBEDDING,
        embedding_task,
        correlation_id=task.task_id,
    )
    logger.info(
        "Queued embedding task for doc_name=%s chunks=%d",
        task.doc_name, chunk_count,
        extra={"task_id": task.task_id},
    )


async def run_processing(task: ProcessingTask) -> dict:
    """Execute the full processing pipeline for a single task.

    This is the function called directly by the Service Bus listener because
    it is safe to call concurrently. The MAF ``@workflow`` wrapper below should
    not be called concurrently on the same instance.

    Args:
        task: Fully populated :class:`~shared.models.ProcessingTask`.

    Returns:
        Result dict with ``status``, ``doc_name``, ``sha256``, ``chunk_count``,
        and ``blob_path`` keys (subset present depends on the path taken).
    """
    if task.is_delete:
        await delete_blobs(task.domain, task.doc_name)
        await queue_embedding_task(task, "", 0)
        return {"status": "delete_forwarded", "doc_name": task.doc_name}

    t_download = time.monotonic()
    file_bytes = await download_raw_file(task)
    logger.info(
        "Downloaded doc_name=%s in %.1fs",
        task.doc_name, time.monotonic() - t_download,
        extra={"task_id": task.task_id, "doc_name": task.doc_name},
    )

    if await sha256_already_indexed(task.doc_name, task.file_sha256):
        return {
            "status":      "skipped_duplicate",
            "doc_name":    task.doc_name,
            "sha256":      task.file_sha256[:12] if task.file_sha256 else "",
            "chunk_count": 0,
        }

    t_parse = time.monotonic()
    chunks = await run_parser(file_bytes, task)
    logger.info(
        "Parsed doc_name=%s in %.1fs chunks=%d",
        task.doc_name, time.monotonic() - t_parse, len(chunks),
        extra={"task_id": task.task_id, "doc_name": task.doc_name, "chunk_count": len(chunks)},
    )

    t_upload = time.monotonic()
    processed_blob_path = await upload_processed_chunks(chunks, task)
    logger.info(
        "Uploaded chunks doc_name=%s in %.1fs",
        task.doc_name, time.monotonic() - t_upload,
        extra={"task_id": task.task_id, "doc_name": task.doc_name},
    )

    await queue_embedding_task(task, processed_blob_path, len(chunks))

    return {
        "status":      "processed",
        "doc_name":    task.doc_name,
        "sha256":      task.file_sha256[:12] if task.file_sha256 else "",
        "chunk_count": len(chunks),
        "blob_path":   processed_blob_path,
    }


@workflow(name="processing_workflow")
async def processing_workflow(task: ProcessingTask) -> dict:
    """MAF workflow wrapper around :func:`run_processing`.

    Do not call ``.run()`` on this concurrently — use :func:`run_processing`
    directly from the Service Bus listener instead.
    """
    return await run_processing(task)
