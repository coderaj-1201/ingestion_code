"""
FastAPI application and route handlers for the Ingestion Agent.

Routes
------
GET  /health                  — liveness probe
POST /ingest/from-logic-app   — primary ingest path; called by Azure Logic Apps
POST /webhook/sharepoint      — legacy SharePoint Graph webhook (validation + change notifications)
POST /ingest/folder           — manual folder scan via Graph API
POST /webhook/subscribe       — create a new Graph subscription
POST /webhook/renew           — renew an expiring Graph subscription
POST /ingest/local            — local dev only; DELETE before production deploy
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from agents.ingestion.blob_ops import (
    blob_sha256,
    delete_raw_blob,
    sha256_hex,
    upload_to_blob_with_sha,
)
from agents.ingestion.search_ops import delete_chunks_from_search
from agents.ingestion.sharepoint_ops import (
    delta_tokens,
    ingestion_workflow,
    item_to_task,
    resolve_all_sites,
    site_cache,
    url_to_domain,
)
from processors.dispatcher import SUPPORTED_EXTENSIONS as _SUPPORTED_EXTENSIONS
from shared.config import settings
from shared.graph_client import graph_client
from shared.logging_config import configure_logging
from shared.models import (
    IngestionTask,
    LogicAppIngestRequest,
    ManualIngestRequest,
    ProcessingTask,
    TriggerType,
)
from shared.service_bus import send_to_queue

configure_logging("rag-ingestion")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Resolve SharePoint site URLs at startup so route handlers don't need to."""
    await resolve_all_sites()
    logger.info("Ingestion Agent started. %d site(s) resolved.", len(site_cache))
    yield
    logger.info("Ingestion Agent stopped.")


