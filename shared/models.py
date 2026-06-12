"""
Shared models for the ingestion pipeline.
Schema is the single source of truth for AI Search index fields.
100% compatible with retrieval pipeline hybrid_search_tool.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Domain(StrEnum):
    HR    = "hr"
    LEGAL = "legal"
    IT    = "it"
    OPS = "ops"


class FileType(StrEnum):
    PDF  = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"


class ChunkType(StrEnum):
    TITLE     = "title"
    HEADING   = "heading"
    PARAGRAPH = "paragraph"
    TABLE     = "table"


class TriggerType(StrEnum):
    WEBHOOK = "webhook"
    MANUAL  = "manual"


# ── Service Bus payloads ──────────────────────────────────────────────────────

@dataclass
class IngestionTask:
    """Ingestion Agent → Processing Agent."""
    task_id: str           = field(default_factory=lambda: str(uuid4()))
    domain: str            = ""
    file_type: str         = ""
    doc_name: str          = ""
    doc_url: str           = ""
    blob_path: str         = ""        # raw/<domain>/<filename>
    site_id: str           = ""
    drive_id: str          = ""
    item_id: str           = ""
    trigger_type: str      = TriggerType.WEBHOOK
    is_delete: bool        = False
    file_sha256: str       = ""        # SHA-256 hex digest of raw file bytes


@dataclass
class ProcessingTask:
    """Processing Agent → Embedding Agent."""
    task_id: str           = field(default_factory=lambda: str(uuid4()))
    ingestion_task_id: str = ""
    domain: str            = ""
    doc_name: str          = ""
    doc_url: str           = ""
    file_type: str         = ""
    processed_blob_path: str = ""      # processed/<domain>/<doc_name>.json
    chunk_count: int       = 0
    is_delete: bool        = False
    file_sha256: str       = ""        # propagated from IngestionTask


# ── Core chunk — maps 1:1 to AI Search index fields ──────────────────────────

@dataclass
class RawChunk:
    """
    Full schema. Every field maps to an AI Search index field.
    Retrieval pipeline selects: id, content, source, domain, content_vector
    plus extended fields for richer responses.
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    chunk_id: str              = field(default_factory=lambda: str(uuid4()))
    parent_id: str             = ""        # empty = this IS the parent
    chunk_type: str            = ChunkType.PARAGRAPH

    # ── Document provenance ───────────────────────────────────────────────────
    domain: str                = ""
    doc_name: str              = ""        # "Leave Policy 2024.pdf"
    source: str                = ""        # alias for doc_name — used by retrieval
    doc_url: str               = ""        # SharePoint URL
    file_type: str             = ""
    blob_path: str             = ""        # raw blob path
    ingested_at: str           = ""        # ISO 8601

    # ── Position in document ──────────────────────────────────────────────────
    page_number: int           = 0
    title: str                 = ""        # document-level title
    section_heading: str       = ""        # nearest H1/H2
    section_subheading: str    = ""        # nearest H3/H4

    # ── Content ───────────────────────────────────────────────────────────────
    content: str               = ""        # NL text — what gets embedded
    table_raw: str             = ""        # original markdown table (tables only)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    file_sha256: str           = ""        # SHA-256 of the source file — dedup key
    is_deleted: bool           = False

    def to_search_doc(self) -> dict:
        """Serialise to AI Search upload format. content_vector added by Embedding Agent."""
        return {
            "id":                  self.chunk_id,
            "parent_id":           self.parent_id,
            "chunk_type":          self.chunk_type,
            "domain":              self.domain,
            "doc_name":            self.doc_name,
            "source":              self.doc_name,   # retrieval uses 'source'
            "doc_url":             self.doc_url,
            "file_type":           self.file_type,
            "blob_path":           self.blob_path,
            "ingested_at":         self.ingested_at,
            "page_number":         self.page_number,
            "title":               self.title,
            "section_heading":     self.section_heading,
            "section_subheading":  self.section_subheading,
            "content":             self.content,
            "table_raw":           self.table_raw,
            "file_sha256":         self.file_sha256,
            "is_deleted":          self.is_deleted,
        }


# ── HTTP API models ───────────────────────────────────────────────────────────

class ManualIngestRequest(BaseModel):
    """POST /ingest/folder
 
    Prefer site_url over site_id.
    site_url: full SharePoint site URL, e.g. https://ironman.sharepoint.com/sites/HR
    site_id:  raw Graph site ID (accepted for backwards compatibility)
    """
    site_url: str     = ""   # preferred — stable human-readable URL
    site_id: str      = ""   # fallback — raw Graph site ID
    folder_path: str  = "/"
    domain: Domain    = Domain.HR
    recursive: bool   = True
 
 
class WebhookNotification(BaseModel):
    """SharePoint change notification POST body."""
    value: list[dict[str, Any]] = Field(default_factory=list)


class IngestStatusResponse(BaseModel):
    task_id: str
    status: str
    message: str = ""
