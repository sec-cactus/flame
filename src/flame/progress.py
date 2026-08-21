from __future__ import annotations

import sys
import threading
from typing import TextIO


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class Progress:
    """Human progress on stderr. stdout stays clean for the final answer."""

    def __init__(self, stream: TextIO | None = None, enabled: bool = True):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self._lock = threading.Lock()

    def line(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.stream.write(message + "\n")
            self.stream.flush()

    def phase(self, name: str, detail: str = "") -> None:
        suffix = f"  {detail}" if detail else ""
        self.line(f"▶ {name}{suffix}")

    def note(self, message: str) -> None:
        self.line(f"  · {message}")

    def tool(self, summary: str, done: bool = False) -> None:
        mark = "ok" if done else "tool"
        self.line(f"  · {mark}  {summary}")

    def assistant(self, text: str) -> None:
        clipped = _truncate(text)
        if not clipped or clipped.startswith("{") or clipped.startswith("```"):
            return
        self.line(f"  · {clipped}")

    def fail(self, message: str) -> None:
        self.line(f"  ✗ {message}")

    def done(self, message: str) -> None:
        self.line(f"✓ {message}")
