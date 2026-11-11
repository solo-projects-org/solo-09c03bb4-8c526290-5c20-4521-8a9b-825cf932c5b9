"""FastAPI 层测试：状态码、响应结构、Swagger 示例与参数校验。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import EX_HALL_CONFLICT, EX_RANGE, EX_TOTAL_CONFLICT

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "suppression-check"}


def test_feasible_endpoint():
    resp = client.post("/api/v1/suppression-check", json=EX_RANGE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "feasible"
    assert body["hidden_count"] == 6
    assert body["recoverable_count"] == 0
    assert len(body["cells"]) == 6
    assert len(body["minimizer_matrix"]) == 3
    assert set(body["cells"][0]) == {"row", "col", "min", "max", "recoverable"}


def test_total_conflict_returns_409_without_ranges():
    resp = client.post("/api/v1/suppression-check", json=EX_TOTAL_CONFLICT)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["reasons"]
    assert "witness" in body
    # 无解响应不得携带任何区间信息
    assert "cells" not in body
    assert "min" not in str(body)


def test_hall_conflict_returns_409_with_witness():
    resp = client.post("/api/v1/suppression-check", json=EX_HALL_CONFLICT)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["witness"]["max_flow"] < body["witness"]["required_flow"]


def test_bad_shape_422():
    # 13 行超出 12x12 限制
    resp = client.post(
        "/api/v1/suppression-check",
        json={
            "matrix": [[0]] * 13,
            "row_totals": [0] * 13,
            "col_totals": [0],
        },
    )
    assert resp.status_code == 422

    # 行长度不一致
    resp = client.post(
        "/api/v1/suppression-check",
        json={"matrix": [[1, 2], [3]], "row_totals": [3, 3], "col_totals": [4, 2]},
    )
    assert resp.status_code == 422

    # 合计长度不匹配
    resp = client.post(
        "/api/v1/suppression-check",
        json={"matrix": [[1, None]], "row_totals": [1], "col_totals": [1]},
    )
    assert resp.status_code == 422

    # 负数 / 浮点 / 布尔
    for bad_value in (-1, 1.5, True):
        resp = client.post(
            "/api/v1/suppression-check",
            json={
                "matrix": [[bad_value, 0]],
                "row_totals": [1],
                "col_totals": [1, 0],
            },
        )
        assert resp.status_code == 422


def test_openapi_contains_examples():
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/suppression-check"]["post"]["requestBody"]
    examples = body["content"]["application/json"]["examples"]
    summaries = {e["summary"] for e in examples.values()}
    assert any("区间" in s for s in summaries)
    assert any("可还原" in s for s in summaries)
    assert any("冲突" in s for s in summaries)
    # Swagger UI 可访问
    assert client.get("/docs").status_code == 200


def test_method_endpoint():
    resp = client.get("/method")
    assert resp.status_code == 200
    assert "全幺模" in resp.json()["model"]
