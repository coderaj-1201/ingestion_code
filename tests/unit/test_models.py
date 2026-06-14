"""
Unit tests for shared/models.py.

Tests the RawChunk dataclass, IngestionTask, ProcessingTask, and ManualIngestRequest.
Assumptions:
- No Azure services are contacted.
- ManualIngestRequest uses pydantic Domain enum validation — only 'hr', 'legal', 'it', 'ops' are valid.
- RawChunk.to_search_doc() maps 'source' to doc_name (not the source field directly).
"""
from __future__ import annotations

import re
from uuid import UUID

import pytest
from pydantic import ValidationError

from shared.models import (
    ChunkType,
    Domain,
    IngestionTask,
    ManualIngestRequest,
    ProcessingTask,
    RawChunk,
)


def test_rawchunk_defaults_are_safe():
    chunk = RawChunk()
    assert chunk.parent_id          == ""
    assert chunk.domain             == ""
    assert chunk.doc_name           == ""
    assert chunk.source             == ""
    assert chunk.doc_url            == ""
    assert chunk.file_type          == ""
    assert chunk.blob_path          == ""
    assert chunk.ingested_at        == ""
    assert chunk.title              == ""
    assert chunk.section_heading    == ""
    assert chunk.section_subheading == ""
    assert chunk.content            == ""
    assert chunk.table_raw          == ""
    assert chunk.file_sha256        == ""
    assert chunk.is_deleted         is False
    assert chunk.page_number        == 0


def test_rawchunk_to_search_doc_maps_all_fields():
    chunk = RawChunk(
        chunk_id           = "cid-001",
        parent_id          = "pid-001",
        chunk_type         = ChunkType.PARAGRAPH,
        domain             = "hr",
        doc_name           = "policy.pdf",
        source             = "policy.pdf",
        doc_url            = "https://example.com/policy.pdf",
        file_type          = "pdf",
        blob_path          = "hr/policy.pdf",
        ingested_at        = "2024-01-01T00:00:00+00:00",
        page_number        = 3,
        title              = "HR Policy",
        section_heading    = "Leave",
        section_subheading = "Annual Leave",
        content            = "All employees get 20 days.",
        table_raw          = "",
        file_sha256        = "a" * 64,
        is_deleted         = False,
    )
    doc = chunk.to_search_doc()

    assert doc["id"]                 == "cid-001"
    assert doc["parent_id"]          == "pid-001"
    assert doc["chunk_type"]         == ChunkType.PARAGRAPH
    assert doc["domain"]             == "hr"
    assert doc["doc_name"]           == "policy.pdf"
    assert doc["doc_url"]            == "https://example.com/policy.pdf"
    assert doc["file_type"]          == "pdf"
    assert doc["blob_path"]          == "hr/policy.pdf"
    assert doc["ingested_at"]        == "2024-01-01T00:00:00+00:00"
    assert doc["page_number"]        == 3
    assert doc["title"]              == "HR Policy"
    assert doc["section_heading"]    == "Leave"
    assert doc["section_subheading"] == "Annual Leave"
    assert doc["content"]            == "All employees get 20 days."
    assert doc["file_sha256"]        == "a" * 64
    assert doc["is_deleted"]         is False


def test_rawchunk_to_search_doc_source_equals_doc_name():
    # The retrieval pipeline reads 'source' but to_search_doc() maps source → doc_name
    chunk = RawChunk(doc_name="report.docx", source="something-else")
    doc = chunk.to_search_doc()
    assert doc["source"] == "report.docx"


def test_rawchunk_to_search_doc_excludes_content_vector():
    chunk = RawChunk(content="some text")
    doc = chunk.to_search_doc()
    assert "content_vector" not in doc


def test_ingestion_task_generates_uuid_task_id():
    t1 = IngestionTask()
    t2 = IngestionTask()
    assert t1.task_id != t2.task_id
    UUID(t1.task_id, version=4)  # raises ValueError if not valid UUID4
    UUID(t2.task_id, version=4)


def test_processing_task_is_delete_defaults_false():
    assert ProcessingTask().is_delete is False


def test_manual_ingest_request_domain_defaults_hr():
    req = ManualIngestRequest(site_url="https://example.sharepoint.com/sites/HR")
    assert req.domain == Domain.HR


def test_manual_ingest_request_rejects_invalid_domain():
    with pytest.raises(ValidationError):
        ManualIngestRequest(
            site_url="https://example.sharepoint.com/sites/HR",
            domain="finance",  # not a valid Domain enum value
        )
