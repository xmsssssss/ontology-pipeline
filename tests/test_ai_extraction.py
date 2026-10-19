from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import kb.web as web_module
from kb.ai_extraction import (
    ALLOWED_PREDICATES,
    AICompletion,
    AIExtractionError,
    AIExtractor,
    AISettings,
    OpenAICompatibleClient,
    parse_ai_entity_response,
    parse_ai_response,
)
from kb.extraction import extract_all, merge_extraction_results
from kb.models import ClauseRecord, DocumentRecord
from kb.pipeline import _build_extraction_report, run_pipeline
from kb.storage import Workspace
from kb.web import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(**changes):
    values = {
        "enabled": True,
        "base_url": "https://ai.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "batch_size": 4,
        "concurrency": 1,
        "max_retries": 0,
        "extraction_strategy": "single_pass",
    }
    values.update(changes)
    return AISettings(**values)


def _ai_payload(**changes):
    values = {
        "enabled": True,
        "provider": "openai-compatible",
        "base_url": "https://ai.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "clear_api_key": False,
        "batch_size": 4,
        "concurrency": 1,
        "timeout_seconds": 60,
        "max_retries": 0,
        "extraction_strategy": "single_pass",
    }
    values.update(changes)
    return values


def _connection_payload(**changes):
    values = {
        "provider": "openai-compatible",
        "base_url": "https://ai.test/v1",
        "model": "test-model",
        "api_key": "secret",
        "clear_api_key": False,
        "timeout_seconds": 10,
    }
    values.update(changes)
    return values


def _document_and_clause(text: str) -> tuple[DocumentRecord, ClauseRecord]:
    document = DocumentRecord(
        document_id="doc_ai",
        file_id="file_ai",
        title="给水泵运行规程",
        file_name="给水泵运行规程.txt",
        file_type="unstructured",
    )
    clause = ClauseRecord(
        clause_id="clause_ai",
        document_id=document.document_id,
        clause_no="第1条",
        text=text,
    )
    return document, clause


class StaticAIClient:
    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
        return AICompletion(
            content=json.dumps(self.payload, ensure_ascii=False),
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 8,
                "total_tokens": 18,
            },
        )


class SequenceAIClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        payload = self.payloads[len(self.system_prompts) - 1]
        return AICompletion(
            content=json.dumps(payload, ensure_ascii=False),
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        )


def test_ai_extractor_accepts_only_valid_candidates():
    text = "给水泵出口压力不应超过1.6MPa。"
    document, clause = _document_and_clause(text)
    client = StaticAIClient(
        {
            "facts": [
                {
                    "source_id": clause.clause_id,
                    "subject": "出口压力",
                    "subject_type": "Parameter",
                    "predicate": "hasUpperBound",
                    "object": "1.6",
                    "object_type": "literal",
                    "unit": "MPa",
                    "condition": None,
                    "evidence_text": text,
                    "confidence": 0.91,
                },
                {
                    "source_id": clause.clause_id,
                    "subject": "给水泵",
                    "subject_type": "Equipment",
                    "predicate": "requiresAction",
                    "object": "停机",
                    "object_type": "literal",
                    "evidence_text": "给水泵温度超过100摄氏度时必须停机。",
                    "confidence": 0.8,
                },
                {
                    "source_id": clause.clause_id,
                    "subject": "给水泵",
                    "subject_type": "Equipment",
                    "predicate": "causesFailure",
                    "object": "给水泵",
                    "object_type": "literal",
                    "evidence_text": text,
                    "confidence": 0.8,
                },
            ]
        }
    )

    result = AIExtractor(_settings(), client=client).extract(
        [document],
        [clause],
        [],
    )

    assert len(result.facts) == 1
    assert result.facts[0].predicate == "hasUpperBound"
    assert result.facts[0].extraction_method == "ai:test-model"
    assert result.facts[0].review_status == "pending"
    assert result.reviews[0].fact_id == result.facts[0].fact_id
    assert result.metadata["ai"]["accepted"] == 1
    assert result.metadata["ai"]["rejected"] == 2
    assert result.metadata["ai"]["total_tokens"] == 18
    assert len(result.warnings) == 2
    report = _build_extraction_report(
        "ai",
        result,
        rule_result=None,
        ai_result=result,
    )
    assert report["strategy"] == "single_pass"
    assert report["stages"]["single_pass"]["successful_batches"] == 1
    assert report["stages"]["single_pass"]["candidates"] == 3
    assert report["stages"]["single_pass"]["accepted"] == 1
    assert report["stages"]["single_pass"]["rejected"] == 2


