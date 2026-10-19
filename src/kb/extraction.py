from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kb.constants import DEFAULT_COLUMN_MAPPING, default_mapping_path
from kb.ids import normalize_text, stable_id
from kb.models import (
    ClauseRecord,
    DocumentRecord,
    EntityMention,
    FactRecord,
    ReviewItem,
    SourceSpan,
    TableRecord,
)

XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

ENTITY_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{1,12}?"
    r"(?:给水泵|循环水泵|水泵|泵体|泵|风机|电机|汽轮机|锅炉|变压器|冷却器|加热器|阀门|轴承|系统)"
)
ROLE_RE = re.compile(
    r"(?:值班人员|操作人员|运行人员|维护人员|检修人员|值班班长|值班长|负责人|操作员)"
)

UNITS = r"摄氏度|℃|MPa|kPa|Pa|mm/s|m/s|r/min|rpm|Hz|kV|V|A|%"
VALUE_UNIT = rf"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>{UNITS})"
PARAM = r"(?P<param>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff\-]{1,24}?)"

LIMIT_PATTERNS = (
    (
        "upper",
        re.compile(
            PARAM + r"(?:不应超过|不得超过|不高于|不应大于|最高不超过|最大不超过)\s*" + VALUE_UNIT
        ),
    ),
    (
        "upper",
        re.compile(PARAM + r"(?:不超过|不得高于|小于等于|≤)\s*" + VALUE_UNIT),
    ),
    (
        "lower",
        re.compile(
            PARAM + r"(?:不应低于|不得低于|不低于|不小于|最低不低于|最小不低于)\s*" + VALUE_UNIT
        ),
    ),
    (
        "lower",
        re.compile(PARAM + r"(?:不低于|大于等于|≥)\s*" + VALUE_UNIT),
    ),
)

RANGE_RE = re.compile(
    PARAM
    + r"(?:的)?(?:正常范围(?:为|是)?|范围为|运行范围(?:为|是)?)\s*"
    + rf"(?P<lower>-?\d+(?:\.\d+)?)\s*(?P<unit>{UNITS})"
    + r"\s*(?:至|到|~|～|-)\s*"
    + rf"(?P<upper>-?\d+(?:\.\d+)?)\s*(?P<upper_unit>{UNITS})?"
)

CONDITION_RE = re.compile(
    PARAM
    + r"(?P<operator>低于|高于|小于|大于|达到|超过)\s*"
    + rf"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>{UNITS})"
)

ACTION_RULES = (
    ("prohibitsAction", ("严禁", "禁止", "不得", "不允许")),
    ("requiresAction", ("必须", "应当", "应该", "需要")),
    ("permitsAction", ("可以", "允许", "可")),
)

EVIDENCE_BOUNDARY_CHARS = "。！？!?；;\n"
ACTION_BOUNDARY_CHARS = "。！？!?；;\n"

ENTITY_PREFIXES = (
    "本规程适用于",
    "情况下拆卸",
    "应熟悉",
    "适用于",
    "情况下",
    "其",
    "流量和",
    "值班人员",
    "操作人员",
    "运行人员",
    "维护人员",
    "检修人员",
    "值班班长",
    "值班长",
    "负责人",
    "检查",
    "启动",
    "关闭",
    "开启",
    "拆卸",
    "报告",
    "记录",
    "执行",
    "必须",
    "应当",
    "严禁",
    "禁止",
    "可以",
    "允许",
    "未",
    "在",
    "对",
    "将",
    "和",
    "及",
    "与",
    "的",
)

ACTION_ONLY_ENTITIES = {
    "紧急停泵",
    "紧急停机",
    "停泵",
    "启泵",
}

PARAMETER_SUFFIXES = re.compile(
    r"(?:不应|不得|不可|不能|严禁|禁止|应当|应该|必须|可以|允许|应|须)$"
)


