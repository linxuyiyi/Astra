"""固定 Skill 目录快照；只装配 Worker 白名单，原生 DSH 负责选择和调用。"""

import hashlib
import re
import shutil
from pathlib import Path

import yaml


def manifest(roots: dict[str, Path]) -> dict[str, dict[str, str]]:
    result = {}
    total = 0
    for name, root in roots.items():
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or root.is_symlink() or not root.is_dir():
            raise ValueError("Skill 名称或目录无效")
        entries = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Skill 不接受符号链接")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Skill 只接受普通文件")
            total += path.stat().st_size
            if total > 20 * 1024 * 1024 or len(entries) >= 500:
                raise ValueError("Skill 快照超过文件数或大小上限")
            entries[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if "SKILL.md" not in entries:
            raise ValueError("Skill 缺少 SKILL.md")
        content = (root / "SKILL.md").read_text()
        sections = content.split("---", 2)
        front = yaml.safe_load(sections[1]) if len(sections) == 3 and not sections[0].strip() else None
        if (
            not isinstance(front, dict)
            or front.get("name") != name
            or not isinstance(front.get("description"), str)
            or not front["description"].strip()
        ):
            raise ValueError("Skill frontmatter 与固定目录不一致")
        result[name] = entries
    return result


def snapshot(roots: dict[str, Path], home: Path) -> dict[str, dict[str, str]]:
    before = manifest(roots)
    target = home / "skills"
    for name, root in roots.items():
        shutil.copytree(root, target / name, symlinks=True)
    after = manifest({name: target / name for name in roots})
    if before != after:
        raise RuntimeError("复制期间 Skill 内容变化，拒绝启动")
    return after


def frozen_manifest(home: Path) -> dict[str, dict[str, str]]:
    root = home / "skills"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Skill 快照目录缺失或被替换")
    return manifest({path.name: path for path in root.iterdir()})
