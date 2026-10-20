from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import kb.web as web_module
from kb.pipeline import PipelineCancelled, create_run, run_pipeline
from kb.storage import Workspace
from kb.web import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_IRI = "https://example.test/web/"
ACTIVE_STATUSES = {"queued", "running", "cancelling"}


def _wait_for_run(client: TestClient, run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}/progress?log_limit=200")
        assert response.status_code == 200
        payload = response.json()
        if payload["summary"]["status"] not in ACTIVE_STATUSES:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run did not finish before timeout: {run_id}")


class FakeGraphDB:
    repository = "regulation-web-test"
    base_url = "http://graphdb.test"

    def status(self):
        return {
            "connected": True,
            "base_url": self.base_url,
            "repository": self.repository,
            "repository_exists": True,
            "repositories": [{"id": self.repository, "title": "Web test", "type": "graphdb"}],
            "error": None,
        }

    def publish_graph(
        self,
        *,
        graph_iri: str,
        content: str,
        shapes_graph_iri: str,
        shapes_content: str = "",
    ):
        return {
            "published": True,
            "graph_iri": graph_iri,
            "shapes_graph_iri": shapes_graph_iri if shapes_content else None,
            "shapes_published": bool(shapes_content),
        }

    def query(self, query: str, *, graph_iri: str | None = None):
        assert graph_iri
        if "?entity a owl:NamedIndividual" in query:
            return {
                "type": "SELECT",
                "columns": ["entity", "label", "class", "class_label"],
                "rows": [
                    {
                        "entity": f"{BASE_IRI}entity/Equipment/feed-water-pump",
                        "label": "给水泵",
                        "class": f"{BASE_IRI}class/Equipment",
                        "class_label": "设备",
                    },
                    {
                        "entity": f"{BASE_IRI}entity/Equipment/standby-feed-water-pump",
                        "label": "备用给水泵",
                        "class": f"{BASE_IRI}class/Equipment",
                        "class_label": "设备",
                    },
                    {
                        "entity": f"{BASE_IRI}entity/Parameter/discharge-pressure",
                        "label": "出口压力",
                        "class": f"{BASE_IRI}class/Parameter",
                        "class_label": "运行参数",
                    },
                ],
                "count": 3,
            }
        if f"<{BASE_IRI}class/Provenance>" in query:
            return {
                "type": "SELECT",
                "columns": [
                    "provenance",
                    "subject",
                    "subject_label",
                    "predicate",
                    "predicate_label",
                    "object",
                    "object_label",
                    "evidence",
                    "clause",
                    "clause_text",
                    "confidence",
                    "method",
                ],
                "rows": [
                    {
                        "provenance": f"{BASE_IRI}provenance/fact-pump-pressure",
                        "subject": f"{BASE_IRI}entity/Equipment/feed-water-pump",
                        "subject_label": "给水泵",
                        "predicate": f"{BASE_IRI}property/hasParameter",
                        "predicate_label": "具有参数",
                        "object": f"{BASE_IRI}entity/Parameter/discharge-pressure",
                        "object_label": "出口压力",
                        "evidence": "给水泵出口压力应保持在规定范围内。",
                        "clause": f"{BASE_IRI}clause/pump-1",
                        "clause_text": "给水泵出口压力应保持在规定范围内。",
                        "confidence": "0.93",
                        "method": "rule",
                    },
                    {
                        "provenance": f"{BASE_IRI}provenance/fact-pump-pressure-lower",
                        "subject": f"{BASE_IRI}entity/Parameter/discharge-pressure",
                        "subject_label": "出口压力",
                        "predicate": f"{BASE_IRI}property/hasLowerBound",
                        "predicate_label": "参数下限",
                        "object": "1.2",
                        "object_label": "",
                        "evidence": "给水泵出口压力应保持在规定范围内。",
                        "clause": f"{BASE_IRI}clause/pump-1",
                        "clause_text": "给水泵出口压力应保持在规定范围内。",
                        "confidence": "0.91",
                        "method": "rule",
                    },
                    {
                        "provenance": f"{BASE_IRI}provenance/fact-pump-pressure-unit",
                        "subject": f"{BASE_IRI}entity/Parameter/discharge-pressure",
                        "subject_label": "出口压力",
                        "predicate": f"{BASE_IRI}property/hasUnit",
                        "predicate_label": "参数单位",
                        "object": "MPa",
                        "object_label": "",
                        "evidence": "给水泵出口压力应保持在规定范围内。",
                        "clause": f"{BASE_IRI}clause/pump-1",
                        "clause_text": "给水泵出口压力应保持在规定范围内。",
                        "confidence": "0.90",
                        "method": "rule",
                    },
                    {
                        "provenance": f"{BASE_IRI}provenance/fact-turbine-noise",
                        "subject": f"{BASE_IRI}entity/Equipment/turbine",
                        "subject_label": "汽轮机",
                        "predicate": f"{BASE_IRI}property/permitsAction",
                        "predicate_label": "允许执行动作",
                        "object": "可靠性",
                        "object_label": "",
                        "evidence": "汽轮机具有较高效率与可靠性。",
                        "clause": f"{BASE_IRI}clause/turbine-1",
                        "clause_text": "汽轮机具有较高效率与可靠性。",
                        "confidence": "0.90",
                        "method": "rule",
                    },
                ],
                "count": 4,
            }
        if f"<{BASE_IRI}class/Clause>" in query:
            return {
                "type": "SELECT",
                "columns": [
                    "clause",
                    "label",
                    "clause_no",
                    "text",
                    "document",
                    "document_label",
                ],
                "rows": [
                    {
                        "clause": f"{BASE_IRI}clause/pump-1",
                        "label": "第 1 条",
                        "clause_no": "1",
                        "text": "给水泵出口压力应保持在规定范围内。" * 30,
                        "document": f"{BASE_IRI}document/regulation",
                        "document_label": "给水泵运行规程",
                    },
                    {
                        "clause": f"{BASE_IRI}clause/pump-2",
                        "label": "第 2 条",
                        "clause_no": "2",
                        "text": "给水泵启动前应确认相关参数正常。",
                        "document": f"{BASE_IRI}document/regulation",
                        "document_label": "给水泵运行规程",
                    },
                ],
                "count": 2,
            }
        raise AssertionError(f"unexpected query: {query}")


