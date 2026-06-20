"""
AI Search helpers for the Embedding Agent.

Responsible for uploading embedded chunks and deleting all chunks for a
document. The Embedding Agent is the sole writer to the AI Search index —
all other agents only read or delete.

Batching constants
------------------
``_SEARCH_BATCH_SIZE``     — documents per ``upload_documents`` call (Search
                             batch limit is 1 000; 100 is a safe default).
``_DELETE_MAX_ITERATIONS`` — safety cap for delete pagination to guard against
                             infinite loops if Search returns stale results.
"""
from __future__ import annotations

import asyncio
import logging

from shared.azure_clients import get_search_client
from shared.models import RawChunk

logger = logging.getLogger(__name__)

_SEARCH_BATCH_SIZE = 100
_DELETE_MAX_ITERATIONS = 50  # 50 × 1 000 = 50 000 chunks max per delete


def odata_str(value: str) -> str:
    """Wrap ``value`` for safe use in an OData filter string literal.

    Escapes internal single-quotes by doubling them, which is the OData
    string-literal escaping convention (analogous to SQL).

    Example::

        odata_str("O'Brien") == "'O''Brien'"
    """
    return "'" + value.replace("'", "''") + "'"


def check_upload_results(results: list, label: str) -> None:
    """Raise ``RuntimeError`` if any document in an upload batch failed.

    Prevents silent data loss — a partial upload without this check would look
    like a success but leave chunks missing from the index.

    Args:
        results: List of upload result objects returned by the Search SDK.
        label:   Human-readable description used in the error message.

    Raises:
        RuntimeError: If one or more documents failed to upload.
    """
    failed = [r for r in results if not r.succeeded]
    if failed:
        raise RuntimeError(
            f"{len(failed)} of {len(results)} documents failed to upload ({label})"
        )


async def upload_to_search(
    embedded: list[tuple[RawChunk, list[float]]],
    doc_name: str,
) -> int:
    """Batch-upload embedded chunks to the AI Search index.

    Iterates over ``embedded`` in slices of ``_SEARCH_BATCH_SIZE`` and calls
    ``upload_documents`` for each batch. Partial failures are logged as
    warnings (not raised) so that a single bad chunk does not abort the entire
    document upload.

    Args:
        embedded:  List of ``(chunk, embedding_vector)`` tuples.
        doc_name:  Document name — used only for log messages.

    Returns:
        Total count of successfully uploaded documents.
    """
    search = get_search_client()
    total = 0

    for i in range(0, len(embedded), _SEARCH_BATCH_SIZE):
        batch = embedded[i: i + _SEARCH_BATCH_SIZE]
        docs = []
        for chunk, vector in batch:
            doc = chunk.to_search_doc()
            doc["content_vector"] = vector
            docs.append(doc)

        results = await asyncio.to_thread(search.upload_documents, docs)
        succeeded = sum(1 for r in results if r.succeeded)
        failed = sum(1 for r in results if not r.succeeded)
        total += succeeded

        if failed:
            logger.warning(
                "Search upload: %d succeeded, %d failed for doc=%s batch=%d",
                succeeded, failed, doc_name, i,
            )
        else:
            logger.debug(
                "Search upload batch %d: %d docs for doc=%s",
                i, succeeded, doc_name,
            )

    return total


async def delete_from_search(doc_name: str) -> int:
    """Remove all chunks for ``doc_name`` from the AI Search index.

    Paginates in batches of 1 000 until no results remain. A single
    ``top=1000`` call would silently leave orphans for large documents
    (> 1 000 chunks). Capped at ``_DELETE_MAX_ITERATIONS`` as a safety guard
    against infinite loops if Search returns stale results after a delete.

    Args:
        doc_name: Value of the ``doc_name`` index field to filter on.

    Returns:
        Total number of chunks deleted.
    """
    search = get_search_client()
    deleted = 0

    for _iteration in range(_DELETE_MAX_ITERATIONS):
        results = await asyncio.to_thread(
            search.search,
            search_text="*",
            filter=f"doc_name eq {odata_str(doc_name)}",
            select=["id"],
            top=1000,
        )
        ids = [r["id"] for r in results]
        if not ids:
            break

        for i in range(0, len(ids), _SEARCH_BATCH_SIZE):
            batch = [{"id": doc_id} for doc_id in ids[i: i + _SEARCH_BATCH_SIZE]]
            result = await asyncio.to_thread(search.delete_documents, batch)
            deleted += sum(1 for r in result if r.succeeded)
    else:
        logger.warning(
            "delete_from_search hit iteration cap (%d) for doc_name=%s — "
            "%d chunks deleted, index may still have orphans",
            _DELETE_MAX_ITERATIONS, doc_name, deleted,
        )

    logger.info("Deleted %d chunks for doc_name=%s", deleted, doc_name)
    return deleted
