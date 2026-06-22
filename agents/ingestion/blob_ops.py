"""
Blob Storage helpers for the Ingestion Agent.

Responsible for:
  - Uploading raw file bytes to the ``raw-documents`` container
  - Reading and writing SHA-256 metadata tags on blobs (used for dedup)
  - Deleting raw blobs when a document is removed from SharePoint

All functions open a fresh async BlobServiceClient per call so they are safe
to call concurrently without sharing connection state.
"""
from __future__ import annotations

import hashlib
import logging

from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient

from shared.config import settings

logger = logging.getLogger(__name__)


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _blob_url() -> str:
    return f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"


async def upload_to_blob_with_sha(blob_path: str, data: bytes, sha: str) -> None:
    """Upload ``data`` to the raw-documents container, storing ``sha`` as blob metadata."""
    async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
        blob_client = svc.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW).get_blob_client(blob_path)
        await blob_client.upload_blob(data, overwrite=True, metadata={"sha256": sha})
        logger.debug("Uploaded blob: %s (%d bytes) sha256=%s", blob_path, len(data), sha[:12])


async def blob_sha256(blob_path: str) -> str | None:
    """Read the ``sha256`` metadata tag from an existing blob, or None if absent/missing."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
            props = await svc.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW).get_blob_client(blob_path).get_blob_properties()
            return props.metadata.get("sha256")
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read blob metadata for %s: %s", blob_path, exc)
        return None


async def delete_raw_blob(blob_path: str) -> None:
    """Delete the raw blob at ``blob_path``, silently ignoring 404."""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        async with DefaultAzureCredential() as cred, AsyncBlobClient(_blob_url(), credential=cred) as svc:
            await svc.get_blob_client(container=settings.AZURE_STORAGE_CONTAINER_RAW, blob=blob_path).delete_blob()
            logger.debug("Deleted raw blob: %s", blob_path)
    except ResourceNotFoundError:
        logger.debug("Raw blob already gone: %s", blob_path)
