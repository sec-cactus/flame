"""Hard JSON contracts for Flame stage artifacts. Extra keys are rejected."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PLAN_KEYS = frozenset(
    {"goal", "approach", "summary", "constraints", "verify_points", "use_ledger", "degraded"}
)
ACT_KEYS = frozenset({"summary", "deliverables"})
VERIFY_KEYS = frozenset(
    {
        "points_met",
        "aligned",
        "evidence_ok",
        "retry",
        "summary",
        "checks",
        "drift",
        "evidence_gaps",
        "diagnosis",
        "passed",
        "degraded",
    }
)
BRIEF_KEYS = frozenset(
    {
        "schema",
        "judge",
        "quadrants",
        "success_factors",
        "failure_factors",
        "decisive_move",
        "summary",
    }
)
MELD_JUDGE_KEYS = frozenset(
    {
        "consensus",
        "contradictions",
        "unique_insights",
        "blind_spots",
        "verification_needed",
    }
)
QUADRANT_KEYS = frozenset(
    {"known_knowns", "known_unknowns", "unknown_knowns", "unknown_unknowns"}
)
FACTORS_KEYS = frozenset(
    {"success_factors", "failure_factors", "decisive_move", "summary"}
)

_MTIME_SLACK = 1.0


def extra_keys(payload: dict[str, Any], allowed: frozenset[str]) -> list[str]:
    return sorted(str(k) for k in payload if str(k) not in allowed)


def _need_object(payload: Any, label: str) -> list[str]:
    if isinstance(payload, dict):
        return []
    return [f"{label} must be a JSON object"]


def _need_str(payload: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    if key not in payload:
        return [f"missing field: {key}"] if required else []
    if not isinstance(payload[key], str):
        return [f"{key} must be a string"]
    return []


def _need_str_list(payload: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    if key not in payload:
        return [f"missing field: {key}"] if required else []
    value = payload[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return [f"{key} must be a list of strings"]
    return []


def _need_bool(payload: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    if key not in payload:
        return [f"missing field: {key}"] if required else []
    if not isinstance(payload[key], bool):
        return [f"{key} must be a boolean"]
    return []


def validate_plan_payload(payload: Any, *, ask_use_ledger: bool) -> list[str]:
    gaps = _need_object(payload, "plan.json")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, PLAN_KEYS)
    if extra:
        gaps.append("plan.json extra keys: " + ", ".join(extra))
    gaps.extend(_need_str(payload, "goal"))
    gaps.extend(_need_str(payload, "approach", required=True))
    gaps.extend(_need_str(payload, "summary"))
    gaps.extend(_need_str_list(payload, "constraints"))
    gaps.extend(_need_str_list(payload, "verify_points"))
    if ask_use_ledger:
        gaps.extend(_need_bool(payload, "use_ledger"))
    elif "use_ledger" in payload:
        gaps.extend(_need_bool(payload, "use_ledger"))
    return gaps


def validate_act_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "act.json")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, ACT_KEYS)
    if extra:
        gaps.append("act.json extra keys: " + ", ".join(extra))
    gaps.extend(_need_str(payload, "summary"))
    gaps.extend(_need_str_list(payload, "deliverables"))
    return gaps


def validate_verify_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "verify.json")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, VERIFY_KEYS)
    if extra:
        gaps.append("verify.json extra keys: " + ", ".join(extra))
    for key in ("points_met", "aligned", "evidence_ok", "retry", "passed"):
        gaps.extend(_need_bool(payload, key))
    gaps.extend(_need_str(payload, "summary"))
    gaps.extend(_need_str(payload, "diagnosis"))
    for key in ("checks", "drift", "evidence_gaps"):
        gaps.extend(_need_str_list(payload, key))
    return gaps


def validate_brief_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "brief.json")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, BRIEF_KEYS)
    if extra:
        gaps.append("brief.json extra keys: " + ", ".join(extra))
    return gaps


def validate_meld_judge_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "meld-judge.json")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, MELD_JUDGE_KEYS)
    if extra:
        gaps.append("meld-judge.json extra keys: " + ", ".join(extra))
    return gaps


def validate_quadrants_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "quadrants")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, QUADRANT_KEYS)
    if extra:
        gaps.append("quadrants extra keys: " + ", ".join(extra))
    return gaps


def validate_factors_payload(payload: Any) -> list[str]:
    gaps = _need_object(payload, "factors")
    if gaps:
        return gaps
    assert isinstance(payload, dict)
    extra = extra_keys(payload, FACTORS_KEYS)
    if extra:
        gaps.append("factors extra keys: " + ", ".join(extra))
    return gaps


def strip_to_allowed(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: payload[key] for key in payload if key in allowed}


def answer_md_path(workspace: Path) -> Path | None:
    """Same preference as the job UI: `.flame/answer.md`, then workspace `answer.md`."""
    flame = workspace / ".flame" / "answer.md"
    if flame.is_file():
        return flame
    root = workspace / "answer.md"
    if root.is_file():
        return root
    return None


def audit_answer_vs_plan(workspace: Path, *, plan_mtime: float) -> list[str]:
    """answer.md must exist and not be older than this cycle's plan.json."""
    path = answer_md_path(workspace)
    if path is None:
        return [
            "no answer.md this cycle (write answer.md or .flame/answer.md; the UI displays that file)"
        ]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [f"cannot stat {path.as_posix()}"]
    if mtime < plan_mtime - _MTIME_SLACK:
        rel = path.name if path.parent.name != ".flame" else ".flame/answer.md"
        return [
            f"{rel} is older than this cycle's plan.json "
            "(leftover file is not this round's answer)"
        ]
    return []
