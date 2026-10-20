from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlsplit

from kb.inference.models import (
    FactPremise,
    FactView,
    InferenceConclusion,
    InferenceResult,
    InferenceSeverity,
    InferenceStatus,
    ProofStep,
    RuleDefinition,
)

_WHITESPACE = re.compile(r"\s+")


def local_name(iri: str) -> str:
    if not iri:
        return ""
    parsed = urlsplit(iri)
    path = parsed.path if parsed.scheme else iri
    return unquote(path.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1])


def predicate_name(fact: FactView) -> str:
    return local_name(str(fact.predicate_iri or ""))


def fact_statement(fact: FactView) -> str:
    return f"{fact.subject} --{fact.predicate}--> {fact.object}"


def best_fact(facts: Iterable[FactView], predicate: str) -> FactView | None:
    matches = [item for item in facts if predicate_name(item) == predicate]
    if not matches:
        return None
    return max(matches, key=lambda item: (float(item.confidence or 0), item.score))


def facts_by_subject(facts: Iterable[FactView]) -> dict[str, list[FactView]]:
    grouped: dict[str, list[FactView]] = {}
    for fact in facts:
        if fact.subject_iri:
            grouped.setdefault(fact.subject_iri, []).append(fact)
    return grouped


def parameter_links(facts: Iterable[FactView]) -> dict[str, list[FactView]]:
    grouped: dict[str, list[FactView]] = {}
    for fact in facts:
        if predicate_name(fact) == "hasParameter" and fact.object_iri:
            grouped.setdefault(fact.object_iri, []).append(fact)
    return grouped


def value_with_unit(value: str, unit: str) -> str:
    value = str(value).strip()
    unit = str(unit).strip()
    if not unit or value.casefold().endswith(unit.casefold()):
        return value
    separator = "" if unit in {"%", "％", "℃", "°C"} else " "
    return f"{value}{separator}{unit}"


def parameter_display_name(parameter: str, equipment: str) -> str:
    for opening, closing in (("（", "）"), ("(", ")")):
        suffix = f"{opening}{equipment}{closing}"
        if parameter.endswith(suffix):
            return parameter[: -len(suffix)].strip()
    return parameter


def decimal_value(value: str) -> Decimal | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value or ""))
    if match is None:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def excerpt(value: str | None, limit: int) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def deduplicate_facts(facts: Iterable[FactView]) -> list[FactView]:
    result: list[FactView] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.premise_key
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(fact)
    return result


def build_premises(facts: Iterable[FactView]) -> list[FactPremise]:
    premises: list[FactPremise] = []
    for index, fact in enumerate(deduplicate_facts(facts), start=1):
        premises.append(
            FactPremise(
                premise_index=index,
                fact_id=fact.premise_key,
                provenance_iri=fact.provenance_iri or None,
                role=predicate_name(fact),
                subject=fact.subject,
                subject_iri=fact.subject_iri,
                predicate=fact.predicate,
                predicate_iri=fact.predicate_iri,
                object=fact.object,
                object_iri=fact.object_iri,
                statement=fact_statement(fact),
                confidence=fact.confidence,
                source_clause=fact.source_clause,
                evidence_text=excerpt(fact.evidence_text, 600),
                score=fact.score,
            )
        )
    return premises


def build_proof_steps(statement: str, premises: list[FactPremise]) -> list[ProofStep]:
    steps = [
        ProofStep(
            step=premise.premise_index,
            kind="premise",
            statement=premise.statement,
            premise_indexes=[premise.premise_index],
            provenance_iri=premise.provenance_iri,
        )
        for premise in premises
    ]
    steps.append(
        ProofStep(
            step=len(steps) + 1,
            kind="conclusion",
            statement=statement,
            premise_indexes=[premise.premise_index for premise in premises],
        )
    )
    return steps


def build_result(
    *,
    definition: RuleDefinition,
    statement: str,
    subject: str,
    subject_iri: str | None,
    predicate: str,
    predicate_iri: str | None,
    object_text: str,
    object_iri: str | None = None,
    unit: str | None = None,
    premise_facts: Iterable[FactView],
    parameter: str | None = None,
    parameter_iri: str | None = None,
    status: InferenceStatus = "derived",
    severity: InferenceSeverity | None = None,
    confidence_basis: str = "minimum_of_premises",
    attributes: dict[str, Any] | None = None,
    identity_suffix: str = "",
    result_rule: str | None = None,
) -> InferenceResult:
    premises = build_premises(premise_facts)
    confidence_values = [
        premise.confidence for premise in premises if premise.confidence is not None
    ]
    evidence_texts: list[str] = []
    for premise in premises:
        if premise.evidence_text and premise.evidence_text not in evidence_texts:
            evidence_texts.append(premise.evidence_text)
    source_clauses = sorted(
        {premise.source_clause for premise in premises if premise.source_clause}
    )
    resolved_rule = result_rule or definition.rule_id
    identity = "|".join(
        (
            resolved_rule,
            statement,
            identity_suffix,
            *[premise.fact_id for premise in premises],
        )
    )
    inferred_id = "inf-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    conclusion = InferenceConclusion(
        subject=subject,
        subject_iri=subject_iri,
        predicate=predicate,
        predicate_iri=predicate_iri,
        object=object_text,
        object_iri=object_iri,
        unit=unit,
        statement=statement,
    )
    return InferenceResult(
        inferred_id=inferred_id,
        rule=resolved_rule,
        rule_id=definition.rule_id,
        rule_version=definition.version,
        rule_label=definition.label,
        category=definition.category,
        status=status,
        severity=severity or definition.default_severity,
        statement=statement,
        subject=subject,
        subject_iri=subject_iri,
        parameter=parameter,
        parameter_iri=parameter_iri,
        predicate=predicate,
        predicate_iri=predicate_iri,
        object=object_text,
        object_iri=object_iri,
        unit=unit,
        conclusion=conclusion,
        premises=premises,
        proof_steps=build_proof_steps(statement, premises),
        source_clauses=source_clauses,
        evidence_text=" ".join(evidence_texts),
        confidence=min(confidence_values) if confidence_values else None,
        confidence_basis=confidence_basis,
        attributes=attributes or {},
    )
