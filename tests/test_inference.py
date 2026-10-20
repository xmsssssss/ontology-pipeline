from __future__ import annotations

from typing import Any

import pytest

from kb.inference import (
    DEFAULT_INFERENCE_ENGINE,
    FactView,
    InferenceConfigurationError,
    InferenceEngine,
    InferenceRegistry,
    InferenceResult,
    RuleDefinition,
)

BASE = "https://example.test/inference/"


def _fact(
    provenance: str,
    subject: str,
    subject_iri: str,
    predicate: str,
    object_value: str,
    *,
    object_iri: str | None = None,
    confidence: float = 0.9,
    clause: str = "clause-1",
    evidence: str = "测试证据",
) -> dict[str, Any]:
    return {
        "provenance_iri": f"{BASE}provenance/{provenance}",
        "subject": subject,
        "subject_iri": subject_iri,
        "predicate": predicate,
        "predicate_iri": f"{BASE}property/{predicate}",
        "object": object_value,
        "object_iri": object_iri,
        "confidence": confidence,
        "source_clause": f"{BASE}clause/{clause}",
        "evidence_text": evidence,
        "score": 100,
    }


def test_builtin_rule_catalog_and_parameter_results_are_structured() -> None:
    catalog = DEFAULT_INFERENCE_ENGINE.rule_catalog()
    assert {item["rule_id"] for item in catalog} == {
        "parameter_conditions",
        "parameter_consistency",
        "parameter_constraints",
        "regulatory_requirements",
    }

    pump_iri = f"{BASE}entity/pump"
    pressure_iri = f"{BASE}entity/pressure"
    facts = [
        _fact(
            "link",
            "给水泵",
            pump_iri,
            "hasParameter",
            "出口压力",
            object_iri=pressure_iri,
            confidence=0.95,
        ),
        _fact(
            "lower",
            "出口压力",
            pressure_iri,
            "hasLowerBound",
            "1.2",
            confidence=0.9,
        ),
        _fact(
            "upper",
            "出口压力",
            pressure_iri,
            "hasUpperBound",
            "1.0",
            confidence=0.8,
        ),
        _fact(
            "unit",
            "出口压力",
            pressure_iri,
            "hasUnit",
            "MPa",
            confidence=0.7,
        ),
        _fact(
            "operator",
            "出口压力",
            pressure_iri,
            "hasConditionOperator",
            ">=",
            confidence=0.85,
        ),
        _fact(
            "threshold",
            "出口压力",
            pressure_iri,
            "hasConditionThreshold",
            "1.1",
            confidence=0.8,
        ),
        _fact(
            "condition-unit",
            "出口压力",
            pressure_iri,
            "hasConditionUnit",
            "MPa",
            confidence=0.8,
        ),
    ]

    evaluation = DEFAULT_INFERENCE_ENGINE.evaluate(facts)
    assert evaluation.errors == []
    by_rule = {item.rule: item for item in evaluation.results}
    assert set(by_rule) == {
        "parameter_bound_conflict",
        "parameter_condition",
        "parameter_range",
    }

    conflict = by_rule["parameter_bound_conflict"]
    assert conflict.rule_id == "parameter_consistency"
    assert conflict.status == "conflict"
    assert conflict.severity == "error"
    assert "下限 1.2 MPa 大于上限 1.0 MPa" in conflict.statement

    result = by_rule["parameter_range"]
    assert result.schema_version == "kb-inference/1.0"
    assert result.rule_id == "parameter_constraints"
    assert result.conclusion.predicate == "参数范围"
    assert result.statement == "给水泵的出口压力范围为 1.2 MPa - 1.0 MPa"
    assert result.confidence == 0.7
    assert result.confidence_basis == "minimum_of_premises"
    assert [premise.premise_index for premise in result.premises] == [1, 2, 3, 4]
    assert all(premise.fact_id for premise in result.premises)
    assert [step.kind for step in result.proof_steps] == [
        "premise",
        "premise",
        "premise",
        "premise",
        "conclusion",
    ]
    assert result.proof_steps[-1].premise_indexes == [1, 2, 3, 4]
    assert result.source_clauses == [f"{BASE}clause/clause-1"]

    condition = by_rule["parameter_condition"]
    assert condition.statement == "给水泵的出口压力运行条件为 >= 1.1 MPa"
    assert condition.attributes["operator"] == ">="
    assert condition.attributes["threshold"] == "1.1"


def test_regulatory_rule_combines_action_and_role_from_same_clause() -> None:
    pump_iri = f"{BASE}entity/pump"
    facts = [
        _fact(
            "action",
            "给水泵",
            pump_iri,
            "requiresAction",
            "启动前检查密封",
            confidence=0.9,
            clause="clause-1",
        ),
        _fact(
            "role",
            "给水泵",
            pump_iri,
            "hasResponsibleRole",
            "操作员",
            confidence=0.85,
            clause="clause-1",
        ),
        _fact(
            "other-role",
            "给水泵",
            pump_iri,
            "hasResponsibleRole",
            "检修员",
            confidence=1.0,
            clause="clause-2",
        ),
    ]

    evaluation = DEFAULT_INFERENCE_ENGINE.evaluate(
        facts,
        enabled_rules=["regulatory_requirements"],
    )

    assert len(evaluation.results) == 1
    result = evaluation.results[0]
    assert result.rule == "regulatory_requirement"
    assert result.statement == "给水泵由操作员必须执行：启动前检查密封"
    assert result.confidence == 0.85
    assert result.attributes["responsible_role"] == "操作员"
    assert {premise.role for premise in result.premises} == {
        "requiresAction",
        "hasResponsibleRole",
    }


class _AlwaysRule:
    definition = RuleDefinition(
        rule_id="always",
        label="测试规则",
        description="用于验证自定义规则注册。",
        category="test",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        return []


class _BrokenRule:
    definition = RuleDefinition(
        rule_id="broken",
        label="故障测试规则",
        description="用于验证单条规则失败不会中断其他规则。",
        category="test",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        raise RuntimeError("rule failed")


def test_registry_supports_custom_rules_and_rejects_unknown_rule() -> None:
    registry = InferenceRegistry()
    registry.register(_AlwaysRule())
    registry.register(_BrokenRule())

    assert [item.definition.rule_id for item in registry.all()] == ["always", "broken"]
    engine = InferenceEngine(registry)
    assert engine.evaluate([], enabled_rules=["always"]).results == []
    broken = engine.evaluate([], enabled_rules=["broken"])
    assert broken.results == []
    assert broken.errors[0].error_type == "RuntimeError"
    assert broken.errors[0].message == "rule failed"

    with pytest.raises(InferenceConfigurationError, match="未知推理规则"):
        engine.evaluate([], enabled_rules=["missing"])
