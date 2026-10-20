from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from math import ceil
from typing import Any
from urllib.parse import unquote, urlsplit

from kb.graphdb import GraphDBClient
from kb.inference import DEFAULT_INFERENCE_ENGINE, INFERENCE_SCHEMA_VERSION

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_SUBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject"
RDF_PREDICATE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate"
RDF_OBJECT = "http://www.w3.org/1999/02/22-rdf-syntax-ns#object"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
OWL_NAMED_INDIVIDUAL = "http://www.w3.org/2002/07/owl#NamedIndividual"
INFERENCE_RULESET = "kb-explainable/1.0"

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+%-]{1,}")
_WHITESPACE = re.compile(r"\s+")
_INTERROGATIVE_MARKERS = (
    "有哪些",
    "什么",
    "哪些",
    "哪个",
    "哪",
    "怎么",
    "如何",
    "是否",
    "为何",
    "多少",
    "请问",
)
_STOP_TERMS = {
    "有关",
    "关于",
    "需要",
    "可以",
    "能否",
    "告诉",
    "一下",
    "相关",
    "规定",
    "说明",
}
_TRAILING_FUNCTION_CHARS = set("的了和与及或对把被将就都也还又很更最有是在为吗呢呀吧要")
_LEADING_FUNCTION_CHARS = set("的了和与及或对把被将就都也还又很更最些该")


def _strip_question_markers(question: str) -> str:
    normalized = _WHITESPACE.sub(" ", question).strip()
    for marker in sorted(_INTERROGATIVE_MARKERS, key=len, reverse=True):
        normalized = normalized.replace(marker, " ")
    return _WHITESPACE.sub(" ", normalized).strip()


def _trim_function_chars(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and value[start] in _LEADING_FUNCTION_CHARS:
        start += 1
    while end > start and value[end - 1] in _TRAILING_FUNCTION_CHARS:
        end -= 1
    return value[start:end]


def _question_anchor(question: str) -> str:
    normalized = _strip_question_markers(question)
    runs = [
        trimmed for run in _CJK_RUN.findall(normalized) if (trimmed := _trim_function_chars(run))
    ]
    if not runs:
        latin = _LATIN_RUN.findall(normalized)
        return max(latin, key=len, default="")
    return max(runs, key=len)


def _sparql_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\x00", "")
    )
    return f'"{escaped}"'


def _question_terms(question: str, *, limit: int = 18) -> list[str]:
    normalized = _strip_question_markers(question)
    candidates: list[str] = []
    for run in _LATIN_RUN.findall(normalized):
        candidates.append(run.casefold())
    for run in _CJK_RUN.findall(normalized):
        run = _trim_function_chars(run)
        if not run:
            continue
        if len(run) == 1:
            candidates.append(run)
            continue
        if len(run) <= 16:
            candidates.append(run)
        for size in range(min(4, len(run)), 1, -1):
            candidates.extend(run[index : index + size] for index in range(len(run) - size + 1))

    filtered: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        if not term or term in seen:
            continue
        if term in _STOP_TERMS:
            continue
        if any(marker in term for marker in _INTERROGATIVE_MARKERS):
            continue
        if term[0] in _LEADING_FUNCTION_CHARS or term[-1] in _TRAILING_FUNCTION_CHARS:
            continue
        seen.add(term)
        filtered.append(term)
    if not filtered and normalized:
        filtered.append(normalized)
    return filtered[:limit]


def _contains_any(fields: Iterable[str], terms: list[str]) -> str:
    comparisons = [
        f"CONTAINS(LCASE({field}), LCASE({_sparql_string(term)}))"
        for field in fields
        for term in terms
    ]
    return " || ".join(comparisons) if comparisons else "false"


def _rows(result: dict[str, Any]) -> list[dict[str, str]]:
    if result.get("type") != "SELECT":
        return []
    values = result.get("rows", [])
    return [item for item in values if isinstance(item, dict)]


def _score(texts: Iterable[str | None], terms: list[str], question: str) -> int:
    haystack = " ".join(str(value or "") for value in texts).casefold()
    score = sum(len(term) * len(term) * haystack.count(term.casefold()) for term in terms)
    anchor = _question_anchor(question).casefold()
    if 2 <= len(anchor) <= 20 and anchor in haystack:
        score += 1200 + len(anchor) * 30
    return score


