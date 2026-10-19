from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kb.models import KbModel


class Workspace:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def ensure(self) -> Workspace:
        for name in (
            "raw",
            "documents",
            "runs",
            "ontology",
            "exports",
            "locks",
            "logs",
            "config",
        ):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        return self

    @property
    def inventory_path(self) -> Path:
        return self.root / "inventory.jsonl"

    def document_dir(self, document_id: str) -> Path:
        path = self.root / "documents" / document_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_dir(self, run_id: str) -> Path:
        path = self.root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ontology_version_dir(self, ontology_id: str, version: str) -> Path:
        path = self.root / "ontology" / ontology_id / version
        path.mkdir(parents=True, exist_ok=True)
        return path

    def atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(40):
                try:
                    os.replace(temp_name, path)
                    break
                except PermissionError:
                    if attempt == 39:
                        raise
                    time.sleep(0.025)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def write_json(self, path: Path, value: Any) -> None:
        if isinstance(value, KbModel):
            value = value.model_dump(mode="json")
        self.atomic_write_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        )

    def read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_jsonl(self, path: Path, values: Iterable[Any]) -> None:
        lines: list[str] = []
        for value in values:
            if isinstance(value, KbModel):
                value = value.model_dump(mode="json")
            lines.append(json.dumps(value, ensure_ascii=False, default=str))
        self.atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_checksums(self, directory: Path) -> Path:
        import hashlib

        lines: list[str] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name == "checksums.sha256":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(directory).as_posix()}")
        target = directory / "checksums.sha256"
        self.atomic_write_text(target, "\n".join(lines) + ("\n" if lines else ""))
        return target

    def append_inventory(self, records: Iterable[Any]) -> None:
        existing = self.read_jsonl(self.inventory_path)
        known = {row.get("file_id") for row in existing}
        new_rows: list[dict[str, Any]] = []
        for record in records:
            value = record.model_dump(mode="json") if isinstance(record, KbModel) else record
            if value.get("file_id") not in known:
                new_rows.append(value)
        self.write_jsonl(self.inventory_path, existing + new_rows)
