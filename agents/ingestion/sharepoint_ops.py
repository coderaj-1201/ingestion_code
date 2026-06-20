"""
SharePoint ingestion logic for the Ingestion Agent.

Responsibilities:
  - Resolve SharePoint site URLs to (site_id, drive_id) at startup
  - Convert Graph API drive items to IngestionTask objects
  - Download files from SharePoint and upload them to Blob Storage
  - Fan-out ingestion of multiple files via the MAF workflow

Module-level state
------------------
``site_cache``    — populated once by :func:`resolve_all_sites` at startup.
``url_to_domain`` — maps each site URL to its business domain string.
``delta_tokens``  — in-process store of Graph delta tokens per site+drive pair.
                    In production these should be persisted to Azure Table Storage
                    so they survive pod restarts.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from agent_framework import step, workflow

from agents.ingestion.blob_ops import blob_sha256, sha256_hex, upload_to_blob_with_sha
from processors.dispatcher import SUPPORTED_EXTENSIONS as _SUPPORTED_EXTENSIONS
from shared.config import settings
from shared.graph_client import graph_client
from shared.models import IngestionTask, ProcessingTask, TriggerType
from shared.service_bus import send_to_queue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (populated at startup by resolve_all_sites)
# ---------------------------------------------------------------------------

# Maps site_url → (site_id, drive_id). Never store IDs in .env — URLs are
# stable across tenant migrations; IDs are not.
site_cache: dict[str, tuple[str, str]] = {}

# Maps site_url → business domain string (e.g. "hr", "legal").
url_to_domain: dict[str, str] = {}

# Maps "{site_id}:{drive_id}" → Graph delta token for incremental change detection.
# Lost on restart — next webhook triggers a full delta from the beginning.
delta_tokens: dict[str, str] = {}


async def resolve_all_sites() -> None:
    """Resolve every URL in ``SHAREPOINT_SITE_URLS`` to ``(site_id, drive_id)``.

    Called once during FastAPI lifespan startup. Populates :data:`site_cache`
    and :data:`url_to_domain` so that webhook handlers can look up IDs without
    hitting Graph on every request.

    Also builds the URL→domain map from the ``SITE_DOMAIN_MAP`` setting.
    Logs each resolved site so ops can verify the mapping without editing .env.
    """
    # Parse "https://tenant.sharepoint.com/sites/HR:hr, ..." into {url: domain}
    domain_map: dict[str, str] = {}
    for pair in settings.SITE_DOMAIN_MAP.split(","):
        pair = pair.strip()
        if ":" in pair:
            # Use rfind so the URL's own colons are not treated as separators.
            idx = pair.rfind(":")
            url_part = pair[:idx].strip().rstrip("/")
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
            site_cache[url] = (site_id, drive_id)
            url_to_domain[url] = domain_map.get(url, "hr")
            logger.info(
                "Resolved site_url=%s → site_id=%s drive_id=%s domain=%s",
                url, site_id, drive_id[:8] + "...", url_to_domain[url],
            )
        except Exception as exc:
            logger.error("Failed to resolve site_url=%s: %s", url, exc)


def item_to_task(
    item: dict,
    domain: str,
    trigger_type: str,
    is_delete: bool = False,
) -> IngestionTask | None:
    """Convert a Graph API drive item dict to an :class:`IngestionTask`.

    Returns ``None`` if the file extension is not in ``SUPPORTED_EXTENSIONS``
    so callers can simply filter out ``None`` values from a list of items.

    Args:
        item:         Graph drive item as returned by the delta or list API.
        domain:       Business domain string, e.g. ``"hr"``.
        trigger_type: One of the :class:`~shared.models.TriggerType` values.
        is_delete:    ``True`` when the item has a ``deleted`` facet.
    """
    doc_name = item.get("name", "")
    ext = "." + doc_name.lower().rsplit(".", 1)[-1] if "." in doc_name else ""
    if ext not in _SUPPORTED_EXTENSIONS:
        return None

    file_type = ext.lstrip(".")
    drive_id = item.get("parentReference", {}).get("driveId", "")
    site_id = item.get("parentReference", {}).get("siteId", "")
    blob_path = f"{domain}/{doc_name}"

    return IngestionTask(
        domain=domain,
        file_type=file_type,
        doc_name=doc_name,
        doc_url=item.get("webUrl", ""),
        blob_path=blob_path,
        site_id=site_id,
        drive_id=drive_id,
        item_id=item.get("id", ""),
        trigger_type=trigger_type,
        is_delete=is_delete,
    )


@step
async def ingest_one_file(task: IngestionTask) -> ProcessingTask:
    """Download one file from SharePoint, upload to Blob, and queue a ProcessingTask.

    Delete flow
    -----------
    When ``task.is_delete`` is ``True`` the file is not downloaded. A
    :class:`~shared.models.ProcessingTask` with ``is_delete=True`` is queued so
    that the Processing Agent can clean up blobs and the Embedding Agent can
    remove index chunks.

    Dedup
    -----
    For upsert tasks, the SHA-256 of the downloaded bytes is compared against
    the ``sha256`` metadata tag on the existing blob. If they match the file has
    not changed and no downstream task is queued.

    Args:
        task: Populated :class:`~shared.models.IngestionTask` for a single file.

    Returns:
        A :class:`~shared.models.ProcessingTask` (may have ``processed_blob_path=""``
        for skipped or delete tasks).
    """
    if task.is_delete:
        processing_task = ProcessingTask(
            ingestion_task_id=task.task_id,
            domain=task.domain,
            doc_name=task.doc_name,
            doc_url=task.doc_url,
            file_type=task.file_type,
            processed_blob_path="",
            is_delete=True,
        )
        await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(processing_task))
        logger.info(
            "Delete signal queued for doc_name=%s",
            task.doc_name,
            extra={"task_id": task.task_id, "doc_name": task.doc_name},
        )
        return processing_task

    logger.info(
        "Downloading doc_name=%s",
        task.doc_name,
        extra={"task_id": task.task_id, "doc_name": task.doc_name, "domain": task.domain},
    )
    file_bytes = await graph_client.download_file(task.site_id, task.drive_id, task.item_id)

    new_sha = sha256_hex(file_bytes)
    existing_sha = await blob_sha256(task.blob_path)
    if existing_sha and existing_sha == new_sha:
        logger.info(
            "Skipping unchanged doc_name=%s sha256=%s (blob tag match)",
            task.doc_name, new_sha[:12],
            extra={"task_id": task.task_id, "doc_name": task.doc_name, "skip_reason": "sha256_match"},
        )
        return ProcessingTask(
            ingestion_task_id=task.task_id,
            domain=task.domain,
            doc_name=task.doc_name,
            doc_url=task.doc_url,
            file_type=task.file_type,
            processed_blob_path="",
            is_delete=False,
            file_sha256=new_sha,
        )

    await upload_to_blob_with_sha(task.blob_path, file_bytes, new_sha)

    processing_task = ProcessingTask(
        ingestion_task_id=task.task_id,
        domain=task.domain,
        doc_name=task.doc_name,
        doc_url=task.doc_url,
        file_type=task.file_type,
        processed_blob_path="",  # Processing Agent fills this in
        is_delete=False,
        file_sha256=new_sha,
    )
    await send_to_queue(
        settings.SB_QUEUE_PROCESSING,
        asdict(processing_task),
        correlation_id=task.task_id,
    )
    logger.info("Queued processing task for doc_name=%s", task.doc_name)
    return processing_task


@workflow(name="ingestion_workflow")
async def ingestion_workflow(tasks: list[IngestionTask]) -> dict:
    """Fan-out ingestion of multiple files, capped at 10 concurrent downloads.

    Args:
        tasks: List of :class:`~shared.models.IngestionTask` objects to process.

    Returns:
        Summary dict with ``total``, ``success``, and ``failed`` counts.
    """
    semaphore = asyncio.Semaphore(10)

    async def _bounded(task: IngestionTask):
        async with semaphore:
            return await ingest_one_file(task)

    results = await asyncio.gather(*[_bounded(t) for t in tasks], return_exceptions=True)
    success = sum(1 for r in results if not isinstance(r, Exception))
    failed = sum(1 for r in results if isinstance(r, Exception))

    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("Failed to ingest task %s: %s", tasks[i].task_id, r)

    return {"total": len(tasks), "success": success, "failed": failed}
