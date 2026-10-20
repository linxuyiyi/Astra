"""JSONC：行注释、块注释、行内 // 和尾逗号。三处配置读取共用这一份。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escape = False
    while i < length:
        char = text[i]
        nxt = text[i + 1] if i + 1 < length else ""
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            i += 2
            while i < length and text[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(length, i + 2)
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escape = False
    while i < length:
        char = text[i]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def loads_jsonc(text: str) -> Any:
    stripped = _strip_trailing_commas(_strip_comments(text))
    if not stripped.strip():
        return {}
    return json.loads(stripped)


def main() -> None:
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    json.dump(loads_jsonc(raw), sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
