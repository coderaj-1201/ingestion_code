"""
PDF Parser
==========

Stack (no Document Intelligence, no OCR, no pymupdf):
  - pdfplumber  — text extraction, font metadata, table detection, bounding boxes
  - LLM (light) — per-page cleaning + table → NL serialisation

Strategy:
  1. pdfplumber extracts char-level font sizes → heading detection via font-size ratio
  2. pdfplumber extracts tables atomically per page
  3. Header/footer removed by y-position threshold (top 7% / bottom 7% of page)
  4. Light LLM pass per page cleans garbled text, confirms structure
  5. Table → LLM NL summary (embedded) + markdown kept as table_raw
  6. Parent-child chunking: parent = full section, children = paragraphs + tables

Heading detection without pymupdf
----------------------------------
pdfplumber exposes per-character font size via page.chars.
We collect all char sizes on the page, find the modal (body) size,
then classify any word/span whose dominant font size is notably larger
as a heading — the same ratio thresholds as before (≥1.6 → h1, ≥1.3 → h2,
≥1.1 → h3).  Blocks are reconstructed from pdfplumber's word objects grouped
by proximity on the same line/paragraph.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import pdfplumber

from shared.azure_clients import get_openai_client
from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _llm_clean_page(raw_text: str, page_num: int) -> str:
    """
    Light LLM pass: fix broken hyphenation, remove artefacts,
    normalise whitespace. Returns cleaned text.
    Skips LLM if page is very short (not worth the call).
    """
    text = raw_text.strip()
    if len(text) < 40:
        return text

    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a document cleaning assistant. "
                    "Fix broken hyphenation at line-ends, remove repeated artefacts, "
                    "normalise whitespace. Return ONLY the cleaned text, nothing else."
                ),
            },
            {"role": "user", "content": f"Page {page_num}:\n\n{text[:3000]}"},
        ],
        temperature=0,
        max_tokens=1500,
    )
    return resp.choices[0].message.content.strip()


def _llm_serialise_table(table_markdown: str, context_heading: str) -> str:
    """
    Convert a markdown table to natural language for embedding.
    Original markdown is preserved separately as table_raw.
    """
    if not table_markdown.strip():
        return ""

    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "Convert this table into 2–5 clear natural language sentences "
                    "that capture all key data. Be factual and complete. "
                    "Return ONLY the sentences, no preamble."
                ),
            },
            {
                "role": "user",
                "content": f"Section: {context_heading}\n\nTable:\n{table_markdown}",
            },
        ],
        temperature=0,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()


# ── Table extraction via pdfplumber ───────────────────────────────────────────

def _pdfplumber_table_to_markdown(table: list[list]) -> str:
    """Convert pdfplumber table (list of rows, each a list of cells) to markdown."""
    if not table or not table[0]:
        return ""

    def cell(v):
        return str(v or "").strip().replace("\n", " ")

    rows = [[cell(c) for c in row] for row in table]
    col_count = max(len(r) for r in rows)

    lines = []
    for i, row in enumerate(rows):
        padded = row + [""] * (col_count - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * col_count) + " |")

    return "\n".join(lines)


# ── Font-size based heading detection (pdfplumber-native) ─────────────────────

def _estimate_body_font_size(chars: list[dict]) -> float:
    """
    Find the modal (most common by character count) font size on a page.
    That is the body text size we compare headings against.
    Falls back to 12.0 if no char data is available.
    """
    if not chars:
        return 12.0
    # Round to 0.5pt buckets to avoid float noise
    sizes = Counter(round(c.get("size", 12.0) * 2) / 2 for c in chars if c.get("text", "").strip())
    return max(sizes, key=sizes.get) if sizes else 12.0


def _detect_heading_level(span_size: float, body_size: float) -> Optional[str]:
    """
    Compare span font size to body font size.
    Returns None if not a heading.
    """
    ratio = span_size / body_size if body_size else 1.0
    if ratio >= 1.6:
        return "h1"
    if ratio >= 1.3:
        return "h2"
    if ratio >= 1.1:
        return "h3"
    return None


# ── Header/footer removal ─────────────────────────────────────────────────────

def _is_header_footer(y0: float, y1: float, page_height: float) -> bool:
    margin = page_height * settings.HEADER_FOOTER_MARGIN_PCT
    return y0 < margin or y1 > (page_height - margin)


# ── Block reconstruction from pdfplumber words ───────────────────────────────

@dataclass
class _TextBlock:
    """A reconstructed paragraph/heading block from pdfplumber word objects."""
    text: str
    heading: Optional[str]  # None | 'h1' | 'h2' | 'h3'
    y0: float


def _reconstruct_blocks(
    words: list[dict],
    chars: list[dict],
    page_height: float,
    table_bboxes: list[tuple],
    body_size: float,
) -> list[_TextBlock]:
    """
    Group pdfplumber word objects into paragraph-level blocks.

    Algorithm:
    1. Filter out words in header/footer zones or overlapping table bboxes.
    2. Group words into lines by y-proximity (within LINE_Y_TOLERANCE pts).
    3. Group lines into blocks by y-gap (gap > PARA_GAP_THRESHOLD pts = new block).
    4. For each block, determine dominant font size from chars that overlap
       the block's bbox → derive heading level.

    pdfplumber word dict keys: text, x0, y0, x1, y1, doctop, fontname, size
    (size is available when extract_words is called with extra_attrs).
    """
    LINE_Y_TOLERANCE    = 3.0   # pts — words on the same line
    PARA_GAP_THRESHOLD  = 8.0   # pts — gap that signals a new paragraph

    if not words:
        return []

    # Build a char lookup by (rounded y-band) for fast font-size queries
    # key: word bbox → dominant font size from overlapping chars
    def dominant_size_for_bbox(x0, y0, x1, y1) -> float:
        overlapping = [
            c.get("size", body_size)
            for c in chars
            if c.get("x0", 0) >= x0 - 1
            and c.get("x1", 0) <= x1 + 1
            and c.get("top", 0) >= y0 - 1
            and c.get("bottom", 0) <= y1 + 1
            and c.get("text", "").strip()
        ]
        if not overlapping:
            return body_size
        cnt = Counter(round(s * 2) / 2 for s in overlapping)
        return max(cnt, key=cnt.get)

    # Filter words
    filtered = []
    for w in words:
        if not w.get("text", "").strip():
            continue
        wy0 = w.get("top", w.get("y0", 0))
        wy1 = w.get("bottom", w.get("y1", 0))
        # Header/footer check
        if _is_header_footer(wy0, wy1, page_height):
            continue
        # Table overlap check (vertical band only is sufficient)
        in_table = any(
            not (wy1 < tb[1] or wy0 > tb[3])
            and not (w.get("x1", 0) < tb[0] or w.get("x0", 0) > tb[2])
            for tb in table_bboxes
        )
        if in_table:
            continue
        filtered.append(w)

    if not filtered:
        return []

    # Sort top-to-bottom, left-to-right
    filtered.sort(key=lambda w: (round(w.get("top", w.get("y0", 0))), w.get("x0", 0)))

    # Group into lines
    lines: list[list[dict]] = []
    current_line: list[dict] = [filtered[0]]
    for w in filtered[1:]:
        prev_top = current_line[-1].get("top", current_line[-1].get("y0", 0))
        this_top = w.get("top", w.get("y0", 0))
        if abs(this_top - prev_top) <= LINE_Y_TOLERANCE:
            current_line.append(w)
        else:
            lines.append(current_line)
            current_line = [w]
    lines.append(current_line)

    # Group lines into paragraph blocks
    para_lines: list[list[list[dict]]] = []
    current_para: list[list[dict]] = [lines[0]]
    for line in lines[1:]:
        prev_bottom = max(w.get("bottom", w.get("y1", 0)) for w in current_para[-1])
        this_top    = min(w.get("top",    w.get("y0", 0)) for w in line)
        gap = this_top - prev_bottom
        if gap > PARA_GAP_THRESHOLD:
            para_lines.append(current_para)
            current_para = [line]
        else:
            current_para.append(line)
    para_lines.append(current_para)

    # Build _TextBlock objects
    blocks: list[_TextBlock] = []
    for para in para_lines:
        all_words_in_para = [w for line in para for w in line]
        text = " ".join(w["text"] for w in all_words_in_para).strip()
        if not text:
            continue

        # Bounding box of the whole block
        bx0 = min(w.get("x0", 0)                       for w in all_words_in_para)
        by0 = min(w.get("top",    w.get("y0", 0))       for w in all_words_in_para)
        bx1 = max(w.get("x1", 0)                       for w in all_words_in_para)
        by1 = max(w.get("bottom", w.get("y1", 0))       for w in all_words_in_para)

        dom_size = dominant_size_for_bbox(bx0, by0, bx1, by1)
        heading  = _detect_heading_level(dom_size, body_size)

        blocks.append(_TextBlock(text=text, heading=heading, y0=by0))

    return blocks


# ── Metadata extraction (title) ───────────────────────────────────────────────

def _extract_doc_title(plumber_doc: pdfplumber.PDF, doc_name: str) -> str:
    """
    Try to get the document title from PDF metadata.
    Falls back to the filename stem.
    """
    meta = plumber_doc.metadata or {}
    title = (meta.get("Title") or meta.get("title") or "").strip()
    if title:
        return title
    # Strip extension from filename
    stem = re.sub(r"\.pdf$", "", doc_name, flags=re.IGNORECASE)
    return stem


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_pdf(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """
    Full native PDF parsing pipeline using pdfplumber only (no pymupdf).
    Returns list[RawChunk] (parents + children).
    """
    ingested_at = datetime.now(timezone.utc).isoformat()
    chunks: list[RawChunk] = []

    plumber_doc = pdfplumber.open(io.BytesIO(file_bytes))

    doc_title = _extract_doc_title(plumber_doc, doc_name)

    current_heading    = ""
    current_subheading = ""
    current_parent_id  = str(uuid4())
    current_parent_content: list[str] = []
    current_parent_page = 1

    def _flush_parent():
        nonlocal current_parent_id, current_parent_content
        if not current_parent_content:
            return
        chunks.append(RawChunk(
            chunk_id           = current_parent_id,
            parent_id          = "",
            chunk_type         = ChunkType.HEADING if current_heading else ChunkType.PARAGRAPH,
            domain             = domain,
            doc_name           = doc_name,
            source             = doc_name,
            doc_url            = doc_url,
            file_type          = "pdf",
            blob_path          = blob_path,
            ingested_at        = ingested_at,
            page_number        = current_parent_page,
            title              = doc_title,
            section_heading    = current_heading,
            section_subheading = current_subheading,
            content            = "\n\n".join(current_parent_content),
        ))
        current_parent_id      = str(uuid4())
        current_parent_content = []

    for page_num, plumber_page in enumerate(plumber_doc.pages):
        display_page = page_num + 1
        page_height  = plumber_page.height

        # ── Tables first ──────────────────────────────────────────────────────
        # find_tables() returns TableFinder objects with .bbox; extract_tables()
        # returns the cell data.  We need both.
        found_tables = plumber_page.find_tables()
        table_bboxes = [t.bbox for t in found_tables]  # (x0, top, x1, bottom)
        tables       = plumber_page.extract_tables() or []

        # ── Characters & body size ────────────────────────────────────────────
        chars     = plumber_page.chars  # list of char dicts with 'size', 'top', etc.
        body_size = _estimate_body_font_size(chars)

        # ── Words with extra font attrs ───────────────────────────────────────
        # extra_attrs pulls 'fontname' and 'size' onto each word dict.
        words = plumber_page.extract_words(
            extra_attrs=["fontname", "size"],
            keep_blank_chars=False,
            use_text_flow=True,
        )

        # ── Reconstruct paragraph blocks ──────────────────────────────────────
        page_blocks = _reconstruct_blocks(
            words=words,
            chars=chars,
            page_height=page_height,
            table_bboxes=table_bboxes,
            body_size=body_size,
        )

        # ── LLM clean (one call per page, all paragraphs combined) ────────────
        if page_blocks:
            combined_raw   = "\n\n".join(b.text for b in page_blocks)
            combined_clean = _llm_clean_page(combined_raw, display_page)
            clean_parts    = [s.strip() for s in combined_clean.split("\n\n") if s.strip()]
            for i, blk in enumerate(page_blocks):
                blk.text = clean_parts[i] if i < len(clean_parts) else blk.text

        # ── Process blocks → chunks ───────────────────────────────────────────
        for blk in page_blocks:
            text    = blk.text
            heading = blk.heading

            if heading in ("h1", "h2"):
                _flush_parent()
                current_heading     = text
                current_subheading  = ""
                current_parent_page = display_page

                # First heading on page 1 may become doc title
                if display_page == 1 and not doc_title:
                    doc_title = text

                chunks.append(RawChunk(
                    chunk_id           = str(uuid4()),
                    parent_id          = current_parent_id,
                    chunk_type         = ChunkType.HEADING,
                    domain             = domain,
                    doc_name           = doc_name,
                    source             = doc_name,
                    doc_url            = doc_url,
                    file_type          = "pdf",
                    blob_path          = blob_path,
                    ingested_at        = ingested_at,
                    page_number        = display_page,
                    title              = doc_title,
                    section_heading    = current_heading,
                    section_subheading = current_subheading,
                    content            = text,
                ))
                current_parent_content.append(text)

            elif heading == "h3":
                current_subheading = text
                current_parent_content.append(text)

            else:
                current_parent_content.append(text)
                chunks.append(RawChunk(
                    chunk_id           = str(uuid4()),
                    parent_id          = current_parent_id,
                    chunk_type         = ChunkType.PARAGRAPH,
                    domain             = domain,
                    doc_name           = doc_name,
                    source             = doc_name,
                    doc_url            = doc_url,
                    file_type          = "pdf",
                    blob_path          = blob_path,
                    ingested_at        = ingested_at,
                    page_number        = display_page,
                    title              = doc_title,
                    section_heading    = current_heading,
                    section_subheading = current_subheading,
                    content            = text,
                ))

        # ── Process tables ────────────────────────────────────────────────────
        for table_data in tables:
            if not table_data:
                continue
            tbl_md     = _pdfplumber_table_to_markdown(table_data)
            if not tbl_md:
                continue
            nl_summary = _llm_serialise_table(tbl_md, current_heading)
            if not nl_summary:
                continue

            chunks.append(RawChunk(
                chunk_id           = str(uuid4()),
                parent_id          = current_parent_id,
                chunk_type         = ChunkType.TABLE,
                domain             = domain,
                doc_name           = doc_name,
                source             = doc_name,
                doc_url            = doc_url,
                file_type          = "pdf",
                blob_path          = blob_path,
                ingested_at        = ingested_at,
                page_number        = display_page,
                title              = doc_title,
                section_heading    = current_heading,
                section_subheading = current_subheading,
                content            = nl_summary,
                table_raw          = tbl_md,
            ))
            current_parent_content.append(nl_summary)

    _flush_parent()
    plumber_doc.close()

    logger.info("PDF parsed: %s → %d chunks", doc_name, len(chunks))
    return chunks
