from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Effort(StrEnum):
    fast = "fast"
    standard = "standard"
    high = "high"
    max = "max"


class Phase(StrEnum):
    preprocess = "preprocess"
    meld = "meld"
    quadrants = "quadrants"
    factors = "factors"
    plan = "plan"
    act = "act"
    verify = "verify"


BRIEF_SCHEMA = "flame.brief.v1"

QUADRANT_KEYS = (
    "known_knowns",
    "known_unknowns",
    "unknown_knowns",
    "unknown_unknowns",
)


@dataclass
class Brief:
    """Preprocess briefing. Python assembles this; agents fill pieces."""

    judge: dict | None = None
    quadrants: dict[str, list[str]] = field(default_factory=dict)
    success_factors: list[str] = field(default_factory=list)
    failure_factors: list[str] = field(default_factory=list)
    decisive_move: str = ""

    def empty(self) -> bool:
        has_q = any(self.quadrants.get(k) for k in QUADRANT_KEYS)
        return (
            self.judge is None
            and not has_q
            and not self.success_factors
            and not self.failure_factors
            and not self.decisive_move
        )

    def to_dict(self) -> dict:
        return {
            "schema": BRIEF_SCHEMA,
            "judge": self.judge,
            "quadrants": {key: list(self.quadrants.get(key) or []) for key in QUADRANT_KEYS},
            "success_factors": self.success_factors[:3],
            "failure_factors": self.failure_factors[:3],
            "decisive_move": self.decisive_move,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Brief:
        quadrants = payload.get("quadrants") if isinstance(payload.get("quadrants"), dict) else {}
        judge = payload.get("judge")
        return cls(
            judge=judge if isinstance(judge, dict) else None,
            quadrants={key: _brief_str_list(quadrants.get(key), 5) for key in QUADRANT_KEYS},
            success_factors=_brief_str_list(payload.get("success_factors"), 3),
            failure_factors=_brief_str_list(payload.get("failure_factors"), 3),
            decisive_move=str(payload.get("decisive_move") or "").strip(),
        )

    def render_for_plan(self) -> str:
        """Compact, labeled brief for the planner — not a raw JSON dump."""
        lines: list[str] = []
        if self.decisive_move:
            lines.append(f"decisive_move: {self.decisive_move}")
        if self.success_factors:
            lines.append("success_factors:")
            lines.extend(f"- {item}" for item in self.success_factors)
        if self.failure_factors:
            lines.append("failure_factors:")
            lines.extend(f"- {item}" for item in self.failure_factors)
        q = self.quadrants
        if any(q.get(key) for key in QUADRANT_KEYS):
            lines.append("quadrants:")
            for key in QUADRANT_KEYS:
                items = q.get(key) or []
                if not items:
                    continue
                lines.append(f"  {key}:")
                lines.extend(f"  - {item}" for item in items)
        if self.judge:
            lines.append("meld_judge (hypotheses, not facts):")
            for key in (
                "consensus",
                "contradictions",
                "unique_insights",
                "blind_spots",
                "verification_needed",
            ):
                value = self.judge.get(key)
                if not value:
                    continue
                lines.append(f"  {key}:")
                if isinstance(value, list):
                    for item in value[:5]:
                        lines.append(f"  - {item}")
                else:
                    lines.append(f"  - {value}")
        return "\n".join(lines) if lines else ""


def _brief_str_list(value: object, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:cap]


@dataclass
class Plan:
    goal: str
    approach: str
    constraints: list[str]
    verify_points: list[str]
    use_ledger: bool | None = None  # high only: mount j-space; default True when asked
    degraded: bool = False


@dataclass
class VerifyResult:
    passed: bool
    points_met: bool
    aligned: bool
    evidence_ok: bool
    retry: bool
    checks: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    diagnosis: str = ""
    degraded: bool = False


@dataclass
class AgentResult:
    text: str
    session_id: str = ""
    model: str = ""
    duration_ms: int = 0
    is_error: bool = False
    returncode: int = 0
    stderr: str = ""
    timed_out: bool = False


@dataclass
class RunResult:
    output: str
    passed: bool
    cycles: int
    log_path: Path
    plan: Plan | None = None
    verify: VerifyResult | None = None
