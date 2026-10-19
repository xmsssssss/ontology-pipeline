from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kb.ai_extraction import AIExtractionCancelled, AISettings, extract_with_ai
from kb.constants import (
    ARTIFACT_FILES,
    DEFAULT_BASE_IRI,
    DEFAULT_ONTOLOGY_ID,
    DEFAULT_ONTOLOGY_VERSION,
)
from kb.extraction import ExtractionResult, extract_all, merge_extraction_results
from kb.ids import stable_id
from kb.models import DocumentRecord, FileRecord, RunRecord, utc_now
from kb.ontology import build_ontology
from kb.parsers import ParsedFile, parse_file
from kb.scanner import scan_inputs
from kb.segmentation import segment_documents
from kb.storage import Workspace
from kb.validation import validate_package

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class PipelineCancelled(RuntimeError):
    pass


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check and cancel_check():
        raise PipelineCancelled("构建任务已取消。")


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    stage: str,
    stage_label: str,
    progress: int,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if callback:
        callback(
            {
                "stage": stage,
                "stage_label": stage_label,
                "progress": max(0, min(100, progress)),
                "current": current,
                "total": total,
            }
        )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _update_run(run_dir: Path, **changes: Any) -> RunRecord:
    path = run_dir / "run.json"
    run = RunRecord.model_validate(_read_json(path))
    updated = run.model_copy(update=changes)
    Workspace(run_dir.parents[1]).write_json(path, updated)
    return updated


def read_run(run_dir: Path | str) -> RunRecord:
    path = Path(run_dir).resolve() / "run.json"
    return RunRecord.model_validate(_read_json(path))


def _new_run_id(inputs: Iterable[Path]) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path_key = stable_id(
        "input",
        *[str(path.resolve()).lower() for path in inputs],
        length=8,
    ).split("_", 1)[-1]
    return f"run_{stamp}_{path_key}"


def _validate_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,120}", run_id):
        raise ValueError("run_id 只能包含字母、数字、点、下划线和连字符。")
    return run_id


def create_run(
    inputs: list[Path],
    workspace: Workspace,
    *,
    run_id: str | None = None,
) -> tuple[RunRecord, Path]:
    workspace.ensure()
    resolved_inputs = [path.expanduser().resolve() for path in inputs]
    actual_run_id = _validate_run_id(run_id) if run_id else _new_run_id(resolved_inputs)
    run_dir = workspace.run_dir(actual_run_id)
    if (run_dir / "run.json").exists():
        raise FileExistsError(f"运行目录已存在: {run_dir}")

    scan = scan_inputs(resolved_inputs, workspace)
    run = RunRecord(
        run_id=actual_run_id,
        input_path=";".join(str(path) for path in resolved_inputs),
        status="scanned",
        counts={"files": len(scan.files)},
        warnings=list(scan.warnings),
        artifact_dir=str(run_dir),
    )
    workspace.write_json(
        run_dir / "source.json",
        {
            "input_paths": [str(path) for path in resolved_inputs],
            "files": [item.model_dump(mode="json") for item in scan.files],
            "warnings": scan.warnings,
            "skipped_duplicates": scan.skipped_duplicates,
        },
    )
    workspace.write_json(
        run_dir / "scan.json",
        {
            "files": [item.model_dump(mode="json") for item in scan.files],
            "warnings": scan.warnings,
            "skipped_duplicates": scan.skipped_duplicates,
        },
    )
    workspace.write_json(run_dir / "run.json", run)
    return run, run_dir


def _load_source_files(run_dir: Path) -> list[FileRecord]:
    source = _read_json(run_dir / "source.json")
    return [FileRecord.model_validate(item) for item in source.get("files", [])]


