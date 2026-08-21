from __future__ import annotations

from flame.types import Effort

# Runaway guard for standard/high/max. Fast is always one verify round.
SAFETY_MAX_CYCLES = 8


def cycle_limit(effort: Effort, safety_cap: int) -> int:
    if effort is Effort.fast:
        return 1
    return safety_cap


def use_preprocess(effort: Effort) -> bool:
    return effort is not Effort.fast


def use_meld(effort: Effort) -> bool:
    return effort is Effort.max


def use_act_skills(effort: Effort) -> bool:
    """j-space / fact-graph are only offered on high and max."""
    return effort in {Effort.high, Effort.max}
