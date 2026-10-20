from __future__ import annotations

from dataclasses import dataclass, field

from kb.inference.models import (
    FactView,
    InferenceResult,
    InferenceRuleError,
    RuleDefinition,
)
from kb.inference.registry import RULE_REGISTRY, InferenceRegistry


class InferenceConfigurationError(ValueError):
    pass


@dataclass
class InferenceEvaluation:
    results: list[InferenceResult] = field(default_factory=list)
    executed_rules: list[RuleDefinition] = field(default_factory=list)
    errors: list[InferenceRuleError] = field(default_factory=list)


class InferenceEngine:
    def __init__(self, registry: InferenceRegistry | None = None) -> None:
        self.registry = registry or RULE_REGISTRY

    def rule_catalog(self) -> list[dict[str, object]]:
        return [
            rule.definition.model_dump(mode="json") | {"enabled_by_default": True}
            for rule in self.registry.all()
        ]

    def evaluate(
        self,
        facts: list[dict[str, object] | FactView],
        *,
        enabled_rules: list[str] | None = None,
    ) -> InferenceEvaluation:
        fact_views = [FactView.from_mapping(item) for item in facts]
        try:
            rules = self.registry.select(enabled_rules)
        except ValueError as exc:
            raise InferenceConfigurationError(str(exc)) from exc

        evaluation = InferenceEvaluation()
        deduplicated: dict[str, InferenceResult] = {}
        for rule in rules:
            definition = rule.definition
            evaluation.executed_rules.append(definition)
            try:
                raw_results = rule.evaluate(fact_views)
                results = [
                    item
                    if isinstance(item, InferenceResult)
                    else InferenceResult.model_validate(item)
                    for item in raw_results
                ]
            except Exception as exc:
                evaluation.errors.append(
                    InferenceRuleError(
                        rule=definition.rule_id,
                        rule_label=definition.label,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            for result in results:
                deduplicated.setdefault(result.inferred_id, result)

        severity_rank = {"error": 0, "warning": 1, "info": 2}
        evaluation.results = sorted(
            deduplicated.values(),
            key=lambda item: (
                severity_rank[item.severity],
                -float(item.confidence or 0),
                item.statement.casefold(),
            ),
        )
        return evaluation


DEFAULT_INFERENCE_ENGINE = InferenceEngine()
