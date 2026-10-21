"""OpenAI-compatible clients use one canonical gateway /v1 base."""

from urllib.parse import urlsplit, urlunsplit


def gateway_base(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.path.rstrip("/") in {"", "/v1"}
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("网关地址必须是 http(s) 主机根地址或 /v1，不含凭据、query 或其他路径")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))
