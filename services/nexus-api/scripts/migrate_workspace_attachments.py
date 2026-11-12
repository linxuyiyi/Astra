"""盘点并导入旧聊天附件。写入前先停止 Nexus，保留旧数据库和文件备份。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from sqlalchemy import inspect, text

from nexus_api.agentscope import AgentScopeClient
from nexus_api.chat_attachments import ATTACHMENT_PREFIX, file_name, reference
from nexus_api.db import connect
from nexus_api.sql import insert_record, upsert_record

LEGACY_ID = re.compile(r"^att_[0-9a-f]{12}$")


def legacy_ids(value):
    return [item["id"] if isinstance(item, dict) else item for item in json.loads(value)]


def replace_ids(value, aliases):
    if isinstance(value, dict):
        return {k: replace_ids(v, aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_ids(item, aliases) for item in value]
    if isinstance(value, str):
        if value.startswith("/api/v1/attachments/"):
            return "/api/v1/attachments/" + aliases.get(value.rsplit("/", 1)[-1], value.rsplit("/", 1)[-1])
        return aliases.get(value, value)
    return value


async def migrate(db, client, root, *, apply=False, archive=None):
    tables = inspect(db).get_table_names()
    if "t_nexus_attachments" not in tables:
        return {"status": "旧表不存在", "imported": 0}
    originals = {
        row["id"]: row for row in db.execute(text("SELECT * FROM t_nexus_attachments")).mappings()
    }
    rows = db.execute(text(
        "SELECT id,conversation_id,channel_id,attachments_json,native_json "
        "FROM t_nexus_messages WHERE attachments_json NOT IN ('[]','')"
    )).mappings().all()
    conversations = {
        row["id"]: row for row in db.execute(text(
            "SELECT c.id,c.agentscope_session_id AS session_id,e.agentscope_agent_id AS agent_id "
            "FROM t_nexus_conversations c JOIN t_nexus_employees e ON c.employee_id=e.id"
        )).mappings()
    }
    db.commit()
    uses = defaultdict(set)
    for row in rows:
        kind = "group" if row["channel_id"] else "private"
        scope = row["channel_id"] or row["conversation_id"]
        for key in legacy_ids(row["attachments_json"]):
            if LEGACY_ID.fullmatch(key):
                uses[key].add((kind, scope))
    native = []
    if client.configured and originals:
        for scope, target in conversations.items():
            if not target["session_id"] or not target["agent_id"]:
                continue
            async for msg in client.iter_session_messages(target["agent_id"], target["session_id"]):
                attachments = (msg.get("metadata") or {}).get("attachments") or []
                keys = [item.get("id", "") for item in attachments if isinstance(item, dict)]
                for key in keys:
                    if LEGACY_ID.fullmatch(key):
                        uses[key].add(("private", scope))
                if any(LEGACY_ID.fullmatch(key) for key in keys):
                    native.append((scope, msg["id"], keys))
    paths, missing, unbound = {}, [], []
    for key, scopes in sorted(uses.items()):
        if key not in originals:
            if "t_nexus_attachment_refs" not in tables or not reference(db, key):
                missing.append(key)
            continue
        path = root / key
        if path.is_symlink() or not path.is_file() or path.stat().st_size != originals[key]["size"]:
            missing.append(key)
            continue
        paths[key] = path
        for kind, scope in scopes:
            if not scope or (kind == "private" and not (
                conversations.get(scope, {}).get("session_id") and conversations[scope].get("agent_id")
            )):
                unbound.append({"id": key, "kind": kind, "scope": scope})
    report = {
        "old_records": len(originals),
        "local_originals": sum((root / key).is_file() for key in originals if LEGACY_ID.fullmatch(key)),
        "referenced_ids": len(uses),
        "scoped_copies": sum(map(len, uses.values())),
        "missing_or_size_mismatch": missing,
        "unbound": unbound,
        "unreferenced": len(originals.keys() - uses.keys()),
        "native_history_checked": client.configured,
        "imported": 0,
    }
    if not apply:
        return report
    if not client.configured or "t_nexus_attachment_refs" not in tables or missing or unbound:
        raise SystemExit(json.dumps({**report, "status": "迁移前置条件未满足"}, ensure_ascii=False))
    aliases = defaultdict(dict)
    group_locations = {}
    for key, scopes in sorted(uses.items()):
        if key not in paths:
            continue
        original = originals[key]
        for index, (kind, scope) in enumerate(sorted(scopes)):
            suffix = hashlib.sha256(f"{key}:{kind}:{scope}".encode()).hexdigest()[:32]
            new_key = key if index == 0 else f"att_{suffix}"
            aliases[(kind, scope)][key] = new_key
            if kind == "private":
                source = conversations[scope]
                target = {"agent_id": source["agent_id"], "session_id": source["session_id"]}
            else:
                if scope not in group_locations:
                    group_locations[scope] = (await client.request(
                        "PUT", f"{ATTACHMENT_PREFIX}/groups/{scope}",
                    )).json()
                target = group_locations[scope]
            digest = hashlib.sha256()

            async def chunks(path, digest):
                with path.open("rb") as file:
                    while chunk := file.read(64 * 1024):
                        digest.update(chunk)
                        yield chunk

            result = (await client.request(
                "PUT", f"{ATTACHMENT_PREFIX}/{new_key}", params=target, content=chunks(paths[key], digest),
            )).json()
            if result["size"] != original["size"] or result["sha256"] != digest.hexdigest():
                raise RuntimeError(f"附件核验失败 {key}")
            downloaded = hashlib.sha256()
            async with client.download_attachment(new_key, target) as response:
                async for chunk in response.aiter_bytes():
                    downloaded.update(chunk)
            if downloaded.hexdigest() != digest.hexdigest():
                raise RuntimeError(f"附件下载核验失败 {key}")
            name = file_name(original["name"])
            values = {
                "id": new_key, "owner_uid": original["owner_uid"], "scope_kind": kind, "scope_id": scope,
                **target, "name": name, "media_type": result["type"], "size": result["size"],
                "sha256": result["sha256"], "linked": 1, "created_at": original["created_at"],
            }
            insert_record(db, "t_nexus_attachment_refs", values, ignore_keys=["id"])
            stored = reference(db, new_key)
            if any(stored[field] != values[field] for field in ("scope_kind", "scope_id", "sha256")):
                raise RuntimeError(f"附件引用冲突 {new_key}")
            db.commit()
            report["imported"] += 1
    # Copies are committed before any visible reference is changed.
    for scope, msg_id, _ in native:
        mapping = aliases[("private", scope)]
        if mapping:
            target = conversations[scope]
            await client.request(
                "PATCH", f"{ATTACHMENT_PREFIX}/legacy-message",
                json={"session_id": target["session_id"], "agent_id": target["agent_id"],
                      "message_id": msg_id, "aliases": mapping},
            )
    for row in rows:
        kind = "group" if row["channel_id"] else "private"
        mapping = aliases[(kind, row["channel_id"] or row["conversation_id"])]
        if mapping:
            db.execute(text("UPDATE t_nexus_messages SET attachments_json=:a,native_json=:n WHERE id=:id"), {
                "id": row["id"], "a": json.dumps(replace_ids(json.loads(row["attachments_json"]), mapping)),
                "n": json.dumps(replace_ids(json.loads(row["native_json"]), mapping)),
            })
    upsert_record(
        db, "t_nexus_settings", {"key": "workspace_attachments_migrated", "value": "1"},
        keys=["key"], update=["value"],
    )
    db.commit()
    if archive:
        if archive == root or archive.is_relative_to(root):
            raise ValueError("归档目录必须位于旧附件目录外")
        archive.mkdir(parents=True, exist_ok=True)
        archived = []
        for key, original in originals.items():
            if not LEGACY_ID.fullmatch(key):
                raise RuntimeError("旧附件标识无效，无法安全归档")
            source = root / key
            if source.is_symlink() or not source.is_file() or source.stat().st_size != original["size"]:
                raise RuntimeError(f"旧附件无法归档 {key}")
            destination = archive / key
            if not destination.exists():
                with tempfile.NamedTemporaryFile(dir=archive) as output, source.open("rb") as origin:
                    shutil.copyfileobj(origin, output, 64 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                    os.link(output.name, destination)
            if destination.is_symlink():
                raise RuntimeError(f"归档目标不是普通文件 {key}")
            with source.open("rb") as origin, destination.open("rb") as output:
                source_digest = hashlib.file_digest(origin, "sha256").digest()
                if source_digest != hashlib.file_digest(output, "sha256").digest():
                    raise RuntimeError(f"归档核验失败 {key}")
            archived.append(source)
        db.execute(text("DROP TABLE t_nexus_attachments"))
        db.commit()
        for source in archived:
            source.unlink()
        report["archived"] = len(archived)
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--archive-legacy", type=Path, help="迁移后核验并归档旧原件，然后移除旧表")

    args = parser.parse_args()
    if args.archive_legacy and not args.apply:
        parser.error("--archive-legacy 需要 --apply")
    client = AgentScopeClient(
        os.environ.get("NEXUS_AGENTSCOPE_BASE_URL"), os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus"),
    )
    try:
        with connect(args.database_url) as db:
            result = await migrate(
                db, client, args.legacy_root.resolve(), apply=args.apply,
                archive=args.archive_legacy.resolve() if args.archive_legacy else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
