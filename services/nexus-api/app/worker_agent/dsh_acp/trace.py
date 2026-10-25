"""DSH 0.2.1-rc.2 原生 v4 无压缩日志的只读、按需投影。

只接受本装配显式启用的 encoding/version。在线 Session/update 不作为完整历史。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.worker_agent.dsh_acp.profile import DSH_VERSION

_SESSION_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
_MAX_LINE = 8 * 1024 * 1024


class TraceUnavailable(RuntimeError):
    pass


def read_trace_page(
    home: Path, workspace: Path, session_id: str, *, cursor: int = 0, limit: int = 100,
    dsh_version: str = DSH_VERSION,
) -> dict:
    if dsh_version != DSH_VERSION or not _SESSION_ID.fullmatch(session_id):
        raise TraceUnavailable("原生轨迹版本或 Session ID 不匹配")
    if cursor < 0 or not 1 <= limit <= 500:
        raise ValueError("无效分页参数")
    root = home / "sessions"
    if root.is_symlink() or not root.is_dir():
        raise TraceUnavailable("原生 Session 磁盘缺失")
    paths = list(root.glob(f"*/{session_id}/session.v4.jsonl"))
    if len(paths) != 1:
        raise TraceUnavailable("原生轨迹缺失、重复或编码不受支持")
    path = paths[0]
    if any(item.is_symlink() for item in (path, path.parent, path.parent.parent)):
        raise TraceUnavailable("拒绝符号链接轨迹")
    before = path.stat()
    digest = hashlib.sha256()
    events = []
    sequence = 0
    types: dict[str, int] = {}
    active_turns: set[int] = set()
    usage_records = 0
    usage_totals: dict[str, int] = {}
    try:
        with path.open("rb") as stream:
            header_line = stream.readline(_MAX_LINE + 1)
            header = json.loads(header_line)
            if (header.get("type") != "session" or header.get("version") != 4
                    or header.get("id") != session_id or header.get("isSeeded") is not False
                    or header.get("parentSession") is not None
                    or Path(header.get("cwd", "")).resolve() != workspace.resolve()):
                raise TraceUnavailable("原生头信息与绑定不一致或继承轨迹未受支持")
            digest.update(header_line)
            while line := stream.readline(_MAX_LINE + 1):
                if len(line) > _MAX_LINE or not line.endswith(b"\n"):
                    raise TraceUnavailable("原生轨迹过大或尾部未提交完整")
                event = json.loads(line)
                if (not isinstance(event, dict) or type(event.get("seq")) is not int
                        or event["seq"] != sequence or not isinstance(event.get("type"), str)):
                    raise TraceUnavailable("原生事件序列缺失或乱序")
                digest.update(line)
                types[event["type"]] = types.get(event["type"], 0) + 1
                if event["type"] == "assistant/message":
                    usage = event.get("data", {}).get("usage")
                    if isinstance(usage, dict):
                        usage_records += 1
                        for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens",
                                    "cacheWriteTokens"):
                            value = usage.get(key)
                            if value is not None:
                                if type(value) is not int or value < 0:
                                    raise TraceUnavailable("原生 token 统计格式不受支持")
                                usage_totals[key] = usage_totals.get(key, 0) + value
                if event["type"] in {"turn/start", "turn/end"}:
                    turn = event.get("data", {}).get("turn")
                    if type(turn) is not int or turn < 1:
                        raise TraceUnavailable("原生轮次标识无效")
                    if event["type"] == "turn/start":
                        if turn in active_turns:
                            raise TraceUnavailable("原生轮次重复开始")
                        active_turns.add(turn)
                    else:
                        if turn not in active_turns:
                            raise TraceUnavailable("原生轮次缺少开始记录")
                        active_turns.remove(turn)
                if cursor <= sequence < cursor + limit:
                    events.append(event)
                sequence += 1
    except (ValueError, TypeError, OSError) as exc:
        raise TraceUnavailable("原生轨迹不可读或格式损坏") from exc
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns,
    ):
        raise TraceUnavailable("轨迹正在变化，请在原生写入停止后重新读取")
    if cursor > sequence:
        raise TraceUnavailable("分页游标超过原生事件数量")
    return {
        "session_id": session_id, "format": "dsh-v4-jsonl", "events": events,
        "next_cursor": cursor + len(events) if cursor + len(events) < sequence else None,
        "event_count": sequence, "event_types": types, "sha256": digest.hexdigest(),
        "complete": not active_turns, "unsettled_turns": sorted(active_turns),
        "native_usage_records": usage_records, "native_usage_totals": usage_totals,
    }
