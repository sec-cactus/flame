from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def factgraph_dir() -> Path | None:
    """Directory that contains fact-graph SKILL.md and scripts/orchestrator.py."""
    override = _env_path("FLAME_FACTGRAPH")
    if override is not None:
        return override if _is_factgraph(override) else None
    bundled = Path(__file__).resolve().parent / "data" / "fact-graph"
    if _is_factgraph(bundled):
        return bundled
    return None


def jspace_dir() -> Path | None:
    """Directory that contains j-space SKILL.md. Not bundled; see docs/SKILLS.md."""
    override = _env_path("FLAME_JSPACE")
    if override is not None:
        return override if _is_jspace(override) else None
    for path in default_jspace_candidates():
        if _is_jspace(path):
            return path
    return None


def default_jspace_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / ".cursor" / "skills-cursor" / "j-space",
        home / ".cursor" / "skills" / "j-space",
    ]


def _is_jspace(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _is_factgraph(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "SKILL.md").is_file()
        and (path / "scripts" / "orchestrator.py").is_file()
    )
