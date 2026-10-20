import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.config.settings import Settings, get_settings
from app.executors.base import ExecutorResult, OpenCodeExecutor
from app.executors.opencode_prompt import build_task_prompt
from app.executors.process import local_command, process_group_options, terminate_process
from app.executors.usage import parse_opencode_usage
from app.models import Task
from app.runtime.semantic_events import extract_inquiry


class OpenCodeCLIExecutor(OpenCodeExecutor):
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        opencode_binary: str | None = None,
        model_name: str | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.settings = settings or get_settings()
        self.opencode_binary = opencode_binary or self.settings.opencode_binary
        self.model_name = model_name or self.settings.opencode_model
        self.event_callback = event_callback
        self._processes: dict[str, Any] = {}

    def _prompt(self, task: Task, workspace: Path, *, retry: bool = False) -> str:
        return build_task_prompt(task, workspace, retry=retry, native_question=False)

    def _command(self, task: Task, workspace: Path, session_id: str | None = None) -> list[str]:
        command = [
            self.opencode_binary,
            "run",
            "--format",
            "json",
            "--dir",
            str(workspace),
            "--title",
            f"Nexus {task.id}",
        ]
        if self.settings.opencode_agent:
            command.extend(["--agent", self.settings.opencode_agent])
        if self.model_name:
            command.extend(["--model", self.model_name])
        if session_id:
            command.extend(["--session", session_id])
        prompt = self._prompt(task, workspace, retry=bool(session_id))
        if os.name == "nt":
            prompt = prompt.replace("\r\n", "\\n").replace("\n", "\\n")
        command.append(prompt)
        return command

    @staticmethod
    def _extract_session(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("sessionID", "session_id", "sessionId"):
                if value.get(key):
                    return str(value[key])
            for child in value.values():
                found = OpenCodeCLIExecutor._extract_session(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = OpenCodeCLIExecutor._extract_session(child)
                if found:
                    return found
        return None

    async def _communicate(
        self,
        task: Task,
        process: Any,
        *,
        session_id: str | None = None,
    ) -> ExecutorResult:
        self._processes[task.id] = process
        stdout_lines: list[bytes] = []
        stderr_chunks: list[bytes] = []
        output: list[dict[str, Any]] = []
        detected_session = session_id
        pending_inquiry: dict[str, Any] | None = None

        async def read_stdout() -> None:
            nonlocal detected_session, pending_inquiry
            assert process.stdout is not None
            while line := await process.stdout.readline():
                stdout_lines.append(line)
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"type": "text", "text": text}
                output.append(payload)
                detected_session = detected_session or self._extract_session(payload)
                pending_inquiry = pending_inquiry or extract_inquiry(payload)
                if self.event_callback:
                    try:
                        await self.event_callback(payload)
                    except Exception:
                        # Observation must never interrupt the employee's work.
                        pass

        async def read_stderr() -> None:
            assert process.stderr is not None
            while chunk := await process.stderr.read(4096):
                stderr_chunks.append(chunk)

        try:
            await asyncio.wait_for(
                asyncio.gather(read_stdout(), read_stderr(), process.wait()),
                timeout=self.settings.opencode_timeout_seconds,
            )
        except TimeoutError:
            await self._terminate_process(process)
            return ExecutorResult(status="FAILED", exit_code=124, stderr="Agent execution timeout",
                                  failure_code="EXECUTOR_TIMEOUT")
        except Exception:
            await self._terminate_process(process)
            raise
        finally:
            self._processes.pop(task.id, None)

        stdout = b"".join(stdout_lines).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        combined = f"{stdout}\n{stderr}".upper()
        blocked_markers = (
            "WAITING_INPUT",
            "ASTRA_INTERVENTION_JSON:",
            "PERMISSION REQUESTED",
            "PERMISSION DENIED",
            "AUTO-REJECTING",
        )
        status = (
            "WAITING_INPUT"
            if pending_inquiry or any(marker in combined for marker in blocked_markers)
            else ("SUCCEEDED" if process.returncode == 0 else "FAILED")
        )
        token_usage, tool_usage, model_name = parse_opencode_usage(output)
        return ExecutorResult(
            status=status,
            exit_code=process.returncode or 0,
            session_id=detected_session,
            output=output,
            stdout=stdout,
            stderr=stderr,
            model_name=model_name,
            inquiry=pending_inquiry,
            token_usage=token_usage,
            tool_usage=tool_usage,
        )

    async def _execute(self, task: Task, workspace: Path, *, session_id: str | None = None) -> ExecutorResult:
        command = self._command(task, workspace, session_id)
        # opencode run reads stdin until EOF when it is not a TTY, so a pipe
        # here hangs the CLI fallback. Discard stdin instead of leaving it open.
        process = await asyncio.create_subprocess_exec(
            *local_command(command[0], command[1:]),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **process_group_options(),
        )
        return await self._communicate(task, process, session_id=session_id)

    async def start(self, task: Task, workspace: Path) -> ExecutorResult:
        return await self._execute(task, workspace)

    async def _terminate_process(self, process: Any) -> None:
        await terminate_process(process)
