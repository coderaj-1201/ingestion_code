"""
Ingestion Agent
===============
MAF Functional Workflow (@workflow / @step).

Two entry points:
  1. POST /webhook/sharepoint  — SharePoint change notification
     → validates clientState secret
     → uses Graph delta API to identify created/updated/deleted files
     → queues IngestionTask per file to SB ingestion-tasks queue

  2. POST /ingest/folder       — manual folder scan
     → lists all files in folder (recursive)
     → queues IngestionTask per file

For each IngestionTask:
  → downloads file bytes via Graph API
  → uploads raw bytes to Blob Storage (raw-documents/<domain>/<filename>)
  → sends ProcessingTask to SB processing-tasks queue
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from agent_framework import step, workflow
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient
from fastapi import FastAPI, Body, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from shared.azure_clients import get_service_bus_client
from shared.config import settings
from shared.graph_client import graph_client
from shared.logging_config import configure_logging, get_logger
from shared.models import (
    IngestionTask,
    ManualIngestRequest,
    ProcessingTask,
    TriggerType,
    WebhookNotification,
)
from shared.service_bus import send_to_queue
from processors.dispatcher import SUPPORTED_EXTENSIONS as _SUPPORTED_EXTENSIONS

configure_logging("rag-ingestion")
logger = get_logger(__name__)

# Persisted delta tokens per (site_id, drive_id) — in production store in Azure Table Storage
_delta_tokens: dict[str, str] = {}

# Resolved at startup: site_url → (site_id, drive_id)
# Populated by _resolve_all_sites() called in lifespan.
# This means we NEVER store site_id/drive_id in .env — only human-readable URLs.
_site_cache: dict[str, tuple[str, str]] = {}   # site_url → (site_id, drive_id)
_url_to_domain: dict[str, str]          = {}   # site_url → domain


async def _resolve_all_sites() -> None:
    """
    At startup: resolve every SHAREPOINT_SITE_URLS entry to (site_id, drive_id)
    and build the url→domain map from SITE_DOMAIN_MAP.
    Logs clearly so ops can see what was resolved without touching .env.
    """
    # Build url → domain map
    domain_map: dict[str, str] = {}
    for pair in settings.SITE_DOMAIN_MAP.split(","):
        pair = pair.strip()
        if ":" in pair:
            # last colon is the separator (URL itself contains colons)
            idx = pair.rfind(":")
            url_part    = pair[:idx].strip().rstrip("/")
            domain_part = pair[idx + 1:].strip()
            if url_part and domain_part:
                domain_map[url_part] = domain_part

    site_urls = [
        u.strip().rstrip("/")
        for u in settings.SHAREPOINT_SITE_URLS.split(",")
        if u.strip()
    ]

    if not site_urls:
        logger.warning(
            "SHAREPOINT_SITE_URLS is empty — no sites will be resolved. "
            "Manual /ingest/folder calls must supply site_id explicitly."
        )
        return

    for url in site_urls:
        try:
            site_id, drive_id = await graph_client.resolve_site_and_drive(url)
            _site_cache[url]    = (site_id, drive_id)
            _url_to_domain[url] = domain_map.get(url, "hr")
            logger.info(
                "Resolved site_url=%s → site_id=%s drive_id=%s domain=%s",
                url, site_id, drive_id[:8] + "...", _url_to_domain[url],
            )
        except Exception as exc:
            logger.error("Failed to resolve site_url=%s: %s", url, exc)


# ── Blob helpers ──────────────────────────────────────────────────────────────

async def _upload_to_blob(blob_path: str, data: bytes) -> None:
    """Upload raw file bytes to raw-documents container."""
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    async with AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    ) as blob_service:
        container   = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
        blob_client = container.get_blob_client(blob_path)
        await blob_client.upload_blob(data, overwrite=True)
        logger.debug("Uploaded blob: %s (%d bytes)", blob_path, len(data))


# ── SHA-256 helpers ───────────────────────────────────────────────────────────

def _sha256_hex(data: bytes) -> str:
    """Return SHA-256 hex digest of raw file bytes."""
    return hashlib.sha256(data).hexdigest()


async def _blob_sha256(blob_path: str) -> str | None:
    """
    Read the 'sha256' metadata tag from an existing blob.
    Returns None if the blob doesn't exist or has no tag.
    """
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    from azure.core.exceptions import ResourceNotFoundError

    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    try:
        async with AsyncBlobClient(
            account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=credential,
        ) as blob_service:
            container   = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
            blob_client = container.get_blob_client(blob_path)
            props = await blob_client.get_blob_properties()
            return props.metadata.get("sha256")
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read blob metadata for %s: %s", blob_path, exc)
        return None


async def _upload_to_blob_with_sha(blob_path: str, data: bytes, sha: str) -> None:
    """Upload raw file bytes to raw-documents container, tagging with SHA-256."""
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    async with AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    ) as blob_service:
        container   = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
        blob_client = container.get_blob_client(blob_path)
        await blob_client.upload_blob(
            data,
            overwrite=True,
            metadata={"sha256": sha},   # stored as blob metadata tag
        )
        logger.debug("Uploaded blob: %s (%d bytes) sha256=%s", blob_path, len(data), sha[:12])


# ── Step: process one file ────────────────────────────────────────────────────

@step
async def ingest_one_file(task: IngestionTask) -> ProcessingTask:
    """
    Download file → upload to Blob → send ProcessingTask to Service Bus.
    """
    if task.is_delete:
        # For deletes we don't download — just forward the delete signal
        processing_task = ProcessingTask(
            ingestion_task_id   = task.task_id,
            domain              = task.domain,
            doc_name            = task.doc_name,
            doc_url             = task.doc_url,
            file_type           = task.file_type,
            processed_blob_path = "",
            is_delete           = True,
        )
        await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(processing_task))
        logger.info("Delete signal queued for doc_name=%s", task.doc_name,
                    extra={"task_id": task.task_id, "doc_name": task.doc_name})
        return processing_task

    # Download from SharePoint
    logger.info("Downloading doc_name=%s", task.doc_name,
                extra={"task_id": task.task_id, "doc_name": task.doc_name, "domain": task.domain})
    file_bytes = await graph_client.download_file(task.site_id, task.drive_id, task.item_id)

    # ── Dedup: SHA-256 check against existing blob metadata ──────────────────
    new_sha      = _sha256_hex(file_bytes)
    existing_sha = await _blob_sha256(task.blob_path)
    if existing_sha and existing_sha == new_sha:
        logger.info(
            "Skipping unchanged doc_name=%s sha256=%s (blob tag match)",
            task.doc_name, new_sha[:12],
            extra={"task_id": task.task_id, "doc_name": task.doc_name, "skip_reason": "sha256_match"},
        )
        return ProcessingTask(
            ingestion_task_id   = task.task_id,
            domain              = task.domain,
            doc_name            = task.doc_name,
            doc_url             = task.doc_url,
            file_type           = task.file_type,
            processed_blob_path = "",
            is_delete           = False,
            file_sha256         = new_sha,
        )

    # Upload to Blob (with SHA-256 tag so future runs can skip unchanged files)
    await _upload_to_blob_with_sha(task.blob_path, file_bytes, new_sha)

    # Queue processing task — carry SHA forward so Processing Agent can dedup too
    processing_task = ProcessingTask(
        ingestion_task_id   = task.task_id,
        domain              = task.domain,
        doc_name            = task.doc_name,
        doc_url             = task.doc_url,
        file_type           = task.file_type,
        processed_blob_path = "",   # Processing Agent fills this
        is_delete           = False,
        file_sha256         = new_sha,
    )
    await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(processing_task),
                        correlation_id=task.task_id)
    logger.info("Queued processing task for doc_name=%s", task.doc_name)
    return processing_task


# ── Workflow: ingest a list of tasks ─────────────────────────────────────────

@workflow(name="ingestion_workflow")
async def ingestion_workflow(tasks: list[IngestionTask]) -> dict:
    """Fan-out: ingest all files in parallel (up to 10 at a time)."""
    semaphore = asyncio.Semaphore(10)

    async def _bounded(task: IngestionTask):
        async with semaphore:
            return await ingest_one_file(task)

    results = await asyncio.gather(*[_bounded(t) for t in tasks], return_exceptions=True)
    success = sum(1 for r in results if not isinstance(r, Exception))
    failed  = sum(1 for r in results if isinstance(r, Exception))
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("Failed to ingest task %s: %s", tasks[i].task_id, r)
    return {"total": len(tasks), "success": success, "failed": failed}


# ── Helpers: build IngestionTask from Graph item ──────────────────────────────

def _item_to_task(item: dict, domain: str, trigger_type: str, is_delete: bool = False) -> IngestionTask | None:
    doc_name  = item.get("name", "")
    ext       = "." + doc_name.lower().rsplit(".", 1)[-1] if "." in doc_name else ""
    if ext not in _SUPPORTED_EXTENSIONS:
        return None

    file_type = ext.lstrip(".")
    drive_id  = item.get("parentReference", {}).get("driveId", "")
    site_id   = item.get("parentReference", {}).get("siteId", "")

    blob_path = f"{domain}/{doc_name}"

    return IngestionTask(
        domain       = domain,
        file_type    = file_type,
        doc_name     = doc_name,
        doc_url      = item.get("webUrl", ""),
        blob_path    = blob_path,
        site_id      = site_id,
        drive_id     = drive_id,
        item_id      = item.get("id", ""),
        trigger_type = trigger_type,
        is_delete    = is_delete,
    )


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Resolve SharePoint site URLs → (site_id, drive_id) at startup.
    # This avoids hardcoding IDs in .env — URLs are stable, IDs are not.
    await _resolve_all_sites()
    logger.info("Ingestion Agent started. %d site(s) resolved.", len(_site_cache))
    yield
    logger.info("Ingestion Agent stopped.")


app = FastAPI(title="RAG Ingestion Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "ingestion"}


@app.post("/webhook/sharepoint")
async def sharepoint_webhook(
    req: Request,
    validationToken: str = Query(default=""),
) -> Response:
    """
    SharePoint webhook endpoint.

    Two cases:
      1. Validation handshake (GET/POST with ?validationToken=...) → echo it back as text/plain
      2. Change notification → extract changed items and queue ingestion tasks
    """
    # Validation handshake
    if validationToken:
        return PlainTextResponse(content=validationToken, status_code=200)

    body = await req.json()

    # Validate clientState secret
    for notification in body.get("value", []):
        if notification.get("clientState") != settings.SHAREPOINT_WEBHOOK_SECRET:
            logger.warning("Invalid clientState in webhook notification — ignoring")
            raise HTTPException(status_code=401, detail="Invalid clientState")

    # Use delta API to get actual changed items per subscription/site
    tasks: list[IngestionTask] = []

    # Build reverse map: site_id → domain from startup-resolved cache
    site_map: dict[str, str] = {
        site_id: _url_to_domain.get(url, "hr")
        for url, (site_id, _drive_id) in _site_cache.items()
    }

    for notification in body.get("value", []):
        resource = notification.get("resource", "")

        # Extract site_id from resource path if available
        # resource format: /sites/<site-id>/drive/root
        site_id = ""
        parts   = resource.split("/")
        if "sites" in parts:
            idx = parts.index("sites")
            if idx + 1 < len(parts):
                site_id = parts[idx + 1]

        # Fall back to first resolved site if webhook doesn't include site_id
        if not site_id and _site_cache:
            site_id, _ = next(iter(_site_cache.values()))

        if not site_id:
            logger.warning("Could not determine site_id from webhook notification — skipping")
            continue

        # Get drive_id from startup cache; fall back to live Graph call
        drive_id = ""
        for url, (cached_site_id, cached_drive_id) in _site_cache.items():
            if cached_site_id == site_id:
                drive_id = cached_drive_id
                break

        if not drive_id:
            try:
                drive_id = await graph_client.get_default_drive_id(site_id)
            except Exception as exc:
                logger.error("Failed to get drive for site=%s: %s", site_id, exc)
                continue

        delta_key   = f"{site_id}:{drive_id}"
        delta_token = _delta_tokens.get(delta_key)

        changed_items, new_token = await graph_client.get_changed_items(site_id, drive_id, delta_token)
        _delta_tokens[delta_key] = new_token

        domain = site_map.get(site_id, "hr")

        for item in changed_items:
            is_delete = "deleted" in item
            task = _item_to_task(item, domain, TriggerType.WEBHOOK, is_delete)
            if task:
                tasks.append(task)

    if tasks:
        await ingestion_workflow.run(tasks)
        logger.info("Webhook triggered ingestion of %d files", len(tasks))

    return Response(status_code=202)


@app.post("/ingest/folder")
async def ingest_folder(req: ManualIngestRequest) -> dict:
    """
    Manually trigger ingestion of all files in a SharePoint folder.
    POST body: {site_url OR site_id, folder_path, domain, recursive}

    Prefer passing site_url (e.g. https://ironman.sharepoint.com/sites/HR).
    site_id is still accepted for backwards compatibility.
    """
    # Resolve site_url → site_id if caller sent a URL instead of a raw ID
    site_id = req.site_id
    if req.site_url and not site_id:
        url_key = req.site_url.rstrip("/")
        if url_key in _site_cache:
            site_id, _ = _site_cache[url_key]
        else:
            # Not in startup cache — resolve on demand (e.g. ad-hoc URL)
            try:
                site_id, _ = await graph_client.resolve_site_and_drive(req.site_url)
                logger.info("On-demand resolved site_url=%s → site_id=%s", req.site_url, site_id)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Cannot resolve site_url: {exc}")

    if not site_id:
        raise HTTPException(status_code=400, detail="Provide either site_url or site_id")

    logger.info("Manual ingest triggered site=%s folder=%s domain=%s",
                site_id, req.folder_path, req.domain)

    items = await graph_client.list_folder_items(site_id, req.folder_path, req.recursive)

    tasks: list[IngestionTask] = []
    for item in items:
        task = _item_to_task(item, req.domain, TriggerType.MANUAL, is_delete=False)
        if task and not task.site_id:
            task.site_id = site_id
        if task:
            tasks.append(task)

    if not tasks:
        return {"status": "no_supported_files", "total": 0}

    result_obj = await ingestion_workflow.run(tasks)
    outputs    = result_obj.get_outputs()
    result     = outputs[0] if outputs else {}

    logger.info("Manual ingest complete: %s", result)
    return {"status": "queued", **result}


@app.post("/webhook/subscribe")
async def subscribe_webhook(site_id: str, notification_url: str) -> dict:
    """Create a new SharePoint webhook subscription."""
    sub = await graph_client.create_subscription(site_id, notification_url)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


@app.post("/webhook/renew")
async def renew_webhook(subscription_id: str) -> dict:
    """Renew an expiring webhook subscription."""
    sub = await graph_client.renew_subscription(subscription_id)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


# ── Local test endpoint (delete before production deploy) ─────────────────────

@app.post("/ingest/local")
async def ingest_local(
    file_path: str = Body(...),
    domain: str    = Body("hr"),
) -> dict:
    """
    Temp endpoint for local testing without SharePoint.
    Reads a local file, computes SHA-256, uploads to blob, queues ProcessingTask.
    DELETE THIS ENDPOINT before production deploy.
    """
    file_bytes = Path(file_path).read_bytes()
    doc_name   = Path(file_path).name
    sha256     = _sha256_hex(file_bytes)
    blob_path = f"{domain}/{doc_name}"
    
    existing_sha = await _blob_sha256(blob_path)
    if existing_sha == sha256:
        return {"status": "skipped", "reason": "duplicate_sha"}

    await _upload_to_blob_with_sha(blob_path=blob_path, data=file_bytes, sha=sha256)

    task = ProcessingTask(
        domain              = domain,
        doc_name            = doc_name,
        file_type           = doc_name.split(".")[-1].lower(),
        processed_blob_path = blob_path,
        file_sha256         = sha256,
    )
    await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(task))

    return {"status": "queued", "doc_name": doc_name, "sha256": sha256}


if __name__ == "__main__":
    uvicorn.run("agents.ingestion_agent:app", host="0.0.0.0", port=8010, reload=False)
