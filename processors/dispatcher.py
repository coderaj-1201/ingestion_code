"""
Parser dispatcher — routes file bytes to the correct parser by file type.
Returns list[RawChunk] regardless of file type.
"""
from __future__ import annotations

import logging

from shared.models import FileType, RawChunk

logger = logging.getLogger(__name__)

_MIME_TO_FILETYPE = {
    "application/pdf":                                                          FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":       FileType.XLSX,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileType.PPTX,
    "application/msword":                                                       FileType.DOCX,
    "application/vnd.ms-excel":                                                 FileType.XLSX,
    "application/vnd.ms-powerpoint":                                            FileType.PPTX,
    "image/png":                                                                FileType.IMAGE,
    "image/jpeg":                                                               FileType.IMAGE,
    "image/gif":                                                                FileType.IMAGE,
    "image/webp":                                                               FileType.IMAGE,
    "image/bmp":                                                                FileType.IMAGE,
    "image/tiff":                                                               FileType.IMAGE,
}

_EXT_TO_FILETYPE = {
    ".pdf":  FileType.PDF,
    ".docx": FileType.DOCX,
    ".doc":  FileType.DOCX,
    ".xlsx": FileType.XLSX,
    ".xls":  FileType.XLSX,
    ".pptx": FileType.PPTX,
    ".ppt":  FileType.PPTX,
    ".png":  FileType.IMAGE,
    ".jpg":  FileType.IMAGE,
    ".jpeg": FileType.IMAGE,
    ".gif":  FileType.IMAGE,
    ".webp": FileType.IMAGE,
    ".bmp":  FileType.IMAGE,
    ".tiff": FileType.IMAGE,
    ".tif":  FileType.IMAGE,
}

# Single source of truth for supported extensions — imported by ingestion_agent.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_EXT_TO_FILETYPE)


def detect_file_type(doc_name: str, mime_type: str = "") -> FileType | None:
    """Detect file type from MIME or extension."""
    if mime_type and mime_type in _MIME_TO_FILETYPE:
        return _MIME_TO_FILETYPE[mime_type]
    ext = "." + doc_name.lower().rsplit(".", 1)[-1] if "." in doc_name else ""
    return _EXT_TO_FILETYPE.get(ext)


def parse_document(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
    mime_type: str = "",
    doc_path: str = "",
) -> list[RawChunk]:
    """
    Route to the correct parser and return chunks.
    Raises ValueError for unsupported file types.

    doc_path is the full relative path within the SharePoint library
    (e.g. "FolderA/Leave Policy.pdf") and is stamped on every returned chunk.
    """
    file_type = detect_file_type(doc_name, mime_type)
    if not file_type:
        raise ValueError(f"Unsupported file type for: {doc_name} (mime={mime_type})")

    logger.info("Parsing %s as %s", doc_name, file_type)

    match file_type:
        case FileType.PDF:
            from processors.pdf_parser import parse_pdf
            chunks = parse_pdf(file_bytes, doc_name, doc_url, domain, blob_path)

        case FileType.DOCX:
            from processors.docx_parser import parse_docx
            chunks = parse_docx(file_bytes, doc_name, doc_url, domain, blob_path)

        case FileType.XLSX:
            from processors.xlsx_parser import parse_xlsx
            chunks = parse_xlsx(file_bytes, doc_name, doc_url, domain, blob_path)

        case FileType.PPTX:
            from processors.pptx_parser import parse_pptx
            chunks = parse_pptx(file_bytes, doc_name, doc_url, domain, blob_path)

        case FileType.IMAGE:
            from processors.image_parser import parse_image
            chunks = parse_image(file_bytes, doc_name, doc_url, domain, blob_path)

        case _:
            raise ValueError(f"No parser implemented for file type: {file_type}")

    if doc_path:
        for chunk in chunks:
            chunk.doc_path = doc_path

    return chunks
