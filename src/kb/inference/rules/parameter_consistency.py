from __future__ import annotations

from kb.inference.models import FactView, InferenceResult, RuleDefinition
from kb.inference.rules.common import (
    best_fact,
    build_result,
    decimal_value,
    facts_by_subject,
    parameter_display_name,
    parameter_links,
    predicate_name,
    value_with_unit,
)


class ParameterConsistencyRule:
    definition = RuleDefinition(
        rule_id="parameter_consistency",
        version="1.0.0",
        label="设备参数一致性检查",
        description="检查同一参数是否存在下限大于上限或单位不一致等数据冲突。",
        category="data_quality",
        default_severity="warning",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        grouped_facts = facts_by_subject(facts)
        results: list[InferenceResult] = []
        for parameter_iri, links in parameter_links(facts).items():
            parameter_facts = grouped_facts.get(parameter_iri, [])
            lower = best_fact(parameter_facts, "hasLowerBound")
            upper = best_fact(parameter_facts, "hasUpperBound")
            units = {
                item.object.strip()
                for item in parameter_facts
                if predicate_name(item) == "hasUnit" and item.object
            }
            for link in links:
                equipment = link.subject
                parameter = link.object
                parameter_name = parameter_display_name(parameter, equipment)
                if lower is not None and upper is not None:
                    lower_value = decimal_value(lower.object)
                    upper_value = decimal_value(upper.object)
                    if (
                        lower_value is not None
                        and upper_value is not None
                        and lower_value > upper_value
                    ):
                        unit = best_fact(parameter_facts, "hasUnit")
                        unit_text = unit.object if unit else ""
                        lower_text = value_with_unit(lower.object, unit_text)
                        upper_text = value_with_unit(upper.object, unit_text)
                        statement = (
                            f"{equipment}的{parameter_name}上下限冲突："
                            f"下限 {lower_text} 大于上限 {upper_text}"
                        )
                        results.append(
                            build_result(
                                definition=self.definition,
                                statement=statement,
                                subject=equipment,
                                subject_iri=link.subject_iri,
                                parameter=parameter,
                                parameter_iri=parameter_iri,
                                predicate="参数冲突",
                                predicate_iri=lower.predicate_iri,
                                object_text=f"{lower_text} > {upper_text}",
                                unit=unit_text or None,
                                premise_facts=[
                                    item for item in (link, lower, upper, unit) if item is not None
                                ],
                                status="conflict",
                                severity="error",
                                attributes={
                                    "conflict_type": "lower_bound_exceeds_upper_bound",
                                    "lower_value": str(lower_value),
                                    "upper_value": str(upper_value),
                                },
                                result_rule="parameter_bound_conflict",
                            )
                        )
                if len(units) > 1:
                    unit_facts = [
                        item
                        for item in parameter_facts
                        if predicate_name(item) == "hasUnit" and item.object.strip() in units
                    ]
                    statement = (
                        f"{equipment}的{parameter_name}存在多个单位：{', '.join(sorted(units))}"
                    )
                    results.append(
                        build_result(
                            definition=self.definition,
                            statement=statement,
                            subject=equipment,
                            subject_iri=link.subject_iri,
                            parameter=parameter,
                            parameter_iri=parameter_iri,
                            predicate="单位冲突",
                            predicate_iri=None,
                            object_text=", ".join(sorted(units)),
                            premise_facts=[link, *unit_facts],
                            status="conflict",
                            severity="error",
                            attributes={
                                "conflict_type": "multiple_units",
                                "units": sorted(units),
                            },
                            result_rule="parameter_unit_conflict",
                        )
                    )
        return results
