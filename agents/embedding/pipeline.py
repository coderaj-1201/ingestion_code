"""
Embedding pipeline for the Embedding Agent.

Steps
-----
1. :func:`download_processed_chunks` — fetch chunk JSON from processed-chunks Blob
2. :func:`embed_chunks`              — call Azure OpenAI embeddings API in batches
3. Upload to AI Search via :mod:`agents.embedding.search_ops`

Parent vs child chunks
-----------------------
Parsers produce a two-level hierarchy:
  - **Parent chunks** (``parent_id == ""``) — larger context windows stored
    for retrieval context; they are uploaded to Search *without* a vector.
  - **Child chunks** (``parent_id != ""``) — smaller windows that are actually
    embedded and used for similarity search.

This split allows hybrid search to retrieve the full parent context when a
child chunk matches, giving the LLM more surrounding text to work with.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from agent_framework import step, workflow

from agents.embedding.search_ops import (
    check_upload_results,
    delete_from_search,
    upload_to_search,
)
from shared.azure_clients import get_openai_client, get_search_client
from shared.config import settings
from shared.models import RawChunk

logger = logging.getLogger(__name__)

# Embed this many texts per OpenAI call. Kept low to avoid hitting the per-request
# token limit for the embedding API.
_EMBED_BATCH_SIZE = 16


async def download_processed_chunks(blob_path: str) -> list[RawChunk]:
    """Download and deserialise the processed chunk JSON from Blob Storage.

    Args:
        blob_path: Path within the processed-chunks container,
                   e.g. ``hr/Leave Policy 2024.pdf.json``.

    Returns:
        List of :class:`~shared.models.RawChunk` objects.
    """
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient

    async with DefaultAzureCredential() as credential, AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    ) as svc:
        blob = (
            svc.get_container_client(settings.AZURE_STORAGE_CONTAINER_PROCESSED)
            .get_blob_client(blob_path)
        )
        stream = await blob.download_blob()
        data = await stream.readall()

    raw_list = json.loads(data.decode("utf-8"))
    return [RawChunk(**item) for item in raw_list]


@step
async def embed_chunks(chunks: list[RawChunk]) -> list[tuple[RawChunk, list[float]]]:
    """Embed all chunks using the configured Azure OpenAI embedding deployment.

    Processes chunks in batches of ``_EMBED_BATCH_SIZE`` to stay within the
    per-request token limit of the embeddings API.

    Args:
        chunks: Child chunks to embed (parent chunks are uploaded without vectors).

    Returns:
        List of ``(chunk, embedding_vector)`` tuples in the same order as ``chunks``.
    """
    oai = get_openai_client()
    results = []

    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[i: i + _EMBED_BATCH_SIZE]
        texts = [c.content for c in batch]

        resp = await asyncio.to_thread(
            oai.embeddings.create,
            input=texts,
            model=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )
        for chunk, emb_data in zip(batch, resp.data):
            results.append((chunk, emb_data.embedding))

        logger.debug("Embedded batch %d-%d of %d", i, i + len(batch), len(chunks))

    return results


async def run_embedding(task: dict) -> dict:
    """Execute the full embedding pipeline for a single task.

    Called directly by the Service Bus listener (safe for concurrent execution).
    The MAF ``@workflow`` wrapper :func:`embedding_workflow` delegates here.

    Delete path
    -----------
    When ``task["is_delete"]`` is ``True``, all chunks for the document are
    removed from AI Search. No blob operations are performed here — the
    Processing Agent already deleted blobs before forwarding the delete signal.

    Args:
        task: Dict decoded from the Service Bus message body. Expected keys:
              ``doc_name``, ``is_delete``, ``processed_blob_path``,
              ``task_id`` (optional).

    Returns:
        Result dict describing the outcome (``status``, chunk counts, etc.).
    """
    doc_name = task["doc_name"]
    doc_path = task.get("doc_path") or doc_name
    is_delete = task.get("is_delete", False)

    logger.info(
        "embedding doc_name=%s doc_path=%s is_delete=%s",
        doc_name, doc_path, is_delete,
        extra={"task_id": task.get("task_id"), "doc_name": doc_name},
    )

    if is_delete:
        deleted = await delete_from_search(doc_path)
        return {"status": "deleted", "doc_name": doc_name, "deleted_chunks": deleted}

    chunks = await download_processed_chunks(task["processed_blob_path"])

    if not chunks:
        logger.warning("No chunks found in blob for doc_name=%s", doc_name)
        return {"status": "empty", "doc_name": doc_name}

    child_chunks = [c for c in chunks if c.parent_id != ""]
    parent_chunks = [c for c in chunks if c.parent_id == ""]

    logger.info(
        "doc_name=%s total=%d parents=%d children=%d",
        doc_name, len(chunks), len(parent_chunks), len(child_chunks),
        extra={"chunk_count": len(chunks), "doc_name": doc_name},
    )

    t_embed = time.monotonic()
    embedded_children = await embed_chunks(child_chunks)
    logger.info(
        "Embedded %d child chunks for doc_name=%s in %.1fs",
        len(child_chunks), doc_name, time.monotonic() - t_embed,
        extra={"doc_name": doc_name, "task_id": task.get("task_id")},
    )

    # Upload parent chunks without vectors — they exist for context retrieval only.
    if parent_chunks:
        parent_docs = []
        for parent in parent_chunks:
            doc = parent.to_search_doc()
            doc["content_vector"] = []
            parent_docs.append(doc)

        try:
            parent_results = await asyncio.to_thread(
                get_search_client().upload_documents, parent_docs
            )
        except Exception as exc:
            if "doc_path" in str(exc):
                logger.debug("Index missing doc_path field — retrying parent chunks without it")
                for d in parent_docs:
                    d.pop("doc_path", None)
                parent_results = await asyncio.to_thread(
                    get_search_client().upload_documents, parent_docs
                )
            else:
                raise
        check_upload_results(parent_results, f"parent chunks for {doc_name}")
        logger.debug("Uploaded %d parent chunks for doc_name=%s", len(parent_docs), doc_name)

    t_upload = time.monotonic()
    uploaded = await upload_to_search(embedded_children, doc_name)
    logger.info(
        "Uploaded %d child chunks for doc_name=%s in %.1fs",
        uploaded, doc_name, time.monotonic() - t_upload,
        extra={"doc_name": doc_name, "task_id": task.get("task_id")},
    )

    return {
        "status":        "embedded",
        "doc_name":      doc_name,
        "total_chunks":  len(chunks),
        "parent_chunks": len(parent_chunks),
        "child_chunks":  len(child_chunks),
        "uploaded":      uploaded,
    }


@workflow(name="embedding_workflow")
async def embedding_workflow(task: dict) -> dict:
    """MAF workflow wrapper around :func:`run_embedding`.

    Do not call ``.run()`` on this concurrently — use :func:`run_embedding`
    directly from the Service Bus listener instead.
    """
    return await run_embedding(task)
