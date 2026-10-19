from __future__ import annotations

import io
import json
import os
import re
import shutil
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import perf_counter, sleep
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kb.agent_context import build_agent_context
from kb.ai_extraction import (
    AIConfigurationError,
    AIExtractionError,
    AISettings,
    OpenAICompatibleClient,
)
from kb.constants import DEFAULT_BASE_IRI, DEFAULT_ONTOLOGY_ID, DEFAULT_ONTOLOGY_VERSION
from kb.graphdb import GraphDBClient, GraphDBError
from kb.models import KbModel
from kb.pipeline import (
    PipelineCancelled,
    build_owl_run,
    create_run,
    execute_pipeline,
    finalize_run,
    read_run,
    review_run,
    validate_run,
)
from kb.registry import (
    InvalidOntology,
    OntologyRegistry,
    RegistryConflict,
    RegistryError,
    RegistryNotFound,
)
from kb.storage import Workspace
from kb.tasks import RunTaskManager, TaskConflictError

WEB_DIR = Path(__file__).resolve().parent / "web"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,120}$")
PERSISTED_AI_FIELDS = (
    "enabled",
    "provider",
    "base_url",
    "model",
    "batch_size",
    "concurrency",
    "timeout_seconds",
    "max_retries",
    "extraction_strategy",
)


class CreateOntologyRequest(KbModel):
    ontology_id: str
    version: str
    base_iri: str = DEFAULT_BASE_IRI
    title: str
    description: str = ""


class UpdateTurtleRequest(KbModel):
    content: str


class QueryRequest(KbModel):
    ontology_id: str
    version: str
    query: str
    source: Literal["local", "graphdb"] = "local"


class AgentContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ontology_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=4000)
    max_entities: int = Field(default=10, ge=1, le=100)
    max_facts: int = Field(default=20, ge=1, le=200)
    max_clauses: int = Field(default=8, ge=1, le=100)
    max_chars: int = Field(default=12000, ge=500, le=50000)


class ReviewRequest(KbModel):
    approve_all: bool = False
    approve: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)


class UpdateAISettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    provider: Literal["openai-compatible"] = "openai-compatible"
    base_url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=200)
    api_key: str | None = None
    clear_api_key: bool = False
    batch_size: int = Field(default=8, ge=1, le=50)
    concurrency: int = Field(default=2, ge=1, le=8)
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    extraction_strategy: Literal["single_pass", "spires"] = "spires"

    @model_validator(mode="after")
    def validate_service_settings(self) -> UpdateAISettingsRequest:
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("AI 服务地址必须是有效的 HTTP 或 HTTPS 地址。")
        if self.enabled and not self.base_url:
            raise ValueError("启用 AI 抽取时必须填写服务地址。")
        if self.enabled and not self.model:
            raise ValueError("启用 AI 抽取时必须填写模型名称。")
        return self


class TestAISettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openai-compatible"] = "openai-compatible"
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = None
    clear_api_key: bool = False
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)

    @model_validator(mode="after")
    def validate_service_settings(self) -> TestAISettingsRequest:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AI 服务地址必须是有效的 HTTP 或 HTTPS 地址。")
        return self


def _ai_settings_path(workspace: Workspace) -> Path:
    return workspace.root / "config" / "ai_settings.json"


def _load_saved_ai_settings(workspace: Workspace, fallback: AISettings) -> AISettings:
    path = _ai_settings_path(workspace)
    if not path.exists():
        return fallback
    try:
        saved = workspace.read_json(path)
        if not isinstance(saved, dict):
            return fallback
        payload = UpdateAISettingsRequest.model_validate(
            {
                **{key: saved[key] for key in PERSISTED_AI_FIELDS if key in saved},
                "api_key": None,
            }
        )
    except (OSError, ValueError, TypeError):
        return fallback
    values = asdict(fallback)
    values.update(payload.model_dump(exclude={"api_key", "clear_api_key"}))
    values["api_key"] = fallback.api_key
    return AISettings(**values)


def _save_ai_settings(workspace: Workspace, settings: AISettings) -> None:
    values = asdict(settings)
    persisted = {key: values[key] for key in PERSISTED_AI_FIELDS}
    workspace.write_json(_ai_settings_path(workspace), persisted)


