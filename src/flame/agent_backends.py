from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from flame.config import normalize_agent_backend, resolve_runtime_model
from flame.progress import Progress
from flame.types import AgentResult, Phase

EventHandler = Callable[[dict[str, Any]], None]


class AgentBackend(Protocol):
    def run(
        self,
        prompt: str,
        *,
        phase: Phase,
        force: bool,
        mode: str | None,
        on_event: EventHandler | None = None,
    ) -> AgentResult: ...

    def run_parallel(self, jobs: Sequence[dict[str, Any]]) -> list[AgentResult]: ...


def find_opencode_bin() -> str | None:
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (Path("/usr/bin/opencode"), Path("/usr/local/bin/opencode")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def normalize_opencode_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map OpenCode JSONL events to Cursor-like shapes for evidence/progress."""
    kind = event.get("type")
    if kind == "tool_use":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool = str(part.get("tool") or "tool")
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        status = str(state.get("status") or "")
        return {
            "type": "tool_call",
            "subtype": "completed" if status == "completed" else "started",
            "tool_call": {tool: {"args": inp}},
        }
    if kind == "text":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        text = str(part.get("text") or "")
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    if kind == "error":
        err = event.get("error")
        msg = err
        if isinstance(err, dict):
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            msg = data.get("message") or err.get("name") or err
        return {"type": "result", "result": str(msg), "is_error": True}
    return event


def _agent_env() -> dict[str, str]:
    env = os.environ.copy()
    home = env.get("HOME") or str(Path.home())
    env["HOME"] = home
    env.setdefault("XDG_CACHE_HOME", str(Path(home) / ".cache"))
    env.setdefault("NO_OPEN_BROWSER", "1")
    return env


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
    on_line: Callable[[dict[str, Any]], None],
) -> tuple[int, list[str], bool]:
    stderr_chunks: list[str] = []
    timed_out = False
    code = 1
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=_agent_env(),
        start_new_session=True,
    ) as proc:
        assert proc.stdout is not None
        assert proc.stderr is not None

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()
        stop_watchdog = threading.Event()

        def _watchdog() -> None:
            nonlocal timed_out
            if not stop_watchdog.wait(timeout_sec):
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    on_line(event)
        finally:
            stop_watchdog.set()
            proc.wait()
            drain.join(timeout=2)
            code = proc.returncode if proc.returncode is not None else 1
    return code, stderr_chunks, timed_out


class _ParallelMixin:
    def run_parallel(self, jobs: Sequence[dict[str, Any]]) -> list[AgentResult]:
        if not jobs:
            return []
        if len(jobs) == 1:
            return [self.run(**jobs[0])]  # type: ignore[attr-defined]
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(self.run, **job) for job in jobs]  # type: ignore[attr-defined]
            return [fut.result() for fut in futures]


class CursorBackend(_ParallelMixin):
    def __init__(self, config: Any, progress: Progress | None = None):
        self.config = config
        self.progress = progress or Progress(enabled=False)
        self.bin = config.resolve_agent_bin()

    def run(
        self,
        prompt: str,
        *,
        phase: Phase,
        force: bool,
        mode: str | None,
        on_event: EventHandler | None = None,
    ) -> AgentResult:
        from flame.backend import (
            assistant_text,
            describe_tool,
            is_duplicate_assistant,
            is_partial_delta,
        )

        cmd = [
            self.bin,
            "-p",
            "--model",
            resolve_runtime_model(self.config),
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--workspace",
            str(self.config.workspace),
        ]
        if self.config.trust:
            cmd.append("--trust")
        if force and self.config.force:
            cmd.append("--force")
        if mode:
            cmd.extend(["--mode", mode])
        cmd.extend(self.config.extra_args)
        cmd.extend(["--", prompt])

        mode_bit = f" --mode {mode}" if mode else ""
        model = resolve_runtime_model(self.config)
        self.progress.note(f"spawn {phase.value} {self.bin} --model {model}{mode_bit}")

        result_text = ""
        session_id = ""
        duration_ms = 0
        is_error = False
        saw_result = False
        assistant_parts: list[str] = []

        def _on_line(event: dict[str, Any]) -> None:
            nonlocal result_text, session_id, model, duration_ms, is_error, saw_result
            if on_event:
                on_event(event)
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                model = str(event.get("model") or model)
                session_id = str(event.get("session_id") or session_id)
                self.progress.note(f"model {model}")
            elif kind == "tool_call":
                summary = describe_tool(event)
                done = event.get("subtype") == "completed"
                self.progress.tool(summary, done=done)
            elif kind == "assistant":
                if is_partial_delta(event) or is_duplicate_assistant(event):
                    return
                if "timestamp_ms" in event:
                    return
                text = assistant_text(event).strip()
                if text:
                    assistant_parts.append(text)
                    self.progress.assistant(text)
            elif kind == "result":
                saw_result = True
                result_text = str(event.get("result") or "")
                session_id = str(event.get("session_id") or session_id)
                duration_ms = int(event.get("duration_ms") or 0)
                is_error = bool(event.get("is_error"))

        code, stderr_chunks, timed_out = _run_subprocess(
            cmd,
            cwd=self.config.workspace,
            timeout_sec=self.config.timeout_sec,
            on_line=_on_line,
        )
        if timed_out:
            is_error = True
            if not result_text:
                partial = "\n".join(assistant_parts).strip()
                prefix = f"agent timed out after {self.config.timeout_sec}s"
                result_text = f"{prefix}\n{partial}".strip() if partial else prefix
        if code != 0 and not is_error:
            is_error = True
        if not saw_result and not result_text:
            result_text = "".join(stderr_chunks).strip() or (
                f"agent exited {code} without a result event"
            )
            is_error = True
        return AgentResult(
            text=result_text,
            session_id=session_id,
            model=model,
            duration_ms=duration_ms,
            is_error=is_error or code != 0,
            returncode=code,
            stderr="".join(stderr_chunks),
            timed_out=timed_out,
        )


class OpenCodeBackend(_ParallelMixin):
    def __init__(self, config: Any, progress: Progress | None = None):
        self.config = config
        self.progress = progress or Progress(enabled=False)
        self.bin = config.resolve_agent_bin()

    def run(
        self,
        prompt: str,
        *,
        phase: Phase,
        force: bool,
        mode: str | None,
        on_event: EventHandler | None = None,
    ) -> AgentResult:
        from flame.backend import describe_tool

        model = resolve_runtime_model(self.config)
        cmd = [
            self.bin,
            "run",
            "--format",
            "json",
            "--auto",
            "--model",
            model,
            "--dir",
            str(self.config.workspace),
        ]
        if mode == "ask":
            cmd.extend(["--agent", "plan"])
        cmd.extend(self.config.extra_args)
        cmd.extend(["--", prompt])

        self.progress.note(f"spawn {phase.value} {self.bin} run --model {model}")

        result_text = ""
        session_id = ""
        duration_ms = 0
        is_error = False
        assistant_parts: list[str] = []
        error_messages: list[str] = []

        def _on_line(event: dict[str, Any]) -> None:
            nonlocal result_text, session_id, duration_ms, is_error
            normalized = normalize_opencode_event(event)
            if on_event:
                on_event(normalized)
            sid = event.get("sessionID")
            if sid:
                session_id = str(sid)
            kind = event.get("type")
            if kind == "tool_use":
                summary = describe_tool(normalized)
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                done = str(state.get("status") or "") == "completed"
                self.progress.tool(summary, done=done)
            elif kind == "text":
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                text = str(part.get("text") or "").strip()
                if text:
                    assistant_parts.append(text)
                    self.progress.assistant(text)
            elif kind == "error":
                is_error = True
                err = event.get("error")
                if isinstance(err, dict):
                    data = err.get("data") if isinstance(err.get("data"), dict) else {}
                    error_messages.append(
                        str(data.get("message") or err.get("name") or err)
                    )
                else:
                    error_messages.append(str(err))
            elif kind == "step_finish":
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                if tokens:
                    duration_ms = int(tokens.get("total") or 0)

        code, stderr_chunks, timed_out = _run_subprocess(
            cmd,
            cwd=self.config.workspace,
            timeout_sec=self.config.timeout_sec,
            on_line=_on_line,
        )
        if error_messages:
            result_text = "; ".join(error_messages)
            is_error = True
        elif assistant_parts:
            result_text = "\n".join(assistant_parts).strip()
        if timed_out:
            is_error = True
            if not result_text:
                result_text = f"opencode timed out after {self.config.timeout_sec}s"
        if code != 0 and not is_error:
            is_error = True
        if not result_text:
            result_text = "".join(stderr_chunks).strip() or f"opencode exited {code}"
            if code != 0:
                is_error = True
        return AgentResult(
            text=result_text,
            session_id=session_id,
            model=model,
            duration_ms=duration_ms,
            is_error=is_error or code != 0,
            returncode=code,
            stderr="".join(stderr_chunks),
            timed_out=timed_out,
        )


def create_agent_backend(config: Any, progress: Progress | None = None) -> AgentBackend:
    backend = normalize_agent_backend(config.agent_backend)
    if backend == "opencode":
        return OpenCodeBackend(config, progress)
    return CursorBackend(config, progress)
