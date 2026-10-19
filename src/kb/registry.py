from __future__ import annotations

import hashlib
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from rdflib import DCTERMS, OWL, RDF, RDFS, Graph, Namespace
from rdflib import Literal as RdfLiteral

from kb.ids import safe_local_name
from kb.models import KbModel, utc_now
from kb.storage import Workspace

ONTOLOGY_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,79}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
logger = logging.getLogger(__name__)


class RegistryError(RuntimeError):
    pass


class RegistryNotFound(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


class InvalidOntology(RegistryError):
    pass


class OntologyVersionInfo(KbModel):
    ontology_id: str
    version: str
    title: str
    description: str = ""
    base_iri: str
    status: Literal["draft", "published"] = "draft"
    source_kind: Literal["created", "imported", "pipeline"] = "created"
    source_run_id: str | None = None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None
    graph_iri: str | None = None
    shapes_graph_iri: str | None = None
    graphdb_repository: str | None = None
    graphdb_url: str | None = None
    triple_count: int = 0
    content_sha256: str


def _validate_id(ontology_id: str) -> str:
    if not ONTOLOGY_ID_PATTERN.fullmatch(ontology_id):
        raise RegistryError("本体 ID 必须以字母开头，只能包含字母、数字、点、下划线和连字符。")
    return ontology_id


def _validate_version(version: str) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise RegistryError("版本只能包含字母、数字、点、下划线和连字符。")
    return version


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _guess_format(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    return {
        ".ttl": "turtle",
        ".turtle": "turtle",
        ".owl": "xml",
        ".rdf": "xml",
        ".xml": "xml",
        ".jsonld": "json-ld",
        ".json": "json-ld",
        ".nt": "nt",
        ".n3": "n3",
    }.get(suffix)


def parse_owl(content: bytes, filename: str, *, public_id: str | None = None) -> Graph:
    preferred = _guess_format(filename)
    candidates = [preferred] if preferred else []
    candidates.extend(item for item in ("turtle", "xml", "json-ld", "nt") if item not in candidates)
    errors: list[str] = []
    for rdf_format in candidates:
        if rdf_format is None:
            continue
        graph = Graph()
        try:
            graph.parse(data=content, format=rdf_format, publicID=public_id)
        except Exception as exc:
            errors.append(f"{rdf_format}: {exc}")
            continue
        if len(graph):
            return graph
    detail = "; ".join(errors[-2:]) if errors else "文件不包含可解析的 RDF 三元组。"
    raise InvalidOntology(f"无法解析 OWL/RDF 文件：{detail}")


def _ontology_iri(base_iri: str, ontology_id: str) -> str:
    return f"{base_iri.rstrip('/')}/ontology/{quote(safe_local_name(ontology_id), safe='')}"


def _new_ontology_graph(
    *,
    ontology_id: str,
    version: str,
    base_iri: str,
    title: str,
    description: str,
) -> Graph:
    namespace = Namespace(base_iri.rstrip("/") + "/")
    graph = Graph()
    graph.bind("owl", OWL)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("dcterms", DCTERMS)
    graph.bind("kb", namespace)
    iri = namespace[f"ontology/{safe_local_name(ontology_id)}"]
    graph.add((iri, RDF.type, OWL.Ontology))
    graph.add((iri, RDFS.label, RdfLiteral(title, lang="zh")))
    graph.add((iri, RDFS.comment, RdfLiteral(description, lang="zh")))
    graph.add((iri, DCTERMS.identifier, RdfLiteral(ontology_id)))
    graph.add((iri, OWL.versionInfo, RdfLiteral(version)))
    return graph


def sparql_result(result: Any) -> dict[str, Any]:
    result_type = str(getattr(result, "type", "UNKNOWN"))
    if result_type == "ASK":
        return {"type": "ASK", "boolean": bool(result.askAnswer)}
    if result_type == "SELECT":
        columns = [str(item) for item in (result.vars or [])]
        rows = [
            {column: ("" if row[column] is None else str(row[column])) for column in columns}
            for row in result
        ]
        return {"type": "SELECT", "columns": columns, "rows": rows, "count": len(rows)}
    if result_type in {"CONSTRUCT", "DESCRIBE"}:
        graph = result.graph
        return {
            "type": result_type,
            "content_type": "text/turtle",
            "data": graph.serialize(format="turtle"),
            "count": len(graph),
        }
    return {"type": result_type, "value": str(result)}


class OntologyRegistry:
    def __init__(self, workspace: Workspace | str | Path) -> None:
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.workspace.ensure()
        self.root = self.workspace.root / "ontology"

    def _version_dir(self, ontology_id: str, version: str) -> Path:
        return self.root / _validate_id(ontology_id) / _validate_version(version)

    def _metadata_path(self, ontology_id: str, version: str) -> Path:
        return self._version_dir(ontology_id, version) / "metadata.json"

    def _load(self, ontology_id: str, version: str) -> OntologyVersionInfo:
        path = self._metadata_path(ontology_id, version)
        if not path.exists():
            raise RegistryNotFound(f"本体版本不存在：{ontology_id}/{version}")
        return OntologyVersionInfo.model_validate(self.workspace.read_json(path))

    def _save(self, info: OntologyVersionInfo) -> OntologyVersionInfo:
        directory = self._version_dir(info.ontology_id, info.version)
        directory.mkdir(parents=True, exist_ok=True)
        self.workspace.write_json(directory / "metadata.json", info)
        return info

    def _write_graph(
        self,
        directory: Path,
        graph: Graph,
        info: OntologyVersionInfo,
    ) -> OntologyVersionInfo:
        content = graph.serialize(format="turtle").encode("utf-8")
        self.workspace.atomic_write_text(directory / "ontology.ttl", content.decode("utf-8"))
        updated = info.model_copy(
            update={
                "triple_count": len(graph),
                "content_sha256": _digest(content),
                "updated_at": utc_now(),
            }
        )
        return self._save(updated)

    def list_versions(self, ontology_id: str | None = None) -> list[OntologyVersionInfo]:
        if ontology_id is not None:
            _validate_id(ontology_id)
        values: list[OntologyVersionInfo] = []
        if not self.root.exists():
            return values
        for path in self.root.glob("*/*/metadata.json"):
            if ontology_id is not None and path.parent.parent.name != ontology_id:
                continue
            try:
                values.append(OntologyVersionInfo.model_validate(self.workspace.read_json(path)))
            except Exception as exc:
                logger.warning("忽略损坏的本体元数据 %s：%s", path, exc)
                continue
        return sorted(values, key=lambda item: item.updated_at, reverse=True)

    def list_ontologies(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[OntologyVersionInfo]] = {}
        for item in self.list_versions():
            grouped.setdefault(item.ontology_id, []).append(item)
        result: list[dict[str, Any]] = []
        for ontology_id, versions in grouped.items():
            versions.sort(key=lambda item: item.updated_at, reverse=True)
            latest = versions[0]
            result.append(
                {
                    "ontology_id": ontology_id,
                    "title": latest.title,
                    "description": latest.description,
                    "base_iri": latest.base_iri,
                    "versions": [item.model_dump(mode="json") for item in versions],
                    "version_count": len(versions),
                    "published_count": len(
                        [item for item in versions if item.status == "published"]
                    ),
                    "triple_count": max(item.triple_count for item in versions),
                    "updated_at": latest.updated_at.isoformat(),
                }
            )
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def get(self, ontology_id: str, version: str) -> tuple[OntologyVersionInfo, str]:
        info = self._load(ontology_id, version)
        path = self._version_dir(ontology_id, version) / "ontology.ttl"
        if not path.exists():
            raise RegistryNotFound(f"本体文件不存在：{ontology_id}/{version}")
        return info, path.read_text(encoding="utf-8")

    def create(
        self,
        *,
        ontology_id: str,
        version: str,
        base_iri: str,
        title: str,
        description: str = "",
    ) -> OntologyVersionInfo:
        _validate_id(ontology_id)
        _validate_version(version)
        directory = self._version_dir(ontology_id, version)
        if directory.exists():
            raise RegistryConflict(f"本体版本已存在：{ontology_id}/{version}")
        if not base_iri.startswith(("http://", "https://")):
            raise RegistryError("base_iri 必须是 http:// 或 https:// 地址。")
        graph = _new_ontology_graph(
            ontology_id=ontology_id,
            version=version,
            base_iri=base_iri,
            title=title.strip() or ontology_id,
            description=description.strip(),
        )
        now = utc_now()
        info = OntologyVersionInfo(
            ontology_id=ontology_id,
            version=version,
            title=title.strip() or ontology_id,
            description=description.strip(),
            base_iri=base_iri.rstrip("/") + "/",
            source_kind="created",
            created_at=now,
            updated_at=now,
            triple_count=len(graph),
            content_sha256="",
        )
        directory.mkdir(parents=True, exist_ok=False)
        return self._write_graph(directory, graph, info)

    def import_owl(
        self,
        *,
        content: bytes,
        filename: str,
        ontology_id: str,
        version: str,
        base_iri: str,
        title: str,
        description: str = "",
    ) -> OntologyVersionInfo:
        _validate_id(ontology_id)
        _validate_version(version)
        directory = self._version_dir(ontology_id, version)
        if directory.exists():
            raise RegistryConflict(f"本体版本已存在：{ontology_id}/{version}")
        graph = parse_owl(content, filename, public_id=base_iri)
        now = utc_now()
        info = OntologyVersionInfo(
            ontology_id=ontology_id,
            version=version,
            title=title.strip() or Path(filename).stem,
            description=description.strip(),
            base_iri=base_iri.rstrip("/") + "/",
            source_kind="imported",
            created_at=now,
            updated_at=now,
            triple_count=len(graph),
            content_sha256="",
        )
        directory.mkdir(parents=True, exist_ok=False)
        import_dir = directory / "imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name) or "imported.owl"
        (import_dir / safe_name).write_bytes(content)
        return self._write_graph(directory, graph, info)

    def update_turtle(self, ontology_id: str, version: str, content: str) -> OntologyVersionInfo:
        info = self._load(ontology_id, version)
        if info.status == "published":
            raise RegistryConflict("已发布版本不可修改，请创建新版本。")
        graph = Graph()
        try:
            graph.parse(data=content, format="turtle", publicID=info.base_iri)
        except Exception as exc:
            raise InvalidOntology(f"Turtle 解析失败：{exc}") from exc
        if not len(graph):
            raise InvalidOntology("本体不能为空。")
        directory = self._version_dir(ontology_id, version)
        self.workspace.atomic_write_text(directory / "ontology.ttl", content)
        updated = info.model_copy(
            update={
                "triple_count": len(graph),
                "content_sha256": _digest(content.encode("utf-8")),
                "updated_at": utc_now(),
            }
        )
        return self._save(updated)

    def query(self, ontology_id: str, version: str, query: str) -> dict[str, Any]:
        _, content = self.get(ontology_id, version)
        graph = Graph()
        graph.parse(data=content, format="turtle")
        try:
            result = graph.query(query)
        except Exception as exc:
            raise InvalidOntology(f"SPARQL 查询失败：{exc}") from exc
        return sparql_result(result)

    def export_turtle(self, ontology_id: str, version: str) -> tuple[str, bytes]:
        _, content = self.get(ontology_id, version)
        filename = f"{ontology_id}-{version}.ttl"
        return filename, content.encode("utf-8")

    def publish(
        self,
        ontology_id: str,
        version: str,
        *,
        graphdb_client: Any,
    ) -> dict[str, Any]:
        info, content = self.get(ontology_id, version)
        graph_iri = f"{info.base_iri.rstrip('/')}/graph/{ontology_id}/{version}"
        shapes_graph_iri = f"{graph_iri}/shapes"
        directory = self._version_dir(ontology_id, version)
        shapes_path = directory / "shapes.ttl"
        shapes = shapes_path.read_text(encoding="utf-8") if shapes_path.exists() else ""
        result = graphdb_client.publish_graph(
            graph_iri=graph_iri,
            content=content,
            shapes_graph_iri=shapes_graph_iri,
            shapes_content=shapes,
        )
        published_at = utc_now()
        updated = info.model_copy(
            update={
                "status": "published",
                "published_at": published_at,
                "updated_at": published_at,
                "graph_iri": graph_iri,
                "shapes_graph_iri": shapes_graph_iri,
                "graphdb_repository": graphdb_client.repository,
                "graphdb_url": graphdb_client.base_url,
            }
        )
        self._save(updated)
        self.workspace.write_json(
            directory / "publication.json",
            {
                **result,
                "ontology_id": ontology_id,
                "version": version,
                "graph_iri": graph_iri,
                "shapes_graph_iri": shapes_graph_iri,
                "repository": graphdb_client.repository,
                "published_at": published_at.isoformat(),
            },
        )
        return {
            **result,
            "ontology_id": ontology_id,
            "version": version,
            "graph_iri": graph_iri,
            "shapes_graph_iri": shapes_graph_iri,
            "repository": graphdb_client.repository,
            "published_at": published_at.isoformat(),
        }

    def register_run(self, run_dir: str | Path) -> OntologyVersionInfo:
        directory = Path(run_dir).resolve()
        run_path = directory / "run.json"
        ontology_path = directory / "ontology.ttl"
        if not run_path.exists() or not ontology_path.exists():
            raise RegistryNotFound("运行目录缺少 run.json 或 ontology.ttl。")
        run = self.workspace.read_json(run_path)
        ontology_meta = run.get("ontology") if isinstance(run.get("ontology"), dict) else {}
        ontology_id = str(ontology_meta.get("id") or "regulation")
        version = str(ontology_meta.get("version") or "0.1.0")
        base_iri = str(ontology_meta.get("base_iri") or "https://example.org/regulation/")
        target = self._version_dir(ontology_id, version)
        if target.exists():
            raise RegistryConflict(f"本体版本已存在：{ontology_id}/{version}")
        content = ontology_path.read_text(encoding="utf-8")
        graph = Graph()
        graph.parse(data=content, format="turtle", publicID=base_iri)
        target.mkdir(parents=True, exist_ok=False)
        for name in (
            "ontology.ttl",
            "shapes.ttl",
            "mapping.yaml",
            "manifest.json",
            "validation_report.json",
            "facts.jsonl",
            "provenance.jsonl",
        ):
            source = directory / name
            if source.exists():
                shutil.copy2(source, target / name)
        now = utc_now()
        info = OntologyVersionInfo(
            ontology_id=ontology_id,
            version=version,
            title=f"{ontology_id} {version}",
            description=f"由运行 {run.get('run_id', directory.name)} 构建。",
            base_iri=base_iri.rstrip("/") + "/",
            source_kind="pipeline",
            source_run_id=str(run.get("run_id") or directory.name),
            created_at=now,
            updated_at=now,
            triple_count=len(graph),
            content_sha256=_digest(content.encode("utf-8")),
        )
        return self._save(info)

    def delete_version(self, ontology_id: str, version: str) -> None:
        info = self._load(ontology_id, version)
        if info.status == "published":
            raise RegistryConflict("已发布版本不可删除。")
        shutil.rmtree(self._version_dir(ontology_id, version))

    def delete_ontology(self, ontology_id: str) -> None:
        _validate_id(ontology_id)
        ontology_dir = self.root / ontology_id
        if not ontology_dir.exists():
            raise RegistryNotFound(f"本体不存在：{ontology_id}")
        for info in self.list_versions(ontology_id):
            if info.status == "published":
                raise RegistryConflict("本体包含已发布版本，不能整体删除。")
        shutil.rmtree(ontology_dir)