@dataclass
class ExtractionResult:
    mentions: list[EntityMention] = field(default_factory=list)
    facts: list[FactRecord] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _trim_entity(value: str) -> str:
    result = normalize_text(value)
    changed = True
    while changed and len(result) > 1:
        changed = False
        for prefix in ENTITY_PREFIXES:
            if result.startswith(prefix) and len(result) > len(prefix):
                result = result[len(prefix) :]
                changed = True
                break
    for connector in ("和", "及", "与"):
        if connector in result:
            result = result.rsplit(connector, 1)[-1]
    return result


def extract_entities(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in ENTITY_RE.finditer(text):
        label = _trim_entity(match.group(0))
        if len(label) < 2 or label in seen or label in ACTION_ONLY_ENTITIES:
            continue
        seen.add(label)
        if label.endswith("系统"):
            entity_type = "System"
        elif label.endswith(("轴承", "阀门", "泵体")):
            entity_type = "Component"
        else:
            entity_type = "Equipment"
        result.append((label, entity_type))
    return result


def _equipment_key(label: str, entity_type: str) -> str:
    return f"{entity_type.lower()}|{normalize_text(label)}"


def _typed_entity_key(label: str, entity_type: str) -> str:
    if entity_type in {"Equipment", "Component", "System"}:
        return _equipment_key(label, entity_type)
    if entity_type == "Role":
        return f"role|{normalize_text(label)}"
    if entity_type == "Parameter":
        return f"parameter|external|{normalize_text(label)}"
    if entity_type == "Document":
        return f"document|{normalize_text(label)}"
    return f"{entity_type.lower()}|{normalize_text(label)}"


EXTERNAL_FACT_TYPES = {
    "rdf:type": "type_assertion",
    "hasParameter": "relationship",
    "hasLowerBound": "parameter_bound",
    "hasUpperBound": "parameter_bound",
    "hasConditionThreshold": "parameter_condition",
    "hasUnit": "parameter_metadata",
    "hasNote": "parameter_metadata",
    "hasConditionOperator": "parameter_condition",
    "hasConditionUnit": "parameter_condition",
    "hasOperatingCondition": "operating_condition",
    "requiresAction": "regulatory_action",
    "prohibitsAction": "regulatory_action",
    "permitsAction": "regulatory_action",
    "hasResponsibleRole": "responsibility",
}

EXTERNAL_DATATYPES = {
    "hasLowerBound": XSD_DECIMAL,
    "hasUpperBound": XSD_DECIMAL,
    "hasConditionThreshold": XSD_DECIMAL,
}


def _parameter_key(equipment: str, parameter: str) -> str:
    return f"parameter|{normalize_text(equipment)}|{normalize_text(parameter)}"


def _parameter_label(equipment: str, parameter: str) -> str:
    return f"{normalize_text(parameter)}（{normalize_text(equipment)}）"


def _primary_equipment(text: str, entities: list[tuple[str, str]]) -> tuple[str, str] | None:
    candidates = [item for item in entities if item[1] in {"Equipment", "Component"}]
    if not candidates:
        return None
    for candidate in candidates:
        if candidate[0].endswith(("泵", "风机", "电机", "汽轮机", "锅炉", "变压器")):
            return candidate
    return candidates[0]


def _document_equipment_context(clauses: list[ClauseRecord]) -> tuple[str, str] | None:
    counts: dict[tuple[str, str], int] = {}
    first_seen: dict[tuple[str, str], int] = {}
    for index, clause in enumerate(clauses):
        for entity in extract_entities(clause.text):
            if entity[1] != "Equipment":
                continue
            counts[entity] = counts.get(entity, 0) + 1
            first_seen.setdefault(entity, index)
    if not counts:
        return None
    return max(counts, key=lambda item: (counts[item], -first_seen[item]))


def _number(value: Any) -> str | None:
    if value is None or value == "":
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return match.group(0) if match else None


def _clean_parameter(raw: str, equipment: str | None) -> str:
    value = normalize_text(raw)
    if equipment:
        value = value.replace(equipment, "", 1)
    value = re.sub(r"^(?:的|其|该|设备|机组)", "", value)
    value = PARAMETER_SUFFIXES.sub("", value)
    return normalize_text(value).strip("，,。；;：:")


def _bounded_snippet(text: str, start: int, end: int, *, limit: int = 600) -> str:
    if not text:
        return ""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))

    left = 0
    for index in range(start - 1, -1, -1):
        if text[index] in EVIDENCE_BOUNDARY_CHARS:
            left = index + 1
            break

    right = len(text)
    for index in range(end, len(text)):
        if text[index] in EVIDENCE_BOUNDARY_CHARS:
            right = index + 1
            break

    value = re.sub(r"\s+", " ", text[left:right]).strip()
    if len(value) <= limit:
        return value

    match_start = max(0, start - left)
    window_start = max(0, match_start - limit // 4)
    value = re.sub(
        r"\s+",
        " ",
        text[left + window_start : left + window_start + limit],
    ).strip()
    if window_start > 0:
        value = "..." + value
    if left + window_start + limit < right:
        value += "..."
    return value


def _action_snippet(text: str, start: int, *, limit: int = 400) -> str:
    end = len(text)
    for index in range(max(0, start), len(text)):
        if text[index] in ACTION_BOUNDARY_CHARS:
            end = index
            break
    value = re.sub(r"\s+", " ", text[start:end]).strip("，,。；;：:")
    if len(value) > limit:
        value = value[:limit].rstrip("，,；;：:") + "..."
    return value


def load_column_mapping(path: Path | None = None) -> dict[str, list[str]]:
    mapping_path = path or default_mapping_path()
    if not mapping_path.exists():
        return {key: list(values) for key, values in DEFAULT_COLUMN_MAPPING.items()}
    raw = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    columns = raw.get("columns", raw)
    result = {key: list(values) for key, values in DEFAULT_COLUMN_MAPPING.items()}
    for key, values in columns.items():
        if isinstance(values, list):
            result[str(key)] = [str(item) for item in values]
    return result


def _find_value(
    columns: dict[str, Any],
    aliases: Iterable[str],
) -> tuple[str | None, Any]:
    normalized = {normalize_text(str(key)).lower(): key for key in columns}
    for alias in aliases:
        key = normalized.get(normalize_text(alias).lower())
        if key is not None:
            return str(key), columns[key]
    return None, None


class Extractor:
    def __init__(self, column_mapping: dict[str, list[str]] | None = None) -> None:
        self.column_mapping = column_mapping or load_column_mapping()
        self.result = ExtractionResult()
        self._mention_ids: set[str] = set()
        self._fact_ids: set[str] = set()
        self._review_ids: set[str] = set()

    def _add_mention(
        self,
        document_id: str,
        clause_id: str | None,
        label: str,
        entity_type: str,
        entity_key: str,
        *,
        span: SourceSpan | None = None,
        confidence: float = 1.0,
    ) -> None:
        mention_id = stable_id(
            "mention",
            document_id,
            clause_id or "",
            entity_type,
            entity_key,
            span.block_id if span else "",
            length=24,
        )
        if mention_id in self._mention_ids:
            return
        self._mention_ids.add(mention_id)
        self.result.mentions.append(
            EntityMention(
                mention_id=mention_id,
                document_id=document_id,
                clause_id=clause_id,
                label=label,
                entity_key=entity_key,
                entity_type=entity_type,
                source_span=span,
                confidence=confidence,
            )
        )

    def _add_fact(
        self,
        *,
        fact_type: str,
        subject: str,
        subject_key: str,
        subject_type: str,
        predicate: str,
        object_value: str,
        object_type: str,
        document_id: str,
        clause_id: str | None,
        clause_no: str | None,
        evidence_text: str,
        source_ref: dict[str, Any],
        object_key: str | None = None,
        object_label: str | None = None,
        object_class: str | None = None,
        datatype: str | None = None,
        unit: str | None = None,
        condition: str | None = None,
        confidence: float = 1.0,
        extraction_method: str = "rule",
    ) -> None:
        fact_id = stable_id(
            "fact",
            document_id,
            clause_id or "",
            fact_type,
            subject_key,
            predicate,
            object_key or object_value,
            unit or "",
            json.dumps(source_ref, ensure_ascii=False, sort_keys=True, default=str),
            length=24,
        )
        if fact_id in self._fact_ids:
            return
        self._fact_ids.add(fact_id)
        fact = FactRecord(
            fact_id=fact_id,
            fact_type=fact_type,
            subject=subject,
            subject_key=subject_key,
            subject_type=subject_type,
            predicate=predicate,
            object=object_value,
            object_key=object_key,
            object_label=object_label,
            object_class=object_class,
            object_type="iri" if object_type == "iri" else "literal",
            datatype=datatype,
            unit=unit,
            condition=condition,
            evidence_text=evidence_text,
            document_id=document_id,
            clause_id=clause_id,
            clause_no=clause_no,
            source_ref=source_ref,
            confidence=confidence,
            extraction_method=extraction_method,
        )
        self.result.facts.append(fact)
        self._add_review(fact)

    def _add_review(self, fact: FactRecord) -> None:
        reason = f"{fact.fact_type}_requires_review"
        review_id = stable_id("review", fact.fact_id, length=24)
        if review_id in self._review_ids:
            return
        self._review_ids.add(review_id)
        self.result.reviews.append(
            ReviewItem(
                review_item_id=review_id,
                fact_id=fact.fact_id,
                reason=reason,
                payload={
                    "subject": fact.subject,
                    "predicate": fact.predicate,
                    "object": fact.object,
                    "confidence": fact.confidence,
                    "evidence_text": fact.evidence_text,
                },
            )
        )

    def add_external_fact(
        self,
        *,
        document_id: str,
        clause_id: str | None,
        clause_no: str | None,
        subject: str,
        subject_type: str,
        predicate: str,
        object_value: str,
        object_type: str,
        object_class: str | None,
        evidence_text: str,
        source_ref: dict[str, Any],
        extraction_method: str,
        confidence: float,
        unit: str | None = None,
        condition: str | None = None,
        span: SourceSpan | None = None,
    ) -> None:
        fact_type = EXTERNAL_FACT_TYPES.get(predicate)
        if fact_type is None:
            raise ValueError(f"不支持的抽取谓词: {predicate}")

        subject_key = _typed_entity_key(subject, subject_type)
        object_key: str | None = None
        object_label: str | None = None
        datatype = EXTERNAL_DATATYPES.get(predicate, XSD_STRING)
        if predicate == "rdf:type":
            object_class = object_class or object_value
            object_type = "iri"
            datatype = None
        elif object_type == "iri":
            object_class = object_class or ("Parameter" if predicate == "hasParameter" else None)
            if object_class:
                object_key = _typed_entity_key(object_value, object_class)
                object_label = object_value
        else:
            object_type = "literal"
            object_class = None

        self._add_mention(
            document_id,
            clause_id,
            subject,
            subject_type,
            subject_key,
            span=span,
            confidence=confidence,
        )
        if object_type == "iri" and object_class:
            self._add_mention(
                document_id,
                clause_id,
                object_value,
                object_class,
                object_key or _typed_entity_key(object_value, object_class),
                span=span,
                confidence=confidence,
            )
        self._add_fact(
            fact_type=fact_type,
            subject=subject,
            subject_key=subject_key,
            subject_type=subject_type,
            predicate=predicate,
            object_value=object_value,
            object_key=object_key,
            object_label=object_label,
            object_class=object_class,
            object_type=object_type,
            datatype=datatype,
            unit=unit,
            condition=condition,
            document_id=document_id,
            clause_id=clause_id,
            clause_no=clause_no,
            evidence_text=evidence_text,
            source_ref=source_ref,
            confidence=confidence,
            extraction_method=extraction_method,
        )

    def _add_parameter_facts(
        self,
        *,
        document_id: str,
        clause_id: str | None,
        clause_no: str | None,
        equipment: str,
        parameter: str,
        evidence_text: str,
        source_ref: dict[str, Any],
        lower: str | None = None,
        upper: str | None = None,
        unit: str | None = None,
        notes: str | None = None,
        confidence: float = 0.95,
        extraction_method: str = "rule",
        span: SourceSpan | None = None,
    ) -> str:
        equipment_type = "Equipment"
        equipment_key = _equipment_key(equipment, equipment_type)
        parameter_key = _parameter_key(equipment, parameter)
        parameter_label = _parameter_label(equipment, parameter)
        self._add_mention(
            document_id,
            clause_id,
            equipment,
            equipment_type,
            equipment_key,
            span=span,
            confidence=confidence,
        )
        self._add_mention(
            document_id,
            clause_id,
            parameter_label,
            "Parameter",
            parameter_key,
            span=span,
            confidence=confidence,
        )
        self._add_fact(
            fact_type="type_assertion",
            subject=equipment,
            subject_key=equipment_key,
            subject_type=equipment_type,
            predicate="rdf:type",
            object_value="Equipment",
            object_type="iri",
            object_class="Equipment",
            document_id=document_id,
            clause_id=clause_id,
            clause_no=clause_no,
            evidence_text=evidence_text,
            source_ref=source_ref,
            confidence=confidence,
            extraction_method=extraction_method,
        )
        self._add_fact(
            fact_type="type_assertion",
            subject=parameter_label,
            subject_key=parameter_key,
            subject_type="Parameter",
            predicate="rdf:type",
            object_value="Parameter",
            object_type="iri",
            object_class="Parameter",
            document_id=document_id,
            clause_id=clause_id,
            clause_no=clause_no,
            evidence_text=evidence_text,
            source_ref=source_ref,
            confidence=confidence,
            extraction_method=extraction_method,
        )
        self._add_fact(
            fact_type="relationship",
            subject=equipment,
            subject_key=equipment_key,
            subject_type=equipment_type,
            predicate="hasParameter",
            object_value=parameter_label,
            object_key=parameter_key,
            object_label=parameter_label,
            object_class="Parameter",
            object_type="iri",
            document_id=document_id,
            clause_id=clause_id,
            clause_no=clause_no,
            evidence_text=evidence_text,
            source_ref=source_ref,
            confidence=confidence,
            extraction_method=extraction_method,
        )
        for predicate, value in (("hasLowerBound", lower), ("hasUpperBound", upper)):
            if value is None:
                continue
            self._add_fact(
                fact_type="parameter_bound",
                subject=parameter_label,
                subject_key=parameter_key,
                subject_type="Parameter",
                predicate=predicate,
                object_value=str(value),
                object_type="literal",
                datatype=XSD_DECIMAL,
                unit=unit,
                document_id=document_id,
                clause_id=clause_id,
                clause_no=clause_no,
                evidence_text=evidence_text,
                source_ref=source_ref,
                confidence=confidence,
                extraction_method=extraction_method,
            )
        if unit:
            self._add_fact(
                fact_type="parameter_metadata",
                subject=parameter_label,
                subject_key=parameter_key,
                subject_type="Parameter",
                predicate="hasUnit",
                object_value=unit,
                object_type="literal",
                datatype=XSD_STRING,
                unit=unit,
                document_id=document_id,
                clause_id=clause_id,
                clause_no=clause_no,
                evidence_text=evidence_text,
                source_ref=source_ref,
                confidence=confidence,
                extraction_method=extraction_method,
            )
        if notes:
            self._add_fact(
                fact_type="parameter_metadata",
                subject=parameter_label,
                subject_key=parameter_key,
                subject_type="Parameter",
                predicate="hasNote",
                object_value=str(notes),
                object_type="literal",
                datatype=XSD_STRING,
                document_id=document_id,
                clause_id=clause_id,
                clause_no=clause_no,
                evidence_text=evidence_text,
                source_ref=source_ref,
                confidence=confidence,
                extraction_method=extraction_method,
            )
        return parameter_key

    def extract_clause(
        self,
        document: DocumentRecord,
        clause: ClauseRecord,
        context_equipment: tuple[str, str] | None = None,
    ) -> None:
        text = clause.text
        entities = extract_entities(text)
        span = clause.source_spans[0] if clause.source_spans else None
        for label, entity_type in entities:
            self._add_mention(
                document.document_id,
                clause.clause_id,
                label,
                entity_type,
                _equipment_key(label, entity_type),
                span=span,
            )
        roles = list(dict.fromkeys(ROLE_RE.findall(text)))
        for role in roles:
            self._add_mention(
                document.document_id,
                clause.clause_id,
                role,
                "Role",
                f"role|{role}",
                span=span,
            )

        document_entities = extract_entities(document.title)
        document_primary = _primary_equipment(document.title, document_entities)
        if document_primary is None:
            document_primary = context_equipment
        primary = _primary_equipment(text, entities)
        if document_primary is not None and (primary is None or primary[1] == "Component"):
            primary = document_primary
        default_equipment = primary[0] if primary else document.title
        source_ref = {
            "document_id": document.document_id,
            "clause_id": clause.clause_id,
            "clause_no": clause.clause_no,
            "source_spans": [item.model_dump(mode="json") for item in clause.source_spans],
        }

        constraint_spans: list[tuple[int, int]] = []
        for match in RANGE_RE.finditer(text):
            equipment = default_equipment
            parameter = _clean_parameter(match.group("param"), equipment)
            if not parameter:
                continue
            unit = match.group("unit")
            self._add_parameter_facts(
                document_id=document.document_id,
                clause_id=clause.clause_id,
                clause_no=clause.clause_no,
                equipment=equipment,
                parameter=parameter,
                lower=match.group("lower"),
                upper=match.group("upper"),
                unit=unit,
                evidence_text=_bounded_snippet(text, match.start(), match.end()),
                source_ref=source_ref,
                confidence=0.96,
                span=span,
            )
            constraint_spans.append(match.span())

        for direction, pattern in LIMIT_PATTERNS:
            for match in pattern.finditer(text):
                if any(start <= match.start() < end for start, end in constraint_spans):
                    continue
                equipment = default_equipment
                parameter = _clean_parameter(match.group("param"), equipment)
                if not parameter:
                    continue
                value = match.group("value")
                self._add_parameter_facts(
                    document_id=document.document_id,
                    clause_id=clause.clause_id,
                    clause_no=clause.clause_no,
                    equipment=equipment,
                    parameter=parameter,
                    lower=value if direction == "lower" else None,
                    upper=value if direction == "upper" else None,
                    unit=match.group("unit"),
                    evidence_text=_bounded_snippet(text, match.start(), match.end()),
                    source_ref=source_ref,
                    confidence=0.94,
                    span=span,
                )
                constraint_spans.append(match.span())

        condition_match = CONDITION_RE.search(text)
        condition: str | None = None
        condition_is_limit = bool(
            condition_match
            and any(start <= condition_match.start() < end for start, end in constraint_spans)
        )
        if condition_match and not condition_is_limit:
            equipment = default_equipment
            parameter = _clean_parameter(condition_match.group("param"), equipment)
            if parameter:
                condition_evidence = _bounded_snippet(
                    text,
                    condition_match.start(),
                    condition_match.end(),
                )
                parameter_key = self._add_parameter_facts(
                    document_id=document.document_id,
                    clause_id=clause.clause_id,
                    clause_no=clause.clause_no,
                    equipment=equipment,
                    parameter=parameter,
                    evidence_text=condition_evidence,
                    source_ref=source_ref,
                    confidence=0.9,
                    span=span,
                )
                parameter_label = _parameter_label(equipment, parameter)
                for predicate, value, datatype in (
                    (
                        "hasConditionOperator",
                        condition_match.group("operator"),
                        XSD_STRING,
                    ),
                    (
                        "hasConditionThreshold",
                        condition_match.group("value"),
                        XSD_DECIMAL,
                    ),
                    ("hasConditionUnit", condition_match.group("unit"), XSD_STRING),
                ):
                    self._add_fact(
                        fact_type="parameter_condition",
                        subject=parameter_label,
                        subject_key=parameter_key,
                        subject_type="Parameter",
                        predicate=predicate,
                        object_value=value,
                        object_type="literal",
                        datatype=datatype,
                        unit=condition_match.group("unit"),
                        document_id=document.document_id,
                        clause_id=clause.clause_id,
                        clause_no=clause.clause_no,
                        evidence_text=condition_evidence,
                        source_ref=source_ref,
                        confidence=0.9,
                    )
            condition = normalize_text(condition_match.group(0))
            if text[condition_match.end() : condition_match.end() + 1] == "时":
                condition += "时"

        for predicate, markers in ACTION_RULES:
            found: tuple[int, str] | None = None
            for marker in markers:
                index = text.find(marker)
                if index >= 0 and (found is None or index < found[0]):
                    found = (index, marker)
            if not found:
                continue
            marker_index, marker = found
            action_start = marker_index + len(marker)
            action = _action_snippet(text, action_start)
            if len(action) < 2:
                continue
            action_evidence = _bounded_snippet(
                text,
                marker_index,
                action_start + len(action),
            )
            prefix = text[:marker_index]
            action_condition = condition
            if not action_condition:
                prefix_without_roles = ROLE_RE.sub("", prefix).strip("，,。；;：: ")
                boundary = re.search(r"(?:时|前|后|情况下)[，,]?\s*$", prefix_without_roles)
                if boundary:
                    action_condition = prefix_without_roles
            embedded_condition = re.match(r"^(在.+?情况下)[，,]?\s*(.+)$", action)
            if embedded_condition:
                if not action_condition:
                    action_condition = normalize_text(embedded_condition.group(1))
                action = embedded_condition.group(2).strip("，,。；;：:")
            subject = primary[0] if primary else document.title
            subject_type = primary[1] if primary else "Document"
            subject_key = (
                _equipment_key(subject, subject_type)
                if primary
                else f"document|{document.document_id}"
            )
            self._add_fact(
                fact_type="regulatory_action",
                subject=subject,
                subject_key=subject_key,
                subject_type=subject_type,
                predicate=predicate,
                object_value=action,
                object_type="literal",
                datatype=XSD_STRING,
                condition=action_condition,
                document_id=document.document_id,
                clause_id=clause.clause_id,
                clause_no=clause.clause_no,
                evidence_text=action_evidence,
                source_ref=source_ref,
                confidence=0.9,
            )
            for role in roles:
                self._add_fact(
                    fact_type="responsibility",
                    subject=subject,
                    subject_key=subject_key,
                    subject_type=subject_type,
                    predicate="hasResponsibleRole",
                    object_value=role,
                    object_type="literal",
                    datatype=XSD_STRING,
                    document_id=document.document_id,
                    clause_id=clause.clause_id,
                    clause_no=clause.clause_no,
                    evidence_text=action_evidence,
                    source_ref=source_ref,
                    confidence=0.88,
                )
            break

    def extract_table(self, document: DocumentRecord, table: TableRecord) -> None:
        mapping = self.column_mapping
        equipment_column, equipment = _find_value(table.columns, mapping["equipment"])
        parameter_column, parameter = _find_value(table.columns, mapping["parameter"])
        if equipment_column is None or parameter_column is None:
            return
        equipment_label = normalize_text(str(equipment or ""))
        parameter_label = normalize_text(str(parameter or ""))
        if not equipment_label or not parameter_label:
            return
        _, lower = _find_value(table.columns, mapping["lower"])
        _, upper = _find_value(table.columns, mapping["upper"])
        _, unit = _find_value(table.columns, mapping["unit"])
        _, clause_no = _find_value(table.columns, mapping["clause_no"])
        _, notes = _find_value(table.columns, mapping["notes"])
        source_ref = {
            "document_id": document.document_id,
            "table_id": table.table_id,
            "table_record_id": table.table_record_id,
            "row_index": table.row_index,
            "sheet_name": table.sheet_name,
            "columns": table.columns,
        }
        span = (
            table.source_spans[0]
            if table.source_spans
            else SourceSpan(
                row_index=table.row_index,
                sheet_name=table.sheet_name,
            )
        )
        self._add_parameter_facts(
            document_id=document.document_id,
            clause_id=None,
            clause_no=str(clause_no) if clause_no not in (None, "") else None,
            equipment=equipment_label,
            parameter=parameter_label,
            lower=_number(lower),
            upper=_number(upper),
            unit=normalize_text(str(unit)) if unit not in (None, "") else None,
            notes=normalize_text(str(notes)) if notes not in (None, "") else None,
            evidence_text=json.dumps(table.columns, ensure_ascii=False, default=str),
            source_ref=source_ref,
            confidence=0.99,
            extraction_method="structured_mapping",
            span=span,
        )

    def extract(
        self,
        documents: list[DocumentRecord],
        clauses: list[ClauseRecord],
        tables: list[TableRecord],
    ) -> ExtractionResult:
        document_by_id = {item.document_id: item for item in documents}
        clauses_by_document: dict[str, list[ClauseRecord]] = {}
        for clause in clauses:
            clauses_by_document.setdefault(clause.document_id, []).append(clause)
        context_by_document = {
            document_id: _document_equipment_context(document_clauses)
            for document_id, document_clauses in clauses_by_document.items()
        }
        for clause in clauses:
            document = document_by_id.get(clause.document_id)
            if document is not None:
                self.extract_clause(
                    document,
                    clause,
                    context_by_document.get(document.document_id),
                )
        for table in tables:
            document = document_by_id.get(table.document_id)
            if document is not None:
                self.extract_table(document, table)
        return self.result


def extract_all(
    documents: list[DocumentRecord],
    clauses: list[ClauseRecord],
    tables: list[TableRecord],
    column_mapping_path: Path | None = None,
) -> ExtractionResult:
    extractor = Extractor(load_column_mapping(column_mapping_path))
    return extractor.extract(documents, clauses, tables)


def merge_extraction_results(
    primary: ExtractionResult,
    supplementary: ExtractionResult,
) -> ExtractionResult:
    result = ExtractionResult(
        mentions=list(primary.mentions),
        facts=list(primary.facts),
        reviews=list(primary.reviews),
        warnings=[*primary.warnings, *supplementary.warnings],
        metadata=dict(primary.metadata),
    )
    mention_ids = {item.mention_id for item in result.mentions}
    fact_ids = {item.fact_id for item in result.facts}
    review_ids = {item.review_item_id for item in result.reviews}
    parameter_predicates = {
        "hasParameter",
        "hasLowerBound",
        "hasUpperBound",
        "hasUnit",
        "hasNote",
        "hasConditionOperator",
        "hasConditionThreshold",
        "hasConditionUnit",
        "hasOperatingCondition",
    }
    semantic_keys = {
        _fact_semantic_key(item, parameter_predicates=parameter_predicates) for item in result.facts
    }
    deduplicated_facts = 0
    for mention in supplementary.mentions:
        if mention.mention_id not in mention_ids:
            mention_ids.add(mention.mention_id)
            result.mentions.append(mention)
    for fact in supplementary.facts:
        semantic_key = _fact_semantic_key(fact, parameter_predicates=parameter_predicates)
        if fact.fact_id in fact_ids or semantic_key in semantic_keys:
            deduplicated_facts += 1
            continue
        fact_ids.add(fact.fact_id)
        semantic_keys.add(semantic_key)
        result.facts.append(fact)
    for review in supplementary.reviews:
        if review.review_item_id not in review_ids:
            review_ids.add(review.review_item_id)
            result.reviews.append(review)
    for key, value in supplementary.metadata.items():
        if isinstance(value, dict) and isinstance(result.metadata.get(key), dict):
            result.metadata[key] = {**result.metadata[key], **value}
        else:
            result.metadata[key] = value
    result.metadata["deduplicated_facts"] = deduplicated_facts
    return result


def _fact_semantic_key(
    fact: FactRecord,
    *,
    parameter_predicates: set[str],
) -> tuple[str, ...]:
    subject = "" if fact.predicate in parameter_predicates else normalize_text(fact.subject)
    return (
        fact.document_id or "",
        fact.clause_id or "",
        subject,
        fact.predicate,
        normalize_text(fact.object),
        normalize_text(fact.unit or ""),
    )