def parse_run(
    run_dir: Path | str,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    files = _load_source_files(directory)
    parsed_files: list[ParsedFile] = []
    warnings = list(run.warnings)

    total = len(files)
    for index, file in enumerate(files, start=1):
        _check_cancelled(cancel_check)
        if file.file_type == "unsupported":
            warnings.append(f"{file.file_name}: 当前格式不支持，已跳过解析。")
        else:
            try:
                parsed_files.append(parse_file(workspace, file))
            except Exception as exc:
                warnings.append(f"{file.file_name}: 解析失败: {exc}")
                parsed_files.append(
                    ParsedFile(
                        document=DocumentRecord(
                            document_id=stable_id("doc", file.file_id, length=24),
                            file_id=file.file_id,
                            title=Path(file.file_name).stem,
                            file_name=file.file_name,
                            file_type=(
                                "structured" if file.file_type == "structured" else "unstructured"
                            ),
                            source_path=file.source_path,
                            status="failed",
                            warnings=[str(exc)],
                        )
                    )
                )
        _emit_progress(
            progress_callback,
            stage="parse",
            stage_label="解析文档",
            progress=5 + int(13 * index / max(1, total)),
            current=index,
            total=total,
        )

    documents = [item.document for item in parsed_files]
    blocks = [block for item in parsed_files for block in item.blocks]
    tables = [table for item in parsed_files for table in item.tables]
    workspace.write_jsonl(directory / "document.jsonl", documents)
    workspace.write_jsonl(directory / "blocks.jsonl", blocks)
    workspace.write_jsonl(directory / "table_records.jsonl", tables)
    counts = {
        "documents": len(documents),
        "blocks": len(blocks),
        "table_records": len(tables),
    }
    _update_run(directory, status="parsed", counts=counts, warnings=warnings)
    return counts


def segment_run(run_dir: Path | str) -> dict[str, int]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    documents = [
        DocumentRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "document.jsonl")
    ]
    from kb.models import BlockRecord

    blocks = [
        BlockRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "blocks.jsonl")
    ]
    clauses = segment_documents(documents, blocks)
    workspace.write_jsonl(directory / "clauses.jsonl", clauses)
    counts = dict(run.counts)
    counts["clauses"] = len(clauses)
    _update_run(directory, status="segmented", counts=counts)
    return {"clauses": len(clauses)}


