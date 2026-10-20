"""测量工作区打开耗时。冷启动和热启动分开统计。

只用 GET 读取任务、工作区、SSE 和 iframe。不新建 Session，不换 Worker，不重启 OpenCode。
脚本只打印这次测量的数字，不判断 P95 是否达标。

示例：
  python tools/perf/workspace_open.py --base-url http://127.0.0.1:18120 --task-id T1
"""

from __future__ import annotations

import argparse
import json
import time

import httpx

IFRAME_PATH = "/opencode-workspace/1.18.35/index.html"


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _get(client: httpx.Client, url: str) -> float:
    started = time.perf_counter()
    response = client.get(url)
    response.raise_for_status()
    return _elapsed_ms(started)


def _sse(client: httpx.Client, url: str) -> dict[str, float]:
    started = time.perf_counter()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        ttfb = _elapsed_ms(started)
        for line in response.iter_lines():
            if line.startswith("data:"):
                return {"ttfb_ms": ttfb, "first_event_ms": _elapsed_ms(started)}
    raise RuntimeError("SSE 没有返回事件")


def open_once(base_url: str, task_id: str, iframe_path: str, client: httpx.Client | None) -> dict[str, float]:
    """client 为空时，每一段都新建连接，用来记冷启动。"""

    started = time.perf_counter()

    def call(func, url):
        if client is None:
            with httpx.Client(timeout=10) as http:
                return func(http, url)
        return func(client, url)

    task_ms = call(_get, f"{base_url}/api/tasks/{task_id}")
    workspace_ms = call(_get, f"{base_url}/api/tasks/{task_id}/workspace")
    sse = call(_sse, f"{base_url}/api/events/stream")
    iframe_ms = call(_get, f"{base_url}{iframe_path}")
    return {
        "http_task_ms": task_ms,
        "http_workspace_ms": workspace_ms,
        "sse_ttfb_ms": sse["ttfb_ms"],
        "sse_first_event_ms": sse["first_event_ms"],
        "iframe_ms": iframe_ms,
        "total_ms": _elapsed_ms(started),
    }


def measure(base_url: str, task_id: str, iframe_path: str = IFRAME_PATH) -> dict:
    base = base_url.rstrip("/")
    cold = open_once(base, task_id, iframe_path, None)
    with httpx.Client(timeout=10) as warm:
        open_once(base, task_id, iframe_path, warm)
        first = open_once(base, task_id, iframe_path, warm)
        again = open_once(base, task_id, iframe_path, warm)
    return {
        "cold": cold,
        "warm_first_open": first,
        "reopen": again,
        "note": "数字是这次测量的耗时。脚本不判断 P95 是否达标。打开只用 GET。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="测量工作区打开耗时")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--iframe-path", default=IFRAME_PATH)
    args = parser.parse_args()
    print(json.dumps(measure(args.base_url, args.task_id, args.iframe_path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
