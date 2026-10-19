from __future__ import annotations

from pathlib import Path
from typing import Any

from rdflib import OWL, RDF, Graph, Namespace

from kb.ids import safe_local_name


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
) -> None:
    checks.append({"name": name, "passed": passed, "detail": detail})


def validate_package(
    ontology_path: Path,
    shapes_path: Path,
    facts_path: Path,
    *,
    base_iri: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    graph = Graph()
    try:
        graph.parse(ontology_path, format="turtle")
        _check(checks, "rdf_parse", True, "ontology.ttl 可由 RDFLib 解析。")
    except Exception as exc:
        errors.append(f"ontology.ttl 解析失败: {exc}")
        _check(checks, "rdf_parse", False, str(exc))
        return {
            "status": "invalid",
            "conforms": False,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
            "counts": {"triples": 0},
        }

    ns = Namespace(base_iri.rstrip("/") + "/")
    declared_classes = {str(subject) for subject in graph.subjects(RDF.type, OWL.Class)}
    declared_properties = {
        str(subject)
        for property_type in (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty)
        for subject in graph.subjects(RDF.type, property_type)
    }

    used_kb_predicates = {
        str(predicate) for _, predicate, _ in graph if str(predicate).startswith(str(ns))
    }
    undefined_properties = sorted(used_kb_predicates - declared_properties)
    _check(
        checks,
        "property_declarations",
        not undefined_properties,
        (
            "所有 kb 命名空间谓词均已声明。"
            if not undefined_properties
            else f"未声明谓词: {', '.join(undefined_properties)}"
        ),
    )
    errors.extend(f"未声明的本体谓词: {item}" for item in undefined_properties)

    used_classes = {
        str(object_value)
        for _, predicate, object_value in graph
        if predicate == RDF.type and str(object_value).startswith(str(ns) + "class/")
    }
    undefined_classes = sorted(used_classes - declared_classes)
    _check(
        checks,
        "class_declarations",
        not undefined_classes,
        (
            "所有实例类型均已声明。"
            if not undefined_classes
            else f"未声明类: {', '.join(undefined_classes)}"
        ),
    )
    errors.extend(f"未声明的本体类: {item}" for item in undefined_classes)

    facts = [item for item in _read_jsonl(facts_path)]
    fact_ids = [str(item.get("fact_id", "")) for item in facts]
    duplicate_fact_ids = sorted(
        {fact_id for fact_id in fact_ids if fact_id and fact_ids.count(fact_id) > 1}
    )
    _check(
        checks,
        "duplicate_fact_ids",
        not duplicate_fact_ids,
        (
            "事实 ID 无重复。"
            if not duplicate_fact_ids
            else f"重复事实 ID: {', '.join(duplicate_fact_ids)}"
        ),
    )
    errors.extend(f"重复事实 ID: {item}" for item in duplicate_fact_ids)

    approved = [item for item in facts if item.get("review_status") == "approved"]
    missing_provenance: list[str] = []
    for fact in approved:
        provenance_iri = ns[f"provenance/{safe_local_name(str(fact.get('fact_id', '')), 'fact')}"]
        if not any(graph.objects(provenance_iri, RDF.subject)):
            missing_provenance.append(str(fact.get("fact_id", "")))
    _check(
        checks,
        "fact_provenance",
        not missing_provenance,
        (
            "每个已批准事实均有 RDF 来源记录。"
            if not missing_provenance
            else f"缺少来源记录: {', '.join(missing_provenance)}"
        ),
    )
    errors.extend(f"事实缺少来源记录: {item}" for item in missing_provenance)

    shacl_text = ""
    conforms = False
    try:
        from pyshacl import validate

        shapes_graph = Graph().parse(shapes_path, format="turtle")
        conforms, _, shacl_text = validate(
            data_graph=graph,
            shacl_graph=shapes_graph,
            inference="rdfs",
            abort_on_first=False,
            allow_infos=True,
            allow_warnings=True,
        )
        _check(
            checks,
            "shacl",
            bool(conforms),
            "SHACL 校验通过。" if conforms else "SHACL 校验未通过，详见报告。",
        )
        if not conforms:
            errors.append("SHACL 校验未通过。")
    except Exception as exc:
        errors.append(f"SHACL 执行失败: {exc}")
        _check(checks, "shacl", False, str(exc))

    pending = len([item for item in facts if item.get("review_status") == "pending"])
    rejected = len([item for item in facts if item.get("review_status") == "rejected"])
    if pending:
        warnings.append(f"{pending} 条事实尚未批准，未写入正式本体。")

    status = "valid" if not errors else "invalid"
    return {
        "status": status,
        "conforms": bool(conforms),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "shacl_report": shacl_text,
        "counts": {
            "triples": len(graph),
            "facts_total": len(facts),
            "facts_approved": len(approved),
            "facts_pending": pending,
            "facts_rejected": rejected,
            "classes": len(declared_classes),
            "properties": len(declared_properties),
        },
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    import json

    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
