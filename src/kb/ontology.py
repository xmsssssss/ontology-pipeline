from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import yaml
from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, PROV, SH

from kb.constants import DEFAULT_BASE_IRI, DEFAULT_COLUMN_MAPPING, DEFAULT_ONTOLOGY_VERSION
from kb.ids import safe_local_name, stable_id
from kb.models import ClauseRecord, DocumentRecord, FactRecord

CLASSES: dict[str, tuple[str, str]] = {
    "Document": ("规章文档", "承载规章内容的来源文档。"),
    "Clause": ("规章条款", "规章文档中的可引用条款。"),
    "Equipment": ("设备", "运行、维护或管理对象。"),
    "Component": ("部件", "设备内部或附属的可识别部件。"),
    "System": ("系统", "由多个设备或部件组成的系统。"),
    "Parameter": ("运行参数", "具有单位和边界条件的设备运行参数。"),
    "Role": ("责任角色", "承担操作、检查或报告责任的角色。"),
}

PROPERTIES: dict[str, tuple[str, str, str | None, str | None]] = {
    "hasClause": ("包含条款", "ObjectProperty", "Document", "Clause"),
    "sourceFile": ("来源文件名", "DatatypeProperty", "Document", "string"),
    "hasClauseNumber": ("条款号", "DatatypeProperty", "Clause", "string"),
    "hasText": ("条款原文", "DatatypeProperty", "Clause", "string"),
    "hasParameter": ("包含参数", "ObjectProperty", "Equipment", "Parameter"),
    "hasLowerBound": ("参数下限", "DatatypeProperty", "Parameter", "decimal"),
    "hasUpperBound": ("参数上限", "DatatypeProperty", "Parameter", "decimal"),
    "hasUnit": ("参数单位", "DatatypeProperty", "Parameter", "string"),
    "hasNote": ("参数备注", "DatatypeProperty", "Parameter", "string"),
    "hasConditionOperator": (
        "条件运算符",
        "DatatypeProperty",
        "Parameter",
        "string",
    ),
    "hasConditionThreshold": (
        "条件阈值",
        "DatatypeProperty",
        "Parameter",
        "decimal",
    ),
    "hasConditionUnit": (
        "条件单位",
        "DatatypeProperty",
        "Parameter",
        "string",
    ),
    "requiresAction": ("要求执行动作", "DatatypeProperty", None, "string"),
    "prohibitsAction": ("禁止执行动作", "DatatypeProperty", None, "string"),
    "permitsAction": ("允许执行动作", "DatatypeProperty", None, "string"),
    "hasResponsibleRole": ("责任角色", "DatatypeProperty", None, "string"),
    "hasOperatingCondition": ("适用条件", "DatatypeProperty", None, "string"),
    "hasProvenance": ("事实来源", "ObjectProperty", None, "Provenance"),
    "sourceClause": ("来源条款", "ObjectProperty", None, "Clause"),
    "evidenceText": ("证据原文", "DatatypeProperty", None, "string"),
    "extractionMethod": ("抽取方法", "DatatypeProperty", None, "string"),
    "confidence": ("置信度", "DatatypeProperty", None, "decimal"),
    "sourceTable": ("来源表", "DatatypeProperty", None, "string"),
    "sourceRow": ("来源行", "DatatypeProperty", None, "integer"),
}

DATATYPES = {
    "string": XSD.string,
    "decimal": XSD.decimal,
    "integer": XSD.integer,
}


