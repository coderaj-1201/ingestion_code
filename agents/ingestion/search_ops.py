"""
AI Search helpers for the Ingestion Agent.

Only contains delete operations — the Embedding Agent owns all index writes.
Deletion is performed here (rather than delegated downstream) because the
Logic App ingest path needs to confirm a document exists in the index before
bothering to delete blobs.
"""
from __future__ import annotations

import logging

from azure.identity.aio import DefaultAzureCredential
from shared.config import settings

logger = logging.getLogger(__name__)


async def delete_chunks_from_search(doc_path: str) -> int:
    """Delete all AI Search index chunks for the document identified by ``doc_path``.

    Paginates in batches of 1 000 until no results remain (a single
    ``top=1000`` call would silently leave orphans for large documents).

    Filters on ``doc_path`` (unique per file within a domain library) rather
    than ``doc_name`` to avoid deleting chunks belonging to same-named files in
    different SharePoint folders.

    Args:
        doc_path: Full relative path within the SharePoint library as stored in
                  the ``doc_path`` index field, e.g. ``FolderA/Leave Policy.pdf``.

    Returns:
        Number of chunks deleted (0 means the document was not found).
    """
    from azure.search.documents.aio import SearchClient

    search_credential = DefaultAzureCredential()

    # Single-quote escape to prevent OData injection from values containing apostrophes.
    escaped = doc_path.replace("'", "''")
    deleted = 0

    async with SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=search_credential,
    ) as client:
        while True:
            results = await client.search(
                search_text="*",
                filter=f"doc_path eq '{escaped}'",
                select=["id"],
                top=1000,
            )
            ids = [r["id"] async for r in results]
            if not ids:
                break
            await client.delete_documents(documents=[{"id": i} for i in ids])
            deleted += len(ids)

    return deleted
