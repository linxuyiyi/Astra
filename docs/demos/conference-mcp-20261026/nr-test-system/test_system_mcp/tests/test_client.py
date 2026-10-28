import httpx
import pytest

from nr_version_mcp.client import VersionApiClient
from nr_version_mcp.errors import ApiError

VERSION_ID = "11111111-1111-4111-8111-111111111111"

PAGE_PAYLOAD = {
    "count": 1,
    "page": 2,
    "page_size": 10,
    "results": [
        {
            "id": VERSION_ID,
            "product_name": "NR 测试套件",
            "version": "v1.2.0",
            "status": "RELEASED",
        }
    ],
}


def make_client(handler):
    return VersionApiClient(
        base_url="http://api.test/api/v1",
        session_cookie="sessionid=test-session",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def test_list_versions_forwards_search_filter_and_pagination():
    captured = {}

    def handler(request):
        captured["url"] = request.url
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json=PAGE_PAYLOAD)

    result = make_client(handler).list_versions(
        search="NR",
        product_name="NR 测试套件",
        status="RELEASED",
        page=2,
        page_size=10,
    )

    assert result == PAGE_PAYLOAD
    assert captured["url"].path == "/api/v1/system/versions/"
    assert captured["url"].params["search"] == "NR"
    assert captured["url"].params["product_name"] == "NR 测试套件"
    assert captured["url"].params["status"] == "RELEASED"
    assert captured["url"].params["page"] == "2"
    assert captured["url"].params["page_size"] == "10"
    assert captured["cookie"] == "sessionid=test-session"


def test_list_versions_omits_empty_optional_params():
    captured = {}

    def handler(request):
        captured["url"] = request.url
        return httpx.Response(200, json=PAGE_PAYLOAD)

    make_client(handler).list_versions(search="", product_name="", status="", page=1, page_size=20)

    assert "search" not in captured["url"].params
    assert "product_name" not in captured["url"].params
    assert "status" not in captured["url"].params
    assert captured["url"].params["page"] == "1"
    assert captured["url"].params["page_size"] == "20"


def test_get_version_uses_version_identifier_path():
    captured = {}

    def handler(request):
        captured["url"] = request.url
        return httpx.Response(200, json={"id": VERSION_ID, "product_name": "NR"})

    result = make_client(handler).get_version(VERSION_ID)

    assert result["id"] == VERSION_ID
    assert captured["url"].path == f"/api/v1/system/versions/{VERSION_ID}/"


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (400, "validation_error"),
        (401, "unauthenticated"),
        (403, "forbidden"),
        (404, "not_found"),
        (429, "upstream_rate_limited"),
        (500, "upstream_error"),
    ],
)
def test_http_errors_are_mapped_to_stable_codes(status_code, expected_code):
    def handler(request):
        return httpx.Response(
            status_code,
            json={"code": "upstream", "message": "upstream failure", "details": {"field": "x"}},
        )

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).get_version(VERSION_ID)

    error = exc_info.value
    assert error.code == expected_code
    assert error.status == status_code
    assert error.message
    assert "test-session" not in str(error)


def test_list_404_uses_page_or_filter_specific_message():
    def handler(request):
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=99, page_size=20)

    assert exc_info.value.code == "not_found"
    assert "页码" in exc_info.value.message


def test_detail_404_uses_version_specific_message():
    def handler(request):
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).get_version(VERSION_ID)

    assert exc_info.value.code == "not_found"
    assert "产品版本不存在" in exc_info.value.message


def test_validation_error_keeps_upstream_details():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "code": "validation_error",
                "message": "请求参数校验失败",
                "details": {"page": ["页码无效"]},
            },
        )

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=1, page_size=20)

    assert exc_info.value.details == {"page": ["页码无效"]}


def test_timeout_is_mapped_to_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=1, page_size=20)

    assert exc_info.value.code == "upstream_timeout"


def test_connection_error_is_mapped_to_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=1, page_size=20)

    assert exc_info.value.code == "upstream_unavailable"


def test_invalid_json_is_mapped_to_upstream_invalid_response():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=1, page_size=20)

    assert exc_info.value.code == "upstream_invalid_response"


def test_list_payload_missing_pagination_fields_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"count": 0, "results": []})

    with pytest.raises(ApiError) as exc_info:
        make_client(handler).list_versions(page=1, page_size=20)

    assert exc_info.value.code == "upstream_invalid_response"


def test_repr_does_not_expose_session_cookie():
    client = make_client(lambda request: httpx.Response(200, json=PAGE_PAYLOAD))

    assert "test-session" not in repr(client)
