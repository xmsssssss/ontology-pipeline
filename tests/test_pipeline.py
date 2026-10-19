from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rdflib import RDF, RDFS, Graph, Namespace

from kb.pipeline import run_pipeline
from kb.storage import Workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_IRI = "https://example.test/regulation/"


def test_sample_pipeline_builds_valid_ontology(tmp_path):
    run, run_dir, manifest = run_pipeline(
        [PROJECT_ROOT / "samples"],
        Workspace(tmp_path / "workspace"),
        run_id="run_test_sample",
        auto_approve=True,
        base_iri=BASE_IRI,
        ontology_id="test_regulation",
        version="0.1.0",
    )

    assert run.status == "completed"
    assert manifest["validation"]["status"] == "valid"
    for name in (
        "manifest.json",
        "source.json",
        "document.jsonl",
        "blocks.jsonl",
        "clauses.jsonl",
        "table_records.jsonl",
        "entity_mentions.jsonl",
        "facts.jsonl",
        "review_items.jsonl",
        "ontology.ttl",
        "shapes.ttl",
        "mapping.yaml",
        "provenance.jsonl",
        "validation_report.json",
        "checksums.sha256",
    ):
        assert (run_dir / name).exists(), name

    graph = Graph().parse(run_dir / "ontology.ttl", format="turtle")
    kb = Namespace(BASE_IRI)
    assert any(graph.subjects(RDF.type, kb["class/Parameter"]))
    labels = {str(value) for value in graph.objects(None, RDFS.label)}
    assert "出口压力（给水泵）" in labels
    assert any(predicate == kb["property/hasUpperBound"] for _, predicate, _ in graph)

    extraction_report = (run_dir / "extraction_report.json").read_text(encoding="utf-8")
    extraction_payload = json.loads(extraction_report)
    assert extraction_payload["strategy"] == "rule"
    assert extraction_payload["stages"] == {}

    for line in (run_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        actual = hashlib.sha256((run_dir / relative).read_bytes()).hexdigest()
        assert digest == actual
