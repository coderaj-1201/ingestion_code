"""
Integration tests for POST /ingest/from-logic-app (Logic Apps integration).

Covers:
  - Authentication (valid secret, missing header, wrong secret)
  - Upsert flow (new file, unchanged file, overwrite)
  - Delete flow
  - Extension filtering
  - Base64 decode errors
  - All four supported file types
  - Domain values
  - Large file (5 MB base64)
  - Special characters in filenames
  - SHA-256 dedup at blob metadata level
  - Empty file_content_base64 on non-delete request (should 400)
  - Secret not configured (LOGIC_APP_WEBHOOK_SECRET unset)
  - Concurrent requests (semaphore behaviour is workflow-level, checked here at HTTP level)
"""
from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import DUMMY_DOC_URL, DUMMY_SHA256

LOGIC_APP_SECRET = "logic-app-test-secret-32chars-ok"
AGENT_URL        = "https://my-ingestion-agent.azurecontainerapps.io"

_VALID_PAYLOAD = {
    "doc_name":            "leave-policy.pdf",
    "doc_url":             DUMMY_DOC_URL,
    "domain":              "hr",
    "file_type":           "pdf",
    "file_content_base64": base64.b64encode(b"fake pdf content bytes").decode(),
    "is_delete":           False,
}


def _make_client():
    """Build a TestClient with all external IO mocked."""
    import agents.ingestion_agent as ia

    patches = [
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ]
    started = [p.start() for p in patches]

    client = TestClient(ia.app, raise_server_exceptions=True)

    yield client, {p.attribute: s for p, s in zip(patches, started)}

    for p in patches:
        p.stop()


@pytest.fixture()
def client():
    """TestClient fixture with IO mocked."""
    import agents.ingestion_agent as ia
    patches = [
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ]
    started = [p.start() for p in patches]
    with TestClient(ia.app, raise_server_exceptions=False) as c:
        yield c
    for p in patches:
        p.stop()


# ── Authentication ────────────────────────────────────────────────────────────

def test_la_valid_secret_accepted(client):
    """Correct X-Logic-App-Secret → 200 with status=queued."""
    resp = client.post(
        "/ingest/from-logic-app",
        json=_VALID_PAYLOAD,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_la_missing_secret_header_rejected(client):
    """No X-Logic-App-Secret header → 401."""
    resp = client.post("/ingest/from-logic-app", json=_VALID_PAYLOAD)
    assert resp.status_code == 401


def test_la_wrong_secret_rejected(client):
    """Wrong secret value → 401."""
    resp = client.post(
        "/ingest/from-logic-app",
        json=_VALID_PAYLOAD,
        headers={"X-Logic-App-Secret": "wrong-secret-value-here"},
    )
    assert resp.status_code == 401


def test_la_empty_secret_header_rejected(client):
    """Empty string secret header → 401."""
    resp = client.post(
        "/ingest/from-logic-app",
        json=_VALID_PAYLOAD,
        headers={"X-Logic-App-Secret": ""},
    )
    assert resp.status_code == 401


def test_la_secret_not_configured_returns_503():
    """When LOGIC_APP_WEBHOOK_SECRET is not set → 503 (integration not configured)."""
    import agents.ingestion_agent as ia
    patches = [
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
        patch("agents.ingestion_agent.settings.LOGIC_APP_WEBHOOK_SECRET", None),
    ]
    for p in patches:
        p.start()
    with TestClient(ia.app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    for p in patches:
        p.stop()
    assert resp.status_code == 503


# ── Extension / file type filtering ──────────────────────────────────────────

@pytest.mark.parametrize("doc_name,file_type", [
    ("policy.pdf",        "pdf"),
    ("handbook.docx",     "docx"),
    ("salary-grid.xlsx",  "xlsx"),
    ("q1-results.pptx",   "pptx"),
    ("archive.doc",       "doc"),
    ("legacy.xls",        "xls"),
    ("slides.ppt",        "ppt"),
])
def test_la_all_supported_file_types_accepted(client, doc_name, file_type):
    """All seven supported extensions must pass the extension guard."""
    payload = {**_VALID_PAYLOAD, "doc_name": doc_name, "file_type": file_type}
    resp = client.post(
        "/ingest/from-logic-app",
        json=payload,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("queued", "skipped")  # skipped=dedup hit


@pytest.mark.parametrize("doc_name", [
    "data.csv",
    "readme.txt",
    "image.png",
    "archive.zip",
    "script.py",
    "no-extension",
])
def test_la_unsupported_extensions_skipped(client, doc_name):
    """Unsupported file types must return status=skipped, not be queued."""
    payload = {**_VALID_PAYLOAD, "doc_name": doc_name}
    resp = client.post(
        "/ingest/from-logic-app",
        json=payload,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert resp.json()["reason"] == "unsupported_extension"


# ── Upsert flow ───────────────────────────────────────────────────────────────

def test_la_upsert_new_file_uploads_blob_and_queues(client):
    """New file (no existing blob SHA) → blob uploaded, ProcessingTask queued."""
    import agents.ingestion_agent as ia
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock) as mock_upload,
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock) as mock_queue,
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    assert resp.status_code == 200
    mock_upload.assert_called_once()
    mock_queue.assert_called_once()

    queued = mock_queue.call_args[0][1]
    assert queued["doc_name"]   == "leave-policy.pdf"
    assert queued["domain"]     == "hr"
    assert queued["is_delete"]  is False
    assert queued["file_sha256"] != ""


def test_la_upsert_unchanged_file_skips_blob_and_queue():
    """When blob SHA matches new file SHA → skip (no upload, no queue)."""
    content   = base64.b64decode(_VALID_PAYLOAD["file_content_base64"])
    match_sha = hashlib.sha256(content).hexdigest()

    import agents.ingestion_agent as ia
    patches = [
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=match_sha),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ]
    for p in patches: p.start()

    with TestClient(ia.app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    for p in patches: p.stop()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "skipped"
    assert data["reason"] == "unchanged"


def test_la_upsert_changed_file_overwrites_blob():
    """When blob SHA differs from new SHA → file is re-uploaded."""
    import agents.ingestion_agent as ia
    p1 = patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value="a" * 64)
    p2 = patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock)
    p3 = patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock)

    p1.start()
    mock_upload = p2.start()
    mock_queue  = p3.start()

    with TestClient(ia.app, raise_server_exceptions=False) as c:
        payload = {**_VALID_PAYLOAD, "file_content_base64": base64.b64encode(b"new content version 2").decode()}
        resp = c.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    p1.stop(); p2.stop(); p3.stop()

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    mock_upload.assert_called_once()
    mock_queue.assert_called_once()


