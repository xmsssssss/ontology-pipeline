from __future__ import annotations

from kb.inference.models import FactView, InferenceResult, RuleDefinition
from kb.inference.rules.common import (
    best_fact,
    build_result,
    facts_by_subject,
    parameter_display_name,
    parameter_links,
    value_with_unit,
)


class ParameterConditionRule:
    definition = RuleDefinition(
        rule_id="parameter_conditions",
        version="1.0.0",
        label="设备参数条件组合",
        description="把参数条件运算符、阈值和单位组合成设备运行条件。",
        category="parameter",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        grouped_facts = facts_by_subject(facts)
        results: list[InferenceResult] = []
        for parameter_iri, links in parameter_links(facts).items():
            parameter_facts = grouped_facts.get(parameter_iri, [])
            operator = best_fact(parameter_facts, "hasConditionOperator")
            threshold = best_fact(parameter_facts, "hasConditionThreshold")
            unit = best_fact(parameter_facts, "hasConditionUnit")
            if operator is None and threshold is None:
                continue
            for link in links:
                equipment = link.subject
                parameter = link.object
                parameter_name = parameter_display_name(parameter, equipment)
                threshold_text = (
                    value_with_unit(threshold.object, unit.object if unit else "")
                    if threshold
                    else ""
                )
                object_text = " ".join(
                    item for item in (operator.object if operator else "", threshold_text) if item
                )
                statement = f"{equipment}的{parameter_name}运行条件为 {object_text}"
                results.append(
                    build_result(
                        definition=self.definition,
                        statement=statement,
                        subject=equipment,
                        subject_iri=link.subject_iri,
                        parameter=parameter,
                        parameter_iri=parameter_iri,
                        predicate="参数条件",
                        predicate_iri=threshold.predicate_iri if threshold else None,
                        object_text=object_text,
                        unit=unit.object if unit else None,
                        premise_facts=[
                            item for item in (link, operator, threshold, unit) if item is not None
                        ],
                        attributes={
                            "operator": operator.object if operator else None,
                            "threshold": threshold.object if threshold else None,
                        },
                        result_rule="parameter_condition",
                    )
                )
        return results
