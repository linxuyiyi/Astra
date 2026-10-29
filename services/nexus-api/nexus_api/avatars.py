"""员工形象目录：与 Web 和聊天岛共享一个静态 manifest。"""

import json
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name("employee_avatars.json").read_text(encoding="utf-8"))
AVATAR_IDS = frozenset(item["id"] for item in CATALOG["items"])
DEFAULT_AVATAR = CATALOG["defaultId"]


def validate_avatar(value: str) -> str:
    if value not in AVATAR_IDS:
        raise ValueError("请选择支持的员工形象")
    return value
