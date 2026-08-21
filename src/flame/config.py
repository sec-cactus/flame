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


@dataclass
class Config:
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
        return cls(
            agent_bin=agent_bin or _env("FLAME_AGENT_BIN", "agent"),
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
        if not found:
            raise FileNotFoundError(
                f"agent binary not found: {self.agent_bin}. "
                "Install Cursor CLI or set FLAME_AGENT_BIN."
            )
        return found