def test_web_page_and_ontology_crud(tmp_path):
    app = create_app(tmp_path / "workspace", graphdb_client=FakeGraphDB())
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "本体工作台" in page.text

        created = client.post(
            "/api/ontologies",
            json={
                "ontology_id": "web_regulation",
                "version": "0.1.0",
                "base_iri": BASE_IRI,
                "title": "Web 规章本体",
                "description": "接口测试",
            },
        )
        assert created.status_code == 201
        assert created.json()["triple_count"] == 5

        detail = client.get("/api/ontologies/web_regulation/versions/0.1.0")
        assert detail.status_code == 200
        turtle = detail.json()["turtle"]

        updated = client.put(
            "/api/ontologies/web_regulation/versions/0.1.0/turtle",
            json={
                "content": turtle + "\n<https://example.test/web/class/Regulation> a owl:Class .\n"
            },
        )
        assert updated.status_code == 200
        assert updated.json()["triple_count"] == 6

        selected = client.post(
            "/api/query",
            json={
                "ontology_id": "web_regulation",
                "version": "0.1.0",
                "source": "local",
                "query": """
                    PREFIX owl: <http://www.w3.org/2002/07/owl#>
                    SELECT ?version WHERE {
                      ?ontology a owl:Ontology ;
                                owl:versionInfo ?version .
                    }
                """,
            },
        )
        assert selected.status_code == 200
        assert selected.json()["rows"] == [{"version": "0.1.0"}]

        published = client.post("/api/ontologies/web_regulation/versions/0.1.0/publish")
        assert published.status_code == 200
        assert published.json()["repository"] == "regulation-web-test"

        blocked = client.put(
            "/api/ontologies/web_regulation/versions/0.1.0/turtle",
            json={"content": turtle},
        )
        assert blocked.status_code == 409

        dashboard = client.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["counts"]["published"] == 1


