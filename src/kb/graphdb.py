from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class GraphDBError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GraphDBSettings:
    base_url: str = "http://localhost:7200"
    repository: str = "regulation"
    username: str | None = None
    password: str | None = None
    timeout: float = 20.0

    @classmethod
    def from_env(cls) -> GraphDBSettings:
        return cls(
            base_url=os.getenv("GRAPHDB_URL", "http://localhost:7200"),
            repository=os.getenv("GRAPHDB_REPOSITORY", "regulation"),
            username=os.getenv("GRAPHDB_USERNAME"),
            password=os.getenv("GRAPHDB_PASSWORD"),
            timeout=float(os.getenv("GRAPHDB_TIMEOUT", "20")),
        )


def _query_accept(query: str) -> str:
    normalized = query.lstrip().upper()
    if "ASK" in normalized[:200] or "SELECT" in normalized[:200]:
        return "application/sparql-results+json"
    return "text/turtle"


class GraphDBClient:
    def __init__(
        self,
        settings: GraphDBSettings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or GraphDBSettings.from_env()
        self.base_url = self.settings.base_url.rstrip("/")
        self.repository = self.settings.repository
        auth = None
        if self.settings.username:
            auth = (self.settings.username, self.settings.password or "")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            auth=auth,
            timeout=self.settings.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _repository_url(self, suffix: str = "") -> str:
        repository = quote(self.repository, safe="")
        return f"/repositories/{repository}{suffix}"

    def _raise(self, response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        detail = response.text.strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise GraphDBError(
            f"GraphDB {action}失败（HTTP {response.status_code}）：{detail}",
            status_code=response.status_code,
        )

    def list_repositories(self) -> list[dict[str, Any]]:
        try:
            response = self._client.get(
                "/rest/repositories",
                headers={"Accept": "application/json"},
            )
            self._raise(response, "仓库列表读取")
            payload = response.json()
        except httpx.HTTPError as exc:
            raise GraphDBError(f"无法连接 GraphDB：{exc}") from exc
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            repositories = payload.get("repositories", [])
            if isinstance(repositories, list):
                return [item for item in repositories if isinstance(item, dict)]
        return []

    def status(self) -> dict[str, Any]:
        try:
            repositories = self.list_repositories()
        except GraphDBError as exc:
            return {
                "connected": False,
                "base_url": self.base_url,
                "repository": self.repository,
                "repository_exists": False,
                "repositories": [],
                "error": str(exc),
            }
        return {
            "connected": True,
            "base_url": self.base_url,
            "repository": self.repository,
            "repository_exists": any(
                str(item.get("id") or item.get("repositoryID") or "") == self.repository
                for item in repositories
            ),
            "repositories": [
                {
                    "id": item.get("id") or item.get("repositoryID"),
                    "title": item.get("title"),
                    "type": item.get("type"),
                }
                for item in repositories
            ],
            "error": None,
        }

    def _put_graph(self, graph_iri: str, content: str, content_type: str) -> None:
        try:
            response = self._client.put(
                self._repository_url("/rdf-graphs/service"),
                params={"graph": graph_iri},
                content=content.encode("utf-8"),
                headers={"Content-Type": content_type},
            )
            self._raise(response, f"命名图 {graph_iri} 写入")
        except httpx.HTTPError as exc:
            raise GraphDBError(f"GraphDB 写入失败：{exc}") from exc

    def publish_graph(
        self,
        *,
        graph_iri: str,
        content: str,
        shapes_graph_iri: str,
        shapes_content: str = "",
    ) -> dict[str, Any]:
        self._put_graph(graph_iri, content, "text/turtle")
        shapes_written = False
        if shapes_content.strip():
            self._put_graph(shapes_graph_iri, shapes_content, "text/turtle")
            shapes_written = True
        return {
            "published": True,
            "graph_iri": graph_iri,
            "shapes_graph_iri": shapes_graph_iri if shapes_written else None,
            "shapes_published": shapes_written,
        }

    def query(self, query: str, *, graph_iri: str | None = None) -> dict[str, Any]:
        accept = _query_accept(query)
        data: dict[str, str] = {"query": query}
        if graph_iri:
            data["default-graph-uri"] = graph_iri
        try:
            response = self._client.post(
                self._repository_url(),
                data=data,
                headers={"Accept": accept},
            )
            self._raise(response, "SPARQL 查询")
        except httpx.HTTPError as exc:
            raise GraphDBError(f"GraphDB 查询失败：{exc}") from exc
        if accept == "application/sparql-results+json":
            payload = response.json()
            if "boolean" in payload:
                return {"type": "ASK", "boolean": bool(payload["boolean"])}
            columns = [str(item) for item in payload.get("head", {}).get("vars", [])]
            rows: list[dict[str, str]] = []
            for binding in payload.get("results", {}).get("bindings", []):
                rows.append(
                    {column: str(binding.get(column, {}).get("value", "")) for column in columns}
                )
            return {"type": "SELECT", "columns": columns, "rows": rows, "count": len(rows)}
        return {
            "type": "RDF",
            "content_type": response.headers.get("content-type", "text/turtle"),
            "data": response.text,
        }

    def delete_graph(self, graph_iri: str) -> None:
        try:
            response = self._client.delete(
                self._repository_url("/rdf-graphs/service"),
                params={"graph": graph_iri},
            )
            self._raise(response, f"命名图 {graph_iri} 删除")
        except httpx.HTTPError as exc:
            raise GraphDBError(f"GraphDB 删除失败：{exc}") from exc
