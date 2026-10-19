from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Literal, TypeVar

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from kb.extraction import ExtractionResult, Extractor
from kb.ids import normalize_text
from kb.linkml_schema import LinkMLSchema, LinkMLSchemaError, load_linkml_schema
from kb.models import ClauseRecord, DocumentRecord, SourceSpan, TableRecord
from kb.ontology import CLASSES, PROPERTIES

ALLOWED_SUBJECT_TYPES = set(CLASSES)
ALLOWED_PREDICATES = {
    "rdf:type",
    *PROPERTIES.keys(),
}
ALLOWED_PREDICATES &= {
    "rdf:type",
    "hasParameter",
    "hasLowerBound",
    "hasUpperBound",
    "hasUnit",
    "hasNote",
    "hasConditionOperator",
    "hasConditionThreshold",
    "hasConditionUnit",
    "requiresAction",
    "prohibitsAction",
    "permitsAction",
    "hasResponsibleRole",
    "hasOperatingCondition",
}
PREDICATE_ALIASES = {
    "具有参数": "hasParameter",
    "包含参数": "hasParameter",
}
ENTITY_TYPE_ALIASES = {
    "equipment": "Equipment",
    "设备": "Equipment",
    "设备设施": "Equipment",
    "component": "Component",
    "部件": "Component",
    "零部件": "Component",
    "组件": "Component",
    "零件": "Component",
    "system": "System",
    "系统": "System",
    "parameter": "Parameter",
    "参数": "Parameter",
    "运行参数": "Parameter",
    "监测参数": "Parameter",
    "role": "Role",
    "角色": "Role",
    "人员角色": "Role",
    "责任角色": "Role",
    "岗位": "Role",
}
OBJECT_TYPE_ALIASES = {
    "iri": "iri",
    "entity": "iri",
    "实体": "iri",
    "实体对象": "iri",
    "literal": "literal",
    "value": "literal",
    "字面值": "literal",
    "文本值": "literal",
}
ExtractionStrategy = Literal["single_pass", "spires"]


class AIConfigurationError(RuntimeError):
    pass


class AIExtractionError(RuntimeError):
    pass


class AIExtractionCancelled(AIExtractionError):
    pass


@dataclass(frozen=True)
class AISettings:
    enabled: bool = False
    provider: str = "openai-compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    batch_size: int = 8
    concurrency: int = 2
    timeout_seconds: float = 60.0
    max_retries: int = 2
    extraction_strategy: ExtractionStrategy = "spires"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AISettings:
        values = env or os.environ
        return cls(
            enabled=_as_bool(values.get("AI_ENABLED", "false")),
            provider=values.get("AI_PROVIDER", "openai-compatible").strip(),
            base_url=values.get("AI_BASE_URL", "").strip().rstrip("/"),
            model=values.get("AI_MODEL", "").strip(),
            api_key=values.get("AI_API_KEY", "").strip(),
            batch_size=_as_int(values.get("AI_BATCH_SIZE"), default=8, minimum=1, maximum=50),
            concurrency=_as_int(
                values.get("AI_CONCURRENCY"),
                default=2,
                minimum=1,
                maximum=8,
            ),
            timeout_seconds=_as_float(
                values.get("AI_TIMEOUT"),
                default=60.0,
                minimum=1.0,
                maximum=600.0,
            ),
            max_retries=_as_int(
                values.get("AI_MAX_RETRIES"),
                default=2,
                minimum=0,
                maximum=5,
            ),
            extraction_strategy=_as_strategy(
                values.get("AI_EXTRACTION_STRATEGY"),
                default="spires",
            ),
        )

    @property
    def readiness_error(self) -> str | None:
        if not self.enabled:
            return "AI 抽取未启用，请在构建页配置并启用。"
        if self.provider != "openai-compatible":
            return f"暂不支持 AI 服务类型：{self.provider}。"
        if not self.base_url:
            return "缺少 AI 服务地址。"
        if not self.model:
            return "缺少 AI 模型名称。"
        return None

    @property
    def ready(self) -> bool:
        return self.readiness_error is None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "extraction_strategy": self.extraction_strategy,
            "message": self.readiness_error or "AI 抽取服务已配置。",
        }


@dataclass
class AICompletion:
    content: str
    usage: dict[str, int]


class AIFactCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    source_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("source_id", "sourceId", "clause_id"),
    )
    subject: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("subject", "subject_name", "entity"),
    )
    subject_type: str = Field(
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("subject_type", "entity_type"),
    )
    predicate: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("predicate", "relation", "property"),
    )
    object: str = Field(
        min_length=1,
        max_length=2000,
        validation_alias=AliasChoices("object", "object_name", "value"),
    )
    object_type: str = "literal"
    object_class: str | None = None
    unit: str | None = None
    condition: str | None = None
    evidence_text: str = Field(
        default="",
        max_length=4000,
        validation_alias=AliasChoices("evidence_text", "evidence", "quote", "text"),
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("confidence", "score"),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_triple_style_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        aliases = {
            "subject": ("head", "head_entity", "subject_name"),
            "subject_type": ("head_type", "head_class"),
            "object": ("tail", "tail_entity", "object_name"),
            "object_class": ("tail_class",),
        }
        for target, source_keys in aliases.items():
            if target in payload:
                continue
            for source_key in source_keys:
                if source_key in payload:
                    payload[target] = payload.pop(source_key)
                    break

        tail_type = payload.pop("tail_type", None)
        if tail_type is not None:
            normalized_tail_type = normalize_entity_type(str(tail_type))
            if "object_type" not in payload:
                payload["object_type"] = (
                    "iri" if normalized_tail_type in ALLOWED_SUBJECT_TYPES else "literal"
                )
            if normalized_tail_type in ALLOWED_SUBJECT_TYPES and "object_class" not in payload:
                payload["object_class"] = normalized_tail_type

        for alias in ("head", "head_entity", "subject_name", "head_type", "head_class"):
            payload.pop(alias, None)
        for alias in ("tail", "tail_entity", "object_name", "tail_class"):
            payload.pop(alias, None)
        return payload


class AIFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[AIFactCandidate] = Field(default_factory=list)


class AIEntityCandidate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    source_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("source_id", "sourceId", "clause_id"),
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("name", "entity_name", "label", "entity"),
    )
    entity_type: str = Field(
        min_length=1,
        max_length=50,
        validation_alias=AliasChoices("entity_type", "type", "class"),
    )
    evidence_text: str = Field(
        default="",
        max_length=4000,
        validation_alias=AliasChoices("evidence_text", "evidence", "quote", "text"),
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("confidence", "score"),
    )


class AIEntityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[AIEntityCandidate] = Field(default_factory=list)