def test_spires_two_stage_grounds_entities_before_relations():
    text = "给水泵包含出口压力，出口压力不应超过1.6MPa。"
    document, clause = _document_and_clause(text)
    client = SequenceAIClient(
        [
            {
                "entities": [
                    {
                        "source_id": clause.clause_id,
                        "name": "给水泵",
                        "entity_type": "Equipment",
                        "evidence_text": text,
                        "confidence": 0.95,
                    },
                    {
                        "source_id": clause.clause_id,
                        "name": "出口压力",
                        "entity_type": "Parameter",
                        "evidence_text": text,
                        "confidence": 0.93,
                    },
                ]
            },
            {
                "facts": [
                    {
                        "source_id": clause.clause_id,
                        "subject": "出口压力",
                        "subject_type": "Parameter",
                        "predicate": "hasUpperBound",
                        "object": "1.6",
                        "object_type": "literal",
                        "unit": "MPa",
                        "evidence_text": text,
                        "confidence": 0.91,
                    }
                ]
            },
        ]
    )

    result = AIExtractor(
        _settings(extraction_strategy="spires"),
        client=client,
    ).extract([document], [clause], [])

    assert len(client.system_prompts) == 2
    assert "SPIRES 两阶段抽取的第一阶段" in client.system_prompts[0]
    assert "SPIRES 两阶段抽取的第二阶段" in client.system_prompts[1]
    assert "运行参数" in client.system_prompts[0]
    assert "LinkML" in client.system_prompts[0]
    assert "单行紧凑 JSON" in client.system_prompts[0]
    assert "entity_name" in client.system_prompts[0]
    assert "禁止输出“设备”“运行参数”等中文类型名称" in client.system_prompts[0]
    assert "单行紧凑 JSON" in client.system_prompts[1]
    assert "relation" in client.system_prompts[1]
    assert "allowed_predicates" in client.system_prompts[1]
    assert "中文谓词标签" in client.system_prompts[1]
    for predicate in ALLOWED_PREDICATES:
        assert predicate in client.system_prompts[1]
    assert "accepted_entities" in client.user_prompts[1]
    assert len(result.facts) == 1
    assert result.facts[0].subject == "出口压力"
    assert result.facts[0].predicate == "hasUpperBound"
    assert result.metadata["ai"]["strategy"] == "spires"
    assert result.metadata["ai"]["entity_candidates"] == 2
    assert result.metadata["ai"]["entity_accepted"] == 2
    assert result.metadata["ai"]["accepted"] == 1
    assert result.metadata["ai"]["total_tokens"] == 30
    assert result.metadata["ai"]["entity_stage_successful_batches"] == 1
    assert result.metadata["ai"]["relation_stage_successful_batches"] == 1
    report = _build_extraction_report(
        "ai",
        result,
        rule_result=None,
        ai_result=result,
    )
    assert report["strategy"] == "spires"
    assert report["stages"]["entities"]["candidates"] == 2
    assert report["stages"]["entities"]["accepted"] == 2
    assert report["stages"]["relations"]["successful_batches"] == 1
    assert report["stages"]["relations"]["candidates"] == 1
    assert report["stages"]["relations"]["accepted"] == 1


