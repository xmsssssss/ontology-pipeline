from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class KbModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceSpan(KbModel):
    block_id: str | None = None
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    row_index: int | None = None
    sheet_name: str | None = None


class FileRecord(KbModel):
    file_id: str
    sha256: str
    file_name: str
    media_type: str
    file_type: Literal["structured", "unstructured", "unsupported"]
    source_path: str
    stored_path: str
    size_bytes: int
    registered_at: datetime = Field(default_factory=utc_now)


class DocumentRecord(KbModel):
    document_id: str
    file_id: str
    title: str
    file_name: str
    file_type: Literal["structured", "unstructured"]
    source_path: str | None = None
    status: str = "parsed"
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class BlockRecord(KbModel):
    block_id: str
    document_id: str
    order: int
    block_type: str
    text: str
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    parent_block_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClauseRecord(KbModel):
    clause_id: str
    document_id: str
    clause_no: str | None = None
    title: str | None = None
    text: str
    parent_clause_id: str | None = None
    source_spans: list[SourceSpan] = Field(default_factory=list)
    confidence: float = 1.0


class TableRecord(KbModel):
    table_record_id: str
    document_id: str
    table_id: str
    row_index: int
    columns: dict[str, Any] = Field(default_factory=dict)
    sheet_name: str | None = None
    source_spans: list[SourceSpan] = Field(default_factory=list)


class EntityMention(KbModel):
    mention_id: str
    document_id: str
    clause_id: str | None = None
    label: str
    entity_key: str | None = None
    entity_type: str
    source_span: SourceSpan | None = None
    confidence: float = 1.0
    review_status: str = "pending"


class FactRecord(KbModel):
    fact_id: str
    fact_type: str
    subject: str
    subject_key: str | None = None
    subject_type: str | None = None
    predicate: str
    object: str
    object_key: str | None = None
    object_label: str | None = None
    object_class: str | None = None
    object_type: Literal["iri", "literal"] = "iri"
    datatype: str | None = None
    unit: str | None = None
    condition: str | None = None
    evidence_text: str | None = None
    document_id: str | None = None
    clause_id: str | None = None
    clause_no: str | None = None
    source_ref: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    review_status: str = "pending"
    extraction_method: str = "rule"
    schema_version: str = "0.1"


class ReviewItem(KbModel):
    review_item_id: str
    fact_id: str
    reason: str
    status: str = "pending"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RunRecord(KbModel):
    run_id: str
    input_path: str
    status: str = "running"
    stage: str = "queued"
    stage_label: str = "等待执行"
    progress: int = Field(default=0, ge=0, le=100)
    current: int | None = None
    total: int | None = None
    cancel_requested: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    artifact_dir: str | None = None
    source_upload_dir: str | None = None
    parent_run_id: str | None = None
    task_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