def _ai_connection_error_message(exc: Exception) -> str:
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    if isinstance(cause, httpx.HTTPStatusError):
        status_code = cause.response.status_code
        if status_code in {401, 403}:
            return f"AI 鉴权失败（HTTP {status_code}），请检查 API Key。"
        if status_code == 404:
            return "AI 接口不存在（HTTP 404），请检查服务地址是否为 OpenAI 兼容接口。"
        if status_code in {400, 422}:
            return f"AI 服务拒绝了测试请求（HTTP {status_code}），请检查模型名称和接口兼容性。"
        if status_code == 429:
            return "AI 服务当前限流（HTTP 429），请稍后重试。"
        if status_code >= 500:
            return f"AI 服务内部错误（HTTP {status_code}），请检查服务日志。"
        return f"AI 服务返回 HTTP {status_code}，连接测试未通过。"
    if isinstance(cause, httpx.TimeoutException):
        return "AI 连接超时，请检查服务地址、网络或调高超时时间。"
    if isinstance(cause, httpx.ConnectError):
        return "无法连接 AI 服务，请检查地址、端口和服务是否正在运行。"
    if isinstance(cause, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
        return "AI 服务响应不是有效的 OpenAI 兼容格式，请检查接口地址和服务实现。"
    message = str(exc).strip()
    return message or "AI 连接测试失败，请检查配置和服务状态。"


def _response_excerpt(content: str, *, limit: int = 160) -> str:
    normalized = " ".join(content.split())
    if not normalized:
        return "模型已返回响应。"
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _run_directory(workspace: Workspace, run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RegistryError("运行 ID 格式无效。")
    path = workspace.root / "runs" / run_id
    if not (path / "run.json").exists():
        raise RegistryNotFound(f"运行不存在：{run_id}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(_read_text(path))


def _read_text(path: Path) -> str:
    for attempt in range(40):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == 39:
                raise
            sleep(0.025)
    raise AssertionError("unreachable")


def _read_task_events(run_dir: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    path = run_dir / "task_events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in _read_text(path).splitlines() if line.strip()]
    return events[-limit:]


def _duration_seconds(run: dict[str, Any]) -> float | None:
    started = run.get("started_at") or run.get("created_at")
    finished = run.get("finished_at")
    if not started or not finished:
        return None

    def parse_timestamp(value: Any) -> datetime:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)

    try:
        start_time = parse_timestamp(started)
        finish_time = parse_timestamp(finished)
    except ValueError:
        return None
    return max(0.0, (finish_time - start_time).total_seconds())


def _run_summary(workspace: Workspace, run_dir: Path) -> dict[str, Any]:
    run = _read_json(run_dir / "run.json")
    source = _read_json(run_dir / "source.json")
    manifest = _read_json(run_dir / "manifest.json")
    validation = _read_json(run_dir / "validation_report.json")
    facts_path = run_dir / "facts.jsonl"
    pending = 0
    approved = 0
    rejected = 0
    ai_facts = 0
    if facts_path.exists():
        for line in _read_text(facts_path).splitlines():
            if not line.strip():
                continue
            fact = json.loads(line)
            status = fact.get("review_status")
            if status == "approved":
                approved += 1
            elif status == "rejected":
                rejected += 1
            else:
                pending += 1
            if str(fact.get("extraction_method", "")).startswith(("ai:", "hybrid:")):
                ai_facts += 1
    extraction = run.get("extraction") if isinstance(run.get("extraction"), dict) else {}
    ai = extraction.get("ai") if isinstance(extraction.get("ai"), dict) else {}
    counts = {
        **(run.get("counts") or {}),
        "files": len(source.get("files") or []),
        "facts_total": approved + pending + rejected,
        "facts_approved": approved,
        "facts_pending": pending,
        "facts_rejected": rejected,
        "facts_ai": ai_facts,
        "ai_batches": int(ai.get("batches") or 0),
        "ai_successful_batches": int(ai.get("successful_batches") or 0),
        "ai_failed_batches": int(ai.get("failed_batches") or 0),
        "ai_tokens": int(ai.get("total_tokens") or 0),
        "ai_prompt_tokens": int(ai.get("prompt_tokens") or 0),
        "ai_completion_tokens": int(ai.get("completion_tokens") or 0),
        "ai_duration_ms": int(ai.get("duration_ms") or 0),
        "ai_strategy": str(ai.get("strategy") or extraction.get("strategy") or ""),
        "ai_entity_candidates": int(ai.get("entity_candidates") or 0),
        "ai_entity_accepted": int(ai.get("entity_accepted") or 0),
        "ai_entity_rejected": int(ai.get("entity_rejected") or 0),
        "ai_entity_stage_successful_batches": int(ai.get("entity_stage_successful_batches") or 0),
        "ai_entity_stage_failed_batches": int(ai.get("entity_stage_failed_batches") or 0),
        "ai_relation_candidates": int(ai.get("candidates") or 0),
        "ai_relation_accepted": int(ai.get("accepted") or 0),
        "ai_relation_rejected": int(ai.get("rejected") or 0),
        "ai_relation_stage_successful_batches": int(
            ai.get("relation_stage_successful_batches") or 0
        ),
        "ai_relation_stage_failed_batches": int(ai.get("relation_stage_failed_batches") or 0),
        "ai_relation_skipped_batches": int(ai.get("relation_skipped_batches") or 0),
        "ai_single_pass_successful_batches": int(ai.get("single_pass_successful_batches") or 0),
        "ai_single_pass_failed_batches": int(ai.get("single_pass_failed_batches") or 0),
    }
    status = run.get("status")
    completed = status in {"completed", "completed_with_validation_errors"}
    progress = run.get("progress")
    if progress is None:
        progress = 100 if completed else 0
    stage_label = run.get("stage_label")
    if not stage_label:
        stage_label = {
            "completed": "构建完成",
            "completed_with_validation_errors": "构建完成，本体校验异常",
            "failed": "构建失败",
            "cancelled": "任务已取消",
            "interrupted": "服务重启，任务中断",
        }.get(status, "等待执行")
    return {
        "run_id": run.get("run_id", run_dir.name),
        "status": status,
        "stage": run.get("stage"),
        "stage_label": stage_label,
        "progress": int(progress),
        "current": run.get("current"),
        "total": run.get("total"),
        "cancel_requested": bool(run.get("cancel_requested")),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": _duration_seconds(run),
        "input_path": run.get("input_path"),
        "ontology": run.get("ontology"),
        "parent_run_id": run.get("parent_run_id"),
        "task_config": run.get("task_config") or {},
        "counts": counts,
        "warnings": run.get("warnings") or [],
        "errors": run.get("errors") or [],
        "extraction": extraction,
        "validation": manifest.get("validation")
        or {
            "status": validation.get("status", "not_run"),
            "conforms": validation.get("conforms", False),
        },
    }


def _safe_remove_tree(path: Path, *, allowed_root: Path) -> None:
    resolved = path.resolve()
    root = allowed_root.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RegistryError(f"拒绝删除工作区范围外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _source_input_paths(run_dir: Path) -> list[Path]:
    source = _read_json(run_dir / "source.json")
    paths = [Path(str(value)).expanduser() for value in source.get("input_paths", [])]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise RegistryError(f"原始输入文件已不存在，无法重跑：{', '.join(missing)}")
    if not paths:
        raise RegistryError("运行记录缺少原始输入路径，无法重跑。")
    return paths


def _upload_is_referenced(workspace: Workspace, upload_dir: Path, *, exclude_run: str) -> bool:
    target = upload_dir.resolve()
    for path in (workspace.root / "runs").glob("*/run.json"):
        run = _read_json(path)
        if str(run.get("run_id")) == exclude_run:
            continue
        source_upload = run.get("source_upload_dir")
        if source_upload and Path(str(source_upload)).resolve() == target:
            return True
    return False


def _execute_queued_build(
    workspace: Workspace,
    tasks: RunTaskManager,
    run_id: str,
    config: dict[str, Any],
    ai_settings: AISettings,
    ai_client: Any | None,
    cancel_event: Event,
) -> None:
    run_dir = workspace.root / "runs" / run_id

    def report_progress(payload: dict[str, Any]) -> None:
        stage_label = str(payload.get("stage_label") or "处理中")
        current = payload.get("current")
        total = payload.get("total")
        detail = f"（{current}/{total}）" if current is not None and total is not None else ""
        tasks.update(
            run_id,
            status="cancelling" if cancel_event.is_set() else "running",
            event=f"{stage_label}{detail}",
            **payload,
        )

    try:
        tasks.update(
            run_id,
            status="running",
            stage="starting",
            stage_label="启动构建",
            progress=max(1, tasks.read(run_id).progress),
            started_at=datetime.now(UTC),
            event="构建任务开始执行。",
        )
        run, _, _ = execute_pipeline(
            run_dir,
            workspace,
            auto_approve=bool(config.get("auto_approve")),
            base_iri=str(config.get("base_iri") or DEFAULT_BASE_IRI),
            ontology_id=str(config.get("ontology_id") or DEFAULT_ONTOLOGY_ID),
            version=str(config.get("version") or DEFAULT_ONTOLOGY_VERSION),
            extraction_mode=str(config.get("extraction_mode") or "rule"),
            ai_settings=ai_settings,
            ai_client=ai_client,
            progress_callback=report_progress,
            cancel_check=cancel_event.is_set,
        )
        validation_status = "valid" if run.status == "completed" else "invalid"
        tasks.update(
            run_id,
            status=run.status,
            stage="completed",
            stage_label=("构建完成" if run.status == "completed" else "构建完成，本体校验异常"),
            progress=100,
            current=run.total,
            total=run.total,
            cancel_requested=False,
            finished_at=run.finished_at or datetime.now(UTC),
            event=f"构建任务结束，校验状态：{validation_status}。",
            level="info" if run.status == "completed" else "warning",
        )
    except PipelineCancelled:
        tasks.update(
            run_id,
            status="cancelled",
            stage="cancelled",
            stage_label="任务已取消",
            cancel_requested=True,
            finished_at=datetime.now(UTC),
            event="构建任务已取消，已完成的阶段产物保留。",
            level="warning",
        )
    except Exception as exc:
        current = tasks.read(run_id)
        errors = list(current.errors)
        message = f"构建失败：{exc}"
        if message not in errors:
            errors.append(message)
        tasks.update(
            run_id,
            status="failed",
            stage="failed",
            stage_label="构建失败",
            cancel_requested=False,
            errors=errors,
            finished_at=datetime.now(UTC),
            event=message,
            level="error",
            details={"error_type": type(exc).__name__},
        )
    finally:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            workspace.write_checksums(run_dir)


def create_app(
    workspace_root: Workspace | Path | str | None = None,
    *,
    graphdb_client: GraphDBClient | None = None,
    ai_settings: AISettings | None = None,
    ai_client: Any | None = None,
) -> FastAPI:
    root = workspace_root or os.getenv("KB_WORKSPACE", "workspace")
    workspace = root if isinstance(root, Workspace) else Workspace(root)
    workspace.ensure()
    registry = OntologyRegistry(workspace)
    graphdb = graphdb_client or GraphDBClient()
    tasks = RunTaskManager(workspace)
    tasks.recover_interrupted()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        tasks.shutdown()

    app = FastAPI(
        title="知识本体平台",
        version="0.3.0",
        description="规则与 AI 抽取、人工审核、本体版本管理与 GraphDB 发布。",
        lifespan=lifespan,
    )
    app.state.workspace = workspace
    app.state.registry = registry
    app.state.graphdb = graphdb
    app.state.tasks = tasks
    initial_ai_settings = ai_settings or AISettings.from_env()
    app.state.ai_settings = (
        initial_ai_settings
        if ai_settings is not None
        else _load_saved_ai_settings(workspace, initial_ai_settings)
    )
    app.state.ai_client = ai_client
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.exception_handler(RegistryNotFound)
    async def handle_not_found(_, exc: RegistryNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(RegistryConflict)
    async def handle_conflict(_, exc: RegistryConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidOntology)
    async def handle_invalid_ontology(_, exc: InvalidOntology) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RegistryError)
    async def handle_registry_error(_, exc: RegistryError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TaskConflictError)
    async def handle_task_conflict(_, exc: TaskConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GraphDBError)
    async def handle_graphdb_error(_, exc: GraphDBError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "graphdb_status": exc.status_code},
        )

    @app.exception_handler(AIExtractionError)
    @app.exception_handler(AIConfigurationError)
    async def handle_ai_error(_, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    terminal_statuses = {
        "completed",
        "completed_with_validation_errors",
        "failed",
        "cancelled",
        "interrupted",
    }

    def runtime_summary(run_dir: Path) -> dict[str, Any]:
        summary = _run_summary(workspace, run_dir)
        if summary.get("status") not in terminal_statuses | {
            "queued",
            "cancelling",
        } and tasks.is_active(str(summary["run_id"])):
            summary["status"] = "running"
        return summary

    def enqueue_run(
        run: Any,
        config: dict[str, Any],
        *,
        source_upload_dir: str | None = None,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        tasks.update(
            run.run_id,
            status="queued",
            stage="queued",
            stage_label="等待执行",
            progress=0,
            current=0,
            total=None,
            cancel_requested=False,
            task_config=config,
            source_upload_dir=source_upload_dir,
            parent_run_id=parent_run_id,
            event="构建任务已加入队列。",
        )
        tasks.submit(
            run.run_id,
            lambda cancel_event: _execute_queued_build(
                workspace,
                tasks,
                run.run_id,
                config,
                app.state.ai_settings,
                app.state.ai_client,
                cancel_event,
            ),
        )
        return runtime_summary(workspace.root / "runs" / run.run_id)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "workspace": str(workspace.root)}

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        versions = registry.list_versions()
        runs = [
            runtime_summary(path.parent)
            for path in sorted(
                (workspace.root / "runs").glob("*/run.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        ]
        active_statuses = {"queued", "running", "cancelling"}
        return {
            "counts": {
                "ontologies": len({item.ontology_id for item in versions}),
                "versions": len(versions),
                "published": len([item for item in versions if item.status == "published"]),
                "drafts": len([item for item in versions if item.status == "draft"]),
                "triples": sum(item.triple_count for item in versions),
                "runs": len(runs),
                "runs_active": len(
                    [item for item in runs if item.get("status") in active_statuses]
                ),
                "runs_completed": len(
                    [
                        item
                        for item in runs
                        if item.get("status") in {"completed", "completed_with_validation_errors"}
                    ]
                ),
                "runs_failed": len(
                    [
                        item
                        for item in runs
                        if item.get("status") in {"failed", "interrupted", "cancelled"}
                    ]
                ),
                "facts": sum(int(item["counts"].get("facts_total") or 0) for item in runs),
                "facts_pending": sum(
                    int(item["counts"].get("facts_pending") or 0) for item in runs
                ),
                "facts_ai": sum(int(item["counts"].get("facts_ai") or 0) for item in runs),
                "ai_tokens": sum(int(item["counts"].get("ai_tokens") or 0) for item in runs),
            },
            "recent_runs": runs[:5],
        }

    @app.get("/api/graphdb/status")
    async def graphdb_status() -> dict[str, Any]:
        return graphdb.status()

    @app.get("/api/ai/status")
    async def ai_status() -> dict[str, Any]:
        return app.state.ai_settings.status()

    @app.put("/api/ai/settings")
    async def update_ai_settings(payload: UpdateAISettingsRequest) -> dict[str, Any]:
        current = app.state.ai_settings
        api_key = current.api_key
        if payload.clear_api_key:
            api_key = ""
        elif payload.api_key:
            api_key = payload.api_key
        updated = AISettings(
            enabled=payload.enabled,
            provider=payload.provider,
            base_url=payload.base_url.rstrip("/"),
            model=payload.model,
            api_key=api_key,
            batch_size=payload.batch_size,
            concurrency=payload.concurrency,
            timeout_seconds=payload.timeout_seconds,
            max_retries=payload.max_retries,
            extraction_strategy=payload.extraction_strategy,
        )
        _save_ai_settings(workspace, updated)
        app.state.ai_settings = updated
        return updated.status()

    @app.post("/api/ai/test")
    def test_ai_connection(payload: TestAISettingsRequest) -> dict[str, Any]:
        current = app.state.ai_settings
        api_key = "" if payload.clear_api_key else (payload.api_key or current.api_key)
        settings = AISettings(
            enabled=True,
            provider=payload.provider,
            base_url=payload.base_url.rstrip("/"),
            model=payload.model,
            api_key=api_key,
            timeout_seconds=min(payload.timeout_seconds, 30.0),
            max_retries=0,
        )
        started = perf_counter()
        client: OpenAICompatibleClient | None = None
        try:
            client = OpenAICompatibleClient(settings)
            completion = client.complete(
                system_prompt="你是连接测试助手，请严格按照用户要求回复。",
                user_prompt="只回复 OK，不要添加其他内容。",
            )
            return {
                "ok": True,
                "model": settings.model,
                "elapsed_ms": max(0, round((perf_counter() - started) * 1000)),
                "message": "连接成功，模型已正常响应。",
                "response": _response_excerpt(completion.content),
                "api_key_configured": bool(api_key),
            }
        except (AIConfigurationError, AIExtractionError) as exc:
            return {
                "ok": False,
                "model": settings.model,
                "elapsed_ms": max(0, round((perf_counter() - started) * 1000)),
                "message": _ai_connection_error_message(exc),
                "response": None,
                "api_key_configured": bool(api_key),
            }
        finally:
            if client is not None:
                client.close()

    @app.get("/api/ontologies")
    async def list_ontologies() -> dict[str, Any]:
        return {"items": registry.list_ontologies()}

    @app.post("/api/ontologies", status_code=201)
    async def create_ontology(payload: CreateOntologyRequest) -> dict[str, Any]:
        info = registry.create(**payload.model_dump())
        return info.model_dump(mode="json")

    @app.post("/api/ontologies/import", status_code=201)
    async def import_ontology(
        file: Annotated[UploadFile, File()],
        ontology_id: Annotated[str, Form()],
        version: Annotated[str, Form()],
        base_iri: Annotated[str, Form()] = DEFAULT_BASE_IRI,
        title: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            raise RegistryError("OWL 文件不能超过 50 MB。")
        info = registry.import_owl(
            content=content,
            filename=file.filename or "ontology.owl",
            ontology_id=ontology_id,
            version=version,
            base_iri=base_iri,
            title=title or Path(file.filename or ontology_id).stem,
            description=description,
        )
        return info.model_dump(mode="json")

    @app.get("/api/ontologies/{ontology_id}")
    async def get_ontology(ontology_id: str) -> dict[str, Any]:
        versions = registry.list_versions(ontology_id)
        if not versions:
            raise RegistryNotFound(f"本体不存在：{ontology_id}")
        return {
            "ontology_id": ontology_id,
            "versions": [item.model_dump(mode="json") for item in versions],
        }

    @app.get("/api/ontologies/{ontology_id}/versions/{version}")
    async def get_ontology_version(ontology_id: str, version: str) -> dict[str, Any]:
        info, content = registry.get(ontology_id, version)
        return {"metadata": info.model_dump(mode="json"), "turtle": content}

    @app.put("/api/ontologies/{ontology_id}/versions/{version}/turtle")
    async def update_turtle(
        ontology_id: str,
        version: str,
        payload: UpdateTurtleRequest,
    ) -> dict[str, Any]:
        info = registry.update_turtle(ontology_id, version, payload.content)
        return info.model_dump(mode="json")

    @app.get("/api/ontologies/{ontology_id}/versions/{version}/download")
    async def download_ontology(ontology_id: str, version: str) -> Response:
        filename, content = registry.export_turtle(ontology_id, version)
        return Response(
            content=content,
            media_type="text/turtle; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/ontologies/{ontology_id}/versions/{version}/publish")
    async def publish_ontology(ontology_id: str, version: str) -> dict[str, Any]:
        return registry.publish(ontology_id, version, graphdb_client=graphdb)

    @app.delete("/api/ontologies/{ontology_id}/versions/{version}", status_code=204)
    async def delete_version(ontology_id: str, version: str) -> Response:
        registry.delete_version(ontology_id, version)
        return Response(status_code=204)

    @app.delete("/api/ontologies/{ontology_id}", status_code=204)
    async def delete_ontology(ontology_id: str) -> Response:
        registry.delete_ontology(ontology_id)
        return Response(status_code=204)

    @app.post("/api/query")
    async def query_ontology(payload: QueryRequest) -> dict[str, Any]:
        if payload.source == "local":
            return registry.query(payload.ontology_id, payload.version, payload.query)
        info = registry._load(payload.ontology_id, payload.version)
        if not info.graph_iri:
            raise RegistryConflict("该版本尚未发布到 GraphDB。")
        return graphdb.query(payload.query, graph_iri=info.graph_iri)

    @app.post("/api/agent/context")
    async def agent_context(payload: AgentContextRequest) -> dict[str, Any]:
        info = registry._load(payload.ontology_id, payload.version)
        if info.status != "published":
            raise RegistryConflict("Agent 只能读取已发布到 GraphDB 的本体版本。")
        if not info.graph_iri:
            raise RegistryConflict("该已发布版本缺少 GraphDB 命名图信息，请重新发布。")
        return build_agent_context(
            graphdb,
            ontology_id=info.ontology_id,
            version=info.version,
            base_iri=info.base_iri,
            graph_iri=info.graph_iri,
            question=payload.question,
            max_entities=payload.max_entities,
            max_facts=payload.max_facts,
            max_clauses=payload.max_clauses,
            max_chars=payload.max_chars,
        )

    @app.get("/api/runs")
    async def list_runs() -> dict[str, Any]:
        paths = sorted(
            (workspace.root / "runs").glob("*/run.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return {"items": [_run_summary(workspace, path.parent) for path in paths]}

    @app.post("/api/builds", status_code=202)
    async def create_build(
        files: Annotated[list[UploadFile], File()],
        ontology_id: Annotated[str, Form()] = DEFAULT_ONTOLOGY_ID,
        version: Annotated[str, Form()] = DEFAULT_ONTOLOGY_VERSION,
        base_iri: Annotated[str, Form()] = DEFAULT_BASE_IRI,
        run_id: Annotated[str, Form()] = "",
        auto_approve: Annotated[bool, Form()] = False,
        extraction_mode: Annotated[str, Form()] = "rule",
    ) -> dict[str, Any]:
        if not files:
            raise RegistryError("至少上传一个文件。")
        mode = extraction_mode.strip().lower()
        if mode not in {"rule", "ai", "hybrid"}:
            raise RegistryError("抽取模式只能是 rule、ai 或 hybrid。")
        if auto_approve and mode != "rule":
            raise RegistryError("AI 或混合抽取结果必须人工审核，不能自动批准。")
        if mode in {"ai", "hybrid"} and not app.state.ai_settings.ready:
            raise RegistryError(app.state.ai_settings.readiness_error or "AI 抽取尚未配置。")
        upload_id = f"upload-{uuid.uuid4().hex[:12]}"
        upload_dir = workspace.root / "uploads" / upload_id
        upload_dir.mkdir(parents=True)
        input_paths: list[Path] = []
        for upload in files:
            content = await upload.read()
            if len(content) > 100 * 1024 * 1024:
                _safe_remove_tree(upload_dir, allowed_root=workspace.root / "uploads")
                raise RegistryError(f"{upload.filename}: 文件不能超过 100 MB。")
            filename = Path(upload.filename or "upload.bin").name
            target = upload_dir / filename
            counter = 2
            while target.exists():
                target = upload_dir / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
                counter += 1
            target.write_bytes(content)
            input_paths.append(target)
        try:
            run, run_dir = create_run(
                input_paths,
                workspace,
                run_id=run_id.strip() or None,
            )
        except FileExistsError as exc:
            _safe_remove_tree(upload_dir, allowed_root=workspace.root / "uploads")
            raise RegistryConflict(str(exc)) from exc
        except ValueError as exc:
            _safe_remove_tree(upload_dir, allowed_root=workspace.root / "uploads")
            raise RegistryError(str(exc)) from exc
        except Exception:
            _safe_remove_tree(upload_dir, allowed_root=workspace.root / "uploads")
            raise
        config = {
            "ontology_id": ontology_id,
            "version": version,
            "base_iri": base_iri,
            "auto_approve": auto_approve,
            "extraction_mode": mode,
            "input_paths": [str(path) for path in input_paths],
        }
        summary = enqueue_run(
            run,
            config,
            source_upload_dir=str(upload_dir),
        )
        return {
            "summary": summary,
            "run_id": summary["run_id"],
            "status": summary["status"],
            "artifact_dir": str(run_dir),
        }

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        return {
            "summary": runtime_summary(run_dir),
            "run": _read_json(run_dir / "run.json"),
            "manifest": _read_json(run_dir / "manifest.json"),
            "validation": _read_json(run_dir / "validation_report.json"),
            "extraction": _read_json(run_dir / "extraction_report.json"),
            "logs": _read_task_events(run_dir),
        }

    @app.get("/api/runs/{run_id}/progress")
    async def get_run_progress(
        run_id: str,
        log_limit: int = Query(20, ge=1, le=500),
    ) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        return {
            "summary": runtime_summary(run_dir),
            "logs": _read_task_events(run_dir, limit=log_limit),
        }

    @app.get("/api/runs/{run_id}/logs")
    async def get_run_logs(
        run_id: str,
        limit: int = Query(200, ge=1, le=2000),
    ) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        return {"items": _read_task_events(run_dir, limit=limit)}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        tasks.request_cancel(run_id)
        return {
            "summary": runtime_summary(run_dir),
            "logs": _read_task_events(run_dir),
        }

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    async def retry_run(run_id: str) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        if not tasks.wait_for_stop(run_id) or tasks.is_active(run_id):
            raise TaskConflictError("运行任务仍在执行，请先取消并等待任务停止。")
        previous = tasks.read(run_id)
        inputs = _source_input_paths(run_dir)
        previous_config = dict(previous.task_config or {})
        ontology = previous.ontology if isinstance(previous.ontology, dict) else {}
        extraction = previous.extraction if isinstance(previous.extraction, dict) else {}
        mode = str(previous_config.get("extraction_mode") or extraction.get("mode") or "rule")
        if mode not in {"rule", "ai", "hybrid"}:
            mode = "rule"
        if mode in {"ai", "hybrid"} and not app.state.ai_settings.ready:
            raise RegistryError(app.state.ai_settings.readiness_error or "AI 抽取尚未配置。")
        config = {
            "ontology_id": str(
                previous_config.get("ontology_id") or ontology.get("id") or DEFAULT_ONTOLOGY_ID
            ),
            "version": str(
                previous_config.get("version")
                or ontology.get("version")
                or DEFAULT_ONTOLOGY_VERSION
            ),
            "base_iri": str(
                previous_config.get("base_iri") or ontology.get("base_iri") or DEFAULT_BASE_IRI
            ),
            "auto_approve": bool(previous_config.get("auto_approve")),
            "extraction_mode": mode,
            "input_paths": [str(path) for path in inputs],
        }
        if config["auto_approve"] and mode != "rule":
            config["auto_approve"] = False
        run, new_run_dir = create_run(inputs, workspace)
        summary = enqueue_run(
            run,
            config,
            source_upload_dir=previous.source_upload_dir,
            parent_run_id=run_id,
        )
        return {
            "summary": summary,
            "run_id": summary["run_id"],
            "artifact_dir": str(new_run_dir),
            "parent_run_id": run_id,
        }

    @app.delete("/api/runs/{run_id}", status_code=204)
    async def delete_run(run_id: str) -> Response:
        run_dir = _run_directory(workspace, run_id)
        if not tasks.wait_for_stop(run_id) or tasks.is_active(run_id):
            raise TaskConflictError("运行任务仍在执行，请先取消并等待任务停止。")
        run = tasks.read(run_id)
        upload_dir: Path | None = None
        if run.source_upload_dir:
            candidate = Path(run.source_upload_dir).resolve()
            uploads_root = (workspace.root / "uploads").resolve()
            if candidate.is_relative_to(uploads_root):
                upload_dir = candidate
        remove_upload = bool(
            upload_dir
            and upload_dir.exists()
            and not _upload_is_referenced(workspace, upload_dir, exclude_run=run_id)
        )
        _safe_remove_tree(run_dir, allowed_root=workspace.root / "runs")
        if remove_upload and upload_dir:
            _safe_remove_tree(upload_dir, allowed_root=workspace.root / "uploads")
        tasks.forget(run_id)
        return Response(status_code=204)

    @app.get("/api/runs/{run_id}/facts")
    async def get_run_facts(
        run_id: str,
        status: str | None = Query(None),
        search: str = Query(""),
        limit: int = Query(300, ge=1, le=2000),
    ) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        path = run_dir / "facts.jsonl"
        values: list[dict[str, Any]] = []
        if path.exists():
            for line in _read_text(path).splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if status and item.get("review_status") != status:
                    continue
                haystack = " ".join(
                    str(item.get(key, ""))
                    for key in (
                        "subject",
                        "predicate",
                        "object",
                        "evidence_text",
                        "extraction_method",
                    )
                ).lower()
                if search and search.lower() not in haystack:
                    continue
                values.append(item)
        return {"items": values[:limit], "total": len(values), "limit": limit}

    @app.post("/api/runs/{run_id}/review")
    async def review_facts(run_id: str, payload: ReviewRequest) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        review_result = review_run(
            run_dir,
            approve_all=payload.approve_all,
            approve=payload.approve,
            reject=payload.reject,
        )
        run = read_run(run_dir)
        ontology_meta = getattr(run, "ontology", None)
        if not isinstance(ontology_meta, dict):
            ontology_meta = {}
        build_result = build_owl_run(
            run_dir,
            base_iri=str(ontology_meta.get("base_iri") or DEFAULT_BASE_IRI),
            ontology_id=str(ontology_meta.get("id") or DEFAULT_ONTOLOGY_ID),
            version=str(ontology_meta.get("version") or DEFAULT_ONTOLOGY_VERSION),
        )
        validation = validate_run(run_dir)
        manifest = finalize_run(run_dir)
        return {
            "review": review_result,
            "build": build_result,
            "validation": validation,
            "manifest": manifest,
        }

    @app.post("/api/runs/{run_id}/register", status_code=201)
    async def register_run(run_id: str) -> dict[str, Any]:
        run_dir = _run_directory(workspace, run_id)
        validation = _read_json(run_dir / "validation_report.json")
        if validation.get("status") != "valid":
            raise RegistryConflict("运行尚未通过本体校验，不能登记为版本。")
        info = registry.register_run(run_dir)
        return info.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/export")
    async def export_run(run_id: str) -> Response:
        run_dir = _run_directory(workspace, run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(run_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(run_dir).as_posix())
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.zip"'},
        )

    return app


app = create_app()
