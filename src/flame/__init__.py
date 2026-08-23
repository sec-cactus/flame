"""Flame: a thin plan-act-verify harness over the local Cursor agent."""

from flame.loop import continue_run, run
from flame.types import RunResult

__all__ = ["run", "continue_run", "RunResult"]
