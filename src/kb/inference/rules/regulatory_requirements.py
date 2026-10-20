from __future__ import annotations

from kb.inference.models import FactView, InferenceResult, RuleDefinition
from kb.inference.rules.common import best_fact, build_result, predicate_name

_ACTION_RULES = {
    "requiresAction": ("必须执行", "regulatory_requirement"),
    "prohibitsAction": ("禁止执行", "regulatory_prohibition"),
    "permitsAction": ("允许执行", "regulatory_permission"),
}


class RegulatoryRequirementRule:
    definition = RuleDefinition(
        rule_id="regulatory_requirements",
        version="1.0.0",
        label="规章动作与责任组合",
        description="把同一主体、同一来源条款下的规范动作和责任角色组合成可执行要求。",
        category="regulatory",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        results: list[InferenceResult] = []
        for action in facts:
            action_name = predicate_name(action)
            action_config = _ACTION_RULES.get(action_name)
            if action_config is None:
                continue
            action_label, conclusion_type = action_config
            related = [
                item
                for item in facts
                if item.subject_iri
                and item.subject_iri == action.subject_iri
                and (
                    (
                        action.source_clause is not None
                        and item.source_clause == action.source_clause
                    )
                    or (
                        action.source_clause is None
                        and action.evidence_text
                        and item.evidence_text == action.evidence_text
                    )
                )
            ]
            role = best_fact(
                [item for item in related if predicate_name(item) == "hasResponsibleRole"],
                "hasResponsibleRole",
            )
            role_text = f"由{role.object}" if role else ""
            statement = f"{action.subject}{role_text}{action_label}：{action.object}"
            results.append(
                build_result(
                    definition=self.definition,
                    statement=statement,
                    subject=action.subject,
                    subject_iri=action.subject_iri,
                    predicate="规章要求",
                    predicate_iri=action.predicate_iri,
                    object_text=action.object,
                    premise_facts=[action, *([role] if role else [])],
                    attributes={
                        "requirement_type": conclusion_type,
                        "action": action.object,
                        "responsible_role": role.object if role else None,
                    },
                    result_rule=conclusion_type,
                )
            )
        return results