def test_spires_normalizes_explicit_equipment_parameter_predicate_alias():
    text = "给水泵具有参数出口压力。"
    document, clause = _document_and_clause(text)
    client = SequenceAIClient(
        [
            {
                "entities": [
                    {
                        "source_id": clause.clause_id,
                        "name": "给水泵",
                        "entity_type": "Equipment",
                        "evidence_text": text,
                        "confidence": 0.95,
                    },
                    {
                        "source_id": clause.clause_id,
                        "name": "出口压力",
                        "entity_type": "Parameter",
                        "evidence_text": text,
                        "confidence": 0.93,
                    },
                ]
            },
            {
                "facts": [
                    {
                        "source_id": clause.clause_id,
                        "subject": "给水泵",
                        "subject_type": "Equipment",
                        "predicate": "具有参数",
                        "object": "出口压力",
                        "object_type": "iri",
                        "object_class": "Parameter",
                        "evidence_text": text,
                        "confidence": 0.9,
                    }
                ]
            },
        ]
    )

    result = AIExtractor(
        _settings(extraction_strategy="spires"),
        client=client,
    ).extract([document], [clause], [])

    assert len(result.facts) == 1
    assert result.facts[0].predicate == "hasParameter"
    assert result.metadata["ai"]["accepted"] == 1


def test_spires_rejects_non_equivalent_chinese_relation_phrase():
    text = "操作人员检查给水泵。"
    document, clause = _document_and_clause(text)
    client = SequenceAIClient(
        [
            {
                "entities": [
                    {
                        "source_id": clause.clause_id,
                        "name": "操作人员",
                        "entity_type": "Role",
                        "evidence_text": text,
                        "confidence": 0.95,
                    },
                    {
                        "source_id": clause.clause_id,
                        "name": "给水泵",
                        "entity_type": "Equipment",
                        "evidence_text": text,
                        "confidence": 0.95,
                    },
                ]
            },
            {
                "facts": [
                    {
                        "source_id": clause.clause_id,
                        "subject": "操作人员",
                        "subject_type": "Role",
                        "predicate": "检查",
                        "object": "给水泵",
                        "object_type": "iri",
                        "object_class": "Equipment",
                        "evidence_text": text,
                        "confidence": 0.9,
                    }
                ]
            },
        ]
    )

    result = AIExtractor(
        _settings(extraction_strategy="spires"),
        client=client,
    ).extract([document], [clause], [])

    assert result.facts == []
    assert result.metadata["ai"]["accepted"] == 0
    assert result.metadata["ai"]["rejected"] == 1
    assert any("非法谓词 检查" in warning for warning in result.warnings)


def test_spires_rejects_relation_subject_missing_from_entity_stage():
    text = "给水泵包含出口压力，出口压力不应超过1.6MPa。"
    document, clause = _document_and_clause(text)
    client = SequenceAIClient(
        [
            {
                "entities": [
                    {
                        "source_id": clause.clause_id,
                        "name": "给水泵",
                        "entity_type": "Equipment",
                        "evidence_text": text,
                        "confidence": 0.95,
                    }
                ]
            },
            {
                "facts": [
                    {
                        "source_id": clause.clause_id,
                        "subject": "出口压力",
                        "subject_type": "Parameter",
                        "predicate": "hasUpperBound",
                        "object": "1.6",
                        "object_type": "literal",
                        "unit": "MPa",
                        "evidence_text": text,
                        "confidence": 0.91,
                    }
                ]
            },
        ]
    )

    result = AIExtractor(
        _settings(extraction_strategy="spires"),
        client=client,
    ).extract([document], [clause], [])

    assert result.facts == []
    assert result.metadata["ai"]["entity_accepted"] == 1
    assert result.metadata["ai"]["accepted"] == 0
    assert result.metadata["ai"]["rejected"] == 1
    assert any("未通过实体对齐" in warning for warning in result.warnings)


def test_parse_ai_response_accepts_fenced_json():
    response = parse_ai_response(
        '```json\n{"facts": [{"source_id": "s1", "subject": "给水泵", '
        '"subject_type": "Equipment", "predicate": "requiresAction", '
        '"object": "检查", "evidence_text": "给水泵必须检查", "confidence": 0.8}]}\n```'
    )

    assert response.facts[0].source_id == "s1"


