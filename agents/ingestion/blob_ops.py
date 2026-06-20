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
import os

from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient

from shared.config import settings

logger = logging.getLogger(__name__)


def blob_credential():
    """Return the appropriate Azure credential for Blob Storage.

    Uses ManagedIdentityCredential inside Azure Container Apps and
    AzureCliCredential for local development.
    """
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

    if os.getenv("RUNNING_IN_AZURE"):
        return ManagedIdentityCredential()
    return AzureCliCredential()


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


async def upload_to_blob_with_sha(blob_path: str, data: bytes, sha: str) -> None:
    """Upload ``data`` to the raw-documents container, storing ``sha`` as blob metadata.

    The SHA-256 is written as a metadata tag (``sha256`` key) so that future
    ingestion runs can detect unchanged files without re-downloading from SharePoint.

    Args:
        blob_path: Path within the container, e.g. ``hr/Leave Policy 2024.pdf``.
        data:      Raw file bytes to upload.
        sha:       SHA-256 hex digest of ``data``.
    """
    async with AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=blob_credential(),
    ) as blob_service:
        container = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
        blob_client = container.get_blob_client(blob_path)
        await blob_client.upload_blob(
            data,
            overwrite=True,
            metadata={"sha256": sha},
        )
        logger.debug(
            "Uploaded blob: %s (%d bytes) sha256=%s",
            blob_path, len(data), sha[:12],
        )


async def blob_sha256(blob_path: str) -> str | None:
    """Read the ``sha256`` metadata tag from an existing blob.

    Returns ``None`` if the blob does not exist or the tag is absent.
    Errors other than 404 are logged as warnings and also return ``None``
    so that the caller falls back to a full re-upload rather than crashing.

    Args:
        blob_path: Path within the raw-documents container.
    """
    from azure.core.exceptions import ResourceNotFoundError

    try:
        async with AsyncBlobClient(
            account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=blob_credential(),
        ) as blob_service:
            container = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
            blob_client = container.get_blob_client(blob_path)
            props = await blob_client.get_blob_properties()
            return props.metadata.get("sha256")
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read blob metadata for %s: %s", blob_path, exc)
        return None


async def delete_raw_blob(domain: str, doc_name: str) -> None:
    """Delete the raw blob for ``doc_name`` under ``domain``.

    Silently ignores 404 errors — the blob may already have been deleted
    by a previous run or manually.

    Args:
        domain:   Business domain subfolder, e.g. ``hr``.
        doc_name: File name as stored in SharePoint, e.g. ``Leave Policy 2024.pdf``.
    """
    from azure.core.exceptions import ResourceNotFoundError

    blob_path = f"{domain}/{doc_name}"
    try:
        async with AsyncBlobClient(
            account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=blob_credential(),
        ) as blob_service:
            await blob_service.get_blob_client(
                container=settings.AZURE_STORAGE_CONTAINER_RAW,
                blob=blob_path,
            ).delete_blob()
            logger.debug("Deleted raw blob: %s", blob_path)
    except ResourceNotFoundError:
        logger.debug("Raw blob already gone: %s", blob_path)
