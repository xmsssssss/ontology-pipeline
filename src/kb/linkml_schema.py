from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kb.constants import default_linkml_path


class LinkMLSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class LinkMLSchema:
    path: Path
    title: str
    description: str
    classes: dict[str, dict[str, Any]]
    slots: dict[str, dict[str, Any]]
    enums: dict[str, dict[str, Any]]

    def class_definition(self, name: str) -> dict[str, Any]:
        return self.classes.get(name, {})

    def slot_definition(self, name: str) -> dict[str, Any]:
        return self.slots.get(name, {})

    def enum_values(self, name: str) -> list[str]:
        definition = self.enums.get(name) or {}
        values = definition.get("permissible_values") or {}
        if isinstance(values, list):
            return [str(value) for value in values]
        if isinstance(values, dict):
            return [str(value) for value in values]
        return []


def load_linkml_schema(path: Path | str | None = None) -> LinkMLSchema:
    schema_path = Path(path) if path is not None else default_linkml_path()
    try:
        payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LinkMLSchemaError(f"无法读取 LinkML 模式：{schema_path}") from exc
    except yaml.YAMLError as exc:
        raise LinkMLSchemaError(f"LinkML 模式 YAML 无效：{schema_path}") from exc

    if not isinstance(payload, dict):
        raise LinkMLSchemaError("LinkML 模式根节点必须是对象。")

    classes = payload.get("classes") or {}
    slots = payload.get("slots") or {}
    enums = payload.get("enums") or {}
    for section_name, section in (
        ("classes", classes),
        ("slots", slots),
        ("enums", enums),
    ):
        if not isinstance(section, dict):
            raise LinkMLSchemaError(f"LinkML 模式 {section_name} 必须是对象。")

    return LinkMLSchema(
        path=schema_path.resolve(),
        title=str(payload.get("title") or payload.get("name") or schema_path.stem),
        description=str(payload.get("description") or ""),
        classes={
            str(name): definition if isinstance(definition, dict) else {}
            for name, definition in classes.items()
        },
        slots={
            str(name): definition if isinstance(definition, dict) else {}
            for name, definition in slots.items()
        },
        enums={
            str(name): definition if isinstance(definition, dict) else {}
            for name, definition in enums.items()
        },
    )
