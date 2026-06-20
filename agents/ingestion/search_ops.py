"""
AI Search helpers for the Ingestion Agent.

Only contains delete operations — the Embedding Agent owns all index writes.
Deletion is performed here (rather than delegated downstream) because the
Logic App ingest path needs to confirm a document exists in the index before
bothering to delete blobs.
"""
from __future__ import annotations

import logging
import os

from shared.config import settings

logger = logging.getLogger(__name__)


async def delete_chunks_from_search(doc_name: str) -> int:
    """Delete all AI Search index chunks for ``doc_name``.

    Paginates in batches of 1 000 until no results remain (a single
    ``top=1000`` call would silently leave orphans for large documents).

    Args:
        doc_name: Document name as stored in the ``doc_name`` index field.

    Returns:
        Number of chunks deleted (0 means the document was not found).
    """
    from azure.core.credentials import AzureKeyCredential
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
    from azure.search.documents.aio import SearchClient

    # Prefer Managed Identity in Azure; fall back to API key or CLI credential locally.
    raw_key = settings.AZURE_SEARCH_API_KEY
    if raw_key:
        search_credential = AzureKeyCredential(raw_key.get_secret_value())
    elif os.getenv("RUNNING_IN_AZURE"):
        search_credential = ManagedIdentityCredential()
    else:
        search_credential = AzureCliCredential()

    # Single-quote escape to prevent OData injection from doc_name values
    # that contain apostrophes (e.g. "O'Brien Policy.pdf").
    escaped = doc_name.replace("'", "''")
    deleted = 0

    async with SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=search_credential,
    ) as client:
        while True:
            results = await client.search(
                search_text="*",
                filter=f"doc_name eq '{escaped}'",
                select=["id"],
                top=1000,
            )
            ids = [r["id"] async for r in results]
            if not ids:
                break
            await client.delete_documents(documents=[{"id": i} for i in ids])
            deleted += len(ids)

    return deleted
