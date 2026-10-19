"""OpenCode 模型字符串。直连用 provider/model，远程经网关用 nexus-gateway/<路由 id>。"""

DEFAULT_MODEL_ID = "deepseek-flash"
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
