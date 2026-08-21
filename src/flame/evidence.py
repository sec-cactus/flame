"""Audit objective evidence handles — existence + touched this cycle, not re-execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_URL_RE = re.compile(r"https?://[^\s\]\"'<>]+", re.I)
# Paths with a slash or a normal file suffix; also bare names like done.txt
_PATH_RE = re.compile(
    r"(?P<p>(?:[\w.-]+/)+[\w.-]+|[\w.-]+\.(?:txt|md|json|py|ts|js|go|rs|toml|yaml|yml|html|csv|log|sh))\b"
)


@dataclass
class ToolTrace:
    """Handles the agent actually touched this cycle (from stream-json tool_call events)."""

    paths: set[str] = field(default_factory=set)
    commands: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    raw: list[dict[str, Any]] = field(default_factory=list)

    def absorb(self, other: ToolTrace) -> None:
        self.paths |= other.paths
        self.commands |= other.commands
        self.urls |= other.urls
        self.raw.extend(other.raw)

    def empty(self) -> bool:
        return not (self.paths or self.commands or self.urls or self.raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": sorted(self.paths),
            "commands": sorted(self.commands),
            "urls": sorted(self.urls),
            "events": len(self.raw),
        }


def collect_tool_event(event: dict[str, Any], into: ToolTrace) -> None:
    if event.get("type") != "tool_call":
        return
    into.raw.append(
        {
            "subtype": event.get("subtype"),
            "tool_call": event.get("tool_call"),
        }
    )
    payload = event.get("tool_call")
    if not isinstance(payload, dict):
        return
    for key, body in payload.items():
        if not isinstance(body, dict):
            continue
        if key == "function":
            args = body.get("arguments") or body.get("args") or ""
            blob = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            _ingest_blob(blob, into)
            continue
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        for field_name in ("path", "file", "filename", "target", "uri", "url"):
            value = args.get(field_name)
            if value:
                _ingest_blob(str(value), into)
        command = args.get("command") or args.get("cmd")
        if command:
            into.commands.add(" ".join(str(command).split()))
            _ingest_blob(str(command), into)
        query = args.get("query") or args.get("pattern")
        if query:
            _ingest_blob(str(query), into)


def _ingest_blob(text: str, into: ToolTrace) -> None:
    for url in _URL_RE.findall(text):
        into.urls.add(url.rstrip(").,;]"))
    for match in _PATH_RE.finditer(text):
        into.paths.add(match.group("p"))
    # bare relative tokens already in path-like args
    stripped = text.strip().strip("'\"")
    if stripped and ("/" in stripped or "." in stripped) and " " not in stripped and not stripped.startswith("-"):
        if _URL_RE.match(stripped):
            into.urls.add(stripped.rstrip(").,;]"))
        else:
            into.paths.add(stripped)


def extract_handles(texts: list[str]) -> list[tuple[str, str]]:
    """Return (kind, value) handles cited in check strings. kind is path|url|command."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        for url in _URL_RE.findall(text):
            item = ("url", url.rstrip(").,;]"))
            if item not in seen:
                seen.add(item)
                found.append(item)
        for match in _PATH_RE.finditer(text):
            item = ("path", match.group("p"))
            if item not in seen:
                seen.add(item)
                found.append(item)
        # shell-ish tokens: word starting the check after common verbs is too heuristic;
        # treat backtick or `$ ` command spans lightly
        for cmd in re.findall(r"`([^`]+)`", text):
            if cmd.strip():
                item = ("command", " ".join(cmd.split()))
                if item not in seen:
                    seen.add(item)
                    found.append(item)
    return found


@dataclass
class AuditResult:
    ok: bool
    gaps: list[str] = field(default_factory=list)


def audit_checks(
    checks: list[str],
    *,
    workspace: Path,
    trace: ToolTrace,
    fail_open_if_no_trace: bool = True,
) -> AuditResult:
    """
    Audit: cited handles must exist and have been touched this cycle.
    Does not re-execute work or judge whether the conclusion is correct.
    """
    gaps: list[str] = []
    if not checks:
        return AuditResult(ok=True, gaps=[])

    if trace.empty() and fail_open_if_no_trace:
        return AuditResult(
            ok=True,
            gaps=["evidence audit skipped: no tool trace this cycle (fail-open)"],
        )

    handles = extract_handles(checks)
    if not handles:
        return AuditResult(
            ok=False,
            gaps=["checks cite no objective handle (path, url, or `command`)"],
        )

    for kind, value in handles:
        if kind == "path":
            gaps.extend(_audit_path(value, workspace, trace))
        elif kind == "url":
            gaps.extend(_audit_url(value, trace))
        elif kind == "command":
            gaps.extend(_audit_command(value, trace))

    return AuditResult(ok=not gaps, gaps=gaps)


def _audit_path(path: str, workspace: Path, trace: ToolTrace) -> list[str]:
    gaps: list[str] = []
    touched = _path_touched(path, trace)
    exists = _path_exists(path, workspace)
    if not exists:
        gaps.append(f"path not found: {path}")
    if not touched:
        gaps.append(f"path not touched in this cycle's tools: {path}")
    return gaps


def _audit_url(url: str, trace: ToolTrace) -> list[str]:
    if _url_touched(url, trace):
        return []
    return [f"url not touched in this cycle's tools: {url}"]


def _audit_command(command: str, trace: ToolTrace) -> list[str]:
    needle = " ".join(command.split())
    for seen in trace.commands:
        if needle in seen or seen in needle:
            return []
    # also allow command text appearing inside path blobs / raw args via paths set? no
    for raw in trace.raw:
        blob = json.dumps(raw, ensure_ascii=False)
        if needle and needle in blob:
            return []
    return [f"command not seen in this cycle's tools: {command}"]


def _path_exists(path: str, workspace: Path) -> bool:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.exists()
    return (workspace / path).exists() or candidate.exists()


def _path_touched(path: str, trace: ToolTrace) -> bool:
    norms = {path, path.lstrip("./")}
    for seen in trace.paths:
        if seen in norms or seen.endswith(path) or path.endswith(seen):
            return True
        if Path(seen).name == Path(path).name and Path(path).name:
            return True
    for seen_cmd in trace.commands:
        if path in seen_cmd:
            return True
    return False


def _url_touched(url: str, trace: ToolTrace) -> bool:
    if url in trace.urls:
        return True
    for seen in trace.urls:
        if url.rstrip("/") == seen.rstrip("/") or url in seen or seen in url:
            return True
    for raw in trace.raw:
        if url in json.dumps(raw, ensure_ascii=False):
            return True
    return False


def write_trace(path: Path, trace: ToolTrace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_trace_for_prompt(trace: ToolTrace, *, limit: int = 40) -> str:
    if trace.empty():
        return "(no tool handles recorded this cycle)"
    lines: list[str] = []
    for label, values in (
        ("paths", sorted(trace.paths)),
        ("commands", sorted(trace.commands)),
        ("urls", sorted(trace.urls)),
    ):
        if not values:
            continue
        lines.append(f"{label}:")
        for item in values[:limit]:
            lines.append(f"- {item}")
    return "\n".join(lines) if lines else "(no tool handles recorded this cycle)"