@dataclass
class OntologyBuildResult:
    graph: Graph
    shapes_graph: Graph
    provenance_records: list[dict[str, Any]]
    mapping: dict[str, Any]

    def ontology_ttl(self) -> str:
        return self.graph.serialize(format="turtle")

    def shapes_ttl(self) -> str:
        return self.shapes_graph.serialize(format="turtle")

    def mapping_yaml(self) -> str:
        return yaml.safe_dump(
            self.mapping,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def _entity_iri(ns: Namespace, class_name: str, key: str, label: str) -> URIRef:
    digest = stable_id("entity", class_name, key, length=20).split("_", 1)[-1]
    slug = safe_local_name(label, class_name)[:40]
    return ns[f"entity/{class_name}/{quote(slug, safe='')}-{digest}"]


def _class_iri(ns: Namespace, class_name: str) -> URIRef:
    return ns[f"class/{safe_local_name(class_name, 'Entity')}"]


def _predicate_iri(ns: Namespace, predicate: str) -> URIRef:
    if predicate == "rdf:type":
        return RDF.type
    return ns[f"property/{safe_local_name(predicate, 'property')}"]


def _clause_iri(ns: Namespace, clause_id: str) -> URIRef:
    return ns[f"clause/{safe_local_name(clause_id, 'clause')}"]


def _document_iri(ns: Namespace, document_id: str) -> URIRef:
    return ns[f"document/{safe_local_name(document_id, 'document')}"]


def _add_tbox(graph: Graph, ns: Namespace) -> None:
    for class_name, (label, comment) in CLASSES.items():
        iri = _class_iri(ns, class_name)
        graph.add((iri, RDF.type, OWL.Class))
        graph.add((iri, RDFS.label, Literal(label, lang="zh")))
        graph.add((iri, RDFS.comment, Literal(comment, lang="zh")))

    provenance_class = ns["class/Provenance"]
    graph.add((provenance_class, RDF.type, OWL.Class))
    graph.add((provenance_class, RDFS.label, Literal("事实来源", lang="zh")))
    graph.add(
        (
            provenance_class,
            RDFS.comment,
            Literal("事实级 RDF 具体化与来源追踪记录。", lang="zh"),
        )
    )

    for predicate, (label, property_type, domain, range_name) in PROPERTIES.items():
        iri = _predicate_iri(ns, predicate)
        graph.add(
            (
                iri,
                RDF.type,
                OWL.ObjectProperty if property_type == "ObjectProperty" else OWL.DatatypeProperty,
            )
        )
        graph.add((iri, RDFS.label, Literal(label, lang="zh")))
        if domain:
            graph.add(
                (
                    iri,
                    RDFS.domain,
                    ns["class/Provenance"] if domain == "Provenance" else _class_iri(ns, domain),
                )
            )
        if range_name:
            if range_name in DATATYPES:
                graph.add((iri, RDFS.range, DATATYPES[range_name]))
            elif range_name == "Provenance":
                graph.add((iri, RDFS.range, ns["class/Provenance"]))
            else:
                graph.add((iri, RDFS.range, _class_iri(ns, range_name)))


def _add_source_entities(
    graph: Graph,
    ns: Namespace,
    documents: list[DocumentRecord],
    clauses: list[ClauseRecord],
) -> None:
    for document in documents:
        iri = _document_iri(ns, document.document_id)
        graph.add((iri, RDF.type, OWL.NamedIndividual))
        graph.add((iri, RDF.type, _class_iri(ns, "Document")))
        graph.add((iri, RDFS.label, Literal(document.title, lang="zh")))
        graph.add((iri, _predicate_iri(ns, "sourceFile"), Literal(document.file_name)))
    for clause in clauses:
        iri = _clause_iri(ns, clause.clause_id)
        graph.add((iri, RDF.type, OWL.NamedIndividual))
        graph.add((iri, RDF.type, _class_iri(ns, "Clause")))
        label = clause.clause_no or f"条款 {clause.clause_id[-8:]}"
        graph.add((iri, RDFS.label, Literal(label, lang="zh")))
        graph.add((iri, _predicate_iri(ns, "hasText"), Literal(clause.text)))
        if clause.clause_no:
            graph.add(
                (
                    iri,
                    _predicate_iri(ns, "hasClauseNumber"),
                    Literal(clause.clause_no),
                )
            )
        document_iri = _document_iri(ns, clause.document_id)
        graph.add(
            (
                document_iri,
                _predicate_iri(ns, "hasClause"),
                iri,
            )
        )


def _literal_for_fact(fact: FactRecord) -> Literal:
    datatype = URIRef(fact.datatype) if fact.datatype else None
    if datatype is not None:
        return Literal(fact.object, datatype=datatype)
    return Literal(fact.object)


def _add_fact(
    graph: Graph,
    ns: Namespace,
    fact: FactRecord,
) -> tuple[URIRef, URIRef, Any]:
    subject_type = fact.subject_type or "Equipment"
    subject = _entity_iri(
        ns,
        subject_type,
        fact.subject_key or fact.subject,
        fact.subject,
    )
    graph.add((subject, RDF.type, OWL.NamedIndividual))
    graph.add((subject, RDF.type, _class_iri(ns, subject_type)))
    graph.add((subject, RDFS.label, Literal(fact.subject, lang="zh")))

    if fact.predicate == "rdf:type":
        object_class = fact.object_class or fact.object
        predicate = RDF.type
        object_value: Any = _class_iri(ns, object_class)
        graph.add((subject, predicate, object_value))
        graph.add((subject, RDF.type, OWL.NamedIndividual))
        return subject, predicate, object_value

    predicate = _predicate_iri(ns, fact.predicate)
    if fact.object_type == "iri":
        object_class = fact.object_class or "Entity"
        object_value = _entity_iri(
            ns,
            object_class,
            fact.object_key or fact.object,
            fact.object_label or fact.object,
        )
        graph.add((object_value, RDF.type, OWL.NamedIndividual))
        graph.add((object_value, RDF.type, _class_iri(ns, object_class)))
        graph.add(
            (
                object_value,
                RDFS.label,
                Literal(fact.object_label or fact.object, lang="zh"),
            )
        )
    else:
        object_value = _literal_for_fact(fact)
    graph.add((subject, predicate, object_value))
    if fact.condition:
        graph.add(
            (
                subject,
                _predicate_iri(ns, "hasOperatingCondition"),
                Literal(fact.condition, lang="zh"),
            )
        )
    return subject, predicate, object_value


def _add_provenance(
    graph: Graph,
    ns: Namespace,
    fact: FactRecord,
    statement: tuple[URIRef, URIRef, Any],
) -> dict[str, Any]:
    subject, predicate, object_value = statement
    provenance_iri = ns[f"provenance/{safe_local_name(fact.fact_id, 'fact')}"]
    graph.add((provenance_iri, RDF.type, RDF.Statement))
    graph.add((provenance_iri, RDF.type, PROV.Entity))
    graph.add((provenance_iri, RDF.type, ns["class/Provenance"]))
    graph.add((provenance_iri, RDF.subject, subject))
    graph.add((provenance_iri, RDF.predicate, predicate))
    graph.add((provenance_iri, RDF.object, object_value))
    graph.add((subject, _predicate_iri(ns, "hasProvenance"), provenance_iri))

    if fact.document_id:
        document_iri = _document_iri(ns, fact.document_id)
        graph.add((provenance_iri, PROV.wasDerivedFrom, document_iri))
    if fact.clause_id:
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "sourceClause"),
                _clause_iri(ns, fact.clause_id),
            )
        )
    if fact.evidence_text:
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "evidenceText"),
                Literal(fact.evidence_text, lang="zh"),
            )
        )
    graph.add(
        (
            provenance_iri,
            _predicate_iri(ns, "extractionMethod"),
            Literal(fact.extraction_method),
        )
    )
    graph.add(
        (
            provenance_iri,
            _predicate_iri(ns, "confidence"),
            Literal(str(fact.confidence), datatype=XSD.decimal),
        )
    )
    if fact.unit:
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "hasUnit"),
                Literal(fact.unit),
            )
        )
    if fact.condition:
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "hasOperatingCondition"),
                Literal(fact.condition, lang="zh"),
            )
        )
    source_ref = fact.source_ref or {}
    if source_ref.get("table_id"):
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "sourceTable"),
                Literal(str(source_ref["table_id"])),
            )
        )
    if source_ref.get("row_index") is not None:
        graph.add(
            (
                provenance_iri,
                _predicate_iri(ns, "sourceRow"),
                Literal(str(source_ref["row_index"]), datatype=XSD.integer),
            )
        )
    return {
        "fact_id": fact.fact_id,
        "provenance_iri": str(provenance_iri),
        "subject": str(subject),
        "predicate": str(predicate),
        "object": str(object_value),
        "document_id": fact.document_id,
        "clause_id": fact.clause_id,
        "clause_no": fact.clause_no,
        "evidence_text": fact.evidence_text,
        "extraction_method": fact.extraction_method,
        "confidence": fact.confidence,
        "source_ref": fact.source_ref,
    }


