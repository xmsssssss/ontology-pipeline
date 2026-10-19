from __future__ import annotations

import json

import httpx
import pytest

from kb.graphdb import GraphDBClient, GraphDBError, GraphDBSettings


def _client(handler) -> GraphDBClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url="http://graphdb.test", transport=transport)
    return GraphDBClient(
        GraphDBSettings(
            base_url="http://graphdb.test",
            repository="regulation-test",
        ),
        client=http_client,
    )


def test_graphdb_status_and_publish_requests():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/rest/repositories":
            return httpx.Response(
                200,
                json=[
                    {"id": "regulation-test", "title": "Test", "type": "graphdb"},
                    {"id": "other", "title": "Other", "type": "graphdb"},
                ],
            )
        if request.method == "PUT" and request.url.path.endswith("/rdf-graphs/service"):
            return httpx.Response(204)
        return httpx.Response(404, text="unexpected request")

    client = _client(handler)
    try:
        status = client.status()
        assert status["connected"] is True
        assert status["repository_exists"] is True
        assert [item["id"] for item in status["repositories"]] == ["regulation-test", "other"]

        result = client.publish_graph(
            graph_iri="https://example.test/graph/regulation/1",
            content="<https://example.test/s> <https://example.test/p> <https://example.test/o> .",
            shapes_graph_iri="https://example.test/graph/regulation/1/shapes",
            shapes_content="<https://example.test/shape> a <https://example.test/Shape> .",
        )
    finally:
        client.close()

    assert result["published"] is True
    assert result["shapes_published"] is True
    graph_requests = [request for request in requests if request.method == "PUT"]
    assert len(graph_requests) == 2
    assert graph_requests[0].url.params["graph"].endswith("/regulation/1")
    assert graph_requests[1].url.params["graph"].endswith("/regulation/1/shapes")
    assert graph_requests[0].headers["content-type"] == "text/turtle"


def test_graphdb_select_ask_and_delete_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = request.content.decode("utf-8")
            if "ASK" in body:
                return httpx.Response(200, json={"head": {}, "boolean": True})
            return httpx.Response(
                200,
                json={
                    "head": {"vars": ["label", "iri"]},
                    "results": {
                        "bindings": [
                            {
                                "label": {"type": "literal", "value": "给水泵"},
                                "iri": {
                                    "type": "uri",
                                    "value": "https://example.test/Equipment/Pump",
                                },
                            }
                        ]
                    },
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    client = _client(handler)
    try:
        selected = client.query(
            "SELECT ?label ?iri WHERE { ?iri <https://example.test/label> ?label . }",
            graph_iri="https://example.test/graph/regulation/1",
        )
        asked = client.query("ASK { ?s ?p ?o }")
        client.delete_graph("https://example.test/graph/regulation/1")
    finally:
        client.close()

    assert selected == {
        "type": "SELECT",
        "columns": ["label", "iri"],
        "rows": [
            {
                "label": "给水泵",
                "iri": "https://example.test/Equipment/Pump",
            }
        ],
        "count": 1,
    }
    assert asked == {"type": "ASK", "boolean": True}


def test_graphdb_status_returns_disconnected_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    try:
        status = client.status()
    finally:
        client.close()

    assert status["connected"] is False
    assert status["repository_exists"] is False
    assert "无法连接 GraphDB" in status["error"]


def test_graphdb_query_rejects_error_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=json.dumps({"message": "Malformed query"}).encode(),
        )

    client = _client(handler)
    try:
        with pytest.raises(GraphDBError, match="HTTP 400"):
            client.query("SELECT WHERE {")
    finally:
        client.close()