app = FastAPI(title="RAG Ingestion Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Liveness probe — returns 200 as long as the process is running."""
    return {"status": "healthy", "agent": "ingestion"}


# ---------------------------------------------------------------------------
# Logic App ingest (primary path)
# ---------------------------------------------------------------------------

@app.post("/ingest/from-logic-app")
async def ingest_from_logic_app(
    req: LogicAppIngestRequest,
    request: Request,
) -> dict:
    """Receive a file from an Azure Logic App and queue it for downstream processing.

    The Logic App owns all SharePoint connectivity (auth, change detection, file
    download). This endpoint is the hand-off point: it validates the shared
    secret, deduplicates via SHA-256, uploads raw bytes to Blob, and queues a
    :class:`~shared.models.ProcessingTask` to the Service Bus processing queue.

    Authentication
    --------------
    The Logic App must set the ``X-Logic-App-Secret`` header to the value of
    ``LOGIC_APP_WEBHOOK_SECRET``. Requests with a missing or wrong secret are
    rejected with HTTP 401.

    Delete flow
    -----------
    When ``is_delete=True`` the ``file_content_base64`` field is ignored. The
    endpoint deletes index chunks and the raw blob directly, returning a count
    of deleted chunks. If the document is not found in the index the request is
    acknowledged but treated as a no-op.
    """
    if not settings.LOGIC_APP_WEBHOOK_SECRET:
        logger.error("LOGIC_APP_WEBHOOK_SECRET is not configured — rejecting Logic App request")
        raise HTTPException(status_code=503, detail="Logic App integration not configured")

    incoming_secret = request.headers.get("X-Logic-App-Secret", "")
    if incoming_secret != settings.LOGIC_APP_WEBHOOK_SECRET.get_secret_value():
        logger.warning("Logic App request with invalid secret for doc_name=%s", req.doc_name)
        raise HTTPException(status_code=401, detail="Invalid secret")

    ext = "." + req.doc_name.lower().rsplit(".", 1)[-1] if "." in req.doc_name else ""
    if ext not in _SUPPORTED_EXTENSIONS:
        logger.info("Skipping unsupported file type doc_name=%s ext=%s", req.doc_name, ext)
        return {"status": "skipped", "reason": "unsupported_extension", "doc_name": req.doc_name}

    import uuid
    task_id = str(uuid.uuid4())
    # Use doc_path for blob storage so same-named files in different folders don't collide.
    doc_path = req.doc_path or req.doc_name
    blob_path = f"{req.domain}/{doc_path}"

    logger.info(
        "Logic App ingest doc_name=%s doc_path=%s domain=%s is_delete=%s",
        req.doc_name, doc_path, req.domain, req.is_delete,
        extra={"task_id": task_id, "doc_name": req.doc_name, "domain": req.domain},
    )

    # -- Delete path ----------------------------------------------------------
    if req.is_delete:
        chunks_deleted = await delete_chunks_from_search(doc_path)
        if chunks_deleted == 0:
            logger.info(
                "Delete ignored — doc not in index doc_path=%s",
                doc_path,
                extra={"task_id": task_id, "doc_name": req.doc_name},
            )
            return {"status": "ignored", "reason": "not_in_index", "doc_name": req.doc_name}
        await delete_raw_blob(blob_path)
        logger.info(
            "Deleted doc_path=%s chunks=%d",
            doc_path, chunks_deleted,
            extra={"task_id": task_id, "doc_name": req.doc_name},
        )
        return {"status": "deleted", "doc_name": req.doc_name, "chunks_deleted": chunks_deleted}

    # -- Upsert path ----------------------------------------------------------
    if not req.file_content_base64:
        raise HTTPException(
            status_code=400,
            detail="file_content_base64 is required for non-delete requests",
        )

    import base64
    try:
        file_bytes = base64.b64decode(req.file_content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="file_content_base64 is not valid base64")

    new_sha = sha256_hex(file_bytes)
    existing_sha = await blob_sha256(blob_path)

    if existing_sha and existing_sha == new_sha:
        logger.info(
            "Skipping unchanged doc_name=%s sha256=%s (blob tag match)",
            req.doc_name, new_sha[:12],
            extra={"task_id": task_id, "doc_name": req.doc_name, "skip_reason": "sha256_match"},
        )
        return {"status": "skipped", "reason": "unchanged", "doc_name": req.doc_name}

    await upload_to_blob_with_sha(blob_path, file_bytes, new_sha)

    processing_task = ProcessingTask(
        task_id=task_id,
        domain=req.domain,
        doc_name=req.doc_name,
        doc_path=doc_path,
        doc_url=req.doc_url,
        file_type=req.file_type,
        file_sha256=new_sha,
    )
    await send_to_queue(
        settings.SB_QUEUE_PROCESSING,
        asdict(processing_task),
        correlation_id=task_id,
    )
    logger.info(
        "Queued processing task doc_name=%s sha256=%s",
        req.doc_name, new_sha[:12],
        extra={"task_id": task_id, "doc_name": req.doc_name},
    )
    return {
        "status": "queued",
        "doc_name": req.doc_name,
        "task_id": task_id,
        "sha256": new_sha[:12],
    }


# ---------------------------------------------------------------------------
# Legacy SharePoint Graph webhook (superseded by Logic Apps path above)
# ---------------------------------------------------------------------------
# These endpoints rely on shared/graph_client.py. Once all SharePoint sites are
# covered by Logic App workflows, this section and graph_client.py can be removed.

@app.post("/webhook/sharepoint")
async def sharepoint_webhook(
    req: Request,
    validationToken: str = Query(default=""),
) -> Response:
    """Handle SharePoint webhook notifications from Microsoft Graph.

    Two cases:
      1. Validation handshake — Graph sends ``?validationToken=...`` and expects
         the exact token echoed back as ``text/plain`` with HTTP 200.
      2. Change notification — extract changed items via the delta API and queue
         an :class:`~shared.models.IngestionTask` for each supported file.
    """
    # Graph validation handshake — must respond within 10 seconds.
    if validationToken:
        return PlainTextResponse(content=validationToken, status_code=200)

    body = await req.json()

    # Validate clientState to reject forged notifications.
    for notification in body.get("value", []):
        if notification.get("clientState") != settings.SHAREPOINT_WEBHOOK_SECRET:
            logger.warning("Invalid clientState in webhook notification — ignoring")
            raise HTTPException(status_code=401, detail="Invalid clientState")

    tasks: list[IngestionTask] = []

    # Build reverse map: site_id → domain using the startup-resolved cache.
    site_map: dict[str, str] = {
        site_id: url_to_domain.get(url, "hr")
        for url, (site_id, _drive_id) in site_cache.items()
    }

    for notification in body.get("value", []):
        resource = notification.get("resource", "")

        # Extract site_id from the resource path, e.g. "/sites/<site-id>/drive/root".
        site_id = ""
        parts = resource.split("/")
        if "sites" in parts:
            idx = parts.index("sites")
            if idx + 1 < len(parts):
                site_id = parts[idx + 1]

        if not site_id and site_cache:
            site_id, _ = next(iter(site_cache.values()))

        if not site_id:
            logger.warning("Could not determine site_id from webhook notification — skipping")
            continue

        # Look up drive_id from startup cache; fall back to a live Graph call.
        drive_id = ""
        for url, (cached_site_id, cached_drive_id) in site_cache.items():
            if cached_site_id == site_id:
                drive_id = cached_drive_id
                break

        if not drive_id:
            try:
                drive_id = await graph_client.get_default_drive_id(site_id)
            except Exception as exc:
                logger.error("Failed to get drive for site=%s: %s", site_id, exc)
                continue

        delta_key = f"{site_id}:{drive_id}"
        delta_token = delta_tokens.get(delta_key)

        changed_items, new_token = await graph_client.get_changed_items(site_id, drive_id, delta_token)
        delta_tokens[delta_key] = new_token

        domain = site_map.get(site_id, "hr")

        for item in changed_items:
            is_delete = "deleted" in item
            task = item_to_task(item, domain, TriggerType.WEBHOOK, is_delete)
            if task:
                tasks.append(task)

    if tasks:
        await ingestion_workflow.run(tasks)
        logger.info("Webhook triggered ingestion of %d files", len(tasks))

    return Response(status_code=202)


@app.post("/ingest/folder")
async def ingest_folder(req: ManualIngestRequest) -> dict:
    """Manually trigger ingestion of all files in a SharePoint folder.

    Prefer passing ``site_url`` (e.g. ``https://ironman.sharepoint.com/sites/HR``);
    ``site_id`` is still accepted for backwards compatibility with existing scripts.

    The site URL is resolved against the startup cache first; only on a cache miss
    is a live Graph call made, allowing ad-hoc URLs not listed in ``SHAREPOINT_SITE_URLS``.
    """
    site_id = req.site_id
    if req.site_url and not site_id:
        url_key = req.site_url.rstrip("/")
        if url_key in site_cache:
            site_id, _ = site_cache[url_key]
        else:
            try:
                site_id, _ = await graph_client.resolve_site_and_drive(req.site_url)
                logger.info("On-demand resolved site_url=%s → site_id=%s", req.site_url, site_id)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Cannot resolve site_url: {exc}")

    if not site_id:
        raise HTTPException(status_code=400, detail="Provide either site_url or site_id")

    logger.info(
        "Manual ingest triggered site=%s folder=%s domain=%s",
        site_id, req.folder_path, req.domain,
    )

    try:
        items = await graph_client.list_folder_items(site_id, req.folder_path, req.recursive)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Folder not found: {req.folder_path}")
        if status == 403:
            raise HTTPException(status_code=403, detail=f"Access denied to folder: {req.folder_path}")
        raise HTTPException(status_code=502, detail=f"Graph API error {status}: {exc.response.text[:200]}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Graph API timed out listing folder contents")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Graph API connection error: {exc}")

    tasks: list[IngestionTask] = []
    for item in items:
        task = item_to_task(item, req.domain, TriggerType.MANUAL, is_delete=False)
        if task and not task.site_id:
            task.site_id = site_id
        if task:
            tasks.append(task)

    if not tasks:
        return {"status": "no_supported_files", "total": 0}

    result_obj = await ingestion_workflow.run(tasks)
    outputs = result_obj.get_outputs()
    result = outputs[0] if outputs else {}

    logger.info("Manual ingest complete: %s", result)
    return {"status": "queued", **result}


@app.post("/webhook/subscribe")
async def subscribe_webhook(site_id: str, notification_url: str) -> dict:
    """Create a new Microsoft Graph subscription for change notifications."""
    sub = await graph_client.create_subscription(site_id, notification_url)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


@app.post("/webhook/renew")
async def renew_webhook(subscription_id: str) -> dict:
    """Renew an expiring Microsoft Graph webhook subscription."""
    sub = await graph_client.renew_subscription(subscription_id)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


# ---------------------------------------------------------------------------
# Local development only — DELETE before production deploy
# ---------------------------------------------------------------------------

@app.post("/ingest/local")
async def ingest_local(
    file_path: str = Body(...),
    domain: str = Body("hr"),
) -> dict:
    """Ingest a local file without SharePoint — for local testing only.

    Reads a file from the local filesystem, uploads it to Blob Storage, and
    queues a ProcessingTask. This bypasses all SharePoint auth and is not safe
    for production use.

    WARNING: DELETE THIS ENDPOINT before deploying to production.
    """
    from pathlib import Path

    file_bytes = Path(file_path).read_bytes()
    doc_name = Path(file_path).name
    sha256 = sha256_hex(file_bytes)
    blob_path = f"{domain}/{doc_name}"

    existing_sha = await blob_sha256(blob_path)
    if existing_sha == sha256:
        return {"status": "skipped", "reason": "duplicate_sha"}

    await upload_to_blob_with_sha(blob_path=blob_path, data=file_bytes, sha=sha256)

    task = ProcessingTask(
        domain=domain,
        doc_name=doc_name,
        file_type=doc_name.split(".")[-1].lower(),
        processed_blob_path=blob_path,
        file_sha256=sha256,
    )
    await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(task))

    return {"status": "queued", "doc_name": doc_name, "sha256": sha256}