def _build_shapes(ns: Namespace) -> Graph:
    graph = Graph()
    graph.bind("sh", SH)
    graph.bind("kb", ns)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)

    equipment_shape = ns["shape/EquipmentShape"]
    graph.add((equipment_shape, RDF.type, SH.NodeShape))
    graph.add((equipment_shape, SH.targetClass, _class_iri(ns, "Equipment")))
    equipment_property = graph.resource(BNode())
    equipment_shape_props = graph.resource(equipment_shape)
    equipment_shape_props.add(SH.property, equipment_property)
    equipment_property.add(RDF.type, SH.PropertyShape)
    equipment_property.add(SH.path, RDFS.label)
    equipment_property.add(SH.minCount, Literal(1))
    equipment_property.add(SH.message, Literal("设备必须有可读标签。", lang="zh"))

    parameter_shape = ns["shape/ParameterShape"]
    graph.add((parameter_shape, RDF.type, SH.NodeShape))
    graph.add((parameter_shape, SH.targetClass, _class_iri(ns, "Parameter")))
    parameter_node = graph.resource(parameter_shape)
    for path, datatype in (
        (_predicate_iri(ns, "hasLowerBound"), XSD.decimal),
        (_predicate_iri(ns, "hasUpperBound"), XSD.decimal),
    ):
        item = graph.resource(BNode())
        parameter_node.add(SH.property, item)
        item.add(RDF.type, SH.PropertyShape)
        item.add(SH.path, path)
        item.add(SH.datatype, datatype)

    provenance_shape = ns["shape/ProvenanceShape"]
    graph.add((provenance_shape, RDF.type, SH.NodeShape))
    graph.add((provenance_shape, SH.targetClass, RDF.Statement))
    provenance_node = graph.resource(provenance_shape)
    for path in (RDF.subject, RDF.predicate, RDF.object, PROV.wasDerivedFrom):
        item = graph.resource(BNode())
        provenance_node.add(SH.property, item)
        item.add(RDF.type, SH.PropertyShape)
        item.add(SH.path, path)
        item.add(SH.minCount, Literal(1))
    return graph