def extract_run(
    run_dir: Path | str,
    *,
    column_mapping_path: Path | None = None,
    extraction_mode: str = "rule",
    ai_settings: AISettings | None = None,
    ai_client: Any | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    documents = [
        DocumentRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "document.jsonl")
    ]
    from kb.models import ClauseRecord, TableRecord

    clauses = [
        ClauseRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "clauses.jsonl")
    ]
    tables = [
        TableRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "table_records.jsonl")
    ]
    mode = _normalize_extraction_mode(extraction_mode)
    result, extraction_report = _extract_documents(
        documents,
        clauses,
        tables,
        mode=mode,
        column_mapping_path=column_mapping_path,
        ai_settings=ai_settings,
        ai_client=ai_client,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    workspace.write_jsonl(directory / "entity_mentions.jsonl", result.mentions)
    workspace.write_jsonl(directory / "facts.jsonl", result.facts)
    workspace.write_jsonl(directory / "review_items.jsonl", result.reviews)
    workspace.write_json(directory / "extraction_report.json", extraction_report)
    counts = dict(run.counts)
    counts.update(
        {
            "entity_mentions": len(result.mentions),
            "facts": len(result.facts),
            "review_items": len(result.reviews),
        }
    )
    warnings = list(run.warnings)
    for warning in result.warnings:
        if warning not in warnings:
            warnings.append(warning)
    _update_run(
        directory,
        status="extracted",
        counts=counts,
        warnings=warnings,
        extraction=extraction_report,
    )
    return {
        "entity_mentions": len(result.mentions),
        "facts": len(result.facts),
        "review_items": len(result.reviews),
    }


def _normalize_extraction_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in {"rule", "ai", "hybrid"}:
        raise ValueError("extraction_mode 只能是 rule、ai 或 hybrid。")
    return mode


def _extract_documents(
    documents: list[Any],
    clauses: list[Any],
    tables: list[Any],
    *,
    mode: str,
    column_mapping_path: Path | None,
    ai_settings: AISettings | None,
    ai_client: Any | None,
    progress_callback: ProgressCallback | None,
    cancel_check: CancelCheck | None,
) -> tuple[ExtractionResult, dict[str, Any]]:
    rule_result: ExtractionResult | None = None
    ai_result: ExtractionResult | None = None
    settings = ai_settings or AISettings.from_env()

    if mode in {"rule", "hybrid"}:
        _check_cancelled(cancel_check)
        _emit_progress(
            progress_callback,
            stage="extraction",
            stage_label="规则抽取",
            progress=40,
        )
        rule_result = extract_all(
            documents,
            clauses,
            tables,
            column_mapping_path=column_mapping_path,
        )
    if mode in {"ai", "hybrid"}:
        _check_cancelled(cancel_check)

        def report_ai_batches(completed: int, total: int) -> None:
            _emit_progress(
                progress_callback,
                stage="ai_extraction",
                stage_label="AI 抽取",
                progress=45 + int(23 * completed / max(1, total)),
                current=completed,
                total=total,
            )

        try:
            ai_result = extract_with_ai(
                documents,
                clauses,
                tables,
                settings=settings,
                client=ai_client,
                strict=mode == "ai",
                progress_callback=report_ai_batches,
                cancel_check=cancel_check,
            )
        except AIExtractionCancelled as exc:
            raise PipelineCancelled(str(exc)) from exc
    _check_cancelled(cancel_check)

    if mode == "rule":
        result = rule_result or ExtractionResult()
    elif mode == "ai":
        result = ai_result or ExtractionResult()
    else:
        result = merge_extraction_results(
            rule_result or ExtractionResult(), ai_result or ExtractionResult()
        )

    report = _build_extraction_report(
        mode,
        result,
        rule_result=rule_result,
        ai_result=ai_result,
    )
    return result, report


def _build_extraction_report(
    mode: str,
    result: ExtractionResult,
    *,
    rule_result: ExtractionResult | None,
    ai_result: ExtractionResult | None,
) -> dict[str, Any]:
    raw_ai = result.metadata.get("ai")
    ai = raw_ai if isinstance(raw_ai, dict) else {}
    strategy = str(ai.get("strategy") or ("rule" if mode == "rule" else "single_pass"))
    stages: dict[str, Any] = {}
    if ai:
        if strategy == "spires":
            stages = {
                "entities": {
                    "successful_batches": _metric(ai, "entity_stage_successful_batches"),
                    "failed_batches": _metric(ai, "entity_stage_failed_batches"),
                    "candidates": _metric(ai, "entity_candidates"),
                    "accepted": _metric(ai, "entity_accepted"),
                    "rejected": _metric(ai, "entity_rejected"),
                },
                "relations": {
                    "successful_batches": _metric(ai, "relation_stage_successful_batches"),
                    "failed_batches": _metric(ai, "relation_stage_failed_batches"),
                    "skipped_batches": _metric(ai, "relation_skipped_batches"),
                    "candidates": _metric(ai, "candidates"),
                    "accepted": _metric(ai, "accepted"),
                    "rejected": _metric(ai, "rejected"),
                },
            }
        else:
            stages = {
                "single_pass": {
                    "successful_batches": _metric(ai, "single_pass_successful_batches"),
                    "failed_batches": _metric(ai, "single_pass_failed_batches"),
                    "candidates": _metric(ai, "candidates"),
                    "accepted": _metric(ai, "accepted"),
                    "rejected": _metric(ai, "rejected"),
                }
            }
    return {
        "mode": mode,
        "strategy": strategy,
        "counts": {
            "mentions": len(result.mentions),
            "facts": len(result.facts),
            "reviews": len(result.reviews),
            "rule_facts": len(rule_result.facts) if rule_result else 0,
            "ai_facts": len(ai_result.facts) if ai_result else 0,
        },
        "ai": ai,
        "stages": stages,
        "warnings": result.warnings,
    }


def _metric(values: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(values.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def review_run(
    run_dir: Path | str,
    *,
    approve_all: bool = False,
    approve: Iterable[str] = (),
    reject: Iterable[str] = (),
) -> dict[str, int]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    facts = workspace.read_jsonl(directory / "facts.jsonl")
    reviews = workspace.read_jsonl(directory / "review_items.jsonl")
    approve_ids = set(approve)
    reject_ids = set(reject)
    overlap = approve_ids & reject_ids
    if overlap:
        raise ValueError(f"同一事实不能同时批准和拒绝: {', '.join(sorted(overlap))}")

    known_ids = {str(item.get("fact_id")) for item in facts}
    unknown = (approve_ids | reject_ids) - known_ids
    if unknown:
        raise ValueError(f"未知事实 ID: {', '.join(sorted(unknown))}")

    decisions: dict[str, str] = {}
    if approve_all:
        decisions.update({str(item["fact_id"]): "approved" for item in facts})
    decisions.update({item: "approved" for item in approve_ids})
    decisions.update({item: "rejected" for item in reject_ids})

    for fact in facts:
        status = decisions.get(str(fact.get("fact_id")))
        if status:
            fact["review_status"] = status
    review_status = {
        "approved": "auto_approved" if approve_all else "approved",
        "rejected": "rejected",
    }
    for review in reviews:
        status = decisions.get(str(review.get("fact_id")))
        if status:
            review["status"] = review_status[status]

    workspace.write_jsonl(directory / "facts.jsonl", facts)
    workspace.write_jsonl(directory / "review_items.jsonl", reviews)
    approved = len([item for item in facts if item.get("review_status") == "approved"])
    rejected = len([item for item in facts if item.get("review_status") == "rejected"])
    pending = len(facts) - approved - rejected
    counts = dict(run.counts)
    counts.update(
        {
            "facts_approved": approved,
            "facts_rejected": rejected,
            "facts_pending": pending,
        }
    )
    _update_run(directory, status="reviewed", counts=counts)
    return {
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
    }


def build_owl_run(
    run_dir: Path | str,
    *,
    base_iri: str = DEFAULT_BASE_IRI,
    ontology_id: str = DEFAULT_ONTOLOGY_ID,
    version: str = DEFAULT_ONTOLOGY_VERSION,
) -> dict[str, int]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    from kb.models import ClauseRecord, FactRecord

    documents = [
        DocumentRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "document.jsonl")
    ]
    clauses = [
        ClauseRecord.model_validate(item)
        for item in workspace.read_jsonl(directory / "clauses.jsonl")
    ]
    facts = [
        FactRecord.model_validate(item) for item in workspace.read_jsonl(directory / "facts.jsonl")
    ]
    result = build_ontology(
        facts,
        documents,
        clauses,
        base_iri=base_iri,
        ontology_id=ontology_id,
        version=version,
    )
    workspace.atomic_write_text(directory / "ontology.ttl", result.ontology_ttl())
    workspace.atomic_write_text(directory / "shapes.ttl", result.shapes_ttl())
    workspace.write_jsonl(directory / "provenance.jsonl", result.provenance_records)
    workspace.atomic_write_text(directory / "mapping.yaml", result.mapping_yaml())
    counts = dict(run.counts)
    counts.update(
        {
            "ontology_triples": len(result.graph),
            "ontology_facts": len(result.provenance_records),
        }
    )
    _update_run(
        directory,
        status="ontology_built",
        counts=counts,
        ontology={"base_iri": base_iri, "id": ontology_id, "version": version},
    )
    return {
        "triples": len(result.graph),
        "approved_facts": len(result.provenance_records),
    }


def validate_run(run_dir: Path | str, *, base_iri: str | None = None) -> dict[str, Any]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    ontology_meta = run.ontology if isinstance(run.ontology, dict) else {}
    selected_base = base_iri or ontology_meta.get("base_iri") or DEFAULT_BASE_IRI
    report = validate_package(
        directory / "ontology.ttl",
        directory / "shapes.ttl",
        directory / "facts.jsonl",
        base_iri=str(selected_base),
    )
    workspace.write_json(directory / "validation_report.json", report)
    errors = list(run.errors)
    for error in report.get("errors", []):
        if error not in errors:
            errors.append(error)
    _update_run(
        directory,
        status="validated" if report["status"] == "valid" else "validation_failed",
        errors=errors,
    )
    return report


def finalize_run(run_dir: Path | str) -> dict[str, Any]:
    directory = Path(run_dir).resolve()
    run = read_run(directory)
    workspace = Workspace(directory.parents[1])
    facts = workspace.read_jsonl(directory / "facts.jsonl")
    review_items = workspace.read_jsonl(directory / "review_items.jsonl")
    validation_path = directory / "validation_report.json"
    validation = _read_json(validation_path) if validation_path.exists() else {}
    artifact_files = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name != "checksums.sha256"
    )
    manifest = {
        "schema_version": "0.1",
        "run_id": run.run_id,
        "input_path": run.input_path,
        "created_at": run.created_at.isoformat(),
        "finished_at": utc_now().isoformat(),
        "status": "completed"
        if validation.get("status") == "valid"
        else "completed_with_validation_errors",
        "counts": {
            **run.counts,
            "facts_total": len(facts),
            "facts_approved": len(
                [item for item in facts if item.get("review_status") == "approved"]
            ),
            "review_items": len(review_items),
        },
        "warnings": run.warnings,
        "errors": run.errors,
        "validation": {
            "status": validation.get("status", "not_run"),
            "conforms": validation.get("conforms", False),
        },
        "artifact_files": artifact_files,
        "checksum_algorithm": "sha256",
        "expected_artifacts": list(ARTIFACT_FILES),
    }
    workspace.write_json(directory / "manifest.json", manifest)
    completed_label = "构建完成" if manifest["status"] == "completed" else "构建完成，本体校验异常"
    _update_run(
        directory,
        status=manifest["status"],
        stage="completed",
        stage_label=completed_label,
        progress=100,
        finished_at=utc_now(),
        counts=manifest["counts"],
    )
    workspace.write_checksums(directory)
    return manifest


def execute_pipeline(
    run_dir: Path | str,
    workspace: Workspace,
    *,
    auto_approve: bool = False,
    base_iri: str = DEFAULT_BASE_IRI,
    ontology_id: str = DEFAULT_ONTOLOGY_ID,
    version: str = DEFAULT_ONTOLOGY_VERSION,
    column_mapping_path: Path | None = None,
    extraction_mode: str = "rule",
    ai_settings: AISettings | None = None,
    ai_client: Any | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[RunRecord, Path, dict[str, Any]]:
    directory = Path(run_dir).resolve()
    mode = _normalize_extraction_mode(extraction_mode)
    if auto_approve and mode != "rule":
        raise ValueError("AI 或混合抽取结果必须经过人工审核，不能启用 auto_approve。")
    run = read_run(directory)
    _update_run(
        directory,
        status="running",
        stage="parse",
        stage_label="准备解析",
        progress=max(1, run.progress),
        cancel_requested=False,
        started_at=run.started_at or utc_now(),
    )
    _emit_progress(
        progress_callback,
        stage="parse",
        stage_label="解析文档",
        progress=5,
    )
    _check_cancelled(cancel_check)
    parse_run(
        directory,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="segment",
        stage_label="切分条款",
        progress=22,
    )
    segment_run(directory)
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="segment",
        stage_label="条款切分完成",
        progress=35,
    )
    _emit_progress(
        progress_callback,
        stage="extraction",
        stage_label="抽取事实",
        progress=40,
    )
    extract_run(
        directory,
        column_mapping_path=column_mapping_path,
        extraction_mode=mode,
        ai_settings=ai_settings,
        ai_client=ai_client,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="extraction",
        stage_label="事实抽取完成",
        progress=70,
    )
    if auto_approve:
        _emit_progress(
            progress_callback,
            stage="review",
            stage_label="自动批准事实",
            progress=73,
        )
        review_run(directory, approve_all=True)
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="ontology",
        stage_label="生成 OWL 本体",
        progress=80,
    )
    build_owl_run(
        directory,
        base_iri=base_iri,
        ontology_id=ontology_id,
        version=version,
    )
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="validation",
        stage_label="SHACL 校验",
        progress=90,
    )
    validate_run(directory, base_iri=base_iri)
    _check_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        stage="finalize",
        stage_label="生成清单与校验和",
        progress=97,
    )
    manifest = finalize_run(directory)
    return read_run(directory), directory, manifest