def test_agent_context_requires_published_version_and_respects_budgets(tmp_path):
    app = create_app(tmp_path / "workspace", graphdb_client=FakeGraphDB())
    with TestClient(app) as client:
        created = client.post(
            "/api/ontologies",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "base_iri": BASE_IRI,
                "title": "Agent 测试本体",
            },
        )
        assert created.status_code == 201

        unpublished = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
            },
        )
        assert unpublished.status_code == 409

        published = client.post("/api/ontologies/agent_regulation/versions/0.1.0/publish")
        assert published.status_code == 200

        rules_response = client.get("/api/inference/rules")
        assert rules_response.status_code == 200
        rule_catalog = rules_response.json()["rules"]
        assert {item["rule_id"] for item in rule_catalog} == {
            "parameter_conditions",
            "parameter_consistency",
            "parameter_constraints",
            "regulatory_requirements",
        }
        schema_response = client.get("/api/inference/schema")
        assert schema_response.status_code == 200
        result_schema = schema_response.json()["inference_result"]
        assert {"rule_id", "conclusion", "premises", "proof_steps"}.issubset(
            result_schema["properties"]
        )

        response = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
                "max_entities": 1,
                "max_facts": 1,
                "max_clauses": 1,
                "max_chars": 500,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ontology"]["ontology_id"] == "agent_regulation"
        assert payload["retrieval"]["mode"] == "lexical"
        assert "给水泵" in payload["retrieval"]["terms"]
        assert "参数" in payload["retrieval"]["terms"]
        assert all("哪" not in term for term in payload["retrieval"]["terms"])
        assert payload["retrieval"]["counts"] == {
            "entities": 1,
            "facts": 1,
            "inferred_facts": 1,
            "clauses": 1,
            "context_chars": len(payload["context_text"]),
        }
        assert len(payload["context_text"]) <= 500
        assert payload["context_text"].endswith("[上下文已按字符预算截断]")
        assert payload["truncated"] is True
        assert payload["entities"][0]["label"] == "给水泵"
        assert payload["facts"][0]["confidence"] == 0.93
        assert payload["facts"][0]["source_clause"].endswith("/clause/pump-1")
        assert payload["inference"]["mode"] == "explainable"
        assert payload["inference"]["engine"] == "application_rules"
        assert payload["inference"]["ruleset"] == "kb-explainable/1.0"
        assert payload["inference"]["schema_version"] == "kb-inference/1.0"
        assert payload["inference"]["available_count"] == 1
        assert payload["inference"]["inferred_count"] == 1
        assert payload["inference"]["rules"] == {"parameter_bound": 1}
        assert payload["inference"]["rule_ids"] == {"parameter_constraints": 1}
        assert payload["inference"]["categories"] == {"parameter": 1}
        assert payload["inference"]["statuses"] == {"derived": 1}
        assert payload["inference"]["executed_rules"] == [
            "parameter_conditions",
            "parameter_consistency",
            "parameter_constraints",
            "regulatory_requirements",
        ]
        assert payload["inference"]["errors"] == []
        inferred = payload["inferred_facts"][0]
        assert inferred["schema_version"] == "kb-inference/1.0"
        assert inferred["rule_id"] == "parameter_constraints"
        assert inferred["statement"] == "给水泵的出口压力下限为 1.2 MPa"
        assert inferred["confidence"] == 0.9
        assert inferred["source_clauses"] == [f"{BASE_IRI}clause/pump-1"]
        assert inferred["conclusion"]["predicate"] == "参数下限"
        assert inferred["conclusion"]["object"] == "1.2 MPa"
        assert inferred["premises"][0]["fact_id"].endswith("/provenance/fact-pump-pressure")
        assert [step["kind"] for step in inferred["proof_steps"]] == [
            "premise",
            "premise",
            "premise",
            "conclusion",
        ]

        selected = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
                "inference_rules": ["parameter_consistency"],
            },
        )
        assert selected.status_code == 200
        assert selected.json()["inference"]["available_count"] == 0

        unknown_rule = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
                "inference_rules": ["missing_rule"],
            },
        )
        assert unknown_rule.status_code == 422

        default_budget = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
            },
        )
        assert default_budget.status_code == 200
        filtered = default_budget.json()
        assert len(filtered["facts"]) == 3
        assert all(item["subject"] != "汽轮机" for item in filtered["facts"])

        rejected_query = client.post(
            "/api/agent/context",
            json={
                "ontology_id": "agent_regulation",
                "version": "0.1.0",
                "question": "给水泵有哪些参数？",
                "query": "SELECT * WHERE { ?s ?p ?o }",
            },
        )
        assert rejected_query.status_code == 422