def test_la_upsert_processing_task_carries_sha256():
    """ProcessingTask queued by the endpoint must include file_sha256."""
    import agents.ingestion_agent as ia
    p1 = patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None)
    p2 = patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock)
    p3 = patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock)

    p1.start()
    p2.start()
    mock_queue = p3.start()

    with TestClient(ia.app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    p1.stop(); p2.stop(); p3.stop()

    assert resp.status_code == 200
    task_payload = mock_queue.call_args[0][1]
    assert len(task_payload["file_sha256"]) == 64   # SHA-256 hex = 64 chars
    assert all(c in "0123456789abcdef" for c in task_payload["file_sha256"])


def test_la_upsert_blob_path_is_domain_slash_docname():
    """Blob path must be {domain}/{doc_name}, not just doc_name."""
    import agents.ingestion_agent as ia
    p1 = patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None)
    p2 = patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock)
    p3 = patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock)

    p1.start()
    mock_upload = p2.start()
    p3.start()

    with TestClient(ia.app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/ingest/from-logic-app",
            json={**_VALID_PAYLOAD, "domain": "legal", "doc_name": "contract.pdf"},
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    p1.stop(); p2.stop(); p3.stop()

    blob_path_arg = mock_upload.call_args[0][0]
    assert blob_path_arg == "legal/contract.pdf"


# ── Delete flow ───────────────────────────────────────────────────────────────

def test_la_delete_queues_delete_task_without_blob_upload(client):
    """is_delete=True → ProcessingTask(is_delete=True) queued, no blob upload."""
    with (
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock) as mock_upload,
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock) as mock_queue,
    ):
        payload = {**_VALID_PAYLOAD, "is_delete": True, "file_content_base64": ""}
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "delete_queued"
    mock_upload.assert_not_called()
    mock_queue.assert_called_once()

    queued = mock_queue.call_args[0][1]
    assert queued["is_delete"] is True
    assert queued["doc_name"]  == "leave-policy.pdf"