def run_pipeline(
    inputs: list[Path],
    workspace: Workspace,
    *,
    run_id: str | None = None,
    auto_approve: bool = False,
    base_iri: str = DEFAULT_BASE_IRI,
    ontology_id: str = DEFAULT_ONTOLOGY_ID,
    version: str = DEFAULT_ONTOLOGY_VERSION,
    column_mapping_path: Path | None = None,
    extraction_mode: str = "rule",
    ai_settings: AISettings | None = None,
    ai_client: Any | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[RunRecord, Path, dict[str, Any]]:
    _, run_dir = create_run(inputs, workspace, run_id=run_id)
    return execute_pipeline(
        run_dir,
        workspace,
        auto_approve=auto_approve,
        base_iri=base_iri,
        ontology_id=ontology_id,
        version=version,
        column_mapping_path=column_mapping_path,
        extraction_mode=extraction_mode,
        ai_settings=ai_settings,
        ai_client=ai_client,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )


def export_package(
    run_dir: Path | str,
    output: Path | str,
    *,
    as_zip: bool = False,
) -> Path:
    directory = Path(run_dir).resolve()
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"导出目标已存在: {target}")
    if not (directory / "manifest.json").exists():
        finalize_run(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    if as_zip:
        archive_base = target.with_suffix("") if target.suffix == ".zip" else target
        archive_path = Path(
            shutil.make_archive(
                str(archive_base),
                "zip",
                root_dir=directory,
            )
        )
        return archive_path
    shutil.copytree(directory, target)
    return target
