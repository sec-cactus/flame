#!/usr/bin/env python3
"""fact-graph orchestrator — 黑板架构(事实-意图图)多智能体协同推理调度器。

编排器是唯一写板人: worker(Cursor CLI `agent` / mock) 只接收渲染好的 prompt 并返回
结构化 JSON; claim、超时、二阶段收尾、结果校验与写回 board.json 全部在这里完成。

纯 stdlib, 要求 Python >= 3.11 (tomllib)。

用法:
  orchestrator.py init   --run-dir D --title T --origin O --goal G [--constraints C] [--config C] [--no-bootstrap]
  orchestrator.py run    --run-dir D [--once] [--extra-budget SECONDS]
  orchestrator.py hint   --run-dir D --content "..." [--creator human]
  orchestrator.py status --run-dir D
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = SKILL_DIR / "prompts"

TASK_TYPES = ("bootstrap", "reason", "explore")
CONCLUDE_PHASE = {"bootstrap": "bootstrap_conclude", "explore": "explore_conclude"}
MOCK_PHASES = ("bootstrap", "bootstrap_conclude", "reason", "explore_execute", "explore_conclude")
MOCK_OUTCOMES = {
    "bootstrap": {"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"},
    "bootstrap_conclude": {"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"},
    "reason": {"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"},
    "explore_execute": {"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"},
    "explore_conclude": {"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"},
}
BOOTSTRAP_DESCRIPTION = "bootstrap"
BOOTSTRAP_CREATOR = "dispatcher.bootstrap"
WORKER_COOLDOWN_SECONDS = 15
STUCK_TICKS_TO_FINALIZE = 10
CURSOR_COMMAND_ALIASES = {"claude": "agent", "cursor": "agent", "cursor-agent": "agent"}

# 每个 prompt 组下各模板必须覆盖的占位符
REQUIRED_PLACEHOLDERS = {
    "default": {
        "bootstrap.md": ["{origin}", "{goal}", "{constraints}", "{hints}"],
        "bootstrap_conclude.md": ["{origin}", "{goal}", "{constraints}", "{hints}"],
        "reason.md": ["{graph_yaml}", "{fact_ids}", "{open_intents}"],
        "explore.md": ["{graph_yaml}", "{intent_id}", "{intent_description}"],
        "explore_conclude.md": ["{graph_yaml}", "{intent_id}", "{intent_description}"],
    },
    "mock": {
        "bootstrap.md": ["{origin}", "{goal}", "{constraints}", "{hints}"],
        "bootstrap_conclude.md": ["{origin}", "{goal}", "{constraints}", "{hints}"],
        "reason.md": ["{fact_ids}", "{open_intents}"],
        "explore.md": ["{intent_id}"],
        "explore_conclude.md": ["{intent_id}"],
    },
}


class ConfigError(Exception):
    pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(msg: str, *args) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    text = msg % args if args else msg
    print(f"[{stamp}] {text}", flush=True)


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------

def new_board(title: str, origin: str, goal: str, bootstrap_enabled: bool,
              constraints: str = "") -> dict:
    return {
        "title": title,
        "status": "active",
        "bootstrap_enabled": bootstrap_enabled,
        "created_at": now_iso(),
        "completed_at": None,
        "completion": None,
        "constraints": constraints,
        "facts": [
            {"id": "origin", "description": origin, "source": "init", "created_at": now_iso()},
            {"id": "goal", "description": goal, "source": "init", "created_at": now_iso()},
        ],
        "intents": [],
        "hints": [],
        "reason_rounds": 0,
        "inbox_merged": 0,
    }


def load_board(run_dir: Path) -> dict:
    with open(run_dir / "board.json", encoding="utf-8") as fh:
        return json.load(fh)


def save_board(run_dir: Path, board: dict) -> None:
    tmp = run_dir / "board.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(board, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, run_dir / "board.json")


def _next_id(items: list[dict], prefix: str) -> str:
    top = 0
    for item in items:
        match = re.fullmatch(rf"{prefix}(\d+)", item["id"])
        if match:
            top = max(top, int(match.group(1)))
    return f"{prefix}{top + 1:03d}"


def add_fact(board: dict, description: str, source: str) -> dict:
    fact = {
        "id": _next_id(board["facts"], "f"),
        "description": description.strip(),
        "source": source,
        "created_at": now_iso(),
    }
    board["facts"].append(fact)
    return fact


def add_intent(
    board: dict,
    from_ids: list[str],
    description: str,
    creator: str,
    *,
    use_ledger: bool = False,
) -> dict:
    # Ledger only when this will be the sole open intent (path settled).
    open_before = len(open_intents(board))
    if use_ledger and open_before != 0:
        use_ledger = False
    intent = {
        "id": _next_id(board["intents"], "i"),
        "from": list(from_ids),
        "to": None,
        "description": description.strip(),
        "creator": creator,
        "worker": None,
        "use_ledger": bool(use_ledger),
        "created_at": now_iso(),
        "concluded_at": None,
    }
    board["intents"].append(intent)
    return intent


def add_hint(board: dict, content: str, creator: str) -> dict:
    hint = {
        "id": _next_id(board["hints"], "h"),
        "content": content.strip(),
        "creator": creator,
        "created_at": now_iso(),
    }
    board["hints"].append(hint)
    return hint


def open_intents(board: dict) -> list[dict]:
    return [i for i in board["intents"] if i["to"] is None]


def unclaimed_intents(board: dict) -> list[dict]:
    return sorted(
        (i for i in open_intents(board) if i["worker"] is None),
        key=lambda i: (i["created_at"], i["id"]),
    )


def is_bootstrap_intent(intent: dict) -> bool:
    return intent["description"] == BOOTSTRAP_DESCRIPTION and intent["creator"] == BOOTSTRAP_CREATOR


def is_initial(board: dict) -> bool:
    if len(board["facts"]) != 2:
        return False
    return all(i["to"] is None and is_bootstrap_intent(i) for i in board["intents"])


def fact_ids_for_prompt(board: dict) -> list[str]:
    return [f["id"] for f in board["facts"] if f["id"] != "goal"]


def _yaml_block(text: str, indent: int) -> list[str]:
    pad = " " * indent
    if not text:
        return [f'{pad}""']
    return [pad + line for line in text.splitlines()]


def render_graph_yaml(board: dict) -> str:
    """手绘的固定结构 YAML 快照: 自由文本一律用 |- 块标量, 免转义。"""
    lines: list[str] = ["project:"]
    lines.append(f'  title: {json.dumps(board["title"], ensure_ascii=False)}')
    lines.append(f'  status: {board["status"]}')
    by_id = {f["id"]: f for f in board["facts"]}
    lines.append("origin: |-")
    lines += _yaml_block(by_id["origin"]["description"], 2)
    lines.append("goal: |-")
    lines += _yaml_block(by_id["goal"]["description"], 2)
    lines.append("constraints: |-")
    lines += _yaml_block(board.get("constraints") or "", 2)
    lines.append("hints:")
    for hint in board["hints"]:
        lines.append(f'  - id: {hint["id"]}')
        lines.append("    content: |-")
        lines += _yaml_block(hint["content"], 6)
        lines.append(f'    creator: {json.dumps(hint["creator"], ensure_ascii=False)}')
    lines.append("facts:")
    for fact in board["facts"]:
        if fact["id"] in ("origin", "goal"):
            continue
        lines.append(f'  - id: {fact["id"]}')
        lines.append("    description: |-")
        lines += _yaml_block(fact["description"], 6)
    lines.append("intents:")
    for intent in board["intents"]:
        lines.append(f'  - id: {intent["id"]}')
        lines.append(f'    from: {json.dumps(intent["from"], ensure_ascii=False)}')
        lines.append(f'    to: {json.dumps(intent["to"])}')
        lines.append("    description: |-")
        lines += _yaml_block(intent["description"], 6)
        lines.append(f'    creator: {json.dumps(intent["creator"], ensure_ascii=False)}')
        lines.append(f'    use_ledger: {json.dumps(bool(intent.get("use_ledger")))}')
        lines.append(f'    worker: {json.dumps(intent["worker"])}')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@dataclass
class WorkerConfig:
    name: str
    command: str
    args: list[str]
    task_types: list[str]
    max_running: int
    priority: int
    skip_permissions: bool
    allowed_tools: list[str]
    env: dict[str, str]

    @property
    def is_mock(self) -> bool:
        return self.command == "mock"


@dataclass
class Config:
    max_workers: int
    interval: float
    max_reason_rounds: int
    max_facts: int
    wallclock_budget: float
    prompt_group: str
    cwd: str
    task_timeouts: dict[str, dict[str, float]]
    workers: list[WorkerConfig]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def resolve_agent_command(command: str) -> str:
    """把 claude/cursor 别名归一到本机 Cursor CLI (`agent`)。"""
    command = CURSOR_COMMAND_ALIASES.get(command, command)
    found = shutil.which(command)
    if found:
        return found
    home_bin = Path.home() / ".local" / "bin" / command
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    return command


def cursor_create_chat(command: str, env: dict[str, str], cwd: str) -> str | None:
    """预创建 Cursor chat, 使超时后的 conclude 仍能 --resume 同一会话。"""
    try:
        proc = subprocess.run(
            [command, "create-chat"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd or None,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _validate_mock_env(worker: dict, raw_env: dict[str, str]) -> None:
    phases: set[str] = set()
    if "bootstrap" in worker["task_types"]:
        phases.update({"bootstrap", "bootstrap_conclude"})
    if "reason" in worker["task_types"]:
        phases.add("reason")
    if "explore" in worker["task_types"]:
        phases.update({"explore_execute", "explore_conclude"})
    for phase in sorted(phases):
        var = f"MOCK_{phase.upper()}"
        _require(var in raw_env, f"mock worker {worker['name']!r} 缺少环境变量 {var}")
        try:
            spec = json.loads(raw_env[var])
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{var} 不是合法 JSON: {exc}") from exc
        delay = spec.get("delay")
        _require(
            isinstance(delay, list) and len(delay) == 2
            and all(isinstance(x, (int, float)) and x >= 0 for x in delay),
            f"{var}.delay 必须是两个非负数字",
        )
        outcomes = spec.get("outcomes")
        _require(isinstance(outcomes, dict) and outcomes, f"{var}.outcomes 缺失")
        unknown = set(outcomes) - MOCK_OUTCOMES[phase]
        _require(not unknown, f"{var} 包含不支持的结果: {sorted(unknown)}")
        total = sum(outcomes.values())
        _require(
            all(isinstance(p, (int, float)) and 0 <= p <= 1 for p in outcomes.values())
            and abs(total - 1.0) < 1e-6,
            f"{var} 概率必须在 [0,1] 且总和严格等于 1.0 (当前 {total})",
        )


def load_config(path: Path) -> Config:
    _require(path.is_file(), f"配置文件不存在: {path}")
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    runtime = raw.get("runtime", {})
    for key in ("max_workers", "interval", "max_reason_rounds", "max_facts",
                "wallclock_budget", "prompt_group"):
        _require(key in runtime, f"runtime.{key} 必须存在")
    prompt_group = runtime["prompt_group"]
    _require(prompt_group in REQUIRED_PLACEHOLDERS, f"不支持的 prompt_group: {prompt_group}")
    group_dir = PROMPTS_DIR / prompt_group
    _require(group_dir.is_dir(), f"prompt 目录不存在: {group_dir}")
    for filename, placeholders in REQUIRED_PLACEHOLDERS[prompt_group].items():
        template_path = group_dir / filename
        _require(template_path.is_file(), f"prompt 模板缺失: {template_path}")
        text = template_path.read_text(encoding="utf-8")
        for placeholder in placeholders:
            _require(placeholder in text, f"{template_path} 缺少占位符 {placeholder}")

    tasks = raw.get("tasks", {})
    timeouts: dict[str, dict[str, float]] = {}
    for task_type in TASK_TYPES:
        section = tasks.get(task_type, {})
        _require("timeout" in section, f"tasks.{task_type}.timeout 必须存在")
        timeouts[task_type] = {"timeout": float(section["timeout"])}
        if task_type in CONCLUDE_PHASE:
            _require("conclude_timeout" in section, f"tasks.{task_type}.conclude_timeout 必须存在")
            timeouts[task_type]["conclude_timeout"] = float(section["conclude_timeout"])

    raw_workers = raw.get("worker")
    _require(isinstance(raw_workers, list) and raw_workers, "至少需要一个 [[worker]]")
    workers: list[WorkerConfig] = []
    seen_names: set[str] = set()
    for entry in raw_workers:
        _require("name" in entry, "每个 worker 都必须有 name")
        _require(entry["name"] not in seen_names, f"worker 名称重复: {entry['name']}")
        seen_names.add(entry["name"])
        task_types = entry.get("task_types")
        _require(isinstance(task_types, list) and task_types, f"worker {entry['name']} 缺少 task_types")
        unknown_types = set(task_types) - set(TASK_TYPES)
        _require(not unknown_types, f"worker {entry['name']} task_types 非法: {sorted(unknown_types)}")
        max_running = entry.get("max_running")
        _require(isinstance(max_running, int) and max_running > 0,
                 f"worker {entry['name']} max_running 必须是正整数")
        command = entry.get("command", "agent")
        env = {str(k): str(v) for k, v in entry.get("env", {}).items()}
        if command == "mock":
            _validate_mock_env({"name": entry["name"], "task_types": task_types}, env)
        else:
            command = resolve_agent_command(command)
        workers.append(WorkerConfig(
            name=entry["name"],
            command=command,
            args=[str(a) for a in entry.get("args", [])],
            task_types=list(task_types),
            max_running=max_running,
            priority=int(entry.get("priority", 0)),
            skip_permissions=bool(entry.get("skip_permissions", True)),
            allowed_tools=[str(t) for t in entry.get("allowed_tools", [])],
            env=env,
        ))

    any_mock = any(w.is_mock for w in workers)
    if any_mock:
        _require(prompt_group == "mock",
                 "存在 mock worker 时 runtime.prompt_group 必须为 \"mock\"")
    if prompt_group == "mock":
        _require(all(w.is_mock for w in workers),
                 "prompt_group=\"mock\" 时所有 worker 都必须是 mock")

    return Config(
        max_workers=int(runtime["max_workers"]),
        interval=float(runtime["interval"]),
        max_reason_rounds=int(runtime["max_reason_rounds"]),
        max_facts=int(runtime["max_facts"]),
        wallclock_budget=float(runtime["wallclock_budget"]),
        prompt_group=prompt_group,
        cwd=str(runtime.get("cwd", ".")),
        task_timeouts=timeouts,
        workers=workers,
    )


def resolve_jspace_dir() -> Path | None:
    """Locate j-space skill root (same idea as Flame skills.jspace_dir, no Flame import)."""
    env = os.environ.get("FLAME_JSPACE", "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    home = Path.home()
    candidates.extend(
        [
            home / ".cursor" / "skills-cursor" / "j-space",
            home / ".cursor" / "skills" / "j-space",
        ]
    )
    for path in candidates:
        if (path / "SKILL.md").is_file():
            return path.resolve()
    return None


def intent_allows_ledger(board: dict, intent: dict) -> bool:
    if not intent.get("use_ledger"):
        return False
    if is_bootstrap_intent(intent):
        return False
    opens = open_intents(board)
    return len(opens) == 1 and opens[0]["id"] == intent["id"]


def ledger_root_for(run_dir: Path, intent_id: str) -> Path:
    return (run_dir / "ledgers" / intent_id).resolve()


def ledger_explore_addendum(*, jspace: Path, ledger_root: Path, workspace_cwd: str) -> str:
    script = jspace / "scripts" / "jspace.py"
    return f"""
