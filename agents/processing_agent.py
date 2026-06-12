"""
Processing Agent
================
MAF Functional Workflow (@workflow / @step).

Listens to SB processing-tasks queue.
For each task:
  1. Download raw file from Blob (raw-documents/<domain>/<filename>)
  2. Route to correct parser (PDF/DOCX/XLSX/PPTX)
  3. Get list[RawChunk] back
  4. Upload processed JSON to Blob (processed-chunks/<domain>/<filename>.json)
  5. Send EmbeddingTask to SB embedding-tasks queue

On delete signal:
  → Forward delete to embedding-tasks queue (Embedding Agent handles index cleanup)
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
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient
from fastapi import FastAPI

from processors.dispatcher import parse_document
from shared.config import settings
from shared.logging_config import configure_logging, get_logger
from shared.models import ProcessingTask, RawChunk
from shared.service_bus import send_to_queue

configure_logging("rag-processing")
logger = get_logger(__name__)


# ── Blob helpers ──────────────────────────────────────────────────────────────

async def _get_blob_client() -> AsyncBlobClient:
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    return AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    )


async def _download_blob(container: str, blob_path: str) -> bytes:
    async with await _get_blob_client() as svc:
        blob = svc.get_container_client(container).get_blob_client(blob_path)
        stream = await blob.download_blob()
        return await stream.readall()


async def _upload_blob(container: str, blob_path: str, data: bytes) -> None:
    async with await _get_blob_client() as svc:
        blob = svc.get_container_client(container).get_blob_client(blob_path)
        await blob.upload_blob(data, overwrite=True)
        logger.debug("Uploaded processed blob: %s", blob_path)



# ── SHA-256 dedup check (AI Search) ──────────────────────────────────────────

async def _sha256_already_indexed(doc_name: str, sha256: str) -> bool:
    """
    Return True if AI Search already contains at least one chunk for this
    doc_name with a matching file_sha256.  This is the authoritative dedup
    gate — even if blob metadata was lost the Search index is the source of
    truth.
    """
    if not sha256:
        return False
    try:
        from shared.azure_clients import get_search_client
        safe_name = doc_name.replace("'", "''")
        safe_sha  = sha256.replace("'", "''")
        results = await asyncio.to_thread(
            lambda: list(get_search_client().search(
                search_text="*",
                filter=f"doc_name eq '{safe_name}' and file_sha256 eq '{safe_sha}'",
                select=["id"],
                top=1,
            ))
        )
        if results:
            logger.info(
                "Dedup: doc_name=%s sha256=%s already indexed — skipping",
                doc_name, sha256[:12],
            )
            return True
        return False
    except Exception as exc:
        # If the check fails, proceed with processing (safe default)
        logger.warning("SHA dedup check failed for %s: %s — proceeding", doc_name, exc)
        return False


# ── Steps ─────────────────────────────────────────────────────────────────────

@step
async def download_raw_file(task: ProcessingTask) -> bytes:
    logger.info("Downloading raw file: %s", task.doc_name,
                extra={"task_id": task.task_id, "doc_name": task.doc_name})
    return await _download_blob(settings.AZURE_STORAGE_CONTAINER_RAW, f"{task.domain}/{task.doc_name}")


@step
async def run_parser(file_bytes: bytes, task: ProcessingTask) -> list[RawChunk]:
    logger.info("Parsing %s (%s)", task.doc_name, task.file_type,
                extra={"task_id": task.task_id, "doc_name": task.doc_name})
    chunks = await asyncio.to_thread(
        parse_document,
        file_bytes,
        task.doc_name,
        task.doc_url,
        task.domain,
        f"{task.domain}/{task.doc_name}",
    )
    # Stamp SHA-256 onto every chunk so it's queryable in AI Search
    if task.file_sha256:
        for chunk in chunks:
            chunk.file_sha256 = task.file_sha256
    return chunks


@step
async def upload_processed_chunks(chunks: list[RawChunk], task: ProcessingTask) -> str:
    """Upload chunks as JSON to processed-chunks Blob. Returns blob path."""
    blob_path = f"{task.domain}/{task.doc_name}.json"
    data = json.dumps([asdict(c) for c in chunks], ensure_ascii=False).encode("utf-8")
    await _upload_blob(settings.AZURE_STORAGE_CONTAINER_PROCESSED, blob_path, data)
    logger.info("Stored %d chunks to blob: %s", len(chunks), blob_path,
                extra={"task_id": task.task_id, "chunk_count": len(chunks)})
    return blob_path


@step
async def queue_embedding_task(task: ProcessingTask, processed_blob_path: str, chunk_count: int) -> None:
    embedding_task = {
        "task_id":            task.task_id,
        "domain":             task.domain,
        "doc_name":           task.doc_name,
        "doc_url":            task.doc_url,
        "file_type":          task.file_type,
        "processed_blob_path": processed_blob_path,
        "chunk_count":        chunk_count,
        "is_delete":          task.is_delete,
    }
    await send_to_queue(settings.SB_QUEUE_EMBEDDING, embedding_task,
                        correlation_id=task.task_id)
    logger.info("Queued embedding task for doc_name=%s chunks=%d",
                task.doc_name, chunk_count,
                extra={"task_id": task.task_id})


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow(name="processing_workflow")
async def processing_workflow(task: ProcessingTask) -> dict:
    if task.is_delete:
        # Forward delete signal directly to embedding queue for index cleanup
        await queue_embedding_task(task, "", 0)
        return {"status": "delete_forwarded", "doc_name": task.doc_name}

    file_bytes = await download_raw_file(task)

    # ── Authoritative dedup gate: check AI Search before parsing ─────────────
    if await _sha256_already_indexed(task.doc_name, task.file_sha256):
        return {
            "status":      "skipped_duplicate",
            "doc_name":    task.doc_name,
            "sha256":      task.file_sha256[:12] if task.file_sha256 else "",
            "chunk_count": 0,
        }

    chunks              = await run_parser(file_bytes, task)
    processed_blob_path = await upload_processed_chunks(chunks, task)
    await queue_embedding_task(task, processed_blob_path, len(chunks))

    return {
        "status":      "processed",
        "doc_name":    task.doc_name,
        "sha256":      task.file_sha256[:12] if task.file_sha256 else "",
        "chunk_count": len(chunks),
        "blob_path":   processed_blob_path,
    }


# ── Service Bus listener ──────────────────────────────────────────────────────

async def _sb_listener():
    """Consume processing-tasks queue continuously."""
    logger.info("Processing Agent SB listener starting on queue '%s'", settings.SB_QUEUE_PROCESSING)
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

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
                    settings.SB_QUEUE_PROCESSING, max_wait_time=30
                ) as receiver:
                    async for msg in receiver:
                        try:
                            payload = json.loads(b"".join(msg.body))
                            task    = ProcessingTask(**payload)
                            result_obj = await processing_workflow.run(task)
                            outputs    = result_obj.get_outputs()
                            result     = outputs[0] if outputs else {}
                            logger.info("Processed: %s", result)
                            await receiver.complete_message(msg)
                        except Exception as exc:
                            logger.error("Processing failed: %s", exc, exc_info=True)
                            await receiver.abandon_message(msg)
        except Exception as exc:
            logger.error("SB listener crashed, restarting in 5s: %s", exc, exc_info=True)
            await asyncio.sleep(5)


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_sb_listener())
    logger.info("Processing Agent started — SB listener active.")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Processing Agent stopped.")


app = FastAPI(title="RAG Processing Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "processing"}


if __name__ == "__main__":
    uvicorn.run("agents.processing_agent:app", host="0.0.0.0", port=8011, reload=False)
