"""固定版真实 DSH ACP 探针；报告只包含元数据，未执行项不是 PASS。

在 services/nexus-api 目录中：
  .venv/bin/python ../../tools/e2e/dsh_acp_probe.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/nexus-api"))

from app.worker_agent.dsh_acp.preflight import inspect_installation
from app.worker_agent.dsh_acp.process import AcpProcessSupervisor
from app.worker_agent.dsh_acp.profile import (
    ACP_VERSION,
    DSH_VERSION,
    PROVIDER,
    GatewayModel,
    prepare_profile,
    private_write,
)
from app.worker_agent.dsh_acp.trace import read_trace_page

CHECKS = (
    "installation", "gateway_catalog", "initialize", "new_session", "model_selection",
    "first_turn", "second_turn",
    "mcp", "resume", "resume_turn", "cancel", "native_trace", "permission",
    "disconnect_recovery", "long_session", "reasoning", "token_usage", "rate_limit", "cleanup",
)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsh", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[2]
                        / "ops/runtime/dsh/package-lock.json")
    parser.add_argument("--root", type=Path, required=True, help="全新绝对证据目录，不复用旧 DSH_HOME")
    parser.add_argument("--gateway-url", required=True, help="明确的 Nexus Gateway /v1 地址")
    parser.add_argument("--model", required=True, help="已登记路由别名")
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--key-env", default="NEXUS_GATEWAY_API_KEY", help="只从环境读取调用 key")
    parser.add_argument("--thinking-format", choices=["deepseek", "zai", "qwen"])
    parser.add_argument("--protocol-only", action="store_true", help="不推理；推理项保留 UNVERIFIED")
    parser.add_argument("--fault-probes", action="store_true", help="在自有进程/目录测试权限和意外断线")
    parser.add_argument("--timeout", type=float, default=180)
    return parser.parse_args()


async def probe(args) -> int:
    root = args.root
    if not root.is_absolute() or args.timeout <= 0:
        raise ValueError("root 必须是新的绝对目录，timeout 必须大于零")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "libc": platform.libc_ver(), "python": platform.python_version()},
        "expected_versions": {"dsh": DSH_VERSION, "agent_client_protocol": ACP_VERSION},
        "model_alias": args.model, "gateway_url": None,
        "checks": {name: {"status": "UNVERIFIED"} for name in CHECKS},
        "production_cutover": False,
    }
    repository = Path(__file__).resolve().parents[2]
    sources = [
        *sorted((repository / "services/nexus-api/app/worker_agent/dsh_acp").glob("*.py")),
        Path(__file__).resolve(), Path(__file__).with_name("dsh_probe_mcp.py").resolve(),
        repository / "services/nexus-api/app/worker_agent/process_env.py",
        repository / "ops/runtime/dsh/package.json", repository / "ops/runtime/dsh/package-lock.json",
        repository / "ops/runtime/dsh/requirements.txt",
    ]
    report["source_files"] = {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources
    }
    report["source_sha256"] = hashlib.sha256(
        json.dumps(report["source_files"], sort_keys=True).encode(),
    ).hexdigest()
    checks = report["checks"]
    current = "installation"
    supervisor = None
    counts = Counter()
    tool_calls = set()
    usage_updates = []
    nonce = uuid.uuid4().hex
    approval_root = None
    proof = root / "mcp-proof.jsonl"
    private_write(proof, "")

    async def updates(session_id, event):
        kind = event.get("sessionUpdate", "unknown")
        counts[kind] += 1
        if kind == "tool_call":
            tool_calls.add(event.get("toolCallId"))
        if kind == "usage_update":
            usage_updates.append({key: event[key] for key in ("used", "size") if key in event})

    def make_supervisor():
        return AcpProcessSupervisor(args.dsh, workspace, home, patch, update_sink=updates)

    def passed(name: str, **evidence):
        checks[name] = {"status": "PASS", **evidence}

    try:
        report["installation"] = inspect_installation(args.dsh, args.lock)
        passed(current, **report["installation"])
        current = "gateway_catalog"
        key = os.environ.get(args.key_env, "")
        model = GatewayModel(args.gateway_url, args.model, args.context_window,
                             args.max_tokens, args.thinking_format)
        report["gateway_url"] = model.base_url
        patch, digest = prepare_profile(root / "dsh-home", model, key)
        home = patch.parent
        report["profile_sha256"] = digest
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(model.base_url.rstrip("/") + "/models",
                                        headers={"Authorization": "Bearer " + key})
            if response.status_code != 200:
                raise RuntimeError("gateway_catalog_http_" + str(response.status_code))
            aliases = [row.get("id") for row in response.json().get("data", [])]
            if args.model not in aliases:
                raise RuntimeError("gateway_model_alias_unavailable")
            passed(current, http_status=200, alias_present=True)
        current = "initialize"
        supervisor = make_supervisor()
        initialization = await supervisor.start()
        report["agent_capabilities"] = initialization.get("agentCapabilities", {})
        passed(current, protocol_version=initialization.get("protocolVersion"))
        current = "new_session"
        from acp.schema import McpServerStdio

        servers = [McpServerStdio(name="nexus-probe", command=sys.executable,
                                 args=[str(Path(__file__).with_name("dsh_probe_mcp.py")), str(proof)],
                                 env=[])]
        session = await asyncio.wait_for(supervisor.create_or_resume(mcp_servers=servers), args.timeout)
        session_id = supervisor.session_id
        report["native_session_id"] = session_id
        passed(current)
        current = "model_selection"
        choices = [item for item in session.get("configOptions", []) if item.get("id") == "model"]
        expected = json.dumps([PROVIDER, args.model], separators=(",", ":"))
        if len(choices) != 1 or choices[0].get("currentValue") != expected:
            raise RuntimeError("native_model_selection_mismatch")
        passed(current, provider=PROVIDER, alias=args.model)
        if not args.protocol_only:
            for name, suffix in (("first_turn", "first"), ("second_turn", "second")):
                current = name
                prompt = (
                    "Compatibility probe. Call nexus_probe_echo exactly once with this exact JSON argument: "
                    + json.dumps({"nonce": nonce + suffix}) + ". "
                    "Do not use other tools, delegate, or edit files. Then reply with that exact nonce."
                )
                result = await asyncio.wait_for(asyncio.shield(supervisor.submit(prompt)), args.timeout)
                if result.stop_reason != "end_turn":
                    raise RuntimeError("unexpected_stop_reason")
                passed(name, stop_reason=result.stop_reason)
            current = "mcp"
            calls = [json.loads(line) for line in proof.read_text().splitlines()]
            if not all(any(row == {"kind": "echo", "nonce": nonce + suffix} for row in calls)
                       for suffix in ("first", "second")):
                raise RuntimeError("real_mcp_tool_evidence_missing")
            passed(current, real_echo_calls=2)
        await supervisor.dispose()
        current = "resume"
        supervisor = make_supervisor()
        await supervisor.start()
        await asyncio.wait_for(supervisor.create_or_resume(session_id=session_id, mcp_servers=servers),
                               args.timeout)
        if supervisor.session_id != session_id:
            raise RuntimeError("resume_changed_native_session")
        passed(current, same_native_session=True)
        if not args.protocol_only:
            current = "resume_turn"
            result = await asyncio.wait_for(asyncio.shield(supervisor.submit(
                f"Call nexus_probe_echo exactly once with nonce {nonce}resume. Do not edit files. Reply OK."
            )), args.timeout)
            if result.stop_reason != "end_turn":
                raise RuntimeError("resume_turn_failed")
            passed(current, stop_reason=result.stop_reason)
            current = "cancel"
            prompt = supervisor.submit(f"Call nexus_probe_delay exactly once with nonce {nonce}cancel. "
                                       "Do not use shell or other tools. Wait for its result.")
            async with asyncio.timeout(args.timeout):
                while not any(json.loads(line).get("kind") == "delay_started"
                              for line in proof.read_text().splitlines()):
                    if prompt.done():
                        raise RuntimeError("prompt_ended_without_delay_tool")
                    await asyncio.sleep(.1)
            cancellation = await supervisor.cancel()
            if not cancellation["confirmed"]:
                raise RuntimeError("cancel_not_confirmed")
            if cancellation["method"] == "prompt_settled" and cancellation.get("stop_reason") != "cancelled":
                raise RuntimeError("cancel_response_not_cancelled")
            passed(current, **cancellation)
            if args.fault_probes:
                current = "disconnect_recovery"
                baseline = sum(json.loads(line).get("kind") == "delay_started"
                               for line in proof.read_text().splitlines())
                interrupted = supervisor.submit(
                    f"Call nexus_probe_delay exactly once with nonce {nonce}disconnect. "
                    "Do not use other tools. Wait for the result."
                )
                async with asyncio.timeout(args.timeout):
                    while sum(json.loads(line).get("kind") == "delay_started"
                              for line in proof.read_text().splitlines()) == baseline:
                        if interrupted.done():
                            raise RuntimeError("disconnect_fixture_not_started")
                        await asyncio.sleep(.1)
                process = supervisor.process
                if (
                    process is None or process.returncode is not None
                    or os.getpgid(process.pid) != process.pid
                ):
                    raise RuntimeError("disconnect_process_ownership_unproven")
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                await supervisor.dispose()
                await asyncio.gather(interrupted, return_exceptions=True)
                supervisor = make_supervisor()
                await supervisor.start()
                await asyncio.wait_for(
                    supervisor.create_or_resume(session_id=session_id, mcp_servers=servers), args.timeout,
                )
                before = len(proof.read_text().splitlines())
                # 恢复只打开原 Session；不重投原 prompt。
                await asyncio.sleep(.5)
                if len(proof.read_text().splitlines()) != before:
                    raise RuntimeError("resume_reexecuted_tool_without_submission")
                passed(current, same_native_session=supervisor.session_id == session_id,
                       automatic_resubmissions=0, original_delay_invocations=1)
                current = "permission"
                # /tmp 在官方 workspace-write 白名单内，不能用作拒绝探针目标。
                approval_root = Path.home() / (".nexus-dsh-p0-approval-" + nonce)
                approval_root.mkdir(mode=0o700, exist_ok=False)
                denied_target = approval_root / "denied-marker.txt"

                async def deny(_session, options, _tool):
                    return next((o["optionId"] for o in options if o["kind"] == "reject_once"), None)

                supervisor.permission_sink = deny
                prior = supervisor.permission_count
                command = f"printf {shlex.quote(nonce)} > {shlex.quote(str(denied_target))}"
                await asyncio.wait_for(asyncio.shield(supervisor.submit(
                    f"Controlled approval probe: use bash once to run exactly {command}\n"
                    'Set sandbox_permissions="danger-full-access" '
                    'and justification="Controlled probe marker". '
                    "This writes a disposable marker outside the workspace, owned by this test. "
                    "If approval is denied, do not retry or use another tool; reply DENIED."
                )), args.timeout)
                if supervisor.permission_count <= prior or denied_target.exists():
                    raise RuntimeError("permission_denial_not_proven")
                allowed_target = approval_root / "allowed-marker.txt"
                command = f"printf {shlex.quote(nonce)} > {shlex.quote(str(allowed_target))}"

                async def allow_exact_fixture(_session, options, tool):
                    # 只授权本探针生成的单条写标记命令；其余一律拒绝。
                    raw = tool.get("rawInput") or {}
                    if raw.get("command") != command:
                        return None
                    return next((o["optionId"] for o in options if o["kind"] == "allow_once"), None)

                supervisor.permission_sink = allow_exact_fixture
                prior = supervisor.permission_count
                await asyncio.wait_for(asyncio.shield(supervisor.submit(
                    f"Controlled one-shot approval probe: use bash once to run exactly {command}\n"
                    'Set sandbox_permissions="danger-full-access" '
                    'and justification="Controlled probe marker". '
                    "This is a disposable marker owned by the probe. Do not change the command or retry."
                )), args.timeout)
                if (supervisor.permission_count <= prior or not allowed_target.is_file()
                        or allowed_target.read_text() != nonce):
                    raise RuntimeError("permission_allow_once_not_proven")
                passed(current, denied_without_effect=True, exact_fixture_allowed_once=True)
        await supervisor.dispose()
        current = "native_trace"
        trace = read_trace_page(home, workspace, session_id, limit=1)
        if not trace["complete"]:
            raise RuntimeError("native_trace_contains_unsettled_turn")
        report["native_trace"] = {key: value for key, value in trace.items() if key != "events"}
        passed(current, event_count=trace["event_count"], sha256=trace["sha256"])
        if counts["agent_thought_chunk"]:
            passed("reasoning", observed_updates=counts["agent_thought_chunk"])
        if trace["native_usage_records"] and all(
            key in trace["native_usage_totals"] for key in ("inputTokens", "outputTokens", "totalTokens")
        ):
            passed("token_usage", native_usage_records=trace["native_usage_records"],
                   native_usage_totals=trace["native_usage_totals"], context_usage_updates=len(usage_updates))
    except Exception as exc:  # noqa: BLE001 - 脱敏记录，任何单项失败都不能报告通过
        # 原始异常/HTTP 响应可能包含凭据或用户内容，报告只写类型和自有错误码。
        reason = (str(exc) if type(exc) is RuntimeError and re.fullmatch(r"[a-z][a-z0-9_]{1,80}", str(exc))
                  else type(exc).__name__)
        checks[current] = {"status": "FAIL", "reason": reason[:100]}
    finally:
        if supervisor:
            try:
                await supervisor.dispose()
                passed("cleanup", owned_process_group_stopped=supervisor.stop_confirmed)
            except Exception as exc:  # noqa: BLE001 - 清理失败独立保留
                report["cleanup_error"] = type(exc).__name__
                checks["cleanup"] = {"status": "FAIL", "reason": type(exc).__name__}
        if approval_root:
            try:
                for name in ("allowed-marker.txt", "denied-marker.txt"):
                    (approval_root / name).unlink(missing_ok=True)
                approval_root.rmdir()
            except OSError:
                checks["cleanup"] = {"status": "FAIL", "reason": "approval_fixture_cleanup_failed"}
        report["online_update_counts"] = dict(counts)
        report["tool_call_count"] = len(tool_calls)
        report["finished_at"] = datetime.now(UTC).isoformat()
        statuses = [item["status"] for item in checks.values()]
        report["overall"] = "FAIL" if "FAIL" in statuses else "INCOMPLETE"
        # 即使探针已实现的项目通过，完整 P0/生产门禁仍包含外部实测项目。
        path = root / "report.json"
        private_write(path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(path), "overall": report["overall"],
                          "checks": {name: item["status"] for name, item in checks.items()}},
                         ensure_ascii=False))
    return 1 if report["overall"] == "FAIL" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(probe(arguments())))
    except (ValueError, FileExistsError) as error:
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