# Ledger (optional deep pass for this sole open intent)

use_ledger=true for this intent. Before exploring:
1. Read `{jspace}/SKILL.md` and follow a `loop` pass.
2. Keep the j-space ledger **only** under `{ledger_root}/` (create it if needed).
   Run the controller with that directory as cwd, e.g.
   `mkdir -p {ledger_root} && cd {ledger_root} && python3 {script} note --goal "..." --next "..."`
   Never write `{workspace_cwd}/.jspace/`.
3. Project tools (read/search/shell against the repo) still use workspace `{workspace_cwd}`.
4. Final JSON output rules above still apply — ledger is for holding state, not a substitute for the fact description.
"""


def render_prompt(group: str, name: str, values: dict[str, str]) -> str:
    text = (PROMPTS_DIR / group / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


# --------------------------------------------------------------------------
# worker 进程执行
# --------------------------------------------------------------------------

@dataclass
class PhaseResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    parsed: dict | None
    elapsed: float
    session_id: str | None = None


def expand_env(env: dict[str, str]) -> dict[str, str]:
    merged = os.environ.copy()
    for key, value in env.items():
        merged[key] = re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), ""),
            value,
        )
    if not merged.get("CURSOR_API_KEY") and merged.get("ANTHROPIC_AUTH_TOKEN"):
        merged["CURSOR_API_KEY"] = merged["ANTHROPIC_AUTH_TOKEN"]
    if not merged.get("CURSOR_API_ENDPOINT") and merged.get("ANTHROPIC_BASE_URL"):
        merged["CURSOR_API_ENDPOINT"] = merged["ANTHROPIC_BASE_URL"]
    return merged


def unwrap_cursor_stdout(text: str) -> tuple[str, str | None]:
    """解析 `agent -p --output-format json` 信封, 取出助手正文与 session_id。"""
    stripped = (text or "").strip()
    if not stripped:
        return "", None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return text or "", None
    if isinstance(obj, dict) and obj.get("type") == "result":
        result = obj.get("result", "")
        sid = obj.get("session_id")
        if isinstance(result, dict):
            result = json.dumps(result, ensure_ascii=False)
        return str(result), (str(sid) if sid else None)
    return text or "", None


def extract_json(text: str) -> dict | None:
    """从 stdout 全文中提取最后一个合法 JSON 对象(优先含 accepted/data 键)。"""
    stripped = text.strip()
    decoder = json.JSONDecoder()

    def _try(segment: str) -> dict | None:
        braces = list(re.finditer(r"\{", segment))[-50:]
        for match in reversed(braces):
            try:
                obj, _end = decoder.raw_decode(segment, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("accepted" in obj or "data" in obj):
                return obj
        return None

    fenced = re.findall(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
    for block in reversed(fenced):
        found = _try(block)
        if found is not None:
            return found
    return _try(stripped)


def run_process(cmd: list[str], env: dict[str, str], cwd: str, timeout: float,
                proc_slot: list | None = None) -> PhaseResult:
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        return PhaseResult(127, False, "", f"spawn failed: {exc}", None, 0.0, None)
    if proc_slot is not None:
        proc_slot.append(proc)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
    elapsed = time.monotonic() - started
    assistant, session_id = unwrap_cursor_stdout(stdout or "")
    parsed = None if timed_out else extract_json(assistant)
    return PhaseResult(
        proc.returncode or 0, timed_out, assistant or stdout or "",
        stderr or "", parsed, elapsed, session_id,
    )


def _sample_outcome(outcomes: dict[str, float]) -> str:
    roll = random.random()
    acc = 0.0
    for name, prob in outcomes.items():
        acc += prob
        if roll <= acc:
            return name
    return next(reversed(outcomes))


def mock_execute(worker: WorkerConfig, prompt: str, timeout: float) -> PhaseResult:
    """mock driver: 解析 JSON prompt 取 phase, 按 MOCK_<PHASE> 分布模拟结果。"""
    started = time.monotonic()
    try:
        payload = json.loads(prompt)
        phase = payload["phase"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return PhaseResult(2, False, "", "mock: prompt 不是合法 JSON 或缺少 phase", None, 0.0, None)
    spec = json.loads(worker.env[f"MOCK_{phase.upper()}"])
    delay = random.uniform(*spec["delay"])
    time.sleep(delay)
    timed_out = delay > timeout
    elapsed = time.monotonic() - started
    if timed_out:
        return PhaseResult(0, True, f"mock: simulated overrun {delay:.2f}s", "", None, elapsed, None)

    outcome = _sample_outcome(spec["outcomes"])
    if outcome == "noop" and phase == "reason" and not payload.get("open_intents"):
        outcome = "intent"  # open_intents 为空时 noop 不合法, mock 自动规避
    body: str
    exit_code = 0
    if outcome == "rejected":
        body = json.dumps({"accepted": False, "reason": "mock_refusal"})
    elif outcome == "invalid_json":
        body = "this is not json {"
    elif outcome == "invalid_payload":
        body = json.dumps({"accepted": True, "data": {"unexpected": True}})
    elif outcome == "command_fail":
        body, exit_code = "", 3
    elif phase == "reason":
        fact_ids = [fid for fid in payload.get("fact_ids", []) if fid != "goal"] or ["origin"]
        if outcome == "complete":
            body = json.dumps({"accepted": True, "data": {"complete": {
                "from": [fact_ids[-1]],
                "description": "mock 判定 goal 已满足"}}})
        else:  # intent
            body = json.dumps({"accepted": True, "data": {"intent": {
                "from": [random.choice(fact_ids)],
                "description": f"mock 探索方向 {uuid.uuid4().hex[:6]}"}}})
    elif phase == "explore_execute" or phase == "explore_conclude":
        body = json.dumps({"accepted": True, "data": {
            "description": f"mock 结论: intent {payload.get('intent_id')} 已完成探索"}})
    elif phase == "bootstrap":
        body = json.dumps({"accepted": True, "data": {
            "fact": {"description": "mock bootstrap 关键事实"},
            "complete": {"description": "mock 判定 bootstrap 已达成 goal"}}})
    else:  # bootstrap_conclude
        body = json.dumps({"accepted": True, "data": {
            "fact": {"description": "mock bootstrap 收尾事实"}}})
    return PhaseResult(exit_code, False, body, "", extract_json(body) if body else None, elapsed, None)


def build_cursor_cmd(worker: WorkerConfig, prompt: str, session: str, cwd: str) -> list[str]:
    """构造 Cursor CLI (`agent`) 非交互调用。"""
    cmd = [worker.command, *worker.args]
    cmd += ["-p", "--output-format", "json", "--trust"]
    if worker.skip_permissions:
        cmd += ["--force", "--approve-mcps", "--sandbox", "disabled"]
    else:
        cmd += ["--sandbox", "enabled"]
    if cwd:
        cmd += ["--workspace", cwd]
    args_has_model = any(a == "--model" or str(a).startswith("--model=") for a in worker.args)
    model = worker.env.get("CURSOR_MODEL") or worker.env.get("ANTHROPIC_MODEL")
    if model and not args_has_model:
        cmd += ["--model", model]
    if session:
        cmd += ["--resume", session]
    cmd += ["--", prompt]
    return cmd


def execute_phase(worker: WorkerConfig, phase: str, prompt: str, session: str,
                  timeout: float, cwd: str, proc_slot: list | None = None) -> PhaseResult:
    if worker.is_mock:
        return mock_execute(worker, prompt, timeout)
    env = expand_env(worker.env)
    cmd = build_cursor_cmd(worker, prompt, session, cwd)
    result = run_process(cmd, env, cwd, timeout, proc_slot)
    if result.session_id is None and session:
        result.session_id = session
    return result


# --------------------------------------------------------------------------
# 输出契约校验
# --------------------------------------------------------------------------

def _accepted(result: PhaseResult) -> bool | None:
    if result.parsed is None or "accepted" not in result.parsed:
        return None
    return bool(result.parsed.get("accepted"))


def valid_bootstrap_main(result: PhaseResult) -> tuple[str, str] | None:
    data = (result.parsed or {}).get("data")
    if not isinstance(data, dict):
        return None
    fact = data.get("fact")
    complete = data.get("complete")
    if not isinstance(fact, dict) or not isinstance(complete, dict):
        return None
    fact_desc = str(fact.get("description") or "").strip()
    complete_desc = str(complete.get("description") or "").strip()
    if not fact_desc or not complete_desc:
        return None
    return fact_desc, complete_desc


def valid_fact_only(result: PhaseResult) -> str | None:
    data = (result.parsed or {}).get("data")
    if not isinstance(data, dict) or "complete" in data:
        return None
    fact = data.get("fact")
    if not isinstance(fact, dict):
        return None
    desc = str(fact.get("description") or "").strip()
    return desc or None


def valid_description(result: PhaseResult) -> str | None:
    data = (result.parsed or {}).get("data")
    if not isinstance(data, dict):
        return None
    desc = str(data.get("description") or "").strip()
    return desc or None


def valid_reason(result: PhaseResult, legal_fact_ids: set[str]) -> tuple | str | None:
    """返回 ("complete", from_ids, desc) / ("intent", from_ids, desc, use_ledger) / "noop" / None(非法)。"""
    data = (result.parsed or {}).get("data")
    if not isinstance(data, dict):
        return None
    if not data:
        return "noop"
    if "complete" in data and "intent" in data:
        return None
    if "complete" in data:
        node = data["complete"]
        if not isinstance(node, dict):
            return None
        from_ids = node.get("from")
        desc = str(node.get("description") or "").strip()
        if (not isinstance(from_ids, list) or not from_ids
                or any(not isinstance(fid, str) for fid in from_ids)
                or not desc):
            return None
        if any(fid not in legal_fact_ids or fid == "goal" for fid in from_ids):
            return None
        return "complete", [fid.strip() for fid in from_ids], desc
    if "intent" in data:
        node = data["intent"]
        if not isinstance(node, dict):
            return None
        from_ids = node.get("from")
        desc = str(node.get("description") or "").strip()
        if (not isinstance(from_ids, list) or not from_ids
                or any(not isinstance(fid, str) for fid in from_ids)
                or not desc):
            return None
        if any(fid not in legal_fact_ids or fid == "goal" for fid in from_ids):
            return None
        use_ledger = bool(node.get("use_ledger"))
        return "intent", [fid.strip() for fid in from_ids], desc, use_ledger
    return None


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------

@dataclass
class TaskRecord:
    task_type: str
    phase: str  # "main" | "conclude"
    worker: WorkerConfig
    intent_id: str | None
    session_id: str
    started_at: float
    future: futures.Future = field(default=None)  # type: ignore[assignment]
    proc_slot: list = field(default_factory=list)


@dataclass
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    had_open_intents: bool


class Orchestrator:
    def __init__(self, run_dir: Path, extra_budget: float = 0.0):
        self.run_dir = run_dir
        self.board = load_board(run_dir)
        self.config = load_config(run_dir / "config.toml")
        if extra_budget > 0:
            self.config.wallclock_budget += extra_budget
        self.cwd = str((run_dir / self.config.cwd).resolve()) \
            if not os.path.isabs(self.config.cwd) else self.config.cwd
        # 恢复语义: stopped / budget_exhausted 可续跑; completed 不可重入
        if self.board["status"] == "completed":
            raise ConfigError("该 run 已 completed, 不可恢复; 请新建 run 或人工编辑 board.json")
        if self.board["status"] in ("stopped", "budget_exhausted", "budget_exceeded"):
            log("恢复 run (原状态 %s → active)", self.board["status"])
            self.board["status"] = "active"
            self.board["completed_at"] = None
        # 中断恢复: 上次运行残留的已认领先全部释放
        for intent in open_intents(self.board):
            if intent["worker"] is not None:
                log("恢复: 释放残留 claim %s (原 worker %s)", intent["id"], intent["worker"])
                intent["worker"] = None
        self.executor = futures.ThreadPoolExecutor(max_workers=self.config.max_workers + 2)
        self.tasks: dict[futures.Future, TaskRecord] = {}
        self.checkpoint: ReasonCheckpoint | None = None
        self.reason_running = False
        self.cooldown_until: dict[str, float] = {}
        self.stuck_ticks = 0
        self.stop_requested = False
        self.final_status: str | None = None
        self.started_at = time.monotonic()
        save_board(self.run_dir, self.board)

    # ---- 小工具 ----------------------------------------------------------

    def event(self, name: str, **fields) -> None:
        record = {"ts": now_iso(), "event": name, **fields}
        with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def running_count(self, worker_name: str | None = None) -> int:
        if worker_name is None:
            return len(self.tasks)
        return sum(1 for t in self.tasks.values() if t.worker.name == worker_name)

    def project_running(self) -> int:
        return len(self.tasks)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def choose_worker(self, task_type: str) -> WorkerConfig | None:
        now = time.monotonic()
        candidates = [
            w for w in self.config.workers
            if task_type in w.task_types
            and self.running_count(w.name) < w.max_running
            and self.cooldown_until.get(w.name, 0.0) <= now
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda w: (w.priority, self.running_count(w.name), random.random()))
        return candidates[0]

    def cooldown(self, worker: WorkerConfig) -> None:
        self.cooldown_until[worker.name] = time.monotonic() + WORKER_COOLDOWN_SECONDS

    # ---- 任务派发 ----------------------------------------------------------

    def _submit(self, task_type: str, phase: str, worker: WorkerConfig,
                prompt_name: str, values: dict[str, str],
                intent_id: str | None, session_id: str,
                prompt_suffix: str = "") -> None:
        prompt = render_prompt(self.config.prompt_group, prompt_name, values)
        if prompt_suffix:
            prompt = prompt.rstrip() + "\n" + prompt_suffix
        timeout_key = "timeout" if phase == "main" else "conclude_timeout"
        timeout = self.config.task_timeouts[task_type][timeout_key]
        if not worker.is_mock and phase == "main":
            allocated = cursor_create_chat(worker.command, expand_env(worker.env), self.cwd)
            if allocated:
                session_id = allocated
                log("Cursor chat 已创建 session=%s", session_id)
            else:
                log("create-chat 失败, 本任务将开新会话 (conclude 可能无法续上)")
        proc_slot: list = []
        future = self.executor.submit(
            execute_phase, worker, prompt_name, prompt, session_id, timeout, self.cwd, proc_slot,
        )
        record = TaskRecord(task_type, phase, worker, intent_id, session_id,
                            time.monotonic(), future, proc_slot)
        self.tasks[future] = record
        self.event("dispatch", task=task_type, phase=phase, worker=worker.name,
                   intent=intent_id, timeout=timeout)
        log("派发 %s/%s → worker=%s intent=%s", task_type, phase, worker.name, intent_id)

    def _prompt_values(self) -> dict[str, str]:
        opens = open_intents(self.board)
        return {
            "origin": next(f["description"] for f in self.board["facts"] if f["id"] == "origin"),
            "goal": next(f["description"] for f in self.board["facts"] if f["id"] == "goal"),
            "constraints": self.board.get("constraints") or "",
            "hints": json.dumps([h["content"] for h in self.board["hints"]], ensure_ascii=False),
            "graph_yaml": render_graph_yaml(self.board),
            "fact_ids": json.dumps(fact_ids_for_prompt(self.board), ensure_ascii=False),
            "open_intents": json.dumps(
                [{"id": i["id"], "from": i["from"], "description": i["description"],
                  "creator": i["creator"], "worker": i["worker"],
                  "use_ledger": bool(i.get("use_ledger"))} for i in opens],
                ensure_ascii=False),
        }

    def dispatch_bootstrap(self) -> bool:
        reserved = next((i for i in open_intents(self.board) if is_bootstrap_intent(i)), None)
        if reserved is None:
            reserved = add_intent(self.board, ["origin"], BOOTSTRAP_DESCRIPTION, BOOTSTRAP_CREATOR)
            self.event("intent_declared", intent=reserved["id"], kind="bootstrap")
        if reserved["worker"] is not None:
            return False
        worker = self.choose_worker("bootstrap")
        if worker is None or self.project_running() >= self.config.max_workers:
            return False
        reserved["worker"] = worker.name
        self.event("intent_claimed", intent=reserved["id"], worker=worker.name)
        values = self._prompt_values()
        self._submit("bootstrap", "main", worker, "bootstrap", values,
                     reserved["id"], uuid.uuid4().hex)
        return True

    def dispatch_reason(self) -> bool:
        if self.reason_running or self.board["reason_rounds"] >= self.config.max_reason_rounds:
            return False
        if self.project_running() >= self.config.max_workers:
            return False
        worker = self.choose_worker("reason")
        if worker is None:
            return False
        self.reason_running = True
        self.board["reason_rounds"] += 1
        trigger = self._reason_trigger()
        self.event("reason_claimed", worker=worker.name, trigger=trigger,
                   round=self.board["reason_rounds"])
        self._submit("reason", "main", worker, "reason", self._prompt_values(),
                     None, uuid.uuid4().hex)
        return True

    def dispatch_explore(self) -> int:
        dispatched = 0
        for intent in unclaimed_intents(self.board):
            if is_bootstrap_intent(intent):
                continue
            if self.project_running() >= self.config.max_workers:
                break
            worker = self.choose_worker("explore")
            if worker is None:
                break
            intent["worker"] = worker.name
            self.event("intent_claimed", intent=intent["id"], worker=worker.name)
            values = self._prompt_values()
            values["intent_id"] = intent["id"]
            values["intent_description"] = intent["description"]
            suffix = ""
            if intent_allows_ledger(self.board, intent):
                jspace = resolve_jspace_dir()
                if jspace is None:
                    log("intent %s use_ledger=true but j-space missing; exploring without ledger",
                        intent["id"])
                    self.event("ledger_skipped", intent=intent["id"], reason="jspace_missing")
                else:
                    root = ledger_root_for(self.run_dir, intent["id"])
                    root.mkdir(parents=True, exist_ok=True)
                    suffix = ledger_explore_addendum(
                        jspace=jspace,
                        ledger_root=root,
                        workspace_cwd=self.cwd,
                    )
                    self.event("ledger_mounted", intent=intent["id"], ledger=str(root))
                    log("intent %s ledger → %s", intent["id"], root)
            elif intent.get("use_ledger"):
                self.event(
                    "ledger_skipped",
                    intent=intent["id"],
                    reason="open_intents!=1",
                    open=len(open_intents(self.board)),
                )
            self._submit(
                "explore",
                "main",
                worker,
                "explore",
                values,
                intent["id"],
                uuid.uuid4().hex,
                prompt_suffix=suffix,
            )
            dispatched += 1
        return dispatched

    # ---- reason 触发去重 ----------------------------------------------------

    def _situation(self) -> ReasonCheckpoint:
        return ReasonCheckpoint(
            fact_count=len(self.board["facts"]),
            hint_count=len(self.board["hints"]),
            had_open_intents=bool(open_intents(self.board)),
        )

    def _reason_trigger(self) -> str:
        cp = self.checkpoint
        now = self._situation()
        if cp is None:
            return "first"
        if now.fact_count > cp.fact_count:
            return "new_facts"
        if now.hint_count > cp.hint_count:
            return "new_hints"
        if cp.had_open_intents and not now.had_open_intents:
            return "intents_drained"
        return "unknown"

    def reason_triggerable(self) -> bool:
        now = self._situation()
        if self.checkpoint is None:
            if now.had_open_intents:
                # 基线 checkpoint: 不吞掉之后第一次新增的 fact/hint
                self.checkpoint = now
                return False
            return True
        cp = self.checkpoint
        return (now.fact_count > cp.fact_count or now.hint_count > cp.hint_count
                or (cp.had_open_intents and not now.had_open_intents))

    # ---- 结果回收 ----------------------------------------------------------

    def _find_intent(self, intent_id: str | None) -> dict | None:
        if intent_id is None:
            return None
        return next((i for i in self.board["intents"] if i["id"] == intent_id), None)

    def _conclude_intent(self, intent: dict, worker_name: str, description: str) -> dict:
        fact = add_fact(self.board, description, worker_name)
        intent["to"] = fact["id"]
        intent["concluded_at"] = now_iso()
        self.event("intent_concluded", intent=intent["id"], fact=fact["id"], worker=worker_name)
        log("结论 %s → %s (worker=%s)", intent["id"], fact["id"], worker_name)
        return fact

    def _release_intent(self, intent: dict, reason: str) -> None:
        self.event("intent_released", intent=intent["id"], worker=intent["worker"], reason=reason)
        intent["worker"] = None
        log("释放 %s (%s)", intent["id"], reason)

    def _complete_run(self, from_ids: list[str], description: str, worker_name: str) -> None:
        self.board["completed_at"] = now_iso()
        self.board["completion"] = {
            "from": from_ids,
            "description": description,
            "worker": worker_name,
            "at": now_iso(),
        }
        self.event("run_completed", from_ids=from_ids, worker=worker_name)
        log("✔ run 完成 (worker=%s): %s", worker_name, description[:120])
        self.finalize("completed")

    def _enter_conclude(self, record: TaskRecord, why: str,
                       result: PhaseResult | None = None) -> bool:
        worker = record.worker
        intent = self._find_intent(record.intent_id)
        if intent is None:
            return False
        prompt_name = CONCLUDE_PHASE[record.task_type]
        values = self._prompt_values()
        if record.task_type == "explore":
            values["intent_id"] = intent["id"]
            values["intent_description"] = intent["description"]
        session_id = record.session_id
        if result and result.session_id:
            session_id = result.session_id
        self.event("conclude_enter", task=record.task_type, intent=intent["id"], reason=why)
        log("%s 进入二阶段收尾 (%s)", intent["id"], why)
        self._submit(record.task_type, "conclude", worker, prompt_name, values,
                     intent["id"], session_id)
        return True

    def reap(self) -> None:
        done = [fut for fut in self.tasks if fut.done()]
        for fut in done:
            record = self.tasks.pop(fut)
            try:
                result: PhaseResult = fut.result()
            except Exception as exc:  # worker 线程自身异常, 按命令失败处理
                result = PhaseResult(1, False, "", f"orchestrator thread error: {exc}", None, 0.0, None)
            handler = {
                ("bootstrap", "main"): self._reap_bootstrap_main,
                ("bootstrap", "conclude"): self._reap_fact_conclude,
                ("explore", "main"): self._reap_explore_main,
                ("explore", "conclude"): self._reap_explore_conclude,
                ("reason", "main"): self._reap_reason,
            }[(record.task_type, record.phase)]
            handler(record, result)

    def _reap_bootstrap_main(self, record: TaskRecord, result: PhaseResult) -> None:
        intent = self._find_intent(record.intent_id)
        if intent is None or self.board["status"] != "active":
            return
        if not result.timed_out and result.exit_code == 0 and _accepted(result) is True:
            valid = valid_bootstrap_main(result)
            if valid is not None:
                fact_desc, complete_desc = valid
                fact = self._conclude_intent(intent, record.worker.name, fact_desc)
                self._complete_run([fact["id"]], complete_desc, record.worker.name)
                return
        # 直接失败(拒绝/命令失败)不进二阶段; 超时不算直接失败
        if not result.timed_out and (result.exit_code != 0 or _accepted(result) is False):
            self.cooldown(record.worker)
            self._release_intent(intent, "bootstrap rejected/failed")
            return
        # 超时 / 输出无法解析 / 结构不合法 → 二阶段收尾
        if not self._enter_conclude(record, "timeout_or_parse_fail", result):
            self._release_intent(intent, "conclude unavailable")

    def _reap_fact_conclude(self, record: TaskRecord, result: PhaseResult) -> None:
        """bootstrap_conclude: 只许返回 fact。"""
        intent = self._find_intent(record.intent_id)
        if intent is None or self.board["status"] != "active":
            return
        if not result.timed_out and result.exit_code == 0 and _accepted(result) is True:
            desc = valid_fact_only(result)
            if desc is not None:
                self._conclude_intent(intent, record.worker.name, desc)
                return
        self.cooldown(record.worker)
        self._release_intent(intent, "bootstrap_conclude failed")

    def _reap_explore_main(self, record: TaskRecord, result: PhaseResult) -> None:
        intent = self._find_intent(record.intent_id)
        if intent is None or self.board["status"] != "active":
            return
        if not result.timed_out and result.exit_code == 0 and _accepted(result) is True:
            desc = valid_description(result)
            if desc is not None:
                self._conclude_intent(intent, record.worker.name, desc)
                return
        if not result.timed_out and (result.exit_code != 0 or _accepted(result) is False):
            self.cooldown(record.worker)
            self._release_intent(intent, "explore rejected/failed")
            return
        if not self._enter_conclude(record, "timeout_or_parse_fail", result):
            self._release_intent(intent, "conclude unavailable")

    def _reap_explore_conclude(self, record: TaskRecord, result: PhaseResult) -> None:
        intent = self._find_intent(record.intent_id)
        if intent is None or self.board["status"] != "active":
            return
        if not result.timed_out and result.exit_code == 0 and _accepted(result) is True:
            desc = valid_description(result)
            if desc is not None:
                self._conclude_intent(intent, record.worker.name, desc)
                return
        self.cooldown(record.worker)
        self._release_intent(intent, "explore_conclude failed")

    def _reap_reason(self, record: TaskRecord, result: PhaseResult) -> None:
        self.reason_running = False
        if self.board["status"] != "active":
            return
        legal = set(fact_ids_for_prompt(self.board))
        if not result.timed_out and result.exit_code == 0 and _accepted(result) is True:
            outcome = valid_reason(result, legal)
            if outcome is not None:
                self.checkpoint = self._situation()
                self.event("reason_done", worker=record.worker.name,
                           outcome=outcome if isinstance(outcome, str) else outcome[0])
                if outcome == "noop":
                    return
                if outcome[0] == "complete":
                    _, from_ids, desc = outcome
                    self._complete_run(from_ids, desc, record.worker.name)
                else:
                    _, from_ids, desc, use_ledger = outcome
                    intent = add_intent(
                        self.board,
                        from_ids,
                        desc,
                        record.worker.name,
                        use_ledger=use_ledger,
                    )
                    self.event(
                        "intent_declared",
                        intent=intent["id"],
                        worker=record.worker.name,
                        use_ledger=bool(intent.get("use_ledger")),
                    )
                    log(
                        "新 intent %s use_ledger=%s: %s",
                        intent["id"],
                        bool(intent.get("use_ledger")),
                        desc[:100],
                    )
                return
        if not result.timed_out and (result.exit_code != 0 or _accepted(result) is False):
            self.cooldown(record.worker)
        self.event("reason_failed", worker=record.worker.name,
                   timed_out=result.timed_out, exit_code=result.exit_code)
        log("reason 作废 (worker=%s, timed_out=%s, exit=%s)",
            record.worker.name, result.timed_out, result.exit_code)

    # ---- hint 合并 ----------------------------------------------------------

    def merge_inbox(self) -> None:
        inbox = self.run_dir / "inbox.jsonl"
        if not inbox.is_file():
            return
        lines = inbox.read_text(encoding="utf-8").splitlines()
        merged = self.board.get("inbox_merged", 0)
        for line in lines[merged:]:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            hint = add_hint(self.board, str(entry.get("content", "")), str(entry.get("creator", "human")))
            self.event("hint_added", hint=hint["id"], creator=hint["creator"])
            log("吸收 hint %s: %s", hint["id"], hint["content"][:100])
        self.board["inbox_merged"] = len(lines)

    # ---- 护栏与收尾 ----------------------------------------------------------

    def budget_exceeded(self) -> bool:
        return (self.elapsed() > self.config.wallclock_budget
                or len(self.board["facts"]) >= self.config.max_facts)

    def cancel_running(self) -> None:
        for record in self.tasks.values():
            if record.proc_slot:
                proc = record.proc_slot[0]
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            else:
                record.future.cancel()

    def finalize(self, status: str) -> None:
        self.board["status"] = status
        self.final_status = status
        self.event("run_finalized", status=status)
        self.cancel_running()
        self._write_result()
        save_board(self.run_dir, self.board)

    def _write_result(self) -> None:
        board = self.board
        lines = [f"# Run 结果: {board['title']}", ""]
        lines.append(f"- 状态: {board['status']}")
        lines.append(f"- 创建: {board['created_at']}")
        lines.append(f"- 事实数: {len(board['facts'])} (含 origin/goal)")
        lines.append(f"- 意图数: {len(board['intents'])} "
                     f"(未结论 {len(open_intents(board))})")
        lines.append(f"- reason 轮次: {board['reason_rounds']}")
        if board.get("constraints"):
            lines.append("")
            lines.append("## 约束")
            lines.append("")
            lines.append(board["constraints"])
        lines.append("")
        completion = board.get("completion")
        if completion:
            lines.append("## 完成结论")
            lines.append("")
            lines.append(completion["description"])
            lines.append("")
            lines.append(f"支撑 facts: {', '.join(completion['from'])} "
                         f"(worker: {completion['worker']})")
            lines.append("")
        lines.append("## 事实链")
        lines.append("")
        intents_by_to = {i["to"]: i for i in board["intents"] if i["to"]}
        for fact in board["facts"]:
            if fact["id"] in ("origin", "goal"):
                continue
            via = intents_by_to.get(fact["id"])
            route = f"经由 {via['id']} ({via['description'][:60]})" if via else "直接写入"
            lines.append(f"- **{fact['id']}** ({fact['source']}, {route}): {fact['description']}")
        opens = open_intents(board)
        if opens:
            lines.append("")
            lines.append("## 未结论意图")
            lines.append("")
            for intent in opens:
                lines.append(f"- {intent['id']} (from {', '.join(intent['from'])}): "
                             f"{intent['description']}")
        if board["hints"]:
            lines.append("")
            lines.append("## Hints")
            lines.append("")
            for hint in board["hints"]:
                lines.append(f"- {hint['id']} ({hint['creator']}): {hint['content']}")
        (self.run_dir / "RESULT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_status(self) -> None:
        running = [
            {"task": t.task_type, "phase": t.phase, "worker": t.worker.name,
             "intent": t.intent_id, "elapsed": round(time.monotonic() - t.started_at, 1)}
            for t in self.tasks.values()
        ]
        status = {
            "status": self.final_status or self.board["status"],
            "title": self.board["title"],
            "elapsed_seconds": round(self.elapsed(), 1),
            "budget_seconds": self.config.wallclock_budget,
            "facts": len(self.board["facts"]),
            "max_facts": self.config.max_facts,
            "intents_open": len(open_intents(self.board)),
            "intents_concluded": sum(1 for i in self.board["intents"] if i["to"]),
            "hints": len(self.board["hints"]),
            "reason_rounds": self.board["reason_rounds"],
            "max_reason_rounds": self.config.max_reason_rounds,
            "running": running,
            "updated_at": now_iso(),
        }
        tmp = self.run_dir / "status.json.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.run_dir / "status.json")

    # ---- 主循环 ----------------------------------------------------------

    def tick(self) -> None:
        self.reap()
        self.merge_inbox()
        if self.board["status"] != "active":
            return
        if self.budget_exceeded():
            if not self.tasks:
                self.finalize("budget_exceeded")
            return
        dispatched = 0
        if is_initial(self.board) and self.board["bootstrap_enabled"] \
                and any("bootstrap" in w.task_types for w in self.config.workers):
            dispatched += 1 if self.dispatch_bootstrap() else 0
        else:
            if self.reason_triggerable():
                dispatched += 1 if self.dispatch_reason() else 0
            dispatched += self.dispatch_explore()
        if dispatched:
            self.stuck_ticks = 0
        elif not self.tasks:
            self.stuck_ticks += 1
            if self.stuck_ticks >= STUCK_TICKS_TO_FINALIZE:
                log("无可推进任务, 收尾")
                self.finalize("budget_exceeded")
        else:
            self.stuck_ticks = 0

    def run(self, once: bool = False) -> str:
        log("orchestrator 启动 run_dir=%s status=%s facts=%d",
            self.run_dir, self.board["status"], len(self.board["facts"]))
        while not self.stop_requested:
            self.tick()
            self.write_status()
            save_board(self.run_dir, self.board)
            if once:
                break
            if self.final_status is not None and not self.tasks:
                break
            if self.board["status"] != "active" and not self.tasks:
                break
            time.sleep(self.config.interval)
        if self.tasks:
            log("等待 %d 个任务结束...", len(self.tasks))
            self.executor.shutdown(wait=True)
            self.reap()
            save_board(self.run_dir, self.board)
            self.write_status()
        self.executor.shutdown(wait=False)
        return self.final_status or self.board["status"]

    def request_stop(self, *_args) -> None:
        log("收到停止信号, 收尾中...")
        self.stop_requested = True
        if self.board["status"] == "active":
            self.finalize("stopped")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_CONFIG_TEMPLATE = """\
