"""
Service Bus listener and FastAPI app for the Processing Agent.

The listener continuously polls the ``processing-queue`` and calls
:func:`~agents.processing.pipeline.run_processing` for each message.
Up to ``_PROCESSING_CONCURRENCY`` messages are processed in parallel per
container instance; the semaphore prevents unbounded goroutine-style spawning.

On failure, messages are abandoned (returned to the queue for retry /
dead-lettering) rather than completed, so no data is silently lost.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agents.processing.pipeline import run_processing
from shared.config import settings
from shared.logging_config import configure_logging
from shared.models import ProcessingTask

configure_logging("rag-processing")
logger = logging.getLogger(__name__)

# Max documents parsed concurrently per container instance. Keep low to avoid
# OOM when processing large PDFs — each parse may load the full document into memory.
_PROCESSING_CONCURRENCY = 4


async def _process_one(receiver, msg, semaphore: asyncio.Semaphore) -> None:
    """Process a single Service Bus message under the concurrency semaphore.

    Completes the message on success; abandons it on any error so the Service
    Bus retry / dead-letter policy takes over.

    Args:
        receiver:  Active Service Bus queue receiver.
        msg:       Received :class:`azure.servicebus.ServiceBusReceivedMessage`.
        semaphore: Shared semaphore that caps concurrent processing.
    """
    async with semaphore:
        payload = None
        try:
            payload = json.loads(b"".join(msg.body))
            task = ProcessingTask(**payload)
            result = await run_processing(task)
            logger.info("Processed: %s", result)
            await receiver.complete_message(msg)
        except Exception as exc:
            doc_name = payload.get("doc_name", "unknown") if payload else "unknown"
            task_id = payload.get("task_id", "") if payload else ""
            domain = payload.get("domain", "") if payload else ""
            logger.error(
                "Processing failed doc_name=%s: %s",
                doc_name, exc,
                exc_info=True,
                extra={"task_id": task_id, "doc_name": doc_name, "domain": domain},
            )
            await receiver.abandon_message(msg)


async def _sb_listener() -> None:
    """Consume the processing queue continuously, restarting on connection errors.

    Spawns a new asyncio Task per message so that ``_PROCESSING_CONCURRENCY``
    messages can be in-flight simultaneously. All tasks are drained before the
    receiver closes so no message is lost on a clean shutdown.
    """
    logger.info(
        "Processing Agent SB listener starting on queue '%s'",
        settings.SB_QUEUE_PROCESSING,
    )
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient

    semaphore = asyncio.Semaphore(_PROCESSING_CONCURRENCY)

    async with DefaultAzureCredential() as credential:
        while True:
            try:
                sb = AsyncSBClient(
                    fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
                    credential=credential,
                )

                tasks: set[asyncio.Task] = set()
                async with sb:
                    async with sb.get_queue_receiver(
                        settings.SB_QUEUE_PROCESSING,
                        max_wait_time=30,
                        prefetch_count=_PROCESSING_CONCURRENCY,
                    ) as receiver:
                        async for msg in receiver:
                            t = asyncio.create_task(_process_one(receiver, msg, semaphore))
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
    """Liveness probe — returns 200 as long as the process is running."""
    return {"status": "healthy", "agent": "processing"}
