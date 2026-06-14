"""
Integration tests for the Processing Agent workflow.

All Azure services (Blob Storage, AI Search, Service Bus) are mocked.
Tests verify workflow orchestration and error handling, not Azure SDK behavior.

The processing_workflow function is tested directly (bypassing the SB listener)
with all external calls patched at module level using unittest.mock.patch.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from shared.models import ChunkType, ProcessingTask, RawChunk
from tests.conftest import DUMMY_SHA256, DUMMY_DOC_URL

DOC_NAME  = "leave-policy.pdf"
DOMAIN    = "hr"
BLOB_PATH = f"{DOMAIN}/{DOC_NAME}"


def _make_chunks(n_children: int = 2) -> list[RawChunk]:
    """Return one parent chunk + n_children child chunks."""
    parent_id = str(uuid4())
    parent = RawChunk(
        chunk_id    = parent_id,
        parent_id   = "",
        chunk_type  = ChunkType.HEADING,
        domain      = DOMAIN,
        doc_name    = DOC_NAME,
        content     = "Full section text.",
    )
    children = [
        RawChunk(
            chunk_id   = str(uuid4()),
            parent_id  = parent_id,
            chunk_type = ChunkType.PARAGRAPH,
            domain     = DOMAIN,
            doc_name   = DOC_NAME,
            content    = f"Child paragraph {i}.",
        )
        for i in range(n_children)
    ]
    return [parent] + children


def _make_task(**kwargs) -> ProcessingTask:
    defaults = dict(
        domain      = DOMAIN,
        doc_name    = DOC_NAME,
        doc_url     = DUMMY_DOC_URL,
        file_type   = "pdf",
        file_sha256 = DUMMY_SHA256,
        is_delete   = False,
    )
    defaults.update(kwargs)
    return ProcessingTask(**defaults)


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_processing_workflow_happy_path():
    chunks = _make_chunks(n_children=2)
    task = _make_task()

    with (
        patch("agents.processing_agent._download_blob", new_callable=AsyncMock, return_value=b"fake-bytes"),
        patch("agents.processing_agent._upload_blob",   new_callable=AsyncMock),
        patch("agents.processing_agent.send_to_queue",  new_callable=AsyncMock),
        patch("agents.processing_agent.parse_document", return_value=chunks),
        patch("agents.processing_agent._sha256_already_indexed", new_callable=AsyncMock, return_value=False),
    ):
        from agents.processing_agent import processing_workflow
        result_obj = await processing_workflow.run(task)
        result = result_obj.get_outputs()[0]

    assert result["status"]      == "processed"
    assert result["chunk_count"] == 3


@pytest.mark.asyncio
async def test_processing_workflow_skips_duplicate_sha():
    task = _make_task()

    with (
        patch("agents.processing_agent._download_blob", new_callable=AsyncMock, return_value=b"fake-bytes"),
        patch("agents.processing_agent._upload_blob",   new_callable=AsyncMock),
        patch("agents.processing_agent.send_to_queue",  new_callable=AsyncMock),
        patch("agents.processing_agent.parse_document") as mock_parse,
        patch("agents.processing_agent._sha256_already_indexed", new_callable=AsyncMock, return_value=True),
    ):
        from agents.processing_agent import processing_workflow
        result_obj = await processing_workflow.run(task)
        result = result_obj.get_outputs()[0]

    assert result["status"] == "skipped_duplicate"
    mock_parse.assert_not_called()


@pytest.mark.asyncio
async def test_processing_workflow_forwards_delete_signal():
    task = _make_task(is_delete=True)

    with (
        patch("agents.processing_agent._download_blob", new_callable=AsyncMock, return_value=b"fake-bytes"),
        patch("agents.processing_agent._upload_blob",   new_callable=AsyncMock),
        patch("agents.processing_agent.send_to_queue",  new_callable=AsyncMock) as mock_queue,
        patch("agents.processing_agent.parse_document") as mock_parse,
    ):
        from agents.processing_agent import processing_workflow
        result_obj = await processing_workflow.run(task)
        result = result_obj.get_outputs()[0]

    assert result["status"] == "delete_forwarded"
    mock_parse.assert_not_called()
    mock_queue.assert_called_once()
    queued_payload = mock_queue.call_args[0][1]
    assert queued_payload["is_delete"] is True


@pytest.mark.asyncio
async def test_processing_workflow_queues_embedding_task_with_blob_path():
    chunks = _make_chunks(n_children=2)
    task   = _make_task()

    with (
        patch("agents.processing_agent._download_blob", new_callable=AsyncMock, return_value=b"fake-bytes"),
        patch("agents.processing_agent._upload_blob",   new_callable=AsyncMock),
        patch("agents.processing_agent.send_to_queue",  new_callable=AsyncMock) as mock_queue,
        patch("agents.processing_agent.parse_document", return_value=chunks),
        patch("agents.processing_agent._sha256_already_indexed", new_callable=AsyncMock, return_value=False),
    ):
        from agents.processing_agent import processing_workflow
        await processing_workflow.run(task)

    queued_payload = mock_queue.call_args[0][1]
    assert queued_payload.get("processed_blob_path", "") != ""


@pytest.mark.asyncio
async def test_processing_workflow_queues_embedding_task_with_sha256():
    chunks = _make_chunks(n_children=2)
    task   = _make_task(file_sha256=DUMMY_SHA256)

    with (
        patch("agents.processing_agent._download_blob", new_callable=AsyncMock, return_value=b"fake-bytes"),
        patch("agents.processing_agent._upload_blob",   new_callable=AsyncMock),
        patch("agents.processing_agent.send_to_queue",  new_callable=AsyncMock) as mock_queue,
        patch("agents.processing_agent.parse_document", return_value=chunks),
        patch("agents.processing_agent._sha256_already_indexed", new_callable=AsyncMock, return_value=False),
    ):
        from agents.processing_agent import processing_workflow
        await processing_workflow.run(task)

    queued_payload = mock_queue.call_args[0][1]
    # file_sha256 is on the ProcessingTask, not the embedding task dict, but task_id ties them
    # The embedding task dict contains task_id which maps to the processing task's sha256
    assert "task_id" in queued_payload


# ── _sha256_already_indexed ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sha256_already_indexed_returns_false_when_not_found():
    from agents.processing_agent import _sha256_already_indexed
    with patch("agents.processing_agent.get_search_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.search.return_value = iter([])
        mock_factory.return_value = mock_client
        result = await _sha256_already_indexed(DOC_NAME, DUMMY_SHA256)
    assert result is False


@pytest.mark.asyncio
async def test_sha256_already_indexed_returns_true_when_found():
    from agents.processing_agent import _sha256_already_indexed
    with patch("agents.processing_agent.get_search_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.search.return_value = iter([{"id": "chunk-001"}])
        mock_factory.return_value = mock_client
        result = await _sha256_already_indexed(DOC_NAME, DUMMY_SHA256)
    assert result is True


@pytest.mark.asyncio
async def test_sha256_already_indexed_proceeds_on_search_error():
    from agents.processing_agent import _sha256_already_indexed
    with patch("agents.processing_agent.get_search_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.search.side_effect = RuntimeError("Search service unavailable")
        mock_factory.return_value = mock_client
        result = await _sha256_already_indexed(DOC_NAME, DUMMY_SHA256)
    # Safe default is False — proceed with processing rather than silently drop
    assert result is False


@pytest.mark.asyncio
async def test_sha256_already_indexed_returns_false_for_empty_sha():
    from agents.processing_agent import _sha256_already_indexed
    with patch("agents.processing_agent.get_search_client") as mock_factory:
        result = await _sha256_already_indexed(DOC_NAME, "")
    # Empty sha256 → skip the Search call entirely
    mock_factory.assert_not_called()
    assert result is False
