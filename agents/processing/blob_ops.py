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

from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient

from shared.config import settings

logger = logging.getLogger(__name__)


def _blob_url() -> str:
    return f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"


async def download_blob(container: str, blob_path: str) -> bytes:
    """Download a blob and return its contents as raw bytes."""
    async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
        stream = await svc.get_container_client(container).get_blob_client(blob_path).download_blob()
        return await stream.readall()


async def upload_blob(container: str, blob_path: str, data: bytes) -> None:
    """Upload ``data`` to ``blob_path`` in ``container``, overwriting any existing blob."""
    async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
        await svc.get_container_client(container).get_blob_client(blob_path).upload_blob(data, overwrite=True)
        logger.debug("Uploaded processed blob: %s", blob_path)


async def delete_blobs(domain: str, doc_path: str) -> None:
    """Delete the raw and processed blobs for a document, silently ignoring 404s."""
    raw_path = f"{domain}/{doc_path}"
    processed_path = f"{domain}/{doc_path}.json"

    async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
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
    if len(sha256) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha256):
        logger.warning("Skipping dedup check: malformed sha256 for doc_name=%s", doc_name)
        return False

    try:
        from azure.search.documents.aio import SearchClient as AsyncSearchClient

        async with DefaultAzureCredential() as cred, AsyncSearchClient(
            endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
            index_name=settings.AZURE_SEARCH_INDEX,
            credential=cred,
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
