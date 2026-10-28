import pytest

from nr_version_mcp.errors import ToolInputError
from nr_version_mcp.service import VersionQueryService, validate_version_id


class FakeApiClient:
    def __init__(self):
        self.list_calls = []
        self.detail_calls = []

    def list_versions(self, **kwargs):
        self.list_calls.append(kwargs)
        return {
            "count": 0,
            "page": kwargs["page"],
            "page_size": kwargs["page_size"],
            "results": [],
        }

    def get_version(self, version_id):
        self.detail_calls.append(version_id)
        return {"id": version_id, "product_name": "NR"}


def test_list_versions_uses_defaults_and_passes_filters():
    api = FakeApiClient()
    service = VersionQueryService(api)

    result = service.list_versions(
        search="NR",
        product_name="NR 套件",
        status="RELEASED",
        page=2,
        page_size=50,
    )

    assert api.list_calls == [
        {
            "search": "NR",
            "product_name": "NR 套件",
            "status": "RELEASED",
            "page": 2,
            "page_size": 50,
        }
    ]
    assert result["page"] == 2


def test_list_versions_defaults_are_applied():
    api = FakeApiClient()
    service = VersionQueryService(api)

    service.list_versions()

    assert api.list_calls == [
        {"search": None, "product_name": None, "status": None, "page": 1, "page_size": 20}
    ]


@pytest.mark.parametrize("page", [0, -1, 1.5, True, "1"])
def test_invalid_page_is_rejected_before_upstream_call(page):
    api = FakeApiClient()
    service = VersionQueryService(api)

    with pytest.raises(ToolInputError) as exc_info:
        service.list_versions(page=page)

    assert exc_info.value.code == "validation_error"
    assert "page" in exc_info.value.details
    assert api.list_calls == []


@pytest.mark.parametrize("page_size", [0, -5, 101, 20.5, False, "20"])
def test_invalid_page_size_is_rejected_before_upstream_call(page_size):
    api = FakeApiClient()
    service = VersionQueryService(api)

    with pytest.raises(ToolInputError) as exc_info:
        service.list_versions(page_size=page_size)

    assert exc_info.value.code == "validation_error"
    assert "page_size" in exc_info.value.details
    assert api.list_calls == []


@pytest.mark.parametrize("status", ["UNKNOWN", "released", 1])
def test_invalid_status_is_rejected_before_upstream_call(status):
    api = FakeApiClient()
    service = VersionQueryService(api)

    with pytest.raises(ToolInputError) as exc_info:
        service.list_versions(status=status)

    assert exc_info.value.code == "validation_error"
    assert "status" in exc_info.value.details
    assert api.list_calls == []


@pytest.mark.parametrize("status", ["", "   "])
def test_empty_status_is_treated_as_no_filter(status):
    api = FakeApiClient()
    service = VersionQueryService(api)

    service.list_versions(status=status)

    assert api.list_calls[0]["status"] is None


@pytest.mark.parametrize("value", ["", "not-a-uuid", "123", None, 42])
def test_invalid_version_id_is_rejected(value):
    with pytest.raises(ToolInputError) as exc_info:
        validate_version_id(value)

    assert exc_info.value.code == "validation_error"
    assert "version_id" in exc_info.value.details


def test_version_id_is_normalized_to_lowercase_uuid():
    api = FakeApiClient()
    service = VersionQueryService(api)

    service.get_version("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")

    assert api.detail_calls == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
