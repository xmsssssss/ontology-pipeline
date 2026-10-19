from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kb.constants import KNOWN_UNSUPPORTED_EXTENSIONS, SUPPORTED_EXTENSIONS
from kb.ids import sha256_file, stable_id
from kb.models import FileRecord
from kb.storage import Workspace


@dataclass
class ScanResult:
    files: list[FileRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_duplicates: list[dict[str, str]] = field(default_factory=list)


def _iter_input_files(inputs: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for raw_input in inputs:
        path = raw_input.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"输入路径不存在: {path}")
        if path.is_file():
            if not path.name.startswith("~$"):
                paths.append(path)
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file() or candidate.name.startswith("~$"):
                continue
            if any(part in {".git", ".venv", "__pycache__"} for part in candidate.parts):
                continue
            paths.append(candidate)
    return sorted(set(paths), key=lambda item: str(item).lower())


def classify_file(path: Path) -> tuple[str, str, str | None]:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_EXTENSIONS:
        file_type, media_type = SUPPORTED_EXTENSIONS[suffix]
        return file_type, media_type, None
    warning = KNOWN_UNSUPPORTED_EXTENSIONS.get(
        suffix,
        f"暂不支持扩展名 {suffix or '<无扩展名>'}，文件已登记但不会进入解析。",
    )
    return "unsupported", "application/octet-stream", warning


def scan_inputs(inputs: Iterable[Path], workspace: Workspace) -> ScanResult:
    workspace.ensure()
    result = ScanResult()
    seen_hashes: dict[str, Path] = {}

    for path in _iter_input_files(inputs):
        digest = sha256_file(path)
        if digest in seen_hashes:
            result.skipped_duplicates.append(
                {
                    "source_path": str(path),
                    "duplicate_of": str(seen_hashes[digest]),
                    "sha256": digest,
                }
            )
            continue
        seen_hashes[digest] = path

        file_type, media_type, warning = classify_file(path)
        if warning:
            result.warnings.append(f"{path}: {warning}")

        suffix = path.suffix.lower()
        relative_stored = Path("raw") / digest[:2] / f"{digest}{suffix}"
        stored_path = workspace.root / relative_stored
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        if not stored_path.exists():
            shutil.copy2(path, stored_path)

        result.files.append(
            FileRecord(
                file_id=stable_id("file", digest, length=24),
                sha256=digest,
                file_name=path.name,
                media_type=media_type,
                file_type=file_type,
                source_path=str(path),
                stored_path=relative_stored.as_posix(),
                size_bytes=path.stat().st_size,
            )
        )

    workspace.append_inventory(result.files)
    return result
