from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from kb.inference.models import FactView, InferenceResult, RuleDefinition


class InferenceRule(Protocol):
    @property
    def definition(self) -> RuleDefinition: ...

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]: ...


class InferenceRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, InferenceRule] = {}

    def register(self, rule: InferenceRule, *, replace: bool = False) -> None:
        rule_id = rule.definition.rule_id
        if not rule_id:
            raise ValueError("推理规则必须声明 rule_id。")
        if rule_id in self._rules and not replace:
            raise ValueError(f"推理规则已注册: {rule_id}")
        self._rules[rule_id] = rule

    def unregister(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)

    def get(self, rule_id: str) -> InferenceRule | None:
        return self._rules.get(rule_id)

    def all(self) -> list[InferenceRule]:
        return [self._rules[rule_id] for rule_id in sorted(self._rules)]

    def select(self, rule_ids: Iterable[str] | None = None) -> list[InferenceRule]:
        if rule_ids is None:
            return self.all()
        selected: list[InferenceRule] = []
        seen: set[str] = set()
        for rule_id in rule_ids:
            normalized = rule_id.strip()
            if not normalized or normalized in seen:
                continue
            rule = self._rules.get(normalized)
            if rule is None:
                raise ValueError(f"未知推理规则: {normalized}")
            seen.add(normalized)
            selected.append(rule)
        return selected


RULE_REGISTRY = InferenceRegistry()


def register_builtin_rules() -> None:
    from kb.inference.rules.parameter_conditions import ParameterConditionRule
    from kb.inference.rules.parameter_consistency import ParameterConsistencyRule
    from kb.inference.rules.parameter_constraints import ParameterConstraintRule
    from kb.inference.rules.regulatory_requirements import RegulatoryRequirementRule

    builtins: list[InferenceRule] = [
        ParameterConstraintRule(),
        ParameterConditionRule(),
        ParameterConsistencyRule(),
        RegulatoryRequirementRule(),
    ]
    for rule in builtins:
        if RULE_REGISTRY.get(rule.definition.rule_id) is None:
            RULE_REGISTRY.register(rule)


register_builtin_rules()
