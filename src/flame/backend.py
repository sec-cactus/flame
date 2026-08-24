from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from flame.config import Config
from flame.progress import Progress

EventHandler = Callable[[dict[str, Any]], None]


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort object extraction from model text."""
    if not text:
        return None
    stripped = text.strip()
    fenced = _fenced_json(stripped)
    if fenced is not None:
        return fenced
    return _first_object(stripped)


def _fenced_json(text: str) -> dict[str, Any] | None:
    marker = "```json"
    start = text.find(marker)
    if start < 0:
        start = text.find("```")
        if start < 0:
            return None
        start = text.find("\n", start)
        if start < 0:
            return None
        start += 1
    else:
        start = text.find("\n", start)
        if start < 0:
            return None
        start += 1
    end = text.find("```", start)
    blob = text[start:end if end >= 0 else None].strip()
    try:
        value = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _first_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def describe_tool(event: dict[str, Any]) -> str:
    payload = event.get("tool_call") or {}
    if not isinstance(payload, dict):
        return "tool"
    for key, body in payload.items():
        if not isinstance(body, dict):
            continue
        if key == "function":
            name = str(body.get("name") or "function")
            args = body.get("arguments") or body.get("args") or ""
            hint = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            return f"{name} {_clip(hint, 80)}"
        name = key.replace("ToolCall", "")
        args = body.get("args") or body.get("input") or {}
        hint = ""
        if isinstance(args, dict):
            hint = str(
                args.get("path")
                or args.get("filePath")
                or args.get("command")
                or args.get("query")
                or args.get("pattern")
                or ""
            )
        return f"{name} {_clip(hint, 80)}".strip()
    return "tool"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def assistant_text(event: dict[str, Any]) -> str:
    message = event.get("message") or {}
    content = message.get("content") or []
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
    elif isinstance(content, str):
        parts.append(content)
    return "".join(parts)


def is_partial_delta(event: dict[str, Any]) -> bool:
    return "timestamp_ms" in event and "model_call_id" not in event


def is_duplicate_assistant(event: dict[str, Any]) -> bool:
    # Skip buffered flushes (duplicates). Keep complete messages and live deltas.
    if event.get("type") != "assistant":
        return False
    has_ts = "timestamp_ms" in event
    has_mc = "model_call_id" in event
    if has_ts and has_mc:
        return True
    if not has_ts and not has_mc:
        # Final flush at end of turn when streaming — duplicate of deltas.
        # Without --stream-partial-output this is the real complete message.
        return False
    return False


def create_agent_backend(config: Config, progress: Progress | None = None):
    from flame.agent_backends import create_agent_backend as _factory

    return _factory(config, progress)