def build_ontology(
    facts: list[FactRecord],
    documents: list[DocumentRecord],
    clauses: list[ClauseRecord],
    *,
    base_iri: str = DEFAULT_BASE_IRI,
    ontology_id: str = "regulation",
    version: str = DEFAULT_ONTOLOGY_VERSION,
) -> OntologyBuildResult:
    ns = Namespace(base_iri.rstrip("/") + "/")
    graph = Graph()
    graph.bind("kb", ns)
    graph.bind("owl", OWL)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)
    graph.bind("prov", PROV)
    graph.bind("dcterms", DCTERMS)

    ontology_iri = ns[f"ontology/{safe_local_name(ontology_id, 'ontology')}"]
    graph.add((ontology_iri, RDF.type, OWL.Ontology))
    graph.add((ontology_iri, RDFS.label, Literal("规章知识本体", lang="zh")))
    graph.add(
        (
            ontology_iri,
            RDFS.comment,
            Literal("由规章文件抽取、审核并发布的版本化 OWL 本体。", lang="zh"),
        )
    )
    graph.add((ontology_iri, OWL.versionInfo, Literal(version)))
    graph.add((ontology_iri, DCTERMS.identifier, Literal(ontology_id)))
    _add_tbox(graph, ns)
    _add_source_entities(graph, ns, documents, clauses)

    approved = [fact for fact in facts if fact.review_status == "approved"]
    provenance_records: list[dict[str, Any]] = []
    for fact in approved:
        statement = _add_fact(graph, ns, fact)
        provenance_records.append(_add_provenance(graph, ns, fact, statement))

    mapping = {
        "schema_version": "0.1",
        "ontology": {
            "id": ontology_id,
            "version": version,
            "base_iri": str(ns),
            "approved_fact_count": len(approved),
        },
        "columns": {key: list(values) for key, values in DEFAULT_COLUMN_MAPPING.items()},
        "properties": {
            key: {
                "type": value[1],
                "domain": value[2],
                "range": value[3],
            }
            for key, value in PROPERTIES.items()
        },
        "notes": [
            "Neo4j 只作为由 OWL 生成的可重建查询投影，不作为 OWL 主库。",
            "历史 Neo4j 数据需经过映射、对齐和审核后，才能并入本体库。",
        ],
    }
    return OntologyBuildResult(
        graph=graph,
        shapes_graph=_build_shapes(ns),
        provenance_records=provenance_records,
        mapping=mapping,
    )
