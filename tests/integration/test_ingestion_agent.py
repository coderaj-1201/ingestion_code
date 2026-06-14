"""
Integration tests for the Ingestion Agent HTTP endpoints.

SharePoint Graph API calls are mocked. Tests verify request validation,
webhook handling, dedup logic, and error responses.

The FastAPI TestClient is used to drive all HTTP endpoints without starting
a real server. The lifespan startup (which resolves SharePoint sites) is
bypassed by pre-populating _site_cache and _url_to_domain module-level dicts.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    DUMMY_DOC_URL,
    DUMMY_DRIVE_ID,
    DUMMY_ITEM_ID,
    DUMMY_SHA256,
    DUMMY_SITE_ID,
)

DUMMY_SITE_URL = "https://ironman.sharepoint.com/sites/HR"
WEBHOOK_SECRET = "somesecret-32chars-minimum-value"

# Two dummy file items returned by graph_client.list_folder_items
DUMMY_FILE_ITEMS = [
    {
        "id":       DUMMY_ITEM_ID,
        "name":     "leave-policy.pdf",
        "webUrl":   DUMMY_DOC_URL,
        "parentReference": {"driveId": DUMMY_DRIVE_ID, "siteId": DUMMY_SITE_ID},
    },
    {
        "id":       "01ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "name":     "handbook.docx",
        "webUrl":   "https://ironman.sharepoint.com/sites/HR/Documents/handbook.docx",
        "parentReference": {"driveId": DUMMY_DRIVE_ID, "siteId": DUMMY_SITE_ID},
    },
]


def _make_client(extra_patches: dict | None = None):
    """
    Build a TestClient with all external calls mocked and _site_cache pre-populated.
    Returns (client, patch_context_manager_dict).
    """
    import agents.ingestion_agent as ia

    # Pre-populate startup caches so lifespan resolution is skipped
    ia._site_cache[DUMMY_SITE_URL]   = (DUMMY_SITE_ID, DUMMY_DRIVE_ID)
    ia._url_to_domain[DUMMY_SITE_URL] = "hr"

    return ia.app


@pytest.fixture()
def client():
    """
    TestClient with all Azure and Graph calls mocked.
    Uses a context manager to keep patches active for the test duration.
    """
    import agents.ingestion_agent as ia

    ia._site_cache.clear()
    ia._url_to_domain.clear()
    ia._site_cache[DUMMY_SITE_URL]    = (DUMMY_SITE_ID, DUMMY_DRIVE_ID)
    ia._url_to_domain[DUMMY_SITE_URL] = "hr"

    patches = [
        patch("agents.ingestion_agent.graph_client.resolve_site_and_drive",
              new_callable=AsyncMock,
              return_value=(DUMMY_SITE_ID, DUMMY_DRIVE_ID)),
        patch("agents.ingestion_agent.graph_client.list_folder_items",
              new_callable=AsyncMock,
              return_value=DUMMY_FILE_ITEMS),
        patch("agents.ingestion_agent.graph_client.download_file",
              new_callable=AsyncMock,
              return_value=b"fake file content bytes for testing"),
        patch("agents.ingestion_agent.graph_client.get_changed_items",
              new_callable=AsyncMock,
              return_value=([], "dummy_delta_token")),
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
        patch("agents.ingestion_agent._blob_sha256",             new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent.send_to_queue",            new_callable=AsyncMock),
        patch("agents.ingestion_agent.ingestion_workflow.run",   new_callable=AsyncMock,
              return_value=MagicMock(get_outputs=lambda: [{"total": 2, "success": 2, "failed": 0}])),
    ]
    started = [p.start() for p in patches]

    with TestClient(ia.app, raise_server_exceptions=True) as c:
        yield c

    for p in patches:
        p.stop()


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_returns_healthy(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ── /webhook/sharepoint ───────────────────────────────────────────────────────

def test_webhook_validation_handshake(client):
    resp = client.post("/webhook/sharepoint?validationToken=abc123")
    assert resp.status_code == 200
    assert resp.text == "abc123"
    assert "text/plain" in resp.headers["content-type"]


def test_webhook_rejects_invalid_client_state(client):
    body = {
        "value": [
            {
                "clientState": "wrong-secret",
                "resource":    f"/sites/{DUMMY_SITE_ID}/drive/root",
            }
        ]
    }
    resp = client.post("/webhook/sharepoint", json=body)
    assert resp.status_code == 401


def test_webhook_accepts_valid_notification(client):
    import agents.ingestion_agent as ia
    from unittest.mock import AsyncMock, patch

    changed_item = {
        "id":       DUMMY_ITEM_ID,
        "name":     "leave-policy.pdf",
        "webUrl":   DUMMY_DOC_URL,
        "parentReference": {"driveId": DUMMY_DRIVE_ID, "siteId": DUMMY_SITE_ID},
    }

    body = {
        "value": [
            {
                "clientState": WEBHOOK_SECRET,
                "resource":    f"/sites/{DUMMY_SITE_ID}/drive/root",
            }
        ]
    }

    with patch(
        "agents.ingestion_agent.graph_client.get_changed_items",
        new_callable=AsyncMock,
        return_value=([changed_item], "new_delta_token"),
    ):
        resp = client.post("/webhook/sharepoint", json=body)

    assert resp.status_code == 202


# ── /ingest/folder ────────────────────────────────────────────────────────────

def test_ingest_folder_returns_queued_for_valid_site_url(client):
    resp = client.post("/ingest/folder", json={
        "site_url":    DUMMY_SITE_URL,
        "folder_path": "/Documents",
        "domain":      "hr",
        "recursive":   True,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_ingest_folder_returns_no_files_when_folder_empty(client):
    import agents.ingestion_agent as ia

    with patch(
        "agents.ingestion_agent.graph_client.list_folder_items",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = client.post("/ingest/folder", json={
            "site_url":    DUMMY_SITE_URL,
            "folder_path": "/Empty",
            "domain":      "hr",
        })
    assert resp.json()["status"] == "no_supported_files"


def test_ingest_folder_rejects_missing_site_url_and_site_id(client):
    import agents.ingestion_agent as ia

    # Override resolve to simulate on-demand resolution failing when no cache match
    with patch(
        "agents.ingestion_agent.graph_client.resolve_site_and_drive",
        new_callable=AsyncMock,
        side_effect=Exception("Cannot resolve"),
    ):
        resp = client.post("/ingest/folder", json={
            "site_url":  "",
            "site_id":   "",
            "folder_path": "/Documents",
            "domain":    "hr",
        })
    assert resp.status_code == 400


def test_ingest_folder_filters_unsupported_file_types(client):
    unsupported_items = [
        {"id": "01A", "name": "data.csv",  "webUrl": "https://example.com/data.csv",
         "parentReference": {"driveId": DUMMY_DRIVE_ID, "siteId": DUMMY_SITE_ID}},
        {"id": "01B", "name": "notes.txt", "webUrl": "https://example.com/notes.txt",
         "parentReference": {"driveId": DUMMY_DRIVE_ID, "siteId": DUMMY_SITE_ID}},
    ]
    with patch(
        "agents.ingestion_agent.graph_client.list_folder_items",
        new_callable=AsyncMock,
        return_value=unsupported_items,
    ):
        resp = client.post("/ingest/folder", json={
            "site_url": DUMMY_SITE_URL,
            "domain":   "hr",
        })
    assert resp.json()["status"] == "no_supported_files"


# ── ingest_one_file dedup logic ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_one_file_skips_on_sha_match():
    """When blob SHA matches the new file SHA, send_to_queue must NOT be called."""
    file_content  = b"identical file content"
    expected_sha  = hashlib.sha256(file_content).hexdigest()

    with (
        patch("agents.ingestion_agent.graph_client.download_file",
              new_callable=AsyncMock, return_value=file_content),
        patch("agents.ingestion_agent._blob_sha256",
              new_callable=AsyncMock, return_value=expected_sha),
        patch("agents.ingestion_agent.send_to_queue",
              new_callable=AsyncMock) as mock_queue,
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
    ):
        from agents.ingestion_agent import ingest_one_file
        from shared.models import IngestionTask

        task = IngestionTask(
            domain    = "hr",
            doc_name  = "leave-policy.pdf",
            doc_url   = DUMMY_DOC_URL,
            file_type = "pdf",
            blob_path = "hr/leave-policy.pdf",
            site_id   = DUMMY_SITE_ID,
            drive_id  = DUMMY_DRIVE_ID,
            item_id   = DUMMY_ITEM_ID,
        )
        await ingest_one_file(task)

    mock_queue.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_one_file_uploads_blob_and_queues_task():
    """When no existing blob SHA, the file must be uploaded and queued."""
    file_content = b"new policy document content"

    with (
        patch("agents.ingestion_agent.graph_client.download_file",
              new_callable=AsyncMock, return_value=file_content),
        patch("agents.ingestion_agent._blob_sha256",
              new_callable=AsyncMock, return_value=None),
        patch("agents.ingestion_agent.send_to_queue",
              new_callable=AsyncMock) as mock_queue,
        patch("agents.ingestion_agent._upload_to_blob_with_sha",
              new_callable=AsyncMock) as mock_upload,
    ):
        from agents.ingestion_agent import ingest_one_file
        from shared.models import IngestionTask

        task = IngestionTask(
            domain    = "hr",
            doc_name  = "leave-policy.pdf",
            doc_url   = DUMMY_DOC_URL,
            file_type = "pdf",
            blob_path = "hr/leave-policy.pdf",
            site_id   = DUMMY_SITE_ID,
            drive_id  = DUMMY_DRIVE_ID,
            item_id   = DUMMY_ITEM_ID,
        )
        await ingest_one_file(task)

    mock_upload.assert_called_once()
    mock_queue.assert_called_once()


@pytest.mark.asyncio
async def test_ingest_one_file_delete_signal_queues_without_download():
    """For delete tasks, graph download must NOT be called; queue IS called with is_delete=True."""
    with (
        patch("agents.ingestion_agent.graph_client.download_file",
              new_callable=AsyncMock) as mock_download,
        patch("agents.ingestion_agent.send_to_queue",
              new_callable=AsyncMock) as mock_queue,
        patch("agents.ingestion_agent._upload_to_blob_with_sha", new_callable=AsyncMock),
    ):
        from agents.ingestion_agent import ingest_one_file
        from shared.models import IngestionTask

        task = IngestionTask(
            domain    = "hr",
            doc_name  = "old-policy.pdf",
            file_type = "pdf",
            blob_path = "hr/old-policy.pdf",
            is_delete = True,
        )
        await ingest_one_file(task)

    mock_download.assert_not_called()
    mock_queue.assert_called_once()
    queued_payload = mock_queue.call_args[0][1]
    assert queued_payload["is_delete"] is True
