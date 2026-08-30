from __future__ import annotations

from flame.types import Effort

# Runaway guard for standard/ledger/meld/graph. Fast is always one verify round.
SAFETY_MAX_CYCLES = 8


def cycle_limit(effort: Effort, safety_cap: int) -> int:
    if effort is Effort.fast:
        return 1
    return safety_cap


def use_preprocess(effort: Effort) -> bool:
    return effort is not Effort.fast


def use_act_meld(effort: Effort) -> bool:
    return effort is Effort.meld


def ask_use_jspace(effort: Effort) -> bool:
    """Plan is asked for use_jspace only on ledger."""
    return effort is Effort.ledger


def use_jspace(effort: Effort, enabled: bool | None) -> bool:
    """ledger + use_jspace true → j-space on act. Default is applied in the loop, not here."""
    return effort is Effort.ledger and bool(enabled)


def use_factgraph(effort: Effort) -> bool:
    """graph → act always opens fact-graph (no j-space)."""
    return effort is Effort.graph
