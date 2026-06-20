"""
Processing agent sub-package.

Public surface re-exported here so callers import from ``agents.processing``
rather than knowing the internal module layout.
"""
from agents.processing.blob_ops import (
    get_blob_client,
    download_blob,
    upload_blob,
    delete_blobs,
    sha256_already_indexed,
)
from agents.processing.pipeline import (
    download_raw_file,
    run_parser,
    upload_processed_chunks,
    queue_embedding_task,
    run_processing,
    processing_workflow,
)

__all__ = [
    "get_blob_client",
    "download_blob",
    "upload_blob",
    "delete_blobs",
    "sha256_already_indexed",
    "download_raw_file",
    "run_parser",
    "upload_processed_chunks",
    "queue_embedding_task",
    "run_processing",
    "processing_workflow",
]
