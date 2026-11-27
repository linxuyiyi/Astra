"""读写模型边界契约：校验只在入库写入侧拦截，查询响应模型不得施加二次长度/正则/枚举限制。"""

from __future__ import annotations

from typing import Any, Literal, get_args, get_origin

from fastapi.routing import APIRoute
from pydantic import BaseModel

from nexus_api.service import create_service

_FORBIDDEN_READ_META = ("min_length", "max_length", "pattern")


def _walk_models(annotation: Any, seen: set[type[BaseModel]]) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return found
        seen.add(annotation)
        found.append(annotation)
        for field in annotation.model_fields.values():
            found.extend(_walk_models(field.annotation, seen))
        return found
    for arg in get_args(annotation):
        found.extend(_walk_models(arg, seen))
    return found


def _contains_literal(annotation: Any) -> bool:
    if get_origin(annotation) is Literal:
        return True
    return any(_contains_literal(arg) for arg in get_args(annotation))


def test_get_response_models_do_not_apply_write_style_validation():
    app = create_service()
    violations: list[str] = []
    seen: set[type[BaseModel]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "GET" not in (route.methods or set()):
            continue
        if route.response_model is None:
            continue
        for model in _walk_models(route.response_model, seen):
            for field_name, field_info in model.model_fields.items():
                for meta in field_info.metadata:
                    for attr in _FORBIDDEN_READ_META:
                        if getattr(meta, attr, None) is not None:
                            violations.append(
                                f"{route.path} -> {model.__name__}.{field_name} 含读侧 {attr}"
                            )
                if _contains_literal(field_info.annotation):
                    violations.append(
                        f"{route.path} -> {model.__name__}.{field_name} 含读侧 Literal 限制"
                    )
    assert not violations, "查询响应模型不应施加写入侧校验：\n" + "\n".join(violations)