def _filter_by_relevance(
    items: list[dict[str, Any]],
    *,
    top_ratio: float,
    minimum_score: int,
) -> list[dict[str, Any]]:
    if not items:
        return []
    top_score = max(int(item.get("score") or 0) for item in items)
    threshold = max(minimum_score, ceil(top_score * top_ratio))
    relevant = [item for item in items if int(item.get("score") or 0) >= threshold]
    return relevant or items[:1]


def _local_name(iri: str) -> str:
    if not iri:
        return ""
    parsed = urlsplit(iri)
    path = parsed.path if parsed.scheme else iri
    return unquote(path.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1])


def _excerpt(value: str | None, limit: int) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _query_entities(
    graphdb: GraphDBClient,
    *,
    graph_iri: str,
    base_iri: str,
    terms: list[str],
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    base = base_iri.rstrip("/")
    search = _contains_any(
        (
            'COALESCE(STR(?label), "")',
            "STR(?entity)",
        ),
        terms,
    )
    query = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?entity ?label ?class ?class_label WHERE {{
  ?entity a owl:NamedIndividual .
  OPTIONAL {{ ?entity rdfs:label ?label . }}
  OPTIONAL {{
    ?entity a ?class .
    FILTER(?class != owl:NamedIndividual)
  }}
  OPTIONAL {{ ?class rdfs:label ?class_label . }}
  FILTER NOT EXISTS {{
    VALUES ?structural_class {{
      <{base}/class/Clause>
      <{base}/class/Document>
      <{base}/class/Provenance>
    }}
    ?entity a ?structural_class .
  }}
  FILTER({search})
}}
LIMIT {limit}
"""
    values: dict[str, dict[str, Any]] = {}
    normalized_terms = {term.casefold() for term in terms}
    for row in _rows(graphdb.query(query, graph_iri=graph_iri)):
        iri = row.get("entity", "")
        if not iri:
            continue
        label = row.get("label") or _local_name(iri)
        class_iri = row.get("class", "")
        class_label = row.get("class_label") or _local_name(class_iri)
        current = values.get(iri)
        candidate = {
            "iri": iri,
            "label": label,
            "class_iri": class_iri or None,
            "class_label": class_label or None,
            "score": _score((label, iri, class_label), terms, question)
            + (500 if str(label).casefold() in normalized_terms else 0),
        }
        if current is None or candidate["score"] > current["score"]:
            values[iri] = candidate
    return sorted(
        values.values(),
        key=lambda item: (-int(item["score"]), str(item["label"]).casefold()),
    )


def _query_facts(
    graphdb: GraphDBClient,
    *,
    graph_iri: str,
    base_iri: str,
    terms: list[str],
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    base = base_iri.rstrip("/")
    search = _contains_any(
        (
            'COALESCE(STR(?subject_label), "")',
            "STR(?subject)",
            'COALESCE(STR(?object_label), "")',
            "STR(?object)",
            'COALESCE(STR(?predicate_label), "")',
            'COALESCE(STR(?evidence), "")',
            'COALESCE(STR(?clause_text), "")',
        ),
        terms,
    )
    query = f"""
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT
  ?provenance ?subject ?subject_label ?predicate ?predicate_label
  ?object ?object_label ?evidence ?clause ?clause_text ?confidence ?method
WHERE {{
  ?provenance a <{base}/class/Provenance> ;
    rdf:subject ?subject ;
    rdf:predicate ?predicate ;
    rdf:object ?object .
  OPTIONAL {{ ?subject rdfs:label ?subject_label . }}
  OPTIONAL {{ ?predicate rdfs:label ?predicate_label . }}
  OPTIONAL {{ ?object rdfs:label ?object_label . }}
  OPTIONAL {{ ?provenance <{base}/property/evidenceText> ?evidence . }}
  OPTIONAL {{ ?provenance <{base}/property/sourceClause> ?clause . }}
  OPTIONAL {{ ?clause <{base}/property/hasText> ?clause_text . }}
  OPTIONAL {{ ?provenance <{base}/property/confidence> ?confidence . }}
  OPTIONAL {{ ?provenance <{base}/property/extractionMethod> ?method . }}
  FILTER(?predicate != rdf:type)
  FILTER({search})
}}
LIMIT {limit}
"""
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _rows(graphdb.query(query, graph_iri=graph_iri)):
        provenance = row.get("provenance", "")
        if not provenance or provenance in seen:
            continue
        seen.add(provenance)
        subject_iri = row.get("subject", "")
        predicate_iri = row.get("predicate", "")
        object_value = row.get("object", "")
        object_label = row.get("object_label", "")
        subject_label = row.get("subject_label") or _local_name(subject_iri)
        predicate_label = row.get("predicate_label") or _local_name(predicate_iri)
        displayed_object = object_label or object_value
        confidence_text = row.get("confidence", "")
        try:
            confidence: float | None = float(confidence_text) if confidence_text else None
        except ValueError:
            confidence = None
        facts.append(
            {
                "provenance_iri": provenance,
                "subject": subject_label,
                "subject_iri": subject_iri,
                "predicate": predicate_label,
                "predicate_iri": predicate_iri,
                "object": displayed_object,
                "object_iri": object_value
                if object_value.startswith(("http://", "https://"))
                else None,
                "evidence_text": row.get("evidence", ""),
                "source_clause": row.get("clause", "") or None,
                "source_clause_text": row.get("clause_text", ""),
                "confidence": confidence,
                "extraction_method": row.get("method", ""),
                "score": _score(
                    (
                        subject_label,
                        subject_iri,
                        predicate_label,
                        predicate_iri,
                        displayed_object,
                        row.get("evidence", ""),
                        row.get("clause_text", ""),
                    ),
                    terms,
                    question,
                ),
            }
        )
    return sorted(
        facts,
        key=lambda item: (
            -int(item["score"]),
            -float(item["confidence"] or 0),
            str(item["subject"]).casefold(),
        ),
    )


def _query_clauses(
    graphdb: GraphDBClient,
    *,
    graph_iri: str,
    base_iri: str,
    terms: list[str],
    question: str,
    limit: int,
) -> list[dict[str, Any]]:
    base = base_iri.rstrip("/")
    search = _contains_any(
        (
            'COALESCE(STR(?text), "")',
            'COALESCE(STR(?clause_no), "")',
            'COALESCE(STR(?label), "")',
        ),
        terms,
    )
    query = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?clause ?label ?clause_no ?text ?document ?document_label WHERE {{
  ?clause a <{base}/class/Clause> ;
    <{base}/property/hasText> ?text .
  OPTIONAL {{ ?clause rdfs:label ?label . }}
  OPTIONAL {{ ?clause <{base}/property/hasClauseNumber> ?clause_no . }}
  OPTIONAL {{ ?document <{base}/property/hasClause> ?clause . }}
  OPTIONAL {{ ?document rdfs:label ?document_label . }}
  FILTER({search})
}}
LIMIT {limit}
"""
    clauses: dict[str, dict[str, Any]] = {}
    for row in _rows(graphdb.query(query, graph_iri=graph_iri)):
        clause_iri = row.get("clause", "")
        if not clause_iri:
            continue
        text = row.get("text", "")
        label = row.get("clause_no") or row.get("label") or _local_name(clause_iri)
        candidate = {
            "clause_iri": clause_iri,
            "label": label,
            "clause_no": row.get("clause_no") or None,
            "text": text,
            "document_iri": row.get("document") or None,
            "document_label": row.get("document_label") or None,
            "score": _score((label, text), terms, question),
        }
        current = clauses.get(clause_iri)
        if current is None or candidate["score"] > current["score"]:
            clauses[clause_iri] = candidate
    return sorted(
        clauses.values(),
        key=lambda item: (-int(item["score"]), str(item["label"]).casefold()),
    )


def _render_context_text(
    *,
    ontology_id: str,
    version: str,
    graph_iri: str,
    question: str,
    terms: list[str],
    entities: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    inferred_facts: list[dict[str, Any]],
    clauses: list[dict[str, Any]],
    max_chars: int,
) -> tuple[str, bool]:
    lines = [
        "# 已发布规章知识上下文",
        f"- 本体：{ontology_id}",
        f"- 版本：{version}",
        f"- 命名图：{graph_iri}",
        f"- 问题：{question}",
        f"- 检索词：{', '.join(terms) if terms else '无'}",
        "",
        "## 相关实体",
    ]
    if entities:
        for item in entities:
            class_label = item.get("class_label") or "未分类"
            lines.append(f"- {item['label']}（{class_label}）：{item['iri']}")
    else:
        lines.append("- 未检索到匹配实体。")

    lines.extend(["", "## 结构化事实"])
    if facts:
        for index, item in enumerate(facts, start=1):
            lines.append(f"{index}. {item['subject']} --{item['predicate']}--> {item['object']}")
            if item.get("evidence_text"):
                lines.append(f"   证据：{_excerpt(item['evidence_text'], 500)}")
            if item.get("source_clause"):
                lines.append(f"   来源条款：{item['source_clause']}")
            if item.get("confidence") is not None:
                lines.append(f"   置信度：{item['confidence']:.3f}")
            if item.get("extraction_method"):
                lines.append(f"   抽取方法：{item['extraction_method']}")
    else:
        lines.append("- 未检索到带来源记录的结构化事实。")

    lines.extend(["", "## 可解释推导"])
    if inferred_facts:
        for index, item in enumerate(inferred_facts, start=1):
            lines.append(f"{index}. {item['statement']}")
            lines.append(f"   规则：{item['rule_label']}（{item['rule']}）")
            if item.get("status") != "derived":
                lines.append(f"   状态：{item.get('status')} / {item.get('severity')}")
            if item.get("confidence") is not None:
                basis = (
                    "取前提最低值"
                    if item.get("confidence_basis") == "minimum_of_premises"
                    else str(item.get("confidence_basis") or "")
                )
                lines.append(f"   置信度：{item['confidence']:.3f}（{basis}）")
            lines.append("   证明链：")
            for step in item.get("proof_steps") or []:
                kind = "结论" if step.get("kind") == "conclusion" else "前提"
                lines.append(f"   {step['step']}. [{kind}] {step['statement']}")
            if item.get("source_clauses"):
                lines.append(f"   来源条款：{', '.join(item['source_clauses'])}")
    else:
        lines.append("- 未生成可解释推导结论。")

    lines.extend(["", "## 相关条款原文"])
    if clauses:
        for index, item in enumerate(clauses, start=1):
            lines.append(f"{index}. {item['label']}：{item['clause_iri']}")
            lines.append(f"   {_excerpt(item['text'], 800)}")
    else:
        lines.append("- 未检索到匹配条款。")

    lines.extend(
        [
            "",
            "## 使用约束",
            "- `结构化事实`是图谱中的断言事实；`可解释推导`是应用规则基于这些事实生成的结论。",
            "- 回答应优先引用上述事实、推导和条款；没有依据时明确说明知识库未检索到相关内容。",
            "- 结构化事实中的来源条款、证据原文和置信度应保留，供后续核验。",
        ]
    )
    full_text = "\n".join(lines)
    if len(full_text) <= max_chars:
        return full_text, False
    suffix = "\n\n[上下文已按字符预算截断]"
    return full_text[: max(0, max_chars - len(suffix))].rstrip() + suffix, True


def build_agent_context(
    graphdb: GraphDBClient,
    *,
    ontology_id: str,
    version: str,
    base_iri: str,
    graph_iri: str,
    question: str,
    max_entities: int,
    max_facts: int,
    max_clauses: int,
    max_chars: int,
    enabled_rules: list[str] | None = None,
) -> dict[str, Any]:
    terms = _question_terms(question)
    entity_candidate_limit = min(400, max(max_entities * 6, max_entities + 1))
    fact_candidate_limit = min(500, max(max_facts * 5, max_facts + 1))
    clause_candidate_limit = min(300, max(max_clauses * 5, max_clauses + 1))

    entity_candidates = _query_entities(
        graphdb,
        graph_iri=graph_iri,
        base_iri=base_iri,
        terms=terms,
        question=question,
        limit=entity_candidate_limit,
    )
    fact_candidates = _query_facts(
        graphdb,
        graph_iri=graph_iri,
        base_iri=base_iri,
        terms=terms,
        question=question,
        limit=fact_candidate_limit,
    )
    clause_candidates = _query_clauses(
        graphdb,
        graph_iri=graph_iri,
        base_iri=base_iri,
        terms=terms,
        question=question,
        limit=clause_candidate_limit,
    )

    all_entities = _filter_by_relevance(
        entity_candidates,
        top_ratio=0.12,
        minimum_score=1,
    )
    all_facts = _filter_by_relevance(
        fact_candidates,
        top_ratio=0.12,
        minimum_score=8,
    )
    all_clauses = _filter_by_relevance(
        clause_candidates,
        top_ratio=0.12,
        minimum_score=8,
    )
    inference_evaluation = DEFAULT_INFERENCE_ENGINE.evaluate(
        all_facts,
        enabled_rules=enabled_rules,
    )
    all_inferred_facts = [item.model_dump(mode="json") for item in inference_evaluation.results]

    entities = all_entities[:max_entities]
    facts = all_facts[:max_facts]
    inferred_facts = all_inferred_facts[:max_facts]
    clauses = all_clauses[:max_clauses]
    list_truncated = (
        len(all_entities) > len(entities)
        or len(all_facts) > len(facts)
        or len(all_inferred_facts) > len(inferred_facts)
        or len(all_clauses) > len(clauses)
    )
    context_text, text_truncated = _render_context_text(
        ontology_id=ontology_id,
        version=version,
        graph_iri=graph_iri,
        question=question,
        terms=terms,
        entities=entities,
        facts=facts,
        inferred_facts=inferred_facts,
        clauses=clauses,
        max_chars=max_chars,
    )
    rule_counts: dict[str, int] = {}
    rule_id_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for item in inferred_facts:
        rule = str(item["rule"])
        rule_counts[rule] = rule_counts.get(rule, 0) + 1
        rule_id = str(item["rule_id"])
        rule_id_counts[rule_id] = rule_id_counts.get(rule_id, 0) + 1
        category = str(item["category"])
        category_counts[category] = category_counts.get(category, 0) + 1
        status = str(item["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "ontology": {
            "ontology_id": ontology_id,
            "version": version,
            "graph_iri": graph_iri,
        },
        "question": question,
        "retrieval": {
            "mode": "lexical",
            "terms": terms,
            "ranking": {
                "method": "phrase_boost_relative_threshold",
                "top_ratios": {
                    "entities": 0.12,
                    "facts": 0.12,
                    "clauses": 0.12,
                },
                "candidate_counts": {
                    "entities": len(entity_candidates),
                    "facts": len(fact_candidates),
                    "clauses": len(clause_candidates),
                },
            },
            "limits": {
                "max_entities": max_entities,
                "max_facts": max_facts,
                "max_clauses": max_clauses,
                "max_chars": max_chars,
            },
            "counts": {
                "entities": len(entities),
                "facts": len(facts),
                "inferred_facts": len(inferred_facts),
                "clauses": len(clauses),
                "context_chars": len(context_text),
            },
        },
        "inference": {
            "mode": "explainable",
            "engine": "application_rules",
            "ruleset": INFERENCE_RULESET,
            "schema_version": INFERENCE_SCHEMA_VERSION,
            "available_count": len(all_inferred_facts),
            "inferred_count": len(inferred_facts),
            "rules": rule_counts,
            "rule_ids": rule_id_counts,
            "categories": category_counts,
            "statuses": status_counts,
            "executed_rules": [item.rule_id for item in inference_evaluation.executed_rules],
            "catalog": DEFAULT_INFERENCE_ENGINE.rule_catalog(),
            "errors": [item.model_dump(mode="json") for item in inference_evaluation.errors],
        },
        "context_text": context_text,
        "entities": entities,
        "facts": facts,
        "inferred_facts": inferred_facts,
        "clauses": clauses,
        "truncated": list_truncated or text_truncated,
        "generated_at": datetime.now(UTC).isoformat(),
    }
