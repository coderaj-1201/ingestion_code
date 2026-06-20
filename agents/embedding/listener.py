"""
Service Bus listener and FastAPI app for the Embedding Agent.

The listener continuously polls the ``embedding-queue`` and calls
:func:`~agents.embedding.pipeline.run_embedding` for each message.
Up to ``_EMBEDDING_CONCURRENCY`` messages are processed in parallel per
container instance; the semaphore prevents unbounded concurrent API calls to
the OpenAI embeddings endpoint.

On failure, messages are abandoned (returned to the queue for retry /
dead-lettering) rather than completed, so no data is silently lost.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agents.embedding.pipeline import run_embedding
from shared.config import settings
from shared.logging_config import configure_logging

configure_logging("rag-embedding")
logger = logging.getLogger(__name__)

# Max documents embedded concurrently per container instance. Increasing this
# will hit OpenAI rate limits faster — tune alongside TPM quota.
_EMBEDDING_CONCURRENCY = 4


async def _embed_one(receiver, msg, semaphore: asyncio.Semaphore) -> None:
    """Process a single Service Bus message under the concurrency semaphore.

    Completes the message on success; abandons it on any error so the Service
    Bus retry / dead-letter policy takes over.

    Args:
        receiver:  Active Service Bus queue receiver.
        msg:       Received :class:`azure.servicebus.ServiceBusReceivedMessage`.
        semaphore: Shared semaphore that caps concurrent embedding calls.
    """
    async with semaphore:
        task = None
        try:
            task = json.loads(b"".join(msg.body))
            result = await run_embedding(task)
            logger.info(
                "Embedding complete doc_name=%s status=%s uploaded=%s parents=%s",
                result.get("doc_name"), result.get("status"),
                result.get("uploaded"), result.get("parent_chunks"),
                extra={
                    "task_id": task.get("task_id"),
                    "doc_name": result.get("doc_name"),
                },
            )
            await receiver.complete_message(msg)
        except Exception as exc:
            doc_name = task.get("doc_name", "unknown") if task else "unknown"
            task_id = task.get("task_id", "") if task else ""
            domain = task.get("domain", "") if task else ""
            logger.error(
                "Embedding failed doc_name=%s: %s",
                doc_name, exc,
                exc_info=True,
                extra={"task_id": task_id, "doc_name": doc_name, "domain": domain},
            )
            await receiver.abandon_message(msg)


async def _sb_listener() -> None:
    """Consume the embedding queue continuously, restarting on connection errors.

    Spawns a new asyncio Task per message so that ``_EMBEDDING_CONCURRENCY``
    messages can be in-flight simultaneously. All tasks are drained before the
    receiver closes so no message is lost on a clean shutdown.
    """
    logger.info(
        "Embedding Agent SB listener starting on queue '%s'",
        settings.SB_QUEUE_EMBEDDING,
    )
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient

    semaphore = asyncio.Semaphore(_EMBEDDING_CONCURRENCY)

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

            tasks: set[asyncio.Task] = set()
            async with sb:
                async with sb.get_queue_receiver(
                    settings.SB_QUEUE_EMBEDDING,
                    max_wait_time=30,
                    prefetch_count=_EMBEDDING_CONCURRENCY,
                ) as receiver:
                    async for msg in receiver:
                        t = asyncio.create_task(_embed_one(receiver, msg, semaphore))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
                    # Drain in-flight tasks before the receiver closes.
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.error("SB listener crashed, restarting in 5s: %s", exc, exc_info=True)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Service Bus listener in the background alongside the HTTP server."""
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
    """Liveness probe — returns 200 as long as the process is running."""
    return {"status": "healthy", "agent": "embedding"}
