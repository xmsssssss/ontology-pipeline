from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb.models import RunRecord, utc_now
from kb.storage import Workspace

ACTIVE_RUN_STATUSES = {"queued", "running", "cancelling"}


class TaskConflictError(RuntimeError):
    pass


@dataclass
class TaskHandle:
    run_id: str
    cancel_event: threading.Event
    thread: threading.Thread


class RunTaskManager:
    """Coordinates in-process pipeline tasks while run state remains on disk."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._handles: dict[str, TaskHandle] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.RLock()

    def _lock_for(self, run_id: str) -> threading.RLock:
        with self._registry_lock:
            return self._locks.setdefault(run_id, threading.RLock())

    def _run_path(self, run_id: str) -> Path:
        return self.workspace.root / "runs" / run_id / "run.json"

    def read(self, run_id: str) -> RunRecord:
        with self._lock_for(run_id):
            return RunRecord.model_validate(self.workspace.read_json(self._run_path(run_id)))

    def update(
        self,
        run_id: str,
        *,
        event: str | None = None,
        level: str = "info",
        **changes: Any,
    ) -> RunRecord:
        with self._lock_for(run_id):
            path = self._run_path(run_id)
            current = RunRecord.model_validate(self.workspace.read_json(path))
            changes.setdefault("updated_at", utc_now())
            updated = current.model_copy(update=changes)
            self.workspace.write_json(path, updated)
            if event:
                self._append_event(
                    path.parent,
                    run_id=run_id,
                    event=event,
                    level=level,
                    stage=updated.stage,
                    stage_label=updated.stage_label,
                    progress=updated.progress,
                )
            return updated

    def log(
        self,
        run_id: str,
        event: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock_for(run_id):
            current = self.read(run_id)
            self._append_event(
                self._run_path(run_id).parent,
                run_id=run_id,
                event=event,
                level=level,
                stage=current.stage,
                stage_label=current.stage_label,
                progress=current.progress,
                details=details,
            )

    def _append_event(
        self,
        run_dir: Path,
        *,
        run_id: str,
        event: str,
        level: str,
        stage: str,
        stage_label: str,
        progress: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        path = run_dir / "task_events.jsonl"
        events = self.workspace.read_jsonl(path)
        events.append(
            {
                "timestamp": utc_now().isoformat(),
                "run_id": run_id,
                "level": level,
                "stage": stage,
                "stage_label": stage_label,
                "progress": progress,
                "event": event,
                "details": details or {},
            }
        )
        self.workspace.write_jsonl(path, events)

    def submit(self, run_id: str, target: Callable[[threading.Event], None]) -> None:
        with self._registry_lock:
            existing = self._handles.get(run_id)
            if existing and existing.thread.is_alive():
                raise TaskConflictError(f"运行任务已在执行：{run_id}")
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=target,
                args=(cancel_event,),
                name=f"kb-run-{run_id}",
                daemon=True,
            )
            self._handles[run_id] = TaskHandle(
                run_id=run_id,
                cancel_event=cancel_event,
                thread=thread,
            )
            thread.start()

    def request_cancel(self, run_id: str) -> RunRecord:
        with self._registry_lock:
            handle = self._handles.get(run_id)
        if not handle or not handle.thread.is_alive():
            raise TaskConflictError("该任务当前不在执行，无法取消。")
        handle.cancel_event.set()
        return self.update(
            run_id,
            status="cancelling",
            stage="cancelling",
            stage_label="正在取消",
            cancel_requested=True,
            event="已请求取消任务，等待当前处理步骤结束。",
            level="warning",
        )

    def is_active(self, run_id: str) -> bool:
        with self._registry_lock:
            handle = self._handles.get(run_id)
        if handle and handle.thread.is_alive():
            return True
        path = self._run_path(run_id)
        if not path.exists():
            return False
        run = RunRecord.model_validate(self.workspace.read_json(path))
        return run.status in ACTIVE_RUN_STATUSES

    def wait_for_stop(self, run_id: str, *, timeout: float = 2.0) -> bool:
        """Wait for terminal cleanup without blocking active builds."""
        with self._registry_lock:
            handle = self._handles.get(run_id)
        if not handle or not handle.thread.is_alive():
            return True
        path = self._run_path(run_id)
        if path.exists():
            run = RunRecord.model_validate(self.workspace.read_json(path))
            if run.status in ACTIVE_RUN_STATUSES:
                return False
        handle.thread.join(timeout=timeout)
        return not handle.thread.is_alive()

    def forget(self, run_id: str) -> None:
        with self._registry_lock:
            handle = self._handles.get(run_id)
            if handle and handle.thread.is_alive():
                raise TaskConflictError("运行任务仍在执行，不能删除。")
            self._handles.pop(run_id, None)
        with self._registry_lock:
            self._locks.pop(run_id, None)

    def recover_interrupted(self) -> int:
        recovered = 0
        for path in (self.workspace.root / "runs").glob("*/run.json"):
            run = RunRecord.model_validate(self.workspace.read_json(path))
            if run.status not in ACTIVE_RUN_STATUSES:
                continue
            errors = list(run.errors)
            message = "服务重启时任务尚未完成，已标记为中断。"
            if message not in errors:
                errors.append(message)
            self.update(
                run.run_id,
                status="interrupted",
                stage="interrupted",
                stage_label="服务重启，任务中断",
                cancel_requested=False,
                errors=errors,
                finished_at=utc_now(),
                event=message,
                level="error",
            )
            recovered += 1
        return recovered

    def shutdown(self) -> None:
        with self._registry_lock:
            handles = list(self._handles.values())
        for handle in handles:
            handle.cancel_event.set()
        for handle in handles:
            handle.thread.join(timeout=1.0)