# fact-graph run 配置。board(title/origin/goal/bootstrap_enabled)由 init 写入 board.json,
# 本文件只描述运行期参数与 worker。

[runtime]
max_workers = 2          # 同时运行中的任务总数上限
interval = 3             # 主循环节拍(秒)
max_reason_rounds = 20   # reason 最大轮次(护栏)
max_facts = 40           # 事实数上限(含 origin/goal, 护栏)
wallclock_budget = 1800  # 总时长预算(秒, 护栏)
prompt_group = "default" # prompts/<group>/
# cwd = "."              # worker 工作目录, 默认编排器启动目录

[tasks.bootstrap]
timeout = 1800
conclude_timeout = 600
[tasks.reason]
timeout = 300
[tasks.explore]
timeout = 1200
conclude_timeout = 600

# 控制面: bootstrap + reason
[[worker]]
name = "reasoner"
command = "agent"
task_types = ["bootstrap", "reason"]
max_running = 1
priority = 0
# skip_permissions = true   # 默认 true, 即 agent --force --trust
# skip_permissions = false 时启用 sandbox, Cursor CLI 无 --allowedTools
env = { CURSOR_MODEL = "auto" }

# 执行面: explore
[[worker]]
name = "explorer"
command = "agent"
task_types = ["explore"]
max_running = 1
priority = 0
env = { CURSOR_MODEL = "auto" }
"""


def cmd_init(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if (run_dir / "board.json").exists():
        print(f"run 目录已存在 board.json: {run_dir}", file=sys.stderr)
        return 1
    run_dir.mkdir(parents=True, exist_ok=True)
    origin = args.origin
    goal = args.goal
    constraints = args.constraints
    hint = args.hint
    if args.seed:
        try:
            seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"无法读取 --seed: {exc}", file=sys.stderr)
            return 1
        if not isinstance(seed, dict):
            print("--seed 必须是 JSON 对象", file=sys.stderr)
            return 1
        origin = str(seed.get("origin") or origin)
        goal = str(seed.get("goal") or goal)
        constraints = str(seed.get("constraints") or constraints)
        hint = str(seed.get("hint") or hint)
    if args.origin_file:
        origin = Path(args.origin_file).read_text(encoding="utf-8").strip()
    if args.goal_file:
        goal = Path(args.goal_file).read_text(encoding="utf-8").strip()
    if args.constraints_file:
        constraints = Path(args.constraints_file).read_text(encoding="utf-8").strip()
    if args.hint_file:
        hint = Path(args.hint_file).read_text(encoding="utf-8").strip()
    origin = (origin or "").strip()
    goal = (goal or "").strip()
    constraints = (constraints or "").strip()
    hint = (hint or "").strip()
    if not origin or not goal:
        print("origin 和 goal 都不能为空", file=sys.stderr)
        return 1
    board = new_board(args.title, origin, goal, not args.no_bootstrap, constraints)
    if hint:
        add_hint(board, hint, "human")
    save_board(run_dir, board)
    if args.config:
        content = Path(args.config).read_text(encoding="utf-8")
    else:
        content = DEFAULT_CONFIG_TEMPLATE
    (run_dir / "config.toml").write_text(content, encoding="utf-8")
    try:
        load_config(run_dir / "config.toml")
    except ConfigError as exc:
        print(f"配置校验失败: {exc}", file=sys.stderr)
        return 1
    print(f"已初始化 run: {run_dir}")
    print(f"  启动: {Path(__file__).name} run --run-dir {run_dir}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        orch = Orchestrator(Path(args.run_dir), extra_budget=args.extra_budget)
    except (ConfigError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, orch.request_stop)
    signal.signal(signal.SIGTERM, orch.request_stop)
    status = orch.run(once=args.once)
    print(f"run 结束: status={status} run_dir={args.run_dir}")
    return 0


def cmd_hint(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not (run_dir / "board.json").exists():
        print(f"run 目录不存在 board.json: {run_dir}", file=sys.stderr)
        return 1
    entry = {"content": args.content, "creator": args.creator, "ts": now_iso()}
    with open(run_dir / "inbox.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"hint 已写入 inbox, 将在下一拍被吸收入图: {args.content[:80]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    status_path = run_dir / "status.json"
    if status_path.is_file():
        print(status_path.read_text(encoding="utf-8"))
        return 0
    board = load_board(run_dir)
    print(json.dumps({
        "status": board["status"],
        "facts": len(board["facts"]),
        "intents_open": len(open_intents(board)),
        "hints": len(board["hints"]),
        "reason_rounds": board["reason_rounds"],
        "note": "status.json 尚未生成(编排器未运行)",
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="fact-graph orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化一个 run 目录")
    p_init.add_argument("--run-dir", required=True)
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--origin", default="")
    p_init.add_argument("--goal", default="")
    p_init.add_argument("--constraints", default="", help="全程硬约束(验收命令/合格判据/禁区); reason 对照此字段")
    p_init.add_argument("--origin-file")
    p_init.add_argument("--goal-file")
    p_init.add_argument("--constraints-file")
    p_init.add_argument("--hint", default="", help="初始 hint(如 P1P2 攻击面); 仅参考，不能覆盖 constraints")
    p_init.add_argument("--hint-file")
    p_init.add_argument(
        "--seed",
        help="JSON 种子文件 (origin/goal/constraints/hint); Flame graph_seed.json",
    )
    p_init.add_argument("--config", help="config.toml 模板; 缺省写入默认配置")
    p_init.add_argument("--no-bootstrap", action="store_true", help="禁用 bootstrap, 初始态直接 reason")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="运行/恢复编排循环")
    p_run.add_argument("--run-dir", required=True)
    p_run.add_argument("--once", action="store_true", help="只跑一拍(调试用)")
    p_run.add_argument("--extra-budget", type=float, default=0.0,
                       help="在 wallclock_budget 上追加秒数后恢复运行")
    p_run.set_defaults(func=cmd_run)

    p_hint = sub.add_parser("hint", help="向运行中的 run 注入一条 hint")
    p_hint.add_argument("--run-dir", required=True)
    p_hint.add_argument("--content", required=True)
    p_hint.add_argument("--creator", default="human")
    p_hint.set_defaults(func=cmd_hint)

    p_status = sub.add_parser("status", help="查看 run 状态")
    p_status.add_argument("--run-dir", required=True)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
