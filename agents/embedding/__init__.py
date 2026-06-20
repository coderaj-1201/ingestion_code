"""
Embedding agent sub-package.

Public surface re-exported here so callers import from ``agents.embedding``
rather than knowing the internal module layout.
"""
from agents.embedding.search_ops import (
    odata_str,
    check_upload_results,
    upload_to_search,
    delete_from_search,
)
from agents.embedding.pipeline import (
    download_processed_chunks,
    embed_chunks,
    run_embedding,
    embedding_workflow,
)

__all__ = [
    "odata_str",
    "check_upload_results",
    "upload_to_search",
    "delete_from_search",
    "download_processed_chunks",
    "embed_chunks",
    "run_embedding",
    "embedding_workflow",
]