def test_web_owl_import_and_runs_flow(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    run, run_dir, manifest = run_pipeline(
        [PROJECT_ROOT / "samples"],
        workspace,
        run_id="web_pipeline",
        auto_approve=True,
        base_iri=BASE_IRI,
        ontology_id="web_pipeline_regulation",
        version="0.2.0",
    )
    assert manifest["validation"]["status"] == "valid"

    app = create_app(workspace, graphdb_client=FakeGraphDB())
    with TestClient(app) as client:
        imported = client.post(
            "/api/ontologies/import",
            data={
                "ontology_id": "imported_web",
                "version": "1.0.0",
                "base_iri": "https://example.test/imported/",
                "title": "导入 Web 本体",
                "description": "multipart 测试",
            },
            files={
                "file": (
                    "imported.ttl",
                    (
                        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                        b"<https://example.test/imported> a owl:Ontology .\n"
                    ),
                    "text/turtle",
                )
            },
        )
        assert imported.status_code == 201
        assert imported.json()["source_kind"] == "imported"

        runs = client.get("/api/runs")
        assert runs.status_code == 200
        assert runs.json()["items"][0]["run_id"] == run.run_id

        dashboard = client.get("/api/dashboard")
        assert dashboard.status_code == 200
        assert dashboard.json()["counts"]["runs"] == 1
        assert dashboard.json()["recent_runs"][0]["run_id"] == run.run_id
        assert dashboard.json()["recent_runs"][0]["status"] == "completed"

        facts = client.get("/api/runs/web_pipeline/facts?status=approved&limit=5")
        assert facts.status_code == 200
        assert facts.json()["total"] > 0
        assert all(item["review_status"] == "approved" for item in facts.json()["items"])

        exported = client.get("/api/runs/web_pipeline/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"

        registered = client.post("/api/runs/web_pipeline/register")
        assert registered.status_code == 201
        assert registered.json()["ontology_id"] == "web_pipeline_regulation"
        assert (run_dir / "ontology.ttl").exists()

        duplicate = client.post("/api/runs/web_pipeline/register")
        assert duplicate.status_code == 409


def test_async_build_progress_logs_retry_and_delete(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    sample = (PROJECT_ROOT / "samples" / "unstructured" / "运行规程样例.txt").read_bytes()
    app = create_app(workspace, graphdb_client=FakeGraphDB())
    with TestClient(app) as client:
        built = client.post(
            "/api/builds",
            data={
                "run_id": "async_web_build",
                "ontology_id": "async_web_regulation",
                "version": "0.1.0",
                "base_iri": BASE_IRI,
                "extraction_mode": "rule",
                "auto_approve": "true",
            },
            files={"files": ("regulation.txt", sample, "text/plain")},
        )
        assert built.status_code == 202
        run_id = built.json()["run_id"]
        progress = _wait_for_run(client, run_id)
        summary = progress["summary"]
        assert summary["status"] == "completed"
        assert summary["progress"] == 100
        assert summary["stage_label"] == "构建完成"
        assert progress["logs"]
        assert summary["counts"]["facts_total"] > 0

        run_dir = workspace.root / "runs" / run_id
        run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_record["updated_at"]
        assert (run_dir / "task_events.jsonl").exists()
        source_upload_dir = Path(run_record["source_upload_dir"])
        assert source_upload_dir.exists()

        duplicate = client.post(
            "/api/builds",
            data={
                "run_id": run_id,
                "ontology_id": "async_web_regulation",
                "version": "0.1.0",
                "base_iri": BASE_IRI,
                "extraction_mode": "rule",
            },
            files={"files": ("duplicate.txt", sample, "text/plain")},
        )
        assert duplicate.status_code == 409

        retried = client.post(f"/api/runs/{run_id}/retry")
        assert retried.status_code == 202
        assert retried.json()["parent_run_id"] == run_id
        retry_id = retried.json()["run_id"]
        assert retry_id != run_id
        assert _wait_for_run(client, retry_id)["summary"]["status"] == "completed"

        retry_record = json.loads(
            (workspace.root / "runs" / retry_id / "run.json").read_text(encoding="utf-8")
        )
        assert retry_record["parent_run_id"] == run_id
        assert Path(retry_record["source_upload_dir"]) == source_upload_dir

        assert client.delete(f"/api/runs/{run_id}").status_code == 204
        assert source_upload_dir.exists()
        assert client.delete(f"/api/runs/{retry_id}").status_code == 204
        assert not source_upload_dir.exists()


def test_cancel_active_run_and_delete_guard(tmp_path, monkeypatch):
    def slow_execute_pipeline(_run_dir, _workspace, **kwargs):
        progress_callback = kwargs["progress_callback"]
        cancel_check = kwargs["cancel_check"]
        progress_callback(
            {
                "stage": "extraction",
                "stage_label": "等待取消",
                "progress": 40,
                "current": 1,
                "total": 2,
            }
        )
        while not cancel_check():
            time.sleep(0.02)
        raise PipelineCancelled("构建任务已取消。")

    monkeypatch.setattr(web_module, "execute_pipeline", slow_execute_pipeline)
    app = create_app(tmp_path / "workspace", graphdb_client=FakeGraphDB())
    with TestClient(app) as client:
        built = client.post(
            "/api/builds",
            data={
                "ontology_id": "cancel_web_regulation",
                "version": "0.1.0",
                "base_iri": BASE_IRI,
                "extraction_mode": "rule",
            },
            files={"files": ("cancel.txt", "第一条 测试取消任务。".encode(), "text/plain")},
        )
        assert built.status_code == 202
        run_id = built.json()["run_id"]

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f"/api/runs/{run_id}/progress").json()["summary"]
            if current["status"] == "running" and current["progress"] == 40:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("run did not enter the controllable stage")

        blocked = client.delete(f"/api/runs/{run_id}")
        assert blocked.status_code == 409

        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        payload = _wait_for_run(client, run_id)
        assert payload["summary"]["status"] == "cancelled"
        assert payload["summary"]["cancel_requested"] is True
        assert any("取消" in item["event"] for item in payload["logs"])
        assert client.delete(f"/api/runs/{run_id}").status_code == 204


def test_startup_marks_orphaned_run_interrupted(tmp_path):
    workspace = Workspace(tmp_path / "workspace").ensure()
    input_file = tmp_path / "orphaned.txt"
    input_file.write_text("第一条 用于验证服务重启恢复。", encoding="utf-8")
    first_app = create_app(workspace, graphdb_client=FakeGraphDB())
    run, _ = create_run([input_file], workspace, run_id="orphaned_web_run")
    first_app.state.tasks.update(
        run.run_id,
        status="running",
        stage="parse",
        stage_label="解析文档",
        progress=25,
        event="模拟服务退出前的运行状态。",
    )

    restarted_app = create_app(workspace, graphdb_client=FakeGraphDB())
    with TestClient(restarted_app) as client:
        payload = client.get(f"/api/runs/{run.run_id}").json()
        summary = payload["summary"]
        assert summary["status"] == "interrupted"
        assert summary["stage_label"] == "服务重启，任务中断"
        assert any("服务重启" in item for item in summary["errors"])
        assert any("服务重启" in item["event"] for item in payload["logs"])
