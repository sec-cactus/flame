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


def ask_use_ledger(effort: Effort) -> bool:
    """Plan is asked for use_ledger only on high."""
    return effort is Effort.high


def use_jspace(effort: Effort, use_ledger: bool | None) -> bool:
    """high + use_ledger (default True) → j-space on act."""
    if effort is not Effort.high:
        return False
    return True if use_ledger is None else bool(use_ledger)


def use_factgraph(effort: Effort) -> bool:
    """max → act always opens fact-graph."""
    return effort is Effort.max
