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


class ParameterConstraintRule:
    definition = RuleDefinition(
        rule_id="parameter_constraints",
        version="1.1.0",
        label="设备参数边界组合",
        description="把设备的参数关联、参数上下限和单位组合成可直接使用的参数约束。",
        category="parameter",
    )

    def evaluate(self, facts: list[FactView]) -> list[InferenceResult]:
        grouped_facts = facts_by_subject(facts)
        results: list[InferenceResult] = []
        for parameter_iri, links in parameter_links(facts).items():
            parameter_facts = grouped_facts.get(parameter_iri, [])
            lower = best_fact(parameter_facts, "hasLowerBound")
            upper = best_fact(parameter_facts, "hasUpperBound")
            unit = best_fact(parameter_facts, "hasUnit")
            if lower is None and upper is None:
                continue
            for link in links:
                equipment = link.subject
                parameter = link.object
                parameter_name = parameter_display_name(parameter, equipment)
                unit_text = unit.object if unit else ""
                lower_text = value_with_unit(lower.object, unit_text) if lower else ""
                upper_text = value_with_unit(upper.object, unit_text) if upper else ""
                if lower is not None and upper is not None:
                    object_text = f"{lower_text} - {upper_text}"
                    statement = f"{equipment}的{parameter_name}范围为 {object_text}"
                    predicate = "参数范围"
                    predicate_iri = lower.predicate_iri
                    result_rule = "parameter_range"
                elif lower is not None:
                    object_text = lower_text
                    statement = f"{equipment}的{parameter_name}下限为 {object_text}"
                    predicate = "参数下限"
                    predicate_iri = lower.predicate_iri
                    result_rule = "parameter_bound"
                else:
                    object_text = upper_text
                    statement = f"{equipment}的{parameter_name}上限为 {object_text}"
                    predicate = "参数上限"
                    predicate_iri = upper.predicate_iri if upper else None
                    result_rule = "parameter_bound"
                premise_facts = [link, lower, upper, unit]
                results.append(
                    build_result(
                        definition=self.definition,
                        statement=statement,
                        subject=equipment,
                        subject_iri=link.subject_iri,
                        parameter=parameter,
                        parameter_iri=parameter_iri,
                        predicate=predicate,
                        predicate_iri=predicate_iri,
                        object_text=object_text,
                        unit=unit_text or None,
                        premise_facts=[item for item in premise_facts if item is not None],
                        attributes={
                            "bound_type": "range"
                            if lower is not None and upper is not None
                            else ("lower" if lower is not None else "upper"),
                            "lower": lower.object if lower else None,
                            "upper": upper.object if upper else None,
                        },
                        result_rule=result_rule,
                    )
                )
        return results
