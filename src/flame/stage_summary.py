"""User-facing stage summaries for job UI (Cairn pipeline chips)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SUMMARY_MAX = 120


def clip_summary(text: str, limit: int = SUMMARY_MAX) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?。！？])\s+", text, maxsplit=1)
    return parts[0].strip()


def brief_summary(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = str(payload.get("summary") or payload.get("decisive_move") or "").strip()
    return clip_summary(raw)


def plan_summary(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = str(payload.get("summary") or "").strip()
    if raw:
        return clip_summary(raw)
    approach = payload.get("approach")
    if isinstance(approach, list):
        approach = " ".join(str(x).strip() for x in approach if str(x).strip())
    else:
        approach = str(approach or "").strip()
    return clip_summary(first_sentence(approach) or approach)


def act_summary(payload: dict[str, Any] | None, *, workspace: Path | None = None) -> str:
    if isinstance(payload, dict):
        raw = str(payload.get("summary") or "").strip()
        if raw:
            return clip_summary(raw)
    if workspace is not None:
        ws = Path(workspace)
        if (ws / ".flame" / "answer.md").is_file():
            return "答案已写入 answer.md"
        if (ws / "answer.md").is_file():
            return "答案已写入 answer.md"
        if (ws / "done.txt").is_file():
            return "已创建 done.txt"
    return ""


def verify_summary(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = str(payload.get("summary") or "").strip()
    if raw:
        return clip_summary(raw)
    passed = payload.get("passed")
    checks = payload.get("checks")
    n = len(checks) if isinstance(checks, list) else 0
    points_met = payload.get("points_met")
    evidence_ok = payload.get("evidence_ok")
    if passed is True:
        return f"✓ 验收通过（{n} 项检查）" if n else "✓ 验收通过"
    if passed is False:
        if points_met is True and evidence_ok is False:
            return f"✗ 实质通过，证据不完整（{n} 项检查）" if n else "✗ 实质通过，证据不完整"
        diag = str(payload.get("diagnosis") or "").strip()
        if diag:
            return clip_summary(diag)
        return "✗ 验收未通过"
    return ""


def synthesize_act_summary(
    workspace: Path,
    act_text: str = "",
    *,
    timed_out: bool = False,
    graph_note: str = "",
) -> str:
    if timed_out:
        return "Act 超时，已移交 verify 判断现有产物"
    if graph_note.strip():
        return clip_summary(graph_note)
    from_file = act_summary(None, workspace=workspace)
    if from_file:
        return from_file
    text = act_text.strip()
    if text and not text.startswith(("{", "```")):
        line = first_sentence(text)
        if line and len(line) > 12:
            return clip_summary(line)
    return "执行完成"
