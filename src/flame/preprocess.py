from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flame import budget, prompts
from flame.agent_backends import AgentBackend
from flame.backend import extract_json
from flame.log import SessionLog
from flame.progress import Progress
from flame.types import Brief, Effort, Phase, QUADRANT_KEYS


class PreprocessResult:
    def __init__(self, *, brief: str = "", degraded: bool = False):
        self.brief = brief
        self.degraded = degraded


def run_preprocess(
    backend: AgentBackend,
    log: SessionLog,
    progress: Progress,
    original_task: str,
    flame_dir: Path,
    effort: Effort,
) -> PreprocessResult:
    """One-shot intake. Never raises; worst case returns no brief."""
    if not budget.use_preprocess(effort):
        return PreprocessResult()

    progress.phase("preprocess")
    log.emit("phase", phase="preprocess")
    try:
        return _build_brief(backend, log, progress, original_task, flame_dir)
    except Exception as err:  # noqa: BLE001 — preprocess must not block plan
        progress.fail(f"preprocess failed, using original: {err}")
        log.emit("preprocess_degraded", error=str(err))
        return PreprocessResult(degraded=True)


def _build_brief(
    backend: AgentBackend,
    log: SessionLog,
    progress: Progress,
    task: str,
    flame_dir: Path,
) -> PreprocessResult:
    brief = Brief()
    brief.quadrants = _quadrants(backend, log, progress, task)
    success, failure, move, summary = _factors(
        backend, log, progress, task, brief.quadrants
    )
    brief.success_factors = success
    brief.failure_factors = failure
    brief.decisive_move = move
    brief.summary = summary or move

    if brief.empty():
        progress.fail("preprocess produced nothing, using original")
        return PreprocessResult(degraded=True)

    payload = brief.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (flame_dir / "brief.json").write_text(text + "\n", encoding="utf-8")
    return PreprocessResult(brief=text)


def _quadrants(
    backend: AgentBackend,
    log: SessionLog,
    progress: Progress,
    task: str,
) -> dict[str, list[str]]:
    progress.note("quadrants")
    result = backend.run(
        prompts.quadrants_prompt(task),
        phase=Phase.quadrants,
        force=False,
        mode="ask",
    )
    log.emit("agent_done", phase="quadrants", error=result.is_error, code=result.returncode)
    empty = {key: [] for key in QUADRANT_KEYS}
    if result.is_error or not result.text.strip():
        progress.fail("quadrants failed; factors continue with an empty table")
        return empty
    payload = extract_json(result.text)
    if not isinstance(payload, dict):
        progress.fail("quadrants JSON missing; factors continue with an empty table")
        return empty
    return {key: _str_list(payload.get(key), cap=5) for key in QUADRANT_KEYS}


def _factors(
    backend: AgentBackend,
    log: SessionLog,
    progress: Progress,
    task: str,
    quadrants: dict[str, list[str]],
) -> tuple[list[str], list[str], str, str]:
    progress.note("factors")
    qtext = json.dumps(quadrants, ensure_ascii=False, indent=2)
    result = backend.run(
        prompts.factors_prompt(task, qtext),
        phase=Phase.factors,
        force=False,
        mode="ask",
    )
    log.emit("agent_done", phase="factors", error=result.is_error, code=result.returncode)
    if result.is_error or not result.text.strip():
        progress.fail("factors failed; brief keeps quadrants only")
        return [], [], "", ""
    payload = extract_json(result.text)
    if not isinstance(payload, dict):
        progress.fail("factors JSON missing; brief keeps quadrants only")
        return [], [], "", ""
    move = str(payload.get("decisive_move") or "").strip()
    summary = str(payload.get("summary") or move).strip()
    return (
        _str_list(payload.get("success_factors"), cap=3),
        _str_list(payload.get("failure_factors"), cap=3),
        move,
        summary,
    )


def _str_list(value: Any, *, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    return items[:cap]
