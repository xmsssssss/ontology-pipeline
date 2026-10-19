from __future__ import annotations

import re
from collections import defaultdict

from kb.ids import normalize_text, stable_id
from kb.models import BlockRecord, ClauseRecord, DocumentRecord, SourceSpan

CLAUSE_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百千]+(?:[.\-][0-9一二三四五六七八九十百千]+)*\s*条)\s*(.*)$"
)
CHAPTER_RE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百千]+(?:[.\-][0-9一二三四五六七八九十百千]+)*\s*[章节])\s*(.*)$"
)


def _clause_id(
    document_id: str,
    clause_no: str | None,
    text: str,
    block_id: str,
    line_start: int | None,
) -> str:
    return stable_id(
        "clause",
        document_id,
        clause_no or "",
        normalize_text(text)[:300],
        block_id,
        line_start or 0,
        length=24,
    )


def segment_document(
    document: DocumentRecord,
    blocks: list[BlockRecord],
) -> list[ClauseRecord]:
    if document.file_type != "unstructured":
        return []

    ordered_blocks = sorted(blocks, key=lambda item: item.order)
    clauses: list[ClauseRecord] = []
    current: dict | None = None
    chapter_title: str | None = None

    def finish() -> None:
        nonlocal current
        if not current:
            return
        text = "\n".join(part for part in current["parts"] if part).strip()
        if not text:
            current = None
            return
        spans: list[SourceSpan] = current["spans"]
        first_block = current["first_block"]
        clauses.append(
            ClauseRecord(
                clause_id=_clause_id(
                    document.document_id,
                    current["clause_no"],
                    text,
                    first_block.block_id,
                    spans[0].char_start if spans else None,
                ),
                document_id=document.document_id,
                clause_no=current["clause_no"],
                title=None,
                text=text,
                source_spans=spans,
                chapter_title=chapter_title,
            )
        )
        current = None

    for block in ordered_blocks:
        cursor = 0
        lines = block.text.splitlines() or [block.text]
        for line in lines:
            value = normalize_text(line)
            if not value:
                continue
            start = block.text.find(line, cursor)
            if start < 0:
                start = cursor
            end = start + len(line)
            cursor = end
            span = SourceSpan(
                block_id=block.block_id,
                page_no=block.page_no,
                char_start=(block.char_start + start) if block.char_start is not None else start,
                char_end=(block.char_start + end) if block.char_start is not None else end,
            )

            chapter_match = CHAPTER_RE.match(value)
            if chapter_match:
                finish()
                chapter_title = normalize_text(f"{chapter_match.group(1)} {chapter_match.group(2)}")
                continue

            clause_match = CLAUSE_RE.match(value)
            if clause_match:
                finish()
                current = {
                    "clause_no": normalize_text(clause_match.group(1)).replace(" ", ""),
                    "parts": [normalize_text(clause_match.group(2))],
                    "spans": [span],
                    "first_block": block,
                }
                continue

            if current is None:
                current = {
                    "clause_no": None,
                    "parts": [value],
                    "spans": [span],
                    "first_block": block,
                }
            elif current["clause_no"] is None:
                finish()
                current = {
                    "clause_no": None,
                    "parts": [value],
                    "spans": [span],
                    "first_block": block,
                }
            else:
                current["parts"].append(value)
                if span.block_id not in {item.block_id for item in current["spans"]}:
                    current["spans"].append(span)

    finish()

    if not clauses:
        for block in ordered_blocks:
            if not block.text.strip():
                continue
            clauses.append(
                ClauseRecord(
                    clause_id=_clause_id(
                        document.document_id,
                        None,
                        block.text,
                        block.block_id,
                        block.char_start,
                    ),
                    document_id=document.document_id,
                    clause_no=None,
                    text=block.text,
                    source_spans=[
                        SourceSpan(
                            block_id=block.block_id,
                            page_no=block.page_no,
                            char_start=block.char_start,
                            char_end=block.char_end,
                        )
                    ],
                    chapter_title=chapter_title,
                )
            )
    return clauses


def segment_documents(
    documents: list[DocumentRecord],
    blocks: list[BlockRecord],
) -> list[ClauseRecord]:
    blocks_by_document: dict[str, list[BlockRecord]] = defaultdict(list)
    for block in blocks:
        blocks_by_document[block.document_id].append(block)
    clauses: list[ClauseRecord] = []
    for document in documents:
        clauses.extend(segment_document(document, blocks_by_document.get(document.document_id, [])))
    return clauses