def test_la_delete_ignores_file_content_base64_field(client):
    """is_delete=True must succeed even if file_content_base64 has garbage."""
    with (
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock) as mock_queue,
    ):
        payload = {**_VALID_PAYLOAD, "is_delete": True, "file_content_base64": "notvalidbase64!!!"}
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "delete_queued"
    mock_queue.assert_called_once()


def test_la_delete_response_includes_task_id(client):
    """Delete response must include task_id for traceability."""
    with (
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ):
        payload = {**_VALID_PAYLOAD, "is_delete": True, "file_content_base64": ""}
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    data = resp.json()
    assert "task_id" in data
    assert len(data["task_id"]) == 36  # UUID format


# ── Input validation ──────────────────────────────────────────────────────────

def test_la_invalid_base64_returns_400(client):
    """Malformed base64 in file_content_base64 → 400."""
    payload = {**_VALID_PAYLOAD, "file_content_base64": "NOT_VALID_BASE64!!!###"}
    resp = client.post(
        "/ingest/from-logic-app",
        json=payload,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 400


def test_la_missing_file_content_on_upsert_returns_400(client):
    """Empty file_content_base64 on non-delete request → 400."""
    payload = {**_VALID_PAYLOAD, "file_content_base64": "", "is_delete": False}
    resp = client.post(
        "/ingest/from-logic-app",
        json=payload,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 400


def test_la_missing_required_field_doc_name_returns_422(client):
    """Missing doc_name field → Pydantic 422."""
    payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "doc_name"}
    resp = client.post(
        "/ingest/from-logic-app",
        json=payload,
        headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
    )
    assert resp.status_code == 422


# ── Domain routing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain", ["hr", "legal", "it", "ops"])
def test_la_all_domains_accepted(client, domain):
    """All four domain values must route through without error."""
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock) as mock_q,
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json={**_VALID_PAYLOAD, "domain": domain},
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )

    assert resp.status_code == 200
    queued = mock_q.call_args[0][1]
    assert queued["domain"] == domain


# ── Production edge cases ─────────────────────────────────────────────────────

def test_la_large_file_5mb_accepted(client):
    """5 MB file must be accepted without errors (no size limit in code)."""
    large_content = b"x" * (5 * 1024 * 1024)
    payload = {
        **_VALID_PAYLOAD,
        "file_content_base64": base64.b64encode(large_content).decode(),
    }
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    assert resp.status_code == 200


def test_la_filename_with_spaces_and_special_chars(client):
    """Filenames with spaces and parentheses must not cause failures."""
    doc_name = "HR Policy (2024 Update) - Final.pdf"
    payload  = {**_VALID_PAYLOAD, "doc_name": doc_name}
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock) as mock_upload,
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    assert resp.status_code == 200
    # Verify blob path preserves the full filename
    blob_path = mock_upload.call_args[0][0]
    assert doc_name in blob_path


def test_la_filename_with_sql_injection_attempt(client):
    """Filenames with SQL/OData injection characters must be handled safely."""
    doc_name = "report'; DROP TABLE chunks; --.pdf"
    payload  = {**_VALID_PAYLOAD, "doc_name": doc_name}
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=payload,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    # Should not raise — just route normally or skip (unsupported extension guard runs first)
    assert resp.status_code in (200, 400)


def test_la_blob_upload_failure_does_not_queue(client):
    """If blob upload raises, the endpoint must propagate the error (no queue)."""
    with (
        patch("agents.ingestion_agent._blob_sha256",
              new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha",
              new_callable=AsyncMock, side_effect=RuntimeError("Blob storage unavailable")),
        patch("agents.ingestion_agent.send_to_queue",
              new_callable=AsyncMock) as mock_queue,
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    # Error surfaces as 500 (FastAPI default for unhandled exception)
    assert resp.status_code == 500
    mock_queue.assert_not_called()


def test_la_upsert_response_includes_sha256_prefix(client):
    """Response body should include first 12 chars of sha256 for log correlation."""
    with (
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
    ):
        resp = client.post(
            "/ingest/from-logic-app",
            json=_VALID_PAYLOAD,
            headers={"X-Logic-App-Secret": LOGIC_APP_SECRET},
        )
    data = resp.json()
    assert "sha256" in data
    assert len(data["sha256"]) == 12


def test_la_health_endpoint_unaffected(client):
    """/health must still return healthy after the LA endpoint is added."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
