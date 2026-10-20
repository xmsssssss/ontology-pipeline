from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

INFERENCE_SCHEMA_VERSION = "kb-inference/1.0"
InferenceStatus = Literal["derived", "conflict", "warning"]
InferenceSeverity = Literal["info", "warning", "error"]
ProofKind = Literal["premise", "conclusion"]


class InferenceModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class FactView(InferenceModel):
    provenance_iri: str = ""
    subject: str = ""
    subject_iri: str | None = None
    predicate: str = ""
    predicate_iri: str | None = None
    object: str = ""
    object_iri: str | None = None
    evidence_text: str = ""
    source_clause: str | None = None
    source_clause_text: str = ""
    confidence: float | None = None
    extraction_method: str = ""
    score: int = 0

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | FactView) -> FactView:
        if isinstance(value, cls):
            return value
        return cls.model_validate(value)

    @property
    def premise_key(self) -> str:
        if self.provenance_iri:
            return self.provenance_iri
        return "|".join(
            (
                self.subject_iri or self.subject,
                self.predicate_iri or self.predicate,
                self.object_iri or self.object,
            )
        )


class RuleDefinition(InferenceModel):
    rule_id: str
    version: str = "1.0.0"
    label: str
    description: str
    category: str
    default_severity: InferenceSeverity = "info"


class FactPremise(InferenceModel):
    premise_index: int
    fact_id: str
    provenance_iri: str | None = None
    role: str
    subject: str
    subject_iri: str | None = None
    predicate: str
    predicate_iri: str | None = None
    object: str
    object_iri: str | None = None
    statement: str
    confidence: float | None = None
    source_clause: str | None = None
    evidence_text: str = ""
    score: int = 0


class ProofStep(InferenceModel):
    step: int
    kind: ProofKind
    statement: str
    premise_indexes: list[int] = Field(default_factory=list)
    provenance_iri: str | None = None


class InferenceConclusion(InferenceModel):
    subject: str
    subject_iri: str | None = None
    predicate: str
    predicate_iri: str | None = None
    object: str
    object_iri: str | None = None
    unit: str | None = None
    statement: str


class InferenceResult(InferenceModel):
    schema_version: str = INFERENCE_SCHEMA_VERSION
    inferred_id: str
    rule: str
    rule_id: str
    rule_version: str
    rule_label: str
    category: str
    status: InferenceStatus = "derived"
    severity: InferenceSeverity = "info"
    statement: str
    subject: str
    subject_iri: str | None = None
    parameter: str | None = None
    parameter_iri: str | None = None
    predicate: str
    predicate_iri: str | None = None
    object: str
    object_iri: str | None = None
    unit: str | None = None
    conclusion: InferenceConclusion
    premises: list[FactPremise] = Field(default_factory=list)
    proof_steps: list[ProofStep] = Field(default_factory=list)
    source_clauses: list[str] = Field(default_factory=list)
    evidence_text: str = ""
    confidence: float | None = None
    confidence_basis: str = "minimum_of_premises"
    attributes: dict[str, Any] = Field(default_factory=dict)


class InferenceRuleError(InferenceModel):
    rule: str
    rule_label: str
    error_type: str
    message: str
