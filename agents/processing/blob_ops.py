"""
Blob Storage helpers for the Processing Agent.

Handles downloading raw files, uploading processed chunk JSON, and deleting
both raw and processed blobs when a document is removed. Also owns the
SHA-256 dedup check against AI Search so that already-indexed files are not
re-parsed.

All functions open a fresh async BlobServiceClient per call via
:func:`get_blob_client` so they are safe to call concurrently.
"""
from __future__ import annotations

import logging
import os

from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient

from shared.config import settings

logger = logging.getLogger(__name__)


async def get_blob_client() -> AsyncBlobClient:
    """Create a new async BlobServiceClient using the appropriate credential.

    Uses ``ManagedIdentityCredential`` inside Azure and ``AzureCliCredential``
    for local development. Always use the returned client as an async context manager.
    """
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    return AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    )


async def download_blob(container: str, blob_path: str) -> bytes:
    """Download a blob and return its contents as raw bytes.

    Args:
        container: Storage container name (e.g. ``raw-documents``).
        blob_path: Path within the container, e.g. ``hr/Leave Policy 2024.pdf``.
    """
    async with await get_blob_client() as svc:
        blob = svc.get_container_client(container).get_blob_client(blob_path)
        stream = await blob.download_blob()
        return await stream.readall()


async def upload_blob(container: str, blob_path: str, data: bytes) -> None:
    """Upload ``data`` to ``blob_path`` in ``container``, overwriting any existing blob.

    Args:
        container: Storage container name (e.g. ``processed-chunks``).
        blob_path: Path within the container.
        data:      Raw bytes to write.
    """
    async with await get_blob_client() as svc:
        blob = svc.get_container_client(container).get_blob_client(blob_path)
        await blob.upload_blob(data, overwrite=True)
        logger.debug("Uploaded processed blob: %s", blob_path)


async def delete_blobs(domain: str, doc_name: str) -> None:
    """Delete the raw and processed blobs for a document.

    Both the raw file (``raw-documents/<domain>/<doc_name>``) and the processed
    chunk JSON (``processed-chunks/<domain>/<doc_name>.json``) are deleted in a
    single Blob client session. 404 errors are silently ignored — the blobs may
    already have been deleted by a previous run.

    Args:
        domain:   Business domain subfolder, e.g. ``hr``.
        doc_name: File name as stored in SharePoint.
    """
    raw_path = f"{domain}/{doc_name}"
    processed_path = f"{domain}/{doc_name}.json"

    async with await get_blob_client() as svc:
        for container, path in [
            (settings.AZURE_STORAGE_CONTAINER_RAW, raw_path),
            (settings.AZURE_STORAGE_CONTAINER_PROCESSED, processed_path),
        ]:
            try:
                await svc.get_container_client(container).get_blob_client(path).delete_blob()
                logger.info("Deleted blob %s/%s", container, path)
            except Exception as exc:
                if "BlobNotFound" in type(exc).__name__ or "ResourceNotFoundError" in type(exc).__name__:
                    logger.debug("Blob already gone: %s/%s", container, path)
                else:
                    raise


async def sha256_already_indexed(doc_name: str, sha256: str) -> bool:
    """Check whether AI Search already contains chunks with ``sha256``.

    This is the authoritative dedup gate — even if the blob metadata tag was
    lost (e.g. container recreated), the Search index is the source of truth.

    Filters on ``file_sha256`` only (a validated hex string) rather than
    ``doc_name`` to avoid OData injection from filenames with special characters.

    Proceeds with processing (returns ``False``) on any check failure so that
    a transient Search outage never causes documents to be silently skipped.

    Args:
        doc_name: Used only for log messages.
        sha256:   SHA-256 hex digest to look up; must be lowercase hex.

    Returns:
        ``True`` if at least one chunk with this SHA-256 is found in the index.
    """
    if not sha256:
        return False

    # Validate before embedding in a filter expression.
    if not all(c in "0123456789abcdefABCDEF" for c in sha256):
        logger.warning("Skipping dedup check: malformed sha256 for doc_name=%s", doc_name)
        return False

    try:
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents.aio import SearchClient as AsyncSearchClient

        async with AsyncSearchClient(
            endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
            index_name=settings.AZURE_SEARCH_INDEX,
            credential=AzureKeyCredential(settings.AZURE_SEARCH_API_KEY.get_secret_value()),
        ) as client:
            results = [
                r async for r in await client.search(
                    search_text="*",
                    filter=f"file_sha256 eq '{sha256}'",
                    select=["id"],
                    top=1,
                )
            ]

        if results:
            logger.info(
                "Dedup: doc_name=%s sha256=%s already indexed — skipping",
                doc_name, sha256[:12],
            )
            return True
        return False
    except Exception as exc:
        # Better to re-process than to silently skip a file that needs indexing.
        logger.warning(
            "SHA dedup check failed for doc_name=%s — proceeding: %s", doc_name, exc
        )
        return False
