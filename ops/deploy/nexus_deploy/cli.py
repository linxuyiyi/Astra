from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .config import DeployConfig, load_config
from .configgen import ensure_user_config
from .deploy import Deployer, DeploymentResult
from .errors import DeployError
from .optional import run_llm_wiki, run_remote_worker

COMMANDS = {
    "install",
    "upgrade",
    "restart",
    "status",
    "health",
    "rollback",
    "preflight",
    "optional-wiki",
    "optional-remote-worker",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-deploy",
        description="Nexus production deployment: install, upgrade, restart, status, health, rollback",
    )
    parser.add_argument("command", nargs="?", default="install", choices=sorted(COMMANDS))
    parser.add_argument("--env-file", help="用户配置文件，默认 <config-dir>/nexus.env")
    parser.add_argument("--config-dir", help="配置目录，默认 /etc/nexus")
    parser.add_argument("--commit", help="完整 40 位 Git commit；覆盖 NEXUS_COMMIT")
    parser.add_argument("--repo", help="Git 仓库地址；覆盖 NEXUS_REPO_URL")
    parser.add_argument("--preflight-only", action="store_true", help="只执行预检")
    parser.add_argument("--skip-web-build", action="store_true", help="跳过 Web 构建（仅测试）")
    parser.add_argument("--service", action="append", default=[], help="restart 时指定服务，可重复")
    parser.add_argument(
        "--restored-revision",
        help="rollback 前已恢复的数据库 Alembic revision；必须与目标成功记录一致",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--llm-wiki-bundle", help="LLM Wiki Web Edition 包路径")
    parser.add_argument("--remote-worker", help="远端 Worker 地址")
    return parser


def _load(
    args: argparse.Namespace, *, require_commit: bool, include_runtime: bool = False
) -> DeployConfig:
    if args.config_dir:
        os.environ["NEXUS_CONFIG_DIR"] = args.config_dir
    config_dir = Path(os.environ.get("NEXUS_CONFIG_DIR", "/etc/nexus")).expanduser()
    env_file = Path(args.env_file).expanduser() if args.env_file else config_dir / "nexus.env"
    if require_commit:
        ensure_user_config(env_file)
    overrides: dict[str, str] = {}
    if args.commit:
        overrides["NEXUS_COMMIT"] = args.commit.strip().lower()
    if args.repo:
        overrides["NEXUS_REPO_URL"] = args.repo.strip()
    if args.llm_wiki_bundle:
        overrides["LLM_WIKI_BUNDLE"] = args.llm_wiki_bundle
    if args.remote_worker:
        overrides["REMOTE_WORKER"] = args.remote_worker
    return load_config(
        env_file=env_file,
        overrides=overrides,
        require_commit=require_commit,
        include_runtime=include_runtime,
    )


def _print_result(result: DeploymentResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"[{result.stage}] {result.message}")
        for check in result.checks:
            if check.get("skipped"):
                marker = "SKIP"
            elif check.get("ok"):
                marker = " OK "
            else:
                marker = "FAIL"
            print(f"  [{marker}] {check.get('name')}: {check.get('detail', '')}")
        rollback = result.record.get("rollback") if result.record else None
        if rollback:
            print(f"  rollback: {rollback}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in COMMANDS:
        raw = ["install", *raw]
    args = build_parser().parse_args(raw)

    try:
        if args.command == "optional-wiki":
            config = _load(args, require_commit=False, include_runtime=True)
            return run_llm_wiki(config)
        if args.command == "optional-remote-worker":
            if not args.remote_worker:
                raise DeployError("optional-remote-worker 需要 --remote-worker HOST")
            config = _load(args, require_commit=False, include_runtime=True)
            return run_remote_worker(config)

        if args.command in {"install", "upgrade", "preflight"}:
            config = _load(args, require_commit=True)
            deployer = Deployer(
                config,
                preflight_only=args.preflight_only or args.command == "preflight",
                skip_web_build=args.skip_web_build,
            )
            return _print_result(deployer.install(), as_json=args.json)

        if args.command == "health":
            config = _load(args, require_commit=False, include_runtime=True)
            return _print_result(Deployer(config).health(), as_json=args.json)

        if args.command == "status":
            config = _load(args, require_commit=False, include_runtime=True)
            payload = Deployer(config).status()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
            else:
                print(f"Nexus current: {payload['current_commit'] or '<none>'}")
                print(f"database: {payload['database']}")
                print(f"config digest: {payload['config_digest']}")
                for unit, info in payload["services"].items():
                    state = "active" if info.get("active") else "inactive"
                    print(f"  {unit:32} {state:9} MainPID={info.get('main_pid')}")
                last = payload.get("last_attempt") or {}
                if last:
                    print(
                        f"last: {last.get('status')} stage={last.get('stage')} "
                        f"commit={last.get('commit')} at={last.get('finished_at')}"
                    )
            return 0

        if args.command == "restart":
            config = _load(args, require_commit=False, include_runtime=True)
            return _print_result(Deployer(config).restart(args.service or None), as_json=args.json)

        if args.command == "rollback":
            config = _load(args, require_commit=False, include_runtime=True)
            return _print_result(
                Deployer(config).rollback(
                    args.commit, restored_revision=args.restored_revision
                ),
                as_json=args.json,
            )
    except DeployError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[fail] 已中断", file=sys.stderr)
        return 130
    return 2
