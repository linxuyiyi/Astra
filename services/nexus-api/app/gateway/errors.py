"""数据面结构化错误。

Gateway 只暴露 OpenAI 兼容错误体，内部状态码与错误码分开：``code`` 是稳定的
机器可读枚举，用于调用日志与前端统计；``status_code`` 是 HTTP 状态。绝不在错误
消息或响应中携带下游凭据、请求体或原始响应。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import JSONResponse

# 鉴权 / 许可
UNAUTHORIZED = "UNAUTHORIZED"
KEY_DISABLED = "KEY_DISABLED"
KEY_EXPIRED = "KEY_EXPIRED"
KEY_FORBIDDEN = "KEY_FORBIDDEN"
KEY_CONCURRENCY_EXCEEDED = "KEY_CONCURRENCY_EXCEEDED"
KEY_QUOTA_EXCEEDED = "KEY_QUOTA_EXCEEDED"
# 路由 / 调度
INVALID_REQUEST = "INVALID_REQUEST"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
NO_ELIGIBLE_NODE = "NO_ELIGIBLE_NODE"
POOL_SATURATED = "POOL_SATURATED"
QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
NODE_CREDENTIAL_ERROR = "NODE_CREDENTIAL_ERROR"
# 上游
UPSTREAM_CONNECT_TIMEOUT = "UPSTREAM_CONNECT_TIMEOUT"
UPSTREAM_CONNECT_ERROR = "UPSTREAM_CONNECT_ERROR"
UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
UPSTREAM_PROTOCOL_ERROR = "UPSTREAM_PROTOCOL_ERROR"
UPSTREAM_ERROR = "UPSTREAM_ERROR"
# 调用终结
CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"
STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
# 流式语义不完整：只有结束标记、没有实际内容，或没有终止标记就 EOF。
STREAM_EMPTY = "STREAM_EMPTY"
STREAM_INCOMPLETE = "STREAM_INCOMPLETE"

AUTHENTICATION_ERROR = "authentication_error"
PERMISSION_ERROR = "permission_error"
RATE_LIMIT_ERROR = "rate_limit_error"
INVALID_REQUEST_ERROR = "invalid_request_error"
NOT_FOUND_ERROR = "not_found_error"
API_ERROR = "api_error"


@dataclass
class GatewayDataPlaneError(Exception):
    code: str
    message: str
    status_code: int = 400
    error_type: str = API_ERROR
    # 请求是否明确未被发送到下游：只有 True 才允许切换节点重试。
    not_sent: bool = False
    # 无节点时用于区分原因的结构化明细。
    detail: dict[str, object] | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        body: dict[str, object] = {
            "error": {"message": self.message, "type": self.error_type, "code": self.code}
        }
        if self.detail:
            body["error"]["detail"] = self.detail  # type: ignore[index]
        return JSONResponse(status_code=self.status_code, content=body)


def unauthorized(message: str = "缺少或无效的 Gateway Key") -> GatewayDataPlaneError:
    return GatewayDataPlaneError(UNAUTHORIZED, message, 401, AUTHENTICATION_ERROR)


def key_disabled() -> GatewayDataPlaneError:
    return GatewayDataPlaneError(KEY_DISABLED, "Gateway Key 已被禁用或撤销", 401, AUTHENTICATION_ERROR)


def key_expired() -> GatewayDataPlaneError:
    return GatewayDataPlaneError(KEY_EXPIRED, "Gateway Key 已过期", 401, AUTHENTICATION_ERROR)


def key_forbidden(model: str) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(
        KEY_FORBIDDEN, f"Gateway Key 不允许访问模型 {model}", 403, PERMISSION_ERROR
    )


def key_concurrency_exceeded() -> GatewayDataPlaneError:
    return GatewayDataPlaneError(
        KEY_CONCURRENCY_EXCEEDED, "Gateway Key 并发已达上限", 429, RATE_LIMIT_ERROR
    )


def key_quota_exceeded(period: str = "hour") -> GatewayDataPlaneError:
    window = "小时" if period == "hour" else "日"
    return GatewayDataPlaneError(
        KEY_QUOTA_EXCEEDED, f"Gateway Key {window}额度已达上限", 429, RATE_LIMIT_ERROR
    )


def invalid_request(message: str) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(INVALID_REQUEST, message, 400, INVALID_REQUEST_ERROR)


def payload_too_large(message: str) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(PAYLOAD_TOO_LARGE, message, 413, INVALID_REQUEST_ERROR)


def model_not_found(model: str) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(
        MODEL_NOT_FOUND, f"没有可用于模型 {model} 的路由", 404, NOT_FOUND_ERROR
    )


def runtime_unavailable(message: str = "运行态 Redis 不可用，已拒绝新请求") -> GatewayDataPlaneError:
    return GatewayDataPlaneError(RUNTIME_UNAVAILABLE, message, 503, API_ERROR)


def no_eligible_node(reason: str, detail: dict[str, object] | None = None) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(
        reason, "资源池没有可调度的节点", 503, API_ERROR, detail=detail
    )


def upstream_protocol_error(message: str = "上游流式响应不符合 SSE 约定") -> GatewayDataPlaneError:
    return GatewayDataPlaneError(UPSTREAM_PROTOCOL_ERROR, message, 502, API_ERROR)


def upstream_error(code: str, message: str, status_code: int) -> GatewayDataPlaneError:
    return GatewayDataPlaneError(code, message, status_code, API_ERROR)