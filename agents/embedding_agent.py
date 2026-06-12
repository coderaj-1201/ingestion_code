"""
Embedding Agent
===============
MAF Functional Workflow (@workflow / @step).

Listens to SB embedding-tasks queue.
For each task:
  1. Download processed chunks JSON from Blob
  2. Embed ONLY child chunks (parent_id != "") using text-embedding-ada-002
     → Parents are stored for context retrieval, not embedded
     → Tables: embed content (NL summary), keep table_raw as metadata
  3. Batch upload to AI Search index ()
  4. On delete signal: remove all chunks for that doc_name from index

Batching:
  - Embed in batches of 16 (API limit awareness)
  - Upload to Search in batches of 100 (Search batch limit is 1000, 100 is safe)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict

import uvicorn
from agent_framework import step, workflow
from azure.search.documents import SearchClient
from azure.search.documents.models import IndexDocumentsBatch
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient
from fastapi import FastAPI

from shared.azure_clients import get_openai_client, get_search_client
from shared.config import settings
from shared.logging_config import configure_logging, get_logger
from shared.models import RawChunk

configure_logging("rag-embedding")
logger = get_logger(__name__)

_EMBED_BATCH_SIZE  = 16    # embed this many texts per OpenAI call
_SEARCH_BATCH_SIZE = 100   # upload this many docs per Search call


# ── Blob helper ───────────────────────────────────────────────────────────────

async def _download_processed_chunks(blob_path: str) -> list[RawChunk]:
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    async with AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    ) as svc:
        blob   = svc.get_container_client(settings.AZURE_STORAGE_CONTAINER_PROCESSED).get_blob_client(blob_path)
        stream = await blob.download_blob()
        data   = await stream.readall()

    raw_list = json.loads(data.decode("utf-8"))
    return [RawChunk(**item) for item in raw_list]


# ── Embedding ─────────────────────────────────────────────────────────────────

@step
async def embed_chunks(chunks: list[RawChunk]) -> list[tuple[RawChunk, list[float]]]:
    """
    Embed all chunks in batches.
    Returns list of (chunk, embedding_vector) tuples.
    """
    oai     = get_openai_client()
    results = []

    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch  = chunks[i : i + _EMBED_BATCH_SIZE]
        texts  = [c.content for c in batch]

        resp = await asyncio.to_thread(
            oai.embeddings.create,
            input=texts,
            model=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        )
        for chunk, emb_data in zip(batch, resp.data):
            results.append((chunk, emb_data.embedding))

        logger.debug("Embedded batch %d-%d of %d", i, i + len(batch), len(chunks))

    return results


# ── Search upload ─────────────────────────────────────────────────────────────

@step
async def upload_to_search(embedded: list[tuple[RawChunk, list[float]]], doc_name: str) -> int:
    """
    Build search documents and batch-upload to AI Search.
    Returns count of successfully uploaded documents.
    """
    search = get_search_client()
    total  = 0

    for i in range(0, len(embedded), _SEARCH_BATCH_SIZE):
        batch = embedded[i : i + _SEARCH_BATCH_SIZE]
        docs  = []
        for chunk, vector in batch:
            doc = chunk.to_search_doc()
            doc["content_vector"] = vector
            docs.append(doc)

        results = await asyncio.to_thread(search.upload_documents, docs)
        succeeded = sum(1 for r in results if r.succeeded)
        failed    = sum(1 for r in results if not r.succeeded)
        total    += succeeded

        if failed:
            logger.warning(
                "Search upload: %d succeeded, %d failed for doc=%s batch=%d",
                succeeded, failed, doc_name, i,
            )
        else:
            logger.debug(
                "Search upload batch %d: %d docs for doc=%s",
                i, succeeded, doc_name,
            )

    return total


@step
async def delete_from_search(doc_name: str) -> int:
    """
    Remove all chunks for a document from AI Search.
    Uses OData filter to find all chunk_ids then batch-deletes.
    """
    search  = get_search_client()
    deleted = 0

    # Fetch all chunk ids for this doc_name.
    # Escape single quotes in OData filter to prevent injection.
    safe_doc_name = doc_name.replace("'", "''")
    results = await asyncio.to_thread(
        search.search,
        search_text="*",
        filter=f"doc_name eq '{safe_doc_name}'",
        select=["id"],
        top=1000,
    )
    ids = [r["id"] for r in results]

    if not ids:
        logger.info("No chunks found to delete for doc_name=%s", doc_name)
        return 0

    for i in range(0, len(ids), _SEARCH_BATCH_SIZE):
        batch   = [{"id": doc_id} for doc_id in ids[i : i + _SEARCH_BATCH_SIZE]]
        result  = await asyncio.to_thread(search.delete_documents, batch)
        deleted += sum(1 for r in result if r.succeeded)

    logger.info("Deleted %d chunks for doc_name=%s", deleted, doc_name)
    return deleted


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow(name="embedding_workflow")
async def embedding_workflow(task: dict) -> dict:
    doc_name  = task["doc_name"]
    is_delete = task.get("is_delete", False)

    logger.info(
        "embedding_workflow doc_name=%s is_delete=%s",
        doc_name, is_delete,
        extra={"task_id": task.get("task_id"), "doc_name": doc_name},
    )

    if is_delete:
        deleted = await delete_from_search(doc_name)
        return {"status": "deleted", "doc_name": doc_name, "deleted_chunks": deleted}

    # Download processed chunks from Blob
    chunks = await _download_processed_chunks(task["processed_blob_path"])

    if not chunks:
        logger.warning("No chunks found in blob for doc_name=%s", doc_name)
        return {"status": "empty", "doc_name": doc_name}

    # Only embed child chunks (parent_id != "")
    # Parents are stored for context but not embedded/searched directly
    child_chunks  = [c for c in chunks if c.parent_id != ""]
    parent_chunks = [c for c in chunks if c.parent_id == ""]

    logger.info(
        "doc_name=%s total=%d parents=%d children=%d",
        doc_name, len(chunks), len(parent_chunks), len(child_chunks),
        extra={"chunk_count": len(chunks), "doc_name": doc_name},
    )

    # Embed children
    embedded_children = await embed_chunks(child_chunks)

    # Also store parents in Search (no vector — used for context retrieval by parent_id)
    parent_docs = []
    for parent in parent_chunks:
        doc = parent.to_search_doc()
        doc["content_vector"] = []   # empty — parents not vector-searched
        parent_docs.append(doc)

    # Upload parents first (children reference them via parent_id)
    if parent_docs:
        await asyncio.to_thread(get_search_client().upload_documents, parent_docs)
        logger.debug("Uploaded %d parent chunks for doc_name=%s", len(parent_docs), doc_name)

    # Upload embedded children
    uploaded = await upload_to_search(embedded_children, doc_name)

    return {
        "status":          "embedded",
        "doc_name":        doc_name,
        "total_chunks":    len(chunks),
        "parent_chunks":   len(parent_chunks),
        "child_chunks":    len(child_chunks),
        "uploaded":        uploaded,
    }


# ── Service Bus listener ──────────────────────────────────────────────────────

async def _sb_listener():
    logger.info("Embedding Agent SB listener starting on queue '%s'", settings.SB_QUEUE_EMBEDDING)
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient

    while True:
        try:
            credential = (
                ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
                else AzureCliCredential()
            )
            if settings.AZURE_SERVICE_BUS_CONNECTION_STR:
                sb = AsyncSBClient.from_connection_string(
                    settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
                )
            else:
                sb = AsyncSBClient(
                    fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
                    credential=credential,
                )
            async with sb:
                async with sb.get_queue_receiver(
                    settings.SB_QUEUE_EMBEDDING, max_wait_time=30
                ) as receiver:
                    async for msg in receiver:
                        try:
                            task       = json.loads(b"".join(msg.body))
                            result_obj = await embedding_workflow.run(task)
                            outputs    = result_obj.get_outputs()
                            result     = outputs[0] if outputs else {}
                            logger.info("Embedding complete: %s", result)
                            await receiver.complete_message(msg)
                        except Exception as exc:
                            logger.error("Embedding failed: %s", exc, exc_info=True)
                            await receiver.abandon_message(msg)
        except Exception as exc:
            logger.error("SB listener crashed, restarting in 5s: %s", exc, exc_info=True)
            await asyncio.sleep(5)


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_sb_listener())
    logger.info("Embedding Agent started — SB listener active.")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Embedding Agent stopped.")


app = FastAPI(title="RAG Embedding Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "embedding"}


if __name__ == "__main__":
    uvicorn.run("agents.embedding_agent:app", host="0.0.0.0", port=8012, reload=False)
