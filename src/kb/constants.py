from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS: dict[str, tuple[str, str]] = {
    ".csv": ("structured", "text/csv"),
    ".json": ("structured", "application/json"),
    ".xml": ("structured", "application/xml"),
    ".xlsx": (
        "structured",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".xls": ("structured", "application/vnd.ms-excel"),
    ".docx": (
        "unstructured",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".pdf": ("unstructured", "application/pdf"),
    ".txt": ("unstructured", "text/plain"),
    ".md": ("unstructured", "text/markdown"),
    ".html": ("unstructured", "text/html"),
    ".htm": ("unstructured", "text/html"),
}

KNOWN_UNSUPPORTED_EXTENSIONS = {
    ".doc": "旧版 DOC 暂不直接解析，请转换为 DOCX 后重试。",
    ".ppt": "旧版 PPT 暂不直接解析，请转换为 PPTX 后重试。",
    ".pptx": "当前版本暂未启用演示文稿解析。",
}

DEFAULT_BASE_IRI = "https://example.org/regulation/"
DEFAULT_ONTOLOGY_ID = "regulation"
DEFAULT_ONTOLOGY_VERSION = "0.1.0"

DEFAULT_COLUMN_MAPPING = {
    "equipment": ["设备名称", "设备", "设备名", "equipment", "equipment_name"],
    "parameter": ["参数名称", "参数", "参数名", "parameter", "parameter_name"],
    "lower": ["下限", "最小值", "min", "lower", "lower_bound"],
    "upper": ["上限", "最大值", "max", "upper", "upper_bound"],
    "unit": ["单位", "unit", "uom"],
    "clause_no": ["条款号", "条款", "clause", "clause_no"],
    "notes": ["备注", "说明", "note", "notes", "description"],
}

ARTIFACT_FILES = (
    "manifest.json",
    "source.json",
    "document.jsonl",
    "blocks.jsonl",
    "clauses.jsonl",
    "table_records.jsonl",
    "entity_mentions.jsonl",
    "facts.jsonl",
    "review_items.jsonl",
    "extraction_report.json",
    "ontology.ttl",
    "shapes.ttl",
    "mapping.yaml",
    "provenance.jsonl",
    "validation_report.json",
    "checksums.sha256",
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_mapping_path() -> Path:
    return project_root() / "schemas" / "columns" / "equipment_parameters.yaml"


def default_linkml_path() -> Path:
    return project_root() / "schemas" / "regulation.linkml.yaml"