@dataclass(frozen=True)
class AISource:
    source_id: str
    document_id: str
    document_title: str
    clause_id: str | None
    clause_no: str | None
    text: str
    source_ref: dict[str, Any]
    span: SourceSpan | None = None

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "document_title": self.document_title,
            "clause_no": self.clause_no,
            "text": self.text,
        }


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: AISettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if settings.readiness_error:
            raise AIConfigurationError(settings.readiness_error)
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)

    def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
        endpoint = (
            self.settings.base_url
            if self.settings.base_url.endswith("/chat/completions")
            else f"{self.settings.base_url}/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        payload = {
            "model": self.settings.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self._client.post(endpoint, headers=headers, json=payload)
                if response.status_code in {408, 409, 429} or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"AI 服务返回 {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                return AICompletion(
                    content=str(content),
                    usage={
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                    },
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                time.sleep(min(0.25 * (2**attempt), 2.0))
        raise AIExtractionError(f"AI 请求失败: {last_error}") from last_error

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class AIExtractor:
    def __init__(
        self,
        settings: AISettings,
        *,
        client: Any | None = None,
    ) -> None:
        if settings.readiness_error:
            raise AIConfigurationError(settings.readiness_error)
        self.settings = settings
        self.client = client or OpenAICompatibleClient(settings)
        self._owns_client = client is None
        self.extractor = Extractor()

    def extract(
        self,
        documents: list[DocumentRecord],
        clauses: list[ClauseRecord],
        tables: list[TableRecord],
        *,
        strict: bool = True,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ExtractionResult:
        sources = _build_sources(documents, clauses, tables)
        if not sources:
            self.extractor.result.metadata["ai"] = {
                **_empty_batch_stats(),
                "sources": 0,
                "batches": 0,
                "duration_ms": 0,
                "strategy": self.settings.extraction_strategy,
            }
            return self.extractor.result

        started = time.perf_counter()
        batch_size = self.settings.batch_size
        if self.settings.extraction_strategy == "spires":
            batch_size = min(batch_size, 4)
        batches = [
            sources[index : index + batch_size] for index in range(0, len(sources), batch_size)
        ]
        executor = ThreadPoolExecutor(max_workers=self.settings.concurrency)
        futures = [executor.submit(self._extract_batch, batch) for batch in batches]
        batch_results = []
        completed_batches = 0
        cancelled = False
        try:
            for future in as_completed(futures):
                if cancel_check and cancel_check() and not cancelled:
                    cancelled = True
                    for pending in futures:
                        pending.cancel()
                if future.cancelled():
                    continue
                batch_results.append(future.result())
                completed_batches += 1
                if progress_callback:
                    progress_callback(completed_batches, len(futures))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            if self._owns_client:
                self.client.close()
        if cancelled:
            raise AIExtractionCancelled("AI 抽取已取消。")

        warnings: list[str] = []
        stats: dict[str, int] = {
            **_empty_batch_stats(),
            "sources": len(sources),
            "batches": len(batches),
        }
        for facts, batch_warnings, batch_stats in batch_results:
            warnings.extend(batch_warnings)
            for key, value in batch_stats.items():
                stats[key] += value
            for candidate, source in facts:
                self.extractor.add_external_fact(
                    document_id=source.document_id,
                    clause_id=source.clause_id,
                    clause_no=source.clause_no,
                    subject=candidate.subject,
                    subject_type=candidate.subject_type,
                    predicate=candidate.predicate,
                    object_value=candidate.object,
                    object_type=candidate.object_type,
                    object_class=candidate.object_class,
                    evidence_text=candidate.evidence_text,
                    source_ref=source.source_ref,
                    extraction_method=f"ai:{self.settings.model}",
                    confidence=candidate.confidence,
                    unit=normalize_text(candidate.unit) if candidate.unit else None,
                    condition=normalize_text(candidate.condition) if candidate.condition else None,
                    span=source.span,
                )
        stats["duration_ms"] = int((time.perf_counter() - started) * 1000)
        self.extractor.result.warnings.extend(warnings)
        self.extractor.result.metadata["ai"] = {
            **stats,
            "model": self.settings.model,
            "provider": self.settings.provider,
            "strategy": self.settings.extraction_strategy,
        }
        if strict and stats["successful_batches"] == 0:
            raise AIExtractionError(
                "所有 AI 抽取批次均失败。" + (f" 首个错误：{warnings[0]}" if warnings else "")
            )
        return self.extractor.result

    def _extract_batch(
        self,
        batch: Sequence[AISource],
    ) -> tuple[
        list[tuple[AIFactCandidate, AISource]],
        list[str],
        dict[str, int],
    ]:
        if self.settings.extraction_strategy == "spires":
            return self._extract_batch_spires(batch)
        return self._extract_batch_single_pass(batch)

    def _extract_batch_single_pass(
        self,
        batch: Sequence[AISource],
    ) -> tuple[
        list[tuple[AIFactCandidate, AISource]],
        list[str],
        dict[str, int],
    ]:
        stats = _empty_batch_stats()
        warnings: list[str] = []
        try:
            completion = self.client.complete(
                system_prompt=_system_prompt(),
                user_prompt=_user_prompt(batch),
            )
            response = parse_ai_response(completion.content)
        except Exception as exc:
            stats["failed_batches"] = 1
            stats["single_pass_failed_batches"] = 1
            warnings.append(f"AI 单轮抽取批次失败: {exc}")
            return [], warnings, stats

        stats["successful_batches"] = 1
        stats["single_pass_successful_batches"] = 1
        _add_usage(stats, completion.usage)
        stats["candidates"] = len(response.facts)
        source_by_id = {source.source_id: source for source in batch}
        accepted: list[tuple[AIFactCandidate, AISource]] = []
        for index, candidate in enumerate(response.facts, start=1):
            source = source_by_id.get(candidate.source_id)
            if source is None:
                stats["rejected"] += 1
                warnings.append(f"AI 候选 {index} 使用未知来源 {candidate.source_id!r}，已拒绝。")
                continue
            normalize_fact_candidate(candidate)
            issue = validate_candidate(candidate, source)
            if issue:
                stats["rejected"] += 1
                warnings.append(f"AI 候选 {candidate.source_id}#{index} 已拒绝: {issue}")
                continue
            stats["accepted"] += 1
            accepted.append((candidate, source))
        return accepted, warnings, stats

    def _extract_batch_spires(
        self,
        batch: Sequence[AISource],
    ) -> tuple[
        list[tuple[AIFactCandidate, AISource]],
        list[str],
        dict[str, int],
    ]:
        stats = _empty_batch_stats()
        warnings: list[str] = []
        source_by_id = {source.source_id: source for source in batch}

        try:
            entity_completion = self.client.complete(
                system_prompt=_entity_system_prompt(),
                user_prompt=_entity_user_prompt(batch),
            )
            _add_usage(stats, entity_completion.usage)
            entity_response = parse_ai_entity_response(entity_completion.content)
        except Exception as exc:
            stats["failed_batches"] = 1
            stats["entity_stage_failed_batches"] = 1
            warnings.append(f"AI 实体阶段批次失败: {exc}")
            return [], warnings, stats

        stats["entity_stage_successful_batches"] = 1
        stats["entity_candidates"] = len(entity_response.entities)
        accepted_entities: dict[str, list[AIEntityCandidate]] = {
            source.source_id: [] for source in batch
        }
        seen_entities: set[tuple[str, str, str]] = set()
        entity_evidence_fallbacks = 0
        entity_confidence_fallbacks = 0
        for index, entity in enumerate(entity_response.entities, start=1):
            source = source_by_id.get(entity.source_id)
            if source is None:
                stats["entity_rejected"] += 1
                warnings.append(f"AI 实体 {index} 使用未知来源 {entity.source_id!r}，已拒绝。")
                continue
            entity.entity_type = normalize_entity_type(entity.entity_type)
            if not entity.evidence_text:
                entity.evidence_text = source.text
                entity_evidence_fallbacks += 1
            if "confidence" not in entity.model_fields_set:
                entity_confidence_fallbacks += 1
            issue = validate_entity_candidate(entity, source)
            if issue:
                stats["entity_rejected"] += 1
                warnings.append(f"AI 实体 {entity.source_id}#{index} 已拒绝: {issue}")
                continue
            dedupe_key = (
                entity.source_id,
                normalize_text(entity.name),
                entity.entity_type,
            )
            if dedupe_key in seen_entities:
                stats["entity_rejected"] += 1
                warnings.append(f"AI 实体 {entity.source_id}#{index} 与已接受实体重复，已跳过。")
                continue
            seen_entities.add(dedupe_key)
            accepted_entities[entity.source_id].append(entity)
            stats["entity_accepted"] += 1

        if entity_evidence_fallbacks:
            warnings.append(
                f"AI 实体阶段有 {entity_evidence_fallbacks} 个候选未返回证据，"
                "已使用对应来源条款原文作为证据。"
            )
        if entity_confidence_fallbacks:
            warnings.append(
                f"AI 实体阶段有 {entity_confidence_fallbacks} 个候选未返回置信度，"
                "已使用默认值 0.5。"
            )

        if stats["entity_accepted"] == 0:
            stats["successful_batches"] = 1
            stats["relation_skipped_batches"] = 1
            return [], warnings, stats

        try:
            fact_completion = self.client.complete(
                system_prompt=_relation_system_prompt(),
                user_prompt=_relation_user_prompt(batch, accepted_entities),
            )
            _add_usage(stats, fact_completion.usage)
            fact_response = parse_ai_response(fact_completion.content)
        except Exception as exc:
            stats["failed_batches"] = 1
            stats["relation_stage_failed_batches"] = 1
            warnings.append(f"AI 关系阶段批次失败: {exc}")
            return [], warnings, stats

        stats["relation_stage_successful_batches"] = 1
        stats["successful_batches"] = 1
        stats["candidates"] = len(fact_response.facts)
        accepted: list[tuple[AIFactCandidate, AISource]] = []
        relation_evidence_fallbacks = 0
        relation_confidence_fallbacks = 0
        for index, candidate in enumerate(fact_response.facts, start=1):
            source = source_by_id.get(candidate.source_id)
            if source is None:
                stats["rejected"] += 1
                warnings.append(f"AI 关系 {index} 使用未知来源 {candidate.source_id!r}，已拒绝。")
                continue
            normalize_fact_candidate(candidate)
            if not candidate.evidence_text:
                candidate.evidence_text = source.text
                relation_evidence_fallbacks += 1
            if "confidence" not in candidate.model_fields_set:
                relation_confidence_fallbacks += 1
            issue = validate_candidate(candidate, source)
            if issue:
                stats["rejected"] += 1
                warnings.append(f"AI 关系 {candidate.source_id}#{index} 已拒绝: {issue}")
                continue
            issue = validate_candidate_grounding(
                candidate,
                accepted_entities.get(candidate.source_id, []),
            )
            if issue:
                stats["rejected"] += 1
                warnings.append(f"AI 关系 {candidate.source_id}#{index} 未通过实体对齐: {issue}")
                continue
            stats["accepted"] += 1
            accepted.append((candidate, source))
        if relation_evidence_fallbacks:
            warnings.append(
                f"AI 关系阶段有 {relation_evidence_fallbacks} 个候选未返回证据，"
                "已使用对应来源条款原文作为证据。"
            )
        if relation_confidence_fallbacks:
            warnings.append(
                f"AI 关系阶段有 {relation_confidence_fallbacks} 个候选未返回置信度，"
                "已使用默认值 0.5。"
            )
        return accepted, warnings, stats


def extract_with_ai(
    documents: list[DocumentRecord],
    clauses: list[ClauseRecord],
    tables: list[TableRecord],
    *,
    settings: AISettings,
    client: Any | None = None,
    strict: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ExtractionResult:
    return AIExtractor(settings, client=client).extract(
        documents,
        clauses,
        tables,
        strict=strict,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def parse_ai_response(content: str) -> AIFactResponse:
    return _parse_json_model(content, AIFactResponse)


def parse_ai_entity_response(content: str) -> AIEntityResponse:
    return _parse_json_model(content, AIEntityResponse)


def _parse_json_model(
    content: str,
    model_type: type[ResponseModel],
) -> ResponseModel:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AIExtractionError("AI 返回内容中没有 JSON 对象。")
    try:
        payload = json.loads(text[start : end + 1])
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise AIExtractionError(f"AI JSON 结构无效: {_validation_error_summary(exc)}") from exc
    except json.JSONDecodeError as exc:
        payload = _salvage_truncated_response(text, model_type)
        if payload is None:
            raise AIExtractionError(f"AI JSON 结构无效: {exc}") from exc
        try:
            return model_type.model_validate(payload)
        except ValidationError as validation_exc:
            raise AIExtractionError(
                f"AI JSON 结构无效: {_validation_error_summary(validation_exc)}"
            ) from validation_exc


def _salvage_truncated_response(
    text: str,
    model_type: type[ResponseModel],
) -> dict[str, list[dict[str, Any]]] | None:
    field_name = next(iter(model_type.model_fields))
    marker = f'"{field_name}"'
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    array_start = text.find("[", marker_index + len(marker))
    if array_start < 0:
        return None

    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    index = array_start + 1
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,":
            index += 1
        if index >= len(text) or text[index] == "]":
            break
        if text[index] != "{":
            next_object = text.find("{", index + 1)
            if next_object < 0:
                break
            index = next_object
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            next_object = text.find("{", index + 1)
            if next_object < 0:
                break
            index = next_object
            continue
        if isinstance(value, dict):
            items.append(value)

    if not items:
        return None
    return {field_name: items}


def _validation_error_summary(exc: ValidationError) -> str:
    errors = exc.errors()
    fields: list[str] = []
    details: list[str] = []
    for error in errors:
        location = ".".join(str(item) for item in error.get("loc", ())) or "<root>"
        if location not in fields:
            fields.append(location)
        if len(details) < 6:
            details.append(f"{location}: {error.get('msg', '校验失败')}")

    summary = f"{len(errors)} 个字段校验错误"
    if fields:
        summary += f"；涉及字段：{', '.join(fields[:6])}"
        if len(fields) > 6:
            summary += " 等"
    if details:
        summary += "；" + "；".join(details)
    return summary


def validate_candidate(candidate: AIFactCandidate, source: AISource) -> str | None:
    if candidate.subject_type not in ALLOWED_SUBJECT_TYPES:
        return f"非法主体类型 {candidate.subject_type}"
    if candidate.predicate not in ALLOWED_PREDICATES:
        return f"非法谓词 {candidate.predicate}"
    if candidate.object_type not in {"iri", "literal"}:
        return f"非法对象类型 {candidate.object_type}"
    if candidate.object_type == "iri" and candidate.object_class not in ALLOWED_SUBJECT_TYPES:
        return "IRI 对象缺少合法 object_class"
    if candidate.predicate == "rdf:type" and candidate.object not in ALLOWED_SUBJECT_TYPES:
        return f"非法本体类 {candidate.object}"

    evidence = normalize_text(candidate.evidence_text)
    source_text = normalize_text(source.text)
    if evidence not in source_text:
        return "evidence_text 不是来源文本的原文片段"
    if normalize_text(candidate.subject) not in evidence:
        return "subject 未出现在证据原文中"
    if normalize_text(candidate.object) not in evidence:
        return "object 未出现在证据原文中"
    return None


def normalize_entity_type(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized in ALLOWED_SUBJECT_TYPES:
        return normalized
    return ENTITY_TYPE_ALIASES.get(normalized.casefold(), normalized)


def normalize_fact_candidate(candidate: AIFactCandidate) -> None:
    candidate.subject_type = normalize_entity_type(candidate.subject_type)
    candidate.predicate = normalize_predicate(candidate.predicate)
    candidate.object_type = OBJECT_TYPE_ALIASES.get(
        str(candidate.object_type or "").strip().casefold(),
        candidate.object_type,
    )
    if candidate.object_class:
        candidate.object_class = normalize_entity_type(candidate.object_class)
    if candidate.predicate == "rdf:type":
        candidate.object = normalize_entity_type(candidate.object)


def normalize_predicate(value: str) -> str:
    normalized = normalize_text(str(value or ""))
    if normalized in ALLOWED_PREDICATES:
        return normalized
    alias = PREDICATE_ALIASES.get(normalized)
    if alias:
        return alias
    folded = normalized.casefold()
    for predicate in ALLOWED_PREDICATES:
        if predicate.casefold() == folded or _snake_case(predicate) == folded:
            return predicate
    return normalized


def validate_entity_candidate(
    candidate: AIEntityCandidate,
    source: AISource,
) -> str | None:
    if candidate.entity_type not in ALLOWED_SUBJECT_TYPES:
        return f"非法实体类型 {candidate.entity_type}"
    evidence = normalize_text(candidate.evidence_text)
    source_text = normalize_text(source.text)
    if evidence not in source_text:
        return "evidence_text 不是来源文本的原文片段"
    if normalize_text(candidate.name) not in evidence:
        return "实体名称未出现在证据原文中"
    return None


def validate_candidate_grounding(
    candidate: AIFactCandidate,
    entities: Sequence[AIEntityCandidate],
) -> str | None:
    subject_matches = [
        entity
        for entity in entities
        if normalize_text(entity.name) == normalize_text(candidate.subject)
    ]
    if not subject_matches:
        return f"主体 {candidate.subject!r} 不在已接受实体清单中"
    if not any(entity.entity_type == candidate.subject_type for entity in subject_matches):
        accepted_types = ", ".join(sorted({entity.entity_type for entity in subject_matches}))
        return (
            f"主体 {candidate.subject!r} 的类型为 {accepted_types}，"
            f"与关系声明的 {candidate.subject_type} 不一致"
        )

    if candidate.object_type != "iri" or candidate.predicate == "rdf:type":
        return None
    object_matches = [
        entity
        for entity in entities
        if normalize_text(entity.name) == normalize_text(candidate.object)
    ]
    if not object_matches:
        return f"IRI 对象 {candidate.object!r} 不在已接受实体清单中"
    if not any(entity.entity_type == candidate.object_class for entity in object_matches):
        accepted_types = ", ".join(sorted({entity.entity_type for entity in object_matches}))
        return (
            f"IRI 对象 {candidate.object!r} 的类型为 {accepted_types}，"
            f"与关系声明的 {candidate.object_class} 不一致"
        )
    return None


def _build_sources(
    documents: list[DocumentRecord],
    clauses: list[ClauseRecord],
    tables: list[TableRecord],
) -> list[AISource]:
    document_by_id = {item.document_id: item for item in documents}
    sources: list[AISource] = []
    for clause in clauses:
        document = document_by_id.get(clause.document_id)
        sources.append(
            AISource(
                source_id=clause.clause_id,
                document_id=clause.document_id,
                document_title=document.title if document else clause.document_id,
                clause_id=clause.clause_id,
                clause_no=clause.clause_no,
                text=clause.text,
                source_ref={
                    "document_id": clause.document_id,
                    "clause_id": clause.clause_id,
                    "clause_no": clause.clause_no,
                    "source_spans": [item.model_dump(mode="json") for item in clause.source_spans],
                },
                span=clause.source_spans[0] if clause.source_spans else None,
            )
        )
    for table in tables:
        document = document_by_id.get(table.document_id)
        text = json.dumps(table.columns, ensure_ascii=False, default=str)
        sources.append(
            AISource(
                source_id=f"table:{table.table_record_id}",
                document_id=table.document_id,
                document_title=document.title if document else table.document_id,
                clause_id=None,
                clause_no=None,
                text=text,
                source_ref={
                    "document_id": table.document_id,
                    "table_id": table.table_id,
                    "table_record_id": table.table_record_id,
                    "row_index": table.row_index,
                    "sheet_name": table.sheet_name,
                    "columns": table.columns,
                },
                span=(
                    table.source_spans[0]
                    if table.source_spans
                    else SourceSpan(
                        row_index=table.row_index,
                        sheet_name=table.sheet_name,
                    )
                ),
            )
        )
    return sources


def _system_prompt() -> str:
    schema = _extraction_schema_spec()
    return (
        "你是规章知识抽取器。只抽取来源文本明确表达的事实，不得补写、推测或改写实体名称。"
        "每个事实必须有逐字证据片段，subject 和 object 必须出现在 evidence_text 中。"
        "参数边界使用 hasLowerBound 或 hasUpperBound；义务、禁止、许可分别使用 "
        "requiresAction、prohibitsAction、permitsAction。"
        "只输出单行紧凑 JSON，不得输出 Markdown 代码块、换行排版、注释、解释或额外字段。"
        "facts 中每项字段名必须严格使用 source_id、subject、subject_type、predicate、object、"
        "object_type、evidence_text、confidence，不得改用 relation、value、quote 等别名。"
        '没有可抽取事实时输出 {"facts": []}。\n'
        f"LinkML 模式约束：{json.dumps(schema, ensure_ascii=False)}"
    )


def _user_prompt(batch: Sequence[AISource]) -> str:
    payload = [source.prompt_payload() for source in batch]
    return (
        "从以下 sources 中抽取候选事实。source_id 必须使用输入中的值。"
        '输出结构必须是 {"facts": [...]}。\n' + json.dumps(payload, ensure_ascii=False)
    )


def _entity_system_prompt() -> str:
    schema = _extraction_schema_spec()
    output = {
        "entities": [
            {
                "source_id": "来源 ID，必须原样返回",
                "name": "来源 text 中逐字出现的实体名称",
                "entity_type": "EntityType 中的一个本体类",
                "evidence_text": "来源 text 中逐字出现的证据片段",
                "confidence": "0 到 1",
            }
        ]
    }
    return (
        "你是规章知识图谱的命名实体识别器，执行 SPIRES 两阶段抽取的第一阶段。"
        "只识别来源文本明确出现的设备、部件、系统、运行参数和责任角色。"
        "不得把动作、数值、单位、条件或泛指词识别为实体。"
        "实体名称必须与来源原文一致，且必须出现在 evidence_text 中。"
        "entity_type 只能取 Equipment、Component、System、Parameter、Role 这五个英文值，"
        "禁止输出“设备”“运行参数”等中文类型名称。"
        "不要在此阶段输出关系事实。"
        "只输出单行紧凑 JSON，不得输出 Markdown 代码块、换行排版、注释、解释或额外字段。"
        "entities 中每项字段名必须严格使用 source_id、name、entity_type、evidence_text、"
        "confidence，不得改用 entity_name、type、label 等别名。"
        '没有实体时输出 {"entities": []}。\n'
        f"LinkML 实体模式：{json.dumps(schema['entity_schema'], ensure_ascii=False)}\n"
        f"输出结构：{json.dumps(output, ensure_ascii=False)}"
    )


def _entity_user_prompt(batch: Sequence[AISource]) -> str:
    payload = [source.prompt_payload() for source in batch]
    return (
        "从以下 sources 中识别候选实体。source_id 必须使用输入中的值。"
        '输出结构必须是 {"entities": [...]}。\n' + json.dumps(payload, ensure_ascii=False)
    )


def _relation_system_prompt() -> str:
    schema = _extraction_schema_spec()
    allowed_predicates = sorted(ALLOWED_PREDICATES)
    output = {
        "facts": [
            {
                "source_id": "来源 ID，必须原样返回",
                "subject": "必须来自输入实体清单的主体名称",
                "subject_type": "主体实体的 EntityType",
                "predicate": "必须逐字取自 allowed_predicates",
                "object": "实体名称或来源中的字面值",
                "object_type": "iri 或 literal",
                "object_class": "object_type=iri 时必填，且与实体清单一致",
                "unit": "单位或 null",
                "condition": "适用条件或 null",
                "evidence_text": "来源 text 中逐字出现的证据片段",
                "confidence": "0 到 1",
            }
        ]
    }
    return (
        "你是规章知识图谱的关系抽取器，执行 SPIRES 两阶段抽取的第二阶段。"
        "只能基于输入中已经识别的实体和来源原文建立关系，不得新增实体或改写实体名称。"
        "subject 必须对齐到同一 source_id 的已接受实体；object_type=iri 时 object 和 "
        "object_class 也必须对齐到已接受实体。object_type=literal 时 object 必须是证据原文中的"
        "数值、动作、单位、条件或说明文字。每条事实必须有逐字证据片段。"
        f"predicate 必须逐字等于 allowed_predicates 中的一个英文名称：{', '.join(allowed_predicates)}。"
        "中文谓词标签、动作短语、路径表达和自由造词都不是合法 predicate；"
        "禁止输出“具有参数”“检查”“关联部件”等中文关系名。"
        "设备到运行参数使用 hasParameter；无法对应白名单谓词时不要输出该事实。"
        "只输出单行紧凑 JSON，不得输出 Markdown 代码块、换行排版、注释、解释或额外字段。"
        "facts 中每项字段名必须严格使用 source_id、subject、subject_type、predicate、object、"
        "object_type、evidence_text、confidence，不得改用 relation、value、quote 等别名。"
        '没有关系事实时输出 {"facts": []}。\n'
        f"LinkML 关系模式：{json.dumps(schema['relation_schema'], ensure_ascii=False)}\n"
        f"allowed_predicates：{json.dumps(allowed_predicates, ensure_ascii=False)}\n"
        f"输出结构：{json.dumps(output, ensure_ascii=False)}"
    )


def _relation_user_prompt(
    batch: Sequence[AISource],
    entities_by_source: Mapping[str, Sequence[AIEntityCandidate]],
) -> str:
    payload = []
    for source in batch:
        item = source.prompt_payload()
        item["accepted_entities"] = [
            {
                "name": entity.name,
                "entity_type": entity.entity_type,
            }
            for entity in entities_by_source.get(source.source_id, [])
        ]
        payload.append(item)
    return (
        "从以下 sources 和 accepted_entities 中抽取候选关系事实。"
        "source_id 必须使用输入中的值，实体名称和类型不得偏离 accepted_entities。"
        '输出结构必须是 {"facts": [...]}。\n' + json.dumps(payload, ensure_ascii=False)
    )


@lru_cache(maxsize=1)
def _load_schema() -> LinkMLSchema:
    try:
        return load_linkml_schema()
    except LinkMLSchemaError as exc:
        raise AIExtractionError(str(exc)) from exc


@lru_cache(maxsize=1)
def _extraction_schema_spec() -> dict[str, Any]:
    schema = _load_schema()
    entity_classes = _schema_classes(schema)
    predicates = _schema_predicates(schema)
    entity_types = schema.enum_values("EntityType") or sorted(ALLOWED_SUBJECT_TYPES)
    return {
        "schema": {
            "path": schema.path.name,
            "title": schema.title,
            "description": schema.description,
        },
        "entity_schema": {
            "classes": entity_classes,
            "entity_types": entity_types,
        },
        "relation_schema": {
            "classes": entity_classes,
            "predicates": predicates,
            "object_types": ["iri", "literal"],
        },
    }


def _schema_classes(schema: LinkMLSchema) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for class_name in sorted(ALLOWED_SUBJECT_TYPES):
        definition = schema.class_definition(class_name)
        attributes = definition.get("attributes") or {}
        items.append(
            {
                "name": class_name,
                "description": str(
                    definition.get("description") or CLASSES.get(class_name, ("", ""))[1]
                ),
                "attributes": sorted(attributes) if isinstance(attributes, dict) else [],
            }
        )
    return items


def _schema_predicates(schema: LinkMLSchema) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for predicate in sorted(ALLOWED_PREDICATES):
        if predicate == "rdf:type":
            items.append(
                {
                    "name": predicate,
                    "description": "声明实体所属的本体类",
                    "domain": "Entity",
                    "range": "EntityType",
                }
            )
            continue
        label, property_type, domain, range_name = PROPERTIES[predicate]
        slot = schema.slot_definition(_snake_case(predicate))
        items.append(
            {
                "name": predicate,
                "description": str(slot.get("description") or label),
                "property_type": property_type,
                "domain": str(slot.get("domain") or domain or "Entity"),
                "range": str(slot.get("range") or range_name or "string"),
            }
        )
    return items


def _snake_case(value: str) -> str:
    characters: list[str] = []
    for character in value:
        if character.isupper() and characters:
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _empty_batch_stats() -> dict[str, int]:
    return {
        "successful_batches": 0,
        "failed_batches": 0,
        "single_pass_successful_batches": 0,
        "single_pass_failed_batches": 0,
        "entity_stage_successful_batches": 0,
        "entity_stage_failed_batches": 0,
        "relation_stage_successful_batches": 0,
        "relation_stage_failed_batches": 0,
        "relation_skipped_batches": 0,
        "entity_candidates": 0,
        "entity_accepted": 0,
        "entity_rejected": 0,
        "candidates": 0,
        "accepted": 0,
        "rejected": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _add_usage(stats: dict[str, int], usage: Mapping[str, int]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        stats[key] += int(usage.get(key) or 0)


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value)) if value not in (None, "") else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(
    value: str | None,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(str(value)) if value not in (None, "") else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_strategy(
    value: str | None,
    *,
    default: ExtractionStrategy,
) -> ExtractionStrategy:
    normalized = str(value or "").strip().lower()
    if normalized in {"single_pass", "spires"}:
        return normalized  # type: ignore[return-value]
    return default


def settings_as_dict(settings: AISettings) -> dict[str, Any]:
    return asdict(settings)
