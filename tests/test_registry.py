from __future__ import annotations

from pathlib import Path

import pytest

from kb.pipeline import run_pipeline
from kb.registry import OntologyRegistry, RegistryConflict
from kb.storage import Workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_IRI = "https://example.test/regulation/"


class FakeGraphDB:
    repository = "regulation-test"
    base_url = "http://graphdb.test"

    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    def publish_graph(
        self,
        *,
        graph_iri: str,
        content: str,
        shapes_graph_iri: str,
        shapes_content: str = "",
    ) -> dict[str, object]:
        self.published.append(
            {
                "graph_iri": graph_iri,
                "content": content,
                "shapes_graph_iri": shapes_graph_iri,
                "shapes_content": shapes_content,
            }
        )
        return {
            "published": True,
            "graph_iri": graph_iri,
            "shapes_graph_iri": shapes_graph_iri,
            "shapes_published": True,
        }


def test_registry_create_edit_query_export_and_delete(tmp_path):
    registry = OntologyRegistry(tmp_path / "workspace")
    info = registry.create(
        ontology_id="regulation",
        version="0.1.0",
        base_iri=BASE_IRI,
        title="规章知识本体",
        description="测试本体",
    )

    assert info.triple_count == 5
    loaded, turtle = registry.get("regulation", "0.1.0")
    assert loaded.content_sha256 == info.content_sha256
    assert "owl:Ontology" in turtle

    updated_turtle = turtle + (
        "\n<https://example.test/regulation/class/Equipment> a owl:Class .\n"
    )
    updated = registry.update_turtle("regulation", "0.1.0", updated_turtle)
    assert updated.triple_count == 6

    select = registry.query(
        "regulation",
        "0.1.0",
        """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT ?version WHERE {
          ?ontology a owl:Ontology ;
                    owl:versionInfo ?version .
        }
        """,
    )
    assert select["type"] == "SELECT"
    assert select["rows"] == [{"version": "0.1.0"}]

    ask = registry.query(
        "regulation",
        "0.1.0",
        """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        ASK {
          <https://example.test/regulation/class/Equipment> a owl:Class .
        }
        """,
    )
    assert ask == {"type": "ASK", "boolean": True}

    filename, content = registry.export_turtle("regulation", "0.1.0")
    assert filename == "regulation-0.1.0.ttl"
    assert b"class/Equipment" in content

    registry.delete_version("regulation", "0.1.0")
    assert registry.list_versions() == []


def test_registry_import_and_published_version_is_immutable(tmp_path):
    registry = OntologyRegistry(tmp_path / "workspace")
    imported = registry.import_owl(
        content=(
            b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            b"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            b"<https://example.test/imported> a owl:Ontology .\n"
            b"<https://example.test/imported/Equipment> a owl:Class ;\n"
            b'  rdfs:label "Equipment"@en .\n'
        ),
        filename="equipment.ttl",
        ontology_id="imported",
        version="1.0.0",
        base_iri="https://example.test/imported/",
        title="导入本体",
    )
    assert imported.source_kind == "imported"
    assert imported.triple_count == 3
    assert (tmp_path / "workspace/ontology/imported/1.0.0/imports/equipment.ttl").exists()

    graphdb = FakeGraphDB()
    publication = registry.publish("imported", "1.0.0", graphdb_client=graphdb)
    assert publication["repository"] == "regulation-test"
    assert len(graphdb.published) == 1

    published = registry.get("imported", "1.0.0")[0]
    assert published.status == "published"
    assert published.graph_iri == publication["graph_iri"]
    assert (tmp_path / "workspace/ontology/imported/1.0.0/publication.json").exists()

    with pytest.raises(RegistryConflict, match="已发布版本不可修改"):
        registry.update_turtle(
            "imported", "1.0.0", published_turtle := registry.get("imported", "1.0.0")[1]
        )
    with pytest.raises(RegistryConflict, match="已发布版本不可删除"):
        registry.delete_version("imported", "1.0.0")

    assert published_turtle


def test_registry_registers_valid_pipeline_run(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    run, run_dir, manifest = run_pipeline(
        [PROJECT_ROOT / "samples"],
        workspace,
        run_id="registry_run",
        auto_approve=True,
        base_iri=BASE_IRI,
        ontology_id="pipeline_regulation",
        version="0.4.0",
    )
    assert manifest["validation"]["status"] == "valid"

    registry = OntologyRegistry(workspace)
    info = registry.register_run(run_dir)

    assert info.source_kind == "pipeline"
    assert info.source_run_id == run.run_id
    assert info.triple_count > 0
    version_dir = workspace.root / "ontology/pipeline_regulation/0.4.0"
    assert (version_dir / "ontology.ttl").exists()
    assert (version_dir / "shapes.ttl").exists()
    assert (version_dir / "facts.jsonl").exists()
