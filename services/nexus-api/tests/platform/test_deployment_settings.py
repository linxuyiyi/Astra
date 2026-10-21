"""部署模式与显式配置的契约。"""

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_worker_executor_is_the_production_remote_only_mode():
    settings = Settings(executor_type="worker")

    assert settings.executor_type == "worker"


def test_unknown_executor_is_rejected():
    with pytest.raises(ValidationError):
        Settings(executor_type="local-opencode")


def test_kb_embedding_json_is_validated_and_kept_out_of_repr():
    raw = '{"model_route_id":"route-embed","dimensions":1024}'
    settings = Settings(kb_embedding_json=raw)

    assert settings.kb_embedding_json == raw
    assert "route-embed" not in repr(settings)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "{}",
        '{"type":"openai_credential"}',
        '{"model_route_id":"embed","dimensions":0}',
        '{"model_route_id":"embed","dimensions":true}',
    ],
)
def test_invalid_kb_embedding_json_fails_at_startup(raw):
    with pytest.raises(ValidationError, match="NEXUS_KB_EMBEDDING_JSON"):
        Settings(kb_embedding_json=raw)
