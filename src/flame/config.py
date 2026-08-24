from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from flame.types import Effort
from flame.budget import SAFETY_MAX_CYCLES


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


DEFAULT_AGENT_BACKEND = "cursor"
DEFAULT_OPENCODE_MODEL = "opencode-go/deepseek-v4-flash"
ALLOWED_AGENT_BACKENDS = frozenset({"cursor", "opencode"})


def normalize_agent_backend(value: str | None) -> str:
    name = (value or DEFAULT_AGENT_BACKEND).strip().lower()
    if name not in ALLOWED_AGENT_BACKENDS:
        raise ValueError(
            f"unknown agent backend: {name} (expected cursor or opencode)"
        )
    return name


def resolve_runtime_model(config: "Config") -> str:
    """Cursor accepts ``auto``; OpenCode needs ``provider/model``."""
    model = (config.model or "").strip() or "auto"
    if normalize_agent_backend(config.agent_backend) == "opencode":
        if model == "auto":
            return _env("FLAME_OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL)
        if "/" not in model:
            raise ValueError(
                f"invalid opencode model: {model} (use provider/model, "
                f"e.g. {DEFAULT_OPENCODE_MODEL})"
            )
    return model


@dataclass
class Config:
    agent_backend: str
    agent_bin: str
    model: str
    workspace: Path
    effort: Effort
    log_dir: Path
    timeout_sec: int
    force: bool
    trust: bool
    extra_args: list[str]
    safety_gate: bool = False
    max_cycles: int = 8

    @classmethod
    def load(
        cls,
        *,
        workspace: str | Path | None = None,
        effort: str | None = None,
        model: str | None = None,
        agent_backend: str | None = None,
        agent_bin: str | None = None,
        force: bool | None = None,
        safety_gate: bool | None = None,
    ) -> Config:
        ws = Path(workspace or _env("FLAME_WORKSPACE", os.getcwd())).resolve()
        effort_name = (effort or _env("FLAME_EFFORT", "standard")).lower()
        if effort_name == "low":
            effort_name = "fast"
        elif effort_name == "medium":
            effort_name = "standard"
        try:
            effort_val = Effort(effort_name)
        except ValueError as exc:
            raise ValueError(f"unknown effort: {effort_name}") from exc
        log_dir = Path(_env("FLAME_LOG_DIR", str(ws / ".flame" / "logs"))).resolve()
        timeout_raw = _env("FLAME_TIMEOUT_SEC", "1800")
        try:
            timeout_sec = int(timeout_raw)
        except ValueError as exc:
            raise ValueError(f"invalid FLAME_TIMEOUT_SEC: {timeout_raw}") from exc
        no_force = _env_bool("FLAME_NO_FORCE")
        cycles_raw = _env("FLAME_MAX_CYCLES", str(SAFETY_MAX_CYCLES))
        try:
            max_cycles = int(cycles_raw)
        except ValueError as exc:
            raise ValueError(f"invalid FLAME_MAX_CYCLES: {cycles_raw}") from exc
        if max_cycles < 1:
            raise ValueError("FLAME_MAX_CYCLES must be >= 1")
        backend = normalize_agent_backend(
            agent_backend or _env("FLAME_AGENT_BACKEND", DEFAULT_AGENT_BACKEND)
        )
        default_bin = "opencode" if backend == "opencode" else "agent"
        return cls(
            agent_backend=backend,
            agent_bin=agent_bin or _env("FLAME_AGENT_BIN", default_bin),
            model=model or _env("FLAME_MODEL", "auto"),
            workspace=ws,
            effort=effort_val,
            log_dir=log_dir,
            timeout_sec=timeout_sec,
            force=False if no_force else (True if force is None else force),
            trust=True,
            extra_args=[],
            safety_gate=_env_bool("FLAME_SAFETY") if safety_gate is None else safety_gate,
            max_cycles=max_cycles,
        )

    def resolve_agent_bin(self) -> str:
        path = Path(self.agent_bin)
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(self.agent_bin)
        if not found and normalize_agent_backend(self.agent_backend) == "opencode":
            from flame.agent_backends import find_opencode_bin

            found = find_opencode_bin()
        if not found:
            raise FileNotFoundError(
                f"agent binary not found: {self.agent_bin} "
                f"(backend={self.agent_backend}). "
                "Install the CLI or set FLAME_AGENT_BIN."
            )
        return found
