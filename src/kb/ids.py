from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    raw = "\x1f".join(normalize_text(str(part)) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def safe_local_name(value: str, fallback: str = "item") -> str:
    value = normalize_text(value)
    value = re.sub(r"[^\w\-.]", "_", value, flags=re.UNICODE)
    value = value.strip("_.-")
    return value or fallback


def normalize_header(value: object, index: int) -> str:
    text = normalize_text("" if value is None else str(value))
    return text or f"column_{index + 1}"
