from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nr_version_mcp.client import ProductVersionClient, VersionApiError
from nr_version_mcp.config import Settings

COOKIE = "sessionid=test-session"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8000/api/v1",
        "session_cookie": COOKIE,
    }
    values.update(overrides)
    return Settings(**values)


def _client(handler: Any, **overrides: Any) -> ProductVersionClient:
    return ProductVersionClient(_settings(**overrides), transport=httpx.MockTransport(handler))


def _page(
    results: list[dict[str, Any]], *, count: int | None = None, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    return {
        "count": len(results) if count is None else count,
        "page": page,
        "page_size": page_size,
        "results": results,
    }


def _item(version_id: str, *, status: str = "RELEASED", updated_at: str = "2026-06-02T08:00:00Z"):
    return {
        "id": version_id,
        "product_name": "NR 测试套件",
        "version": "v1.2.0",
        "status": status,
        "release_date": "2026-06-01",
        "description": "稳定版本",
        "is_default": True,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2026-05-01T08:00:00Z",
        "updated_at": updated_at,
    }


def test_search_uses_default_status_and_forwards_pagination() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_page([_item("11111111-1111-4111-8111-111111111111")]))

    with _client(handler) as client:
        result = client.search(keyword="1.2", product_name="NR 测试套件", page=2, page_size=10)

    request = captured[0]
    query = parse_qs(urlparse(str(request.url)).query)
    assert request.headers["Cookie"] == COOKIE
    assert request.url.path == "/api/v1/system/versions/"
    assert query == {
        "search": ["1.2"],
        "product_name": ["NR 测试套件"],
        "status": ["RELEASED"],
        "page": ["2"],
        "page_size": ["10"],
    }
    assert result["count"] == 1
    assert result["applied_statuses"] == ["RELEASED"]
    assert result["client_side_filtering"] is False


def test_explicit_status_overrides_default() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_page([]))

    with _client(handler) as client:
        client.search(statuses=["ARCHIVED"])

    query = parse_qs(urlparse(str(captured[0].url)).query)
    assert query["status"] == ["ARCHIVED"]


def test_multiple_statuses_are_aggregated_and_paginated_locally() -> None:
    captured: list[str] = []
    all_items = [
        _item("11111111-1111-4111-8111-111111111111", status="RELEASED"),
        _item("33333333-3333-4333-8333-333333333333", status="DRAFT"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        captured.append(query["status"][0])
        status = query["status"][0]
        results = [item for item in all_items if item["status"] == status]
        return httpx.Response(200, json=_page(results))

    with _client(handler) as client:
        result = client.search(statuses=["RELEASED", "DRAFT"], page=1, page_size=1)

    assert captured == ["RELEASED", "DRAFT"]
    assert result["count"] == 2
    assert result["page"] == 1
    assert result["page_size"] == 1
    assert len(result["results"]) == 1
    assert result["client_side_filtering"] is True
    assert result["applied_statuses"] == ["RELEASED", "DRAFT"]


def test_date_filter_is_applied_client_side() -> None:
    items = [
        _item("11111111-1111-4111-8111-111111111111"),
        {**_item("22222222-2222-4222-8222-222222222222"), "release_date": "2025-01-01"},
        {**_item("33333333-3333-4333-8333-333333333333"), "release_date": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(items))

    with _client(handler) as client:
        result = client.search(release_date_from="2026-01-01", release_date_to="2026-12-31")

    assert result["client_side_filtering"] is True
    assert result["count"] == 1
    assert result["results"][0]["id"] == "11111111-1111-4111-8111-111111111111"
    assert result["applied_release_date"] == {"from": "2026-01-01", "to": "2026-12-31"}


def test_get_returns_full_version_detail() -> None:
    version = _item("11111111-1111-4111-8111-111111111111")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=version)

    with _client(handler) as client:
        result = client.get("11111111-1111-4111-8111-111111111111")

    assert captured[0].url.path == ("/api/v1/system/versions/11111111-1111-4111-8111-111111111111/")
    assert result["description"] == "稳定版本"
    assert result["status"] == "RELEASED"


def test_unauthorized_maps_to_explicit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthenticated", "message": "请先登录"})

    with _client(handler) as client:
        with pytest.raises(VersionApiError) as excinfo:
            client.search()

    assert excinfo.value.code == "unauthenticated"
    assert excinfo.value.status_code == 401
    assert COOKIE not in excinfo.value.as_text()


def test_not_found_detail_maps_to_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "not_found", "message": "请求的资源不存在"})

    with _client(handler) as client:
        with pytest.raises(VersionApiError) as excinfo:
            client.get("11111111-1111-4111-8111-111111111111")

    assert excinfo.value.code == "not_found"


def test_connection_failure_maps_to_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _client(handler) as client:
        with pytest.raises(VersionApiError) as excinfo:
            client.search()

    assert excinfo.value.code == "connection_error"
    assert "127.0.0.1:8000" in excinfo.value.message


def test_invalid_arguments_are_rejected_before_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发出请求")

    with _client(handler) as client:
        with pytest.raises(VersionApiError, match="page_size"):
            client.search(page_size=101)
        with pytest.raises(VersionApiError, match="release_date_from"):
            client.search(release_date_from="2026-13-01")
        with pytest.raises(VersionApiError, match="未知版本状态"):
            client.search(statuses=["UNKNOWN"])
        with pytest.raises(VersionApiError, match="version_id"):
            client.get("not-a-uuid")


def test_invalid_response_shape_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    with _client(handler) as client:
        with pytest.raises(VersionApiError) as excinfo:
            client.search()

    assert excinfo.value.code == "invalid_response"


def test_multiple_status_fetch_guard_raises_when_too_large() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = parse_qs(urlparse(str(request.url)).query)["page"][0]
        return httpx.Response(
            200,
            json=_page(
                [_item(f"11111111-1111-4111-8111-{page.zfill(12)}")],
                count=1000,
                page=int(page),
                page_size=100,
            ),
        )

    with _client(handler, max_fetch_pages=2) as client:
        with pytest.raises(VersionApiError) as excinfo:
            client.search(statuses=["RELEASED", "DRAFT"])

    assert excinfo.value.code == "result_too_large"


def test_response_payload_is_not_logged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(_page([])).encode())

    with _client(handler) as client:
        result = client.search()

    assert result["count"] == 0
