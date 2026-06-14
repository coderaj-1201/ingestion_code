"""
Unit tests for shared/logging_config.py — StructuredFormatter.

Verifies:
  - JSON output is valid and parseable
  - Required fields are always present (time, level, service, logger, msg)
  - service field is set correctly from configure_logging() argument
  - Named extra fields (task_id, doc_name, domain, chunk_count, chunk_id) are included
  - Exception info is included when exc_info is set
  - Unknown extra fields are NOT leaked into the JSON (no pollution)
  - Field names are stable (breaking change guard for App Insights KQL queries)
"""
from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from shared.logging_config import StructuredFormatter


def _make_logger(service_name: str = "test-service") -> tuple[logging.Logger, StringIO]:
    """Create a logger wired to a StringIO handler using StructuredFormatter."""
    buf     = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(StructuredFormatter(service_name))
    handler.setLevel(logging.DEBUG)

    logger = logging.getLogger(f"test.{id(buf)}")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, buf


def _last_record(buf: StringIO) -> dict:
    """Parse the last JSON line written to the buffer."""
    lines = [l for l in buf.getvalue().strip().splitlines() if l]
    assert lines, "No log output was produced"
    return json.loads(lines[-1])


# ── Required fields ───────────────────────────────────────────────────────────

def test_log_output_is_valid_json():
    logger, buf = _make_logger()
    logger.info("Hello from test")
    rec = _last_record(buf)
    assert isinstance(rec, dict)


def test_log_contains_all_required_fields():
    """time, level, service, logger, msg must always be present."""
    logger, buf = _make_logger("rag-ingestion")
    logger.warning("Something happened")
    rec = _last_record(buf)
    for field in ("time", "level", "service", "logger", "msg"):
        assert field in rec, f"Required field '{field}' missing from log record"


def test_service_field_reflects_configure_logging_argument():
    logger, buf = _make_logger("rag-embedding")
    logger.info("embed start")
    rec = _last_record(buf)
    assert rec["service"] == "rag-embedding"


def test_service_field_different_per_service():
    """Each agent must emit a distinct service name — guards App Insights KQL queries."""
    for service in ("rag-ingestion", "rag-processing", "rag-embedding"):
        logger, buf = _make_logger(service)
        logger.info("ping")
        rec = _last_record(buf)
        assert rec["service"] == service


def test_level_field_matches_log_level():
    logger, buf = _make_logger()
    logger.error("bad thing")
    rec = _last_record(buf)
    assert rec["level"] == "ERROR"


def test_msg_field_contains_formatted_message():
    logger, buf = _make_logger()
    logger.info("Processing doc_name=%s", "policy.pdf")
    rec = _last_record(buf)
    assert "policy.pdf" in rec["msg"]


# ── Named extra fields ────────────────────────────────────────────────────────

def test_extra_task_id_appears_in_output():
    logger, buf = _make_logger()
    logger.info("task started", extra={"task_id": "abc-123"})
    rec = _last_record(buf)
    assert rec.get("task_id") == "abc-123"


def test_extra_doc_name_appears_in_output():
    logger, buf = _make_logger()
    logger.info("processing", extra={"doc_name": "leave-policy.pdf"})
    rec = _last_record(buf)
    assert rec.get("doc_name") == "leave-policy.pdf"


def test_extra_domain_appears_in_output():
    logger, buf = _make_logger()
    logger.info("domain tagged", extra={"domain": "hr"})
    rec = _last_record(buf)
    assert rec.get("domain") == "hr"


def test_extra_chunk_count_appears_in_output():
    logger, buf = _make_logger()
    logger.info("chunks ready", extra={"chunk_count": 42})
    rec = _last_record(buf)
    assert rec.get("chunk_count") == 42


def test_extra_chunk_id_appears_in_output():
    logger, buf = _make_logger()
    logger.info("chunk detail", extra={"chunk_id": "chunk-001"})
    rec = _last_record(buf)
    assert rec.get("chunk_id") == "chunk-001"


def test_unknown_extra_fields_not_leaked():
    """Fields not in the allowlist must NOT appear in JSON output (no accidental leakage)."""
    logger, buf = _make_logger()
    logger.info("msg", extra={"password": "secret", "internal_state": "xyz"})
    rec = _last_record(buf)
    assert "password" not in rec
    assert "internal_state" not in rec


# ── Exception handling ────────────────────────────────────────────────────────

def test_exception_info_included_when_exc_info_set():
    logger, buf = _make_logger()
    try:
        raise ValueError("disk full")
    except ValueError:
        logger.error("Storage error", exc_info=True)
    rec = _last_record(buf)
    assert "exception" in rec
    assert "ValueError" in rec["exception"]
    assert "disk full" in rec["exception"]


def test_no_exception_field_when_no_error():
    logger, buf = _make_logger()
    logger.info("all good")
    rec = _last_record(buf)
    assert "exception" not in rec


# ── Field name stability (breaking-change guard) ──────────────────────────────

def test_field_names_are_stable():
    """
    Exact field names used by App Insights KQL queries.
    Renaming any of these would break existing dashboards and alerts.
    If this test fails, update KQL queries BEFORE renaming fields.
    """
    EXPECTED_FIELDS = {"time", "level", "service", "logger", "msg"}
    logger, buf = _make_logger("rag-test")
    logger.info("stability check", extra={
        "task_id": "t1", "doc_name": "doc.pdf", "domain": "hr",
        "chunk_count": 5, "chunk_id": "c1",
    })
    rec = _last_record(buf)
    for field in EXPECTED_FIELDS:
        assert field in rec, f"Stable field '{field}' missing — KQL queries will break"
    # Named extras should also be present
    for field in ("task_id", "doc_name", "domain", "chunk_count", "chunk_id"):
        assert field in rec, f"Named extra '{field}' missing"
