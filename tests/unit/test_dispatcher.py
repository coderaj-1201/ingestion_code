"""
Unit tests for processors/dispatcher.py.

Tests detect_file_type() routing logic and parse_document() dispatch to the
correct parser. All parsers are mocked — no file bytes are processed.

Assumptions:
- detect_file_type() prefers MIME type over file extension.
- parse_document() raises ValueError for unsupported types.
- All four parser functions (parse_pdf, parse_docx, parse_xlsx, parse_pptx)
  are importable and patchable within the dispatcher module's match/case block.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from processors.dispatcher import detect_file_type, parse_document
from shared.models import FileType, RawChunk


def test_detect_file_type_from_pdf_extension():
    assert detect_file_type("report.pdf") == FileType.PDF


def test_detect_file_type_from_docx_extension():
    assert detect_file_type("document.docx") == FileType.DOCX


def test_detect_file_type_from_xlsx_extension():
    assert detect_file_type("spreadsheet.xlsx") == FileType.XLSX


def test_detect_file_type_from_pptx_extension():
    assert detect_file_type("slides.pptx") == FileType.PPTX


def test_detect_file_type_prefers_mime_over_extension():
    # File is named .txt but MIME says it's a PDF
    result = detect_file_type("file.txt", mime_type="application/pdf")
    assert result == FileType.PDF


def test_detect_file_type_returns_none_for_unsupported():
    assert detect_file_type("data.csv") is None


def test_detect_file_type_case_insensitive_extension():
    assert detect_file_type("REPORT.PDF") == FileType.PDF


def test_parse_document_raises_for_unsupported_type():
    with pytest.raises(ValueError, match="Unsupported file type"):
        parse_document(b"", "data.csv", "https://example.com", "hr", "hr/data.csv")


def test_parse_document_routes_to_pdf_parser():
    dummy_chunk = MagicMock(spec=RawChunk)
    with patch("processors.pdf_parser.parse_pdf", return_value=[dummy_chunk]) as mock_pdf:
        result = parse_document(
            b"fake-pdf-bytes", "report.pdf",
            "https://example.com/report.pdf", "hr", "hr/report.pdf",
        )
    mock_pdf.assert_called_once_with(
        b"fake-pdf-bytes", "report.pdf",
        "https://example.com/report.pdf", "hr", "hr/report.pdf",
    )
    assert result == [dummy_chunk]


def test_parse_document_routes_to_docx_parser():
    dummy_chunk = MagicMock(spec=RawChunk)
    with patch("processors.docx_parser.parse_docx", return_value=[dummy_chunk]) as mock_docx:
        result = parse_document(
            b"fake-docx-bytes", "policy.docx",
            "https://example.com/policy.docx", "legal", "legal/policy.docx",
        )
    mock_docx.assert_called_once_with(
        b"fake-docx-bytes", "policy.docx",
        "https://example.com/policy.docx", "legal", "legal/policy.docx",
    )
    assert result == [dummy_chunk]


def test_parse_document_routes_to_xlsx_parser():
    dummy_chunk = MagicMock(spec=RawChunk)
    with patch("processors.xlsx_parser.parse_xlsx", return_value=[dummy_chunk]) as mock_xlsx:
        result = parse_document(
            b"fake-xlsx-bytes", "data.xlsx",
            "https://example.com/data.xlsx", "ops", "ops/data.xlsx",
        )
    mock_xlsx.assert_called_once_with(
        b"fake-xlsx-bytes", "data.xlsx",
        "https://example.com/data.xlsx", "ops", "ops/data.xlsx",
    )
    assert result == [dummy_chunk]


def test_parse_document_routes_to_pptx_parser():
    dummy_chunk = MagicMock(spec=RawChunk)
    with patch("processors.pptx_parser.parse_pptx", return_value=[dummy_chunk]) as mock_pptx:
        result = parse_document(
            b"fake-pptx-bytes", "deck.pptx",
            "https://example.com/deck.pptx", "hr", "hr/deck.pptx",
        )
    mock_pptx.assert_called_once_with(
        b"fake-pptx-bytes", "deck.pptx",
        "https://example.com/deck.pptx", "hr", "hr/deck.pptx",
    )
    assert result == [dummy_chunk]
