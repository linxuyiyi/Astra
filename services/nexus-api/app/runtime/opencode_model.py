"""平台受管 OpenCode 统一使用 nexus-gateway/<模型别名>。CLI 独立 fallback 可显式直连。"""

DEFAULT_MODEL_ID = "default"
DIRECT_PROVIDER = "deepseek"
GATEWAY_PROVIDER = "nexus-gateway"


def route_model_id(value: str | None) -> str:
    """网关路由表里的模型 id。带 provider 前缀时只保留最后一段。"""

    raw = (value or "").strip()
    if not raw:
        return DEFAULT_MODEL_ID
    if raw.startswith(f"{GATEWAY_PROVIDER}/"):
        raw = raw.split("/", 1)[1]
    elif "/" in raw:
        raw = raw.rsplit("/", 1)[1]
    return raw or DEFAULT_MODEL_ID


def gateway_model(value: str | None) -> str:
    return f"{GATEWAY_PROVIDER}/{route_model_id(value)}"


def direct_model(value: str | None, fallback: str | None) -> str | None:
    """本地 OpenCode 会话要求 `provider/model`。已带斜杠的值保持原样。"""

    raw = (value or fallback or "").strip()
    if not raw:
        return None
    if "/" not in raw:
        return f"{DIRECT_PROVIDER}/{raw}"
    return raw


def gateway_environment(model: str | None, source: dict[str, str]) -> dict[str, str]:
    """只覆盖受管 Serve 的模型连接，保留工作区工具配置，不写用户全局文件。"""
    import json

    from app.gateway.urls import gateway_base

    if not source.get("NEXUS_GATEWAY_API_KEY", "").strip():
        raise RuntimeError("本机受管 OpenCode 需要 NEXUS_GATEWAY_API_KEY")
    environment = dict(source)
    try:
        config = json.loads(source.get("OPENCODE_CONFIG_CONTENT") or "{}")
        if not isinstance(config, dict):
            raise ValueError
    except ValueError:
        raise RuntimeError("OPENCODE_CONFIG_CONTENT 必须为合法 JSON 对象") from None
    routed = gateway_model(model)
    config.update({"enabled_providers": [GATEWAY_PROVIDER], "model": routed, "small_model": routed})
    providers = config.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise RuntimeError("OpenCode provider 配置必须为对象")
    providers[GATEWAY_PROVIDER] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Nexus API 中转",
        "options": {
            "baseURL": gateway_base(source.get("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1")),
            "apiKey": "{env:NEXUS_GATEWAY_API_KEY}",
        },
        "models": {route_model_id(model): {"name": route_model_id(model)}},
    }
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return environment


def validate_gateway_config(config: dict, source: dict[str, str]) -> None:
    """复用 Serve 前验证其实际配置，避免健康的旧进程继续直连。"""
    from app.gateway.urls import gateway_base

    expected = gateway_base(source.get("NEXUS_GATEWAY_OPENAI_BASE", "http://127.0.0.1:18110/v1"))
    providers = config.get("provider") or {}
    options = (providers.get(GATEWAY_PROVIDER) or {}).get("options") or {}
    models = [config.get("model"), config.get("small_model")]
    if (
        config.get("enabled_providers") != [GATEWAY_PROVIDER]
        or options.get("baseURL") != expected
        or any(not isinstance(model, str) or not model.startswith(f"{GATEWAY_PROVIDER}/")
               or not model.split("/", 1)[1].strip() for model in models)
    ):
        raise RuntimeError("已有 Serve 未使用统一网关配置，请显式重启该 Serve")