def test_parse_ai_responses_accept_common_model_field_aliases():
    entity_response = parse_ai_entity_response(
        json.dumps(
            {
                "entities": [
                    {
                        "clause_id": "s1",
                        "entity_name": "给水泵",
                        "type": "Equipment",
                        "evidence": "给水泵运行",
                        "score": 0.92,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    fact_response = parse_ai_response(
        json.dumps(
            {
                "facts": [
                    {
                        "sourceId": "s1",
                        "entity": "给水泵",
                        "entity_type": "Equipment",
                        "relation": "requiresAction",
                        "value": "检查",
                        "object_type": "literal",
                        "quote": "给水泵必须检查",
                        "score": 0.88,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    assert entity_response.entities[0].name == "给水泵"
    assert entity_response.entities[0].confidence == 0.92
    assert fact_response.facts[0].subject == "给水泵"
    assert fact_response.facts[0].predicate == "requiresAction"
    assert fact_response.facts[0].evidence_text == "给水泵必须检查"


def test_parse_ai_response_accepts_spires_head_tail_triples():
    response = parse_ai_response(
        json.dumps(
            {
                "facts": [
                    {
                        "source_id": "s1",
                        "head": "给水泵",
                        "head_type": "设备",
                        "relation": "hasParameter",
                        "tail": "出口压力",
                        "tail_type": "运行参数",
                    },
                    {
                        "source_id": "s1",
                        "head": "出口压力",
                        "head_type": "运行参数",
                        "relation": "hasUpperBound",
                        "tail": "1.6",
                        "tail_type": "literal",
                    },
                ]
            },
            ensure_ascii=False,
        )
    )

    assert response.facts[0].subject == "给水泵"
    assert response.facts[0].subject_type == "设备"
    assert response.facts[0].object == "出口压力"
    assert response.facts[0].object_type == "iri"
    assert response.facts[0].object_class == "Parameter"
    assert response.facts[1].object_type == "literal"


def test_parse_ai_entity_response_salvages_complete_items_from_truncated_json():
    response = parse_ai_entity_response(
        '{"entities":['
        '{"source_id":"s1","entity_name":"给水泵","type":"Equipment",'
        '"evidence":"给水泵运行","score":0.9},'
        '{"source_id":"s2","entity_name":"出口压力","type":"Parameter"'
    )

    assert len(response.entities) == 1
    assert response.entities[0].name == "给水泵"
    assert response.entities[0].entity_type == "Equipment"


def test_spires_backfills_evidence_and_confidence_missing_from_model():
    text = "给水泵包含出口压力，出口压力不应超过1.6MPa。"
    document, clause = _document_and_clause(text)
    client = SequenceAIClient(
        [
            {
                "entities": [
                    {
                        "source_id": clause.clause_id,
                        "entity_name": "出口压力",
                        "type": "运行参数",
                    }
                ]
            },
            {
                "facts": [
                    {
                        "source_id": clause.clause_id,
                        "subject": "出口压力",
                        "subject_type": "运行参数",
                        "predicate": "hasUpperBound",
                        "object": "1.6",
                        "object_type": "字面值",
                        "unit": "MPa",
                    }
                ]
            },
        ]
    )

    result = AIExtractor(
        _settings(extraction_strategy="spires"),
        client=client,
    ).extract([document], [clause], [])

    assert len(result.facts) == 1
    assert result.facts[0].evidence_text == text
    assert result.facts[0].confidence == 0.5
    assert result.metadata["ai"]["entity_accepted"] == 1
    assert result.metadata["ai"]["accepted"] == 1
    assert any("未返回证据" in warning for warning in result.warnings)
    assert any("默认值 0.5" in warning for warning in result.warnings)


def test_parse_ai_response_salvages_complete_items_from_truncated_json():
    response = parse_ai_response(
        '{"facts":['
        '{"source_id":"s1","subject":"给水泵","subject_type":"Equipment",'
        '"predicate":"requiresAction","object":"检查","object_type":"literal",'
        '"evidence_text":"给水泵必须检查","confidence":0.9},'
        '{"source_id":"s2","subject":"出口压力","subject_type":"Parameter"'
    )

    assert len(response.facts) == 1
    assert response.facts[0].subject == "给水泵"
    assert response.facts[0].object == "检查"


def test_spires_limits_model_batches_to_four_sources(monkeypatch):
    document = DocumentRecord(
        document_id="doc_batch",
        file_id="file_batch",
        title="批量测试规程",
        file_name="批量测试规程.txt",
        file_type="unstructured",
    )
    clauses = [
        ClauseRecord(
            clause_id=f"clause_batch_{index}",
            document_id=document.document_id,
            clause_no=f"第{index}条",
            text="给水泵运行。",
        )
        for index in range(5)
    ]
    batch_sizes: list[int] = []

    def fake_extract_batch(self, batch):
        batch_sizes.append(len(batch))
        return [], [], {"successful_batches": 1}

    monkeypatch.setattr(AIExtractor, "_extract_batch", fake_extract_batch)
    AIExtractor(
        _settings(
            extraction_strategy="spires",
            batch_size=8,
            concurrency=1,
        ),
        client=object(),
    ).extract([document], clauses, [])

    assert sorted(batch_sizes) == [1, 4]


def test_parse_ai_response_validation_error_does_not_echo_source_text():
    long_text = "给水泵运行要求。" * 300

    with pytest.raises(AIExtractionError) as error:
        parse_ai_response(
            json.dumps(
                {
                    "facts": [
                        {
                            "source_id": "s1",
                            "text": long_text,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    message = str(error.value)
    assert "字段校验错误" in message
    assert "subject" in message
    assert len(message) < 1000
    assert long_text[:80] not in message


def test_openai_client_retries_server_errors(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, request=request, json={"error": "temporary"})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": '{"facts": []}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    monkeypatch.setattr("kb.ai_extraction.time.sleep", lambda _: None)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(_settings(max_retries=1), client=http_client)

    completion = client.complete(system_prompt="system", user_prompt="user")

    assert attempts == 2
    assert completion.content == '{"facts": []}'
    assert completion.usage["total_tokens"] == 5


def test_hybrid_merge_deduplicates_parameter_bound():
    text = "给水泵出口压力不应超过1.6MPa。"
    document, clause = _document_and_clause(text)
    rule_result = extract_all([document], [clause], [])
    client = StaticAIClient(
        {
            "facts": [
                {
                    "source_id": clause.clause_id,
                    "subject": "出口压力",
                    "subject_type": "Parameter",
                    "predicate": "hasUpperBound",
                    "object": "1.6",
                    "object_type": "literal",
                    "unit": "MPa",
                    "evidence_text": text,
                    "confidence": 0.9,
                }
            ]
        }
    )
    ai_result = AIExtractor(_settings(), client=client).extract(
        [document],
        [clause],
        [],
    )

    merged = merge_extraction_results(rule_result, ai_result)
    upper_bounds = [item for item in merged.facts if item.predicate == "hasUpperBound"]

    assert len(upper_bounds) == 1
    assert merged.metadata["deduplicated_facts"] == 1


def test_ai_modes_cannot_auto_approve(tmp_path):
    with pytest.raises(ValueError, match="必须经过人工审核"):
        run_pipeline(
            [PROJECT_ROOT / "samples"],
            Workspace(tmp_path / "workspace"),
            auto_approve=True,
            extraction_mode="hybrid",
            ai_settings=_settings(),
        )


class DynamicAIClient:
    def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
        payload = json.loads(user_prompt.split("\n", 1)[1])
        source = payload[0]
        return AICompletion(
            content=json.dumps(
                {
                    "facts": [
                        {
                            "source_id": source["source_id"],
                            "subject": "出口阀门",
                            "subject_type": "Component",
                            "predicate": "hasNote",
                            "object": "全开",
                            "object_type": "literal",
                            "evidence_text": source["text"],
                            "confidence": 0.82,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            usage={
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        )


def _wait_for_run(client: TestClient, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}/progress?log_limit=200")
        assert response.status_code == 200
        payload = response.json()
        if payload["summary"]["status"] not in {"queued", "running", "cancelling"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run did not finish before timeout: {run_id}")


def test_web_ai_status_and_hybrid_build(tmp_path):
    app = create_app(
        tmp_path / "workspace",
        ai_settings=_settings(batch_size=1),
        ai_client=DynamicAIClient(),
    )
    with TestClient(app) as client:
        status = client.get("/api/ai/status")
        assert status.status_code == 200
        assert status.json()["ready"] is True
        assert status.json()["model"] == "test-model"

        built = client.post(
            "/api/builds",
            data={
                "ontology_id": "ai_web",
                "version": "0.1.0",
                "base_iri": "https://example.test/ai-web/",
                "extraction_mode": "hybrid",
                "auto_approve": "false",
            },
            files={
                "files": (
                    "regulation.txt",
                    "给水泵启动前，应确认出口阀门全开。".encode(),
                    "text/plain",
                )
            },
        )
        assert built.status_code == 202
        run_id = built.json()["run_id"]
        assert _wait_for_run(client, run_id)["summary"]["status"] == "completed"

        facts = client.get(f"/api/runs/{run_id}/facts")
        assert facts.status_code == 200
        assert any(item["extraction_method"] == "ai:test-model" for item in facts.json()["items"])
        report = client.get(f"/api/runs/{run_id}")
        report_payload = report.json()
        assert report_payload["extraction"]["mode"] == "hybrid"
        assert report_payload["extraction"]["strategy"] == "single_pass"
        assert report_payload["extraction"]["stages"]["single_pass"]["accepted"] >= 1
        assert report_payload["extraction"]["ai"]["accepted"] >= 1
        assert report_payload["summary"]["counts"]["ai_strategy"] == "single_pass"
        assert report_payload["summary"]["counts"]["ai_relation_accepted"] >= 1


def test_web_ai_settings_persist_without_api_key_and_enable_build(tmp_path):
    workspace_root = tmp_path / "workspace"
    app = create_app(
        workspace_root,
        ai_settings=AISettings(),
        ai_client=DynamicAIClient(),
    )
    with TestClient(app) as client:
        saved = client.put("/api/ai/settings", json=_ai_payload(api_key="process-only-secret"))
        assert saved.status_code == 200
        assert saved.json()["ready"] is True
        assert saved.json()["api_key_configured"] is True
        assert "api_key" not in saved.json()

        config_text = (workspace_root / "config" / "ai_settings.json").read_text(encoding="utf-8")
        assert "process-only-secret" not in config_text
        assert "api_key" not in config_text

        built = client.post(
            "/api/builds",
            data={
                "ontology_id": "configured_ai_web",
                "version": "0.1.0",
                "base_iri": "https://example.test/configured-ai-web/",
                "extraction_mode": "hybrid",
                "auto_approve": "false",
            },
            files={
                "files": (
                    "regulation.txt",
                    "给水泵启动前，应确认出口阀门全开。".encode(),
                    "text/plain",
                )
            },
        )
        assert built.status_code == 202
        run_id = built.json()["run_id"]
        assert _wait_for_run(client, run_id)["summary"]["status"] == "completed"

    restarted = create_app(workspace_root, ai_client=DynamicAIClient())
    with TestClient(restarted) as client:
        status = client.get("/api/ai/status")
        assert status.status_code == 200
        assert status.json()["ready"] is True
        assert status.json()["model"] == "test-model"
        assert status.json()["extraction_strategy"] == "single_pass"
        assert status.json()["api_key_configured"] is False


def test_web_ai_settings_blank_key_preserves_and_clear_removes(tmp_path):
    app = create_app(tmp_path / "workspace", ai_settings=_settings(api_key="keep-me"))
    with TestClient(app) as client:
        preserved = client.put("/api/ai/settings", json=_ai_payload(api_key=None))
        assert preserved.status_code == 200
        assert preserved.json()["api_key_configured"] is True
        assert app.state.ai_settings.api_key == "keep-me"

        cleared = client.put(
            "/api/ai/settings",
            json=_ai_payload(api_key=None, clear_api_key=True),
        )
        assert cleared.status_code == 200
        assert cleared.json()["api_key_configured"] is False
        assert app.state.ai_settings.api_key == ""


def test_web_ai_settings_reject_invalid_values(tmp_path):
    app = create_app(tmp_path / "workspace", ai_settings=AISettings())
    with TestClient(app) as client:
        invalid_number = client.put("/api/ai/settings", json=_ai_payload(batch_size=0))
        assert invalid_number.status_code == 422

        incomplete = client.put(
            "/api/ai/settings",
            json=_ai_payload(base_url="", model=""),
        )
        assert incomplete.status_code == 422
        assert "服务地址" in incomplete.text

        invalid_url = client.put(
            "/api/ai/settings",
            json=_ai_payload(base_url="localhost:11434/v1"),
        )
        assert invalid_url.status_code == 422
        assert "HTTP" in invalid_url.text


def test_web_ai_connection_test_uses_form_values_without_saving(tmp_path, monkeypatch):
    captured: list[AISettings] = []

    class ConnectionTestClient:
        def __init__(self, settings):
            captured.append(settings)

        def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
            assert "连接测试" in system_prompt
            assert "OK" in user_prompt
            return AICompletion(
                content="OK",
                usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            )

        def close(self):
            return None

    monkeypatch.setattr(web_module, "OpenAICompatibleClient", ConnectionTestClient)
    app = create_app(
        tmp_path / "workspace",
        ai_settings=_settings(api_key="saved-secret"),
    )
    with TestClient(app) as client:
        result = client.post(
            "/api/ai/test",
            json=_connection_payload(
                base_url="https://form-ai.test/v1/",
                model="form-model",
                api_key="form-secret",
                timeout_seconds=7,
            ),
        )
        assert result.status_code == 200
        assert result.json()["ok"] is True
        assert result.json()["model"] == "form-model"
        assert result.json()["response"] == "OK"
        assert result.json()["api_key_configured"] is True
        assert captured[-1].base_url == "https://form-ai.test/v1"
        assert captured[-1].model == "form-model"
        assert captured[-1].api_key == "form-secret"
        assert captured[-1].timeout_seconds == 7
        assert app.state.ai_settings.model == "test-model"
        assert app.state.ai_settings.api_key == "saved-secret"

        preserved_key = client.post(
            "/api/ai/test",
            json=_connection_payload(api_key=None),
        )
        assert preserved_key.status_code == 200
        assert captured[-1].api_key == "saved-secret"

        cleared_key = client.post(
            "/api/ai/test",
            json=_connection_payload(api_key=None, clear_api_key=True),
        )
        assert cleared_key.status_code == 200
        assert captured[-1].api_key == ""
        assert "saved-secret" not in cleared_key.text


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            httpx.HTTPStatusError(
                "unauthorized",
                request=httpx.Request("POST", "https://ai.test/v1/chat/completions"),
                response=httpx.Response(401),
            ),
            "鉴权失败",
        ),
        (httpx.ConnectError("connection refused"), "无法连接"),
        (httpx.TimeoutException("timed out"), "连接超时"),
    ],
)
def test_web_ai_connection_test_returns_readable_failures(
    tmp_path,
    monkeypatch,
    failure,
    expected,
):
    class FailingConnectionTestClient:
        def __init__(self, settings):
            self.settings = settings

        def complete(self, *, system_prompt: str, user_prompt: str) -> AICompletion:
            raise AIExtractionError("AI 请求失败") from failure

        def close(self):
            return None

    monkeypatch.setattr(web_module, "OpenAICompatibleClient", FailingConnectionTestClient)
    app = create_app(tmp_path / "workspace", ai_settings=AISettings())
    with TestClient(app) as client:
        result = client.post(
            "/api/ai/test",
            json=_connection_payload(api_key="never-return-this-secret"),
        )
        assert result.status_code == 200
        assert result.json()["ok"] is False
        assert expected in result.json()["message"]
        assert "never-return-this-secret" not in result.text


def test_web_ai_connection_test_requires_address_and_model(tmp_path):
    app = create_app(tmp_path / "workspace", ai_settings=AISettings())
    with TestClient(app) as client:
        missing_address = client.post(
            "/api/ai/test",
            json=_connection_payload(base_url=""),
        )
        assert missing_address.status_code == 422

        missing_model = client.post(
            "/api/ai/test",
            json=_connection_payload(model=""),
        )
        assert missing_model.status_code == 422
