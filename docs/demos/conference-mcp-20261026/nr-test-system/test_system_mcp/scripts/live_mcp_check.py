"""真实 MCP 协议联调：登录本地 Django、准备数据、stdio 握手并调用工具。

会话 Cookie 只在内存与子进程环境变量中传递，不打印、不落盘。
凭据通过本地环境变量 NR_MCP_TEST_USERNAME / NR_MCP_TEST_PASSWORD 提供。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_DIR / "src"
DEFAULT_API_BASE_URL = "http://127.0.0.1:8000/api/v1"

FIXTURES = [
    {
        "product_name": "NR 测试套件",
        "version": "v1.2.0",
        "status": "RELEASED",
        "description": "MCP 联调样例：已发布版本",
    },
    {
        "product_name": "NR 测试套件",
        "version": "v1.2.1",
        "status": "RELEASED",
        "description": "MCP 联调样例：补丁版本",
    },
    {
        "product_name": "NR 测试套件",
        "version": "v1.3.0",
        "status": "DRAFT",
        "description": "MCP 联调样例：草稿版本",
    },
    {
        "product_name": "NR 其他产品",
        "version": "v2.0.0",
        "status": "ARCHIVED",
        "description": "MCP 联调样例：其他产品归档版本",
    },
]


def _csrf_headers(http: httpx.AsyncClient) -> dict[str, str]:
    token = http.cookies.get("csrftoken")
    return {"X-CSRFToken": token} if token else {}


async def _login(http: httpx.AsyncClient, username: str, password: str) -> str:
    csrf_response = await http.get("auth/csrf/")
    csrf_response.raise_for_status()
    csrf_token = csrf_response.json()["csrf_token"]
    login_response = await http.post(
        "auth/login/",
        json={"username": username, "password": password},
        headers={"X-CSRFToken": csrf_token},
    )
    login_response.raise_for_status()
    session_id = http.cookies.get("sessionid")
    if not session_id:
        raise RuntimeError("登录成功但未获得 sessionid Cookie")
    return session_id


async def _ensure_fixtures(http: httpx.AsyncClient) -> None:
    for fixture in FIXTURES:
        response = await http.get(
            "system/versions/",
            params={"product_name": fixture["product_name"], "page_size": 100},
        )
        response.raise_for_status()
        existing_versions = {item["version"] for item in response.json().get("results", [])}
        if fixture["version"] in existing_versions:
            continue
        created = await http.post(
            "system/versions/",
            json=fixture,
            headers=_csrf_headers(http),
        )
        created.raise_for_status()


def _structured(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _error_payload(result: Any) -> dict[str, Any]:
    text = result.content[0].text if result.content else ""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            pass
    return {"raw": text}


async def _call(
    session: ClientSession,
    http: httpx.AsyncClient,
    *,
    tool: str,
    arguments: dict[str, Any],
    direct_path: str,
    direct_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    record: dict[str, Any] = {
        "tool": tool,
        "arguments": arguments,
        "is_error": bool(result.is_error),
    }
    if result.is_error:
        record["error"] = _error_payload(result)
        return record

    structured = _structured(result)
    record["structured_content"] = structured
    direct = await http.get(direct_path, params=direct_params or {})
    direct.raise_for_status()
    record["direct_http"] = direct.json()
    record["matches_direct_http"] = structured == record["direct_http"]
    return record


async def run(output_path: Path | None, seed: bool) -> dict[str, Any]:
    api_base_url = os.getenv("NR_MCP_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
    username = os.getenv("NR_MCP_TEST_USERNAME")
    password = os.getenv("NR_MCP_TEST_PASSWORD")

    async with httpx.AsyncClient(base_url=f"{api_base_url}/", timeout=15) as http:
        if username and password:
            session_id = await _login(http, username, password)
            if seed:
                await _ensure_fixtures(http)
            cookie_header = f"sessionid={session_id}"
        else:
            cookie_header = os.getenv("NR_MCP_SESSION_COOKIE", "")
            if not cookie_header:
                raise RuntimeError(
                    "需要 NR_MCP_SESSION_COOKIE，或本地测试用 "
                    "NR_MCP_TEST_USERNAME / NR_MCP_TEST_PASSWORD"
                )

        env = dict(os.environ)
        env["NR_MCP_API_BASE_URL"] = api_base_url
        env["NR_MCP_SESSION_COOKIE"] = cookie_header
        env["PYTHONPATH"] = os.pathsep.join([str(SOURCE_DIR), env.get("PYTHONPATH", "")]).strip(
            os.pathsep
        )

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nr_version_mcp"],
            env=env,
            cwd=str(PROJECT_DIR),
        )

        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()

            list_call = await _call(
                session,
                http,
                tool="list_versions",
                arguments={"search": "NR", "page": 1, "page_size": 2},
                direct_path="system/versions/",
                direct_params={"search": "NR", "page": 1, "page_size": 2},
            )

            filter_call = await _call(
                session,
                http,
                tool="list_versions",
                arguments={
                    "product_name": "NR 测试套件",
                    "status": "RELEASED",
                    "page": 1,
                    "page_size": 20,
                },
                direct_path="system/versions/",
                direct_params={
                    "product_name": "NR 测试套件",
                    "status": "RELEASED",
                    "page": 1,
                    "page_size": 20,
                },
            )

            results = _structured(
                await session.call_tool(
                    "list_versions",
                    {"search": "NR", "page": 1, "page_size": 2},
                )
            )
            version_id = results["results"][0]["id"]
            detail_call = await _call(
                session,
                http,
                tool="get_version",
                arguments={"version_id": version_id},
                direct_path=f"system/versions/{version_id}/",
            )

            error_result = await session.call_tool("list_versions", {"page": 0})
            error_call = {
                "tool": "list_versions",
                "arguments": {"page": 0},
                "is_error": bool(error_result.is_error),
                "error": _error_payload(error_result),
            }

            return {
                "timestamp": datetime.now(UTC).isoformat(),
                "api_base_url": api_base_url,
                "protocol_version": initialized.protocol_version,
                "server_info": initialized.server_info.model_dump(mode="json"),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                        "annotations": (
                            tool.annotations.model_dump(mode="json") if tool.annotations else None
                        ),
                    }
                    for tool in tools.tools
                ],
                "calls": [list_call, filter_call, detail_call, error_call],
            }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    record = asyncio.run(run(args.output, seed=not args.no_seed))
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"写入真实调用记录：{args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
