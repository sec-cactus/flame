from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flame import budget, evidence, preprocess, prompts, schema, skills, stage_summary
from flame.agent_backends import AgentBackend
from flame.backend import create_agent_backend, extract_json
from flame.config import Config
from flame.log import SessionLog
from flame.progress import Progress
from flame.safety import SafetyDenied, deny_reason
from flame.types import AgentResult, Effort, Phase, Plan, RunResult, VerifyResult


class FlameError(RuntimeError):
    pass


_STAGE_MARKERS = (
    "brief.json",
    "meld-judge.json",
    "plan.json",
    "plan.raw.txt",
    "act.json",
    "act_status.json",
    "act_skill.json",
    "verify.json",
    "verify_debug.txt",
    "tool_trace.json",
    "graph_seed.json",
    "graph_run.json",
    "graph-result.md",
)


def _archive_stage_markers(flame_dir: Path, *, keep: tuple[str, ...] = ()) -> None:
    """Move leftover stage JSON so a new run doesn't inherit the previous pipeline."""
    dest: Path | None = None
    for name in _STAGE_MARKERS:
        if name in keep:
            continue
        src = flame_dir / name
        if not src.is_file():
            continue
        if dest is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = flame_dir / "prior" / stamp
            dest.mkdir(parents=True, exist_ok=True)
        src.rename(dest / name)


DEFAULT_GRAPH_EXTRA_BUDGET_SEC = 900.0


def continue_run(
    task: str,
    *,
    workspace: str | Path | None = None,
    model: str | None = None,
    agent_backend: str | None = None,
    agent_bin: str | None = None,
    force: bool | None = None,
    extra_budget: float | None = None,
    progress: Progress | None = None,
    config: Config | None = None,
) -> RunResult:
    """Resume graph fact-graph from `.flame/graph_run.json`: hint + orchestrator run + verify."""
    cfg = config or Config.load(
        workspace=workspace,
        effort="graph",
        model=model,
        agent_backend=agent_backend,
        agent_bin=agent_bin,
        force=force,
    )
    if cfg.effort is not Effort.graph:
        raise FlameError("continue requires effort=graph")
    progress = progress or Progress()
    flame_dir = cfg.workspace / ".flame"
    flame_dir.mkdir(parents=True, exist_ok=True)
    _archive_stage_markers(flame_dir, keep=("brief.json", "plan.json", "graph_run.json"))
    session_id = uuid.uuid4().hex[:12]
    log = SessionLog(cfg.log_dir / f"{session_id}.jsonl")
    log.emit("start", task=task, effort="graph", model=cfg.model, mode="continue")
    backend = create_agent_backend(cfg, progress)

    original_task = task.strip()
    if not original_task:
        raise FlameError("empty task")
    (flame_dir / "original.md").write_text(original_task + "\n", encoding="utf-8")

    graph_run = _read_json_file(flame_dir / "graph_run.json")
    run_rel = str((graph_run or {}).get("run_dir") or "").strip()
    if not run_rel:
        raise FlameError("continue requires .flame/graph_run.json from a prior graph run")

    plan_payload = _read_json_file(flame_dir / "plan.json")
    if plan_payload:
        plan = _plan_from(plan_payload, ask_use_jspace=False)
    else:
        plan = _stub_plan(original_task, ask_use_jspace=False)
    plan.goal = original_task
    _write_plan(flame_dir / "plan.json", plan)
    plan_mtime = (flame_dir / "plan.json").stat().st_mtime

    factgraph = skills.factgraph_dir()
    if factgraph is None:
        raise FlameError("fact-graph skill missing on disk")

    run_dir = cfg.workspace / run_rel
    if not (run_dir / "board.json").is_file():
        raise FlameError(f"fact-graph run missing board.json: {run_rel}")

    progress.phase("act", "continue graph")
    log.emit("phase", phase="act", mode="continue_graph", run_dir=run_rel)
    _graph_reopen_if_completed(
        factgraph=factgraph,
        run_dir=run_dir,
        workspace=cfg.workspace,
    )
    _graph_hint(
        factgraph=factgraph,
        run_dir=run_dir,
        content=original_task,
        creator="proceed",
        workspace=cfg.workspace,
    )
    budget_sec = (
        DEFAULT_GRAPH_EXTRA_BUDGET_SEC
        if extra_budget is None
        else float(extra_budget)
    )
    graph_status = _graph_run_orchestrator(
        factgraph=factgraph,
        run_dir=run_dir,
        workspace=cfg.workspace,
        extra_budget=budget_sec,
    )
    progress.note(f"fact-graph continue ended: {graph_status}")
    act_output = _read_graph_act_output(cfg.workspace, run_dir)
    _write_answer_md(cfg.workspace, act_output)
    _finalize_act_json(
        flame_dir,
        cfg.workspace,
        act_text=act_output,
        graph_note=f"继续 fact-graph：{graph_status}",
    )
    cycle_trace = evidence.ToolTrace()
    act_note = (
        "Harness continued fact-graph via orchestrator (no act agent). "
        "Judge workspace artifacts; paths may predate this cycle's tool trace."
    )

    log.emit("phase", phase="verify", cycle=1)
    progress.phase("verify", "cycle 1")
    verify = _run_verify(
        backend,
        log,
        original_task,
        plan,
        flame_dir,
        workspace=cfg.workspace,
        cycle_trace=cycle_trace,
        act_note=act_note,
        require_evidence_touch=False,
        plan_mtime=plan_mtime,
        schema_gaps=list(plan.schema_gaps),
    )
    if verify.degraded:
        progress.fail("verify degraded; delivering act output")
        log.emit("finish", passed=False, cycles=1, reason="verify_degraded")
        return RunResult(
            output=act_output,
            passed=False,
            cycles=1,
            log_path=log.path,
            plan=plan,
            verify=verify,
        )
    if verify.passed:
        progress.done("passed (continue)")
        log.emit("finish", passed=True, cycles=1, mode="continue")
        return RunResult(
            output=act_output,
            passed=True,
            cycles=1,
            log_path=log.path,
            plan=plan,
            verify=verify,
        )
    progress.fail(verify.diagnosis or "verify rejected")
    log.emit("finish", passed=False, cycles=1, reason="verify_failed", mode="continue")
    return RunResult(
        output=verify.diagnosis or act_output,
        passed=False,
        cycles=1,
        log_path=log.path,
        plan=plan,
        verify=verify,
    )


def run(
    task: str,
    *,
    workspace: str | Path | None = None,
    effort: str | None = None,
    model: str | None = None,
    agent_backend: str | None = None,
    agent_bin: str | None = None,
    force: bool | None = None,
    safety_gate: bool | None = None,
    progress: Progress | None = None,
    config: Config | None = None,
) -> RunResult:
    cfg = config or Config.load(
        workspace=workspace,
        effort=effort,
        model=model,
        agent_backend=agent_backend,
        agent_bin=agent_bin,
        force=force,
        safety_gate=safety_gate,
    )
    progress = progress or Progress()
    # Harness-side gate is off by default; Cursor agent applies its own refusal/degrade.
    if cfg.safety_gate:
        reason = deny_reason(task)
        if reason:
            raise SafetyDenied(reason)

    flame_dir = cfg.workspace / ".flame"
    flame_dir.mkdir(parents=True, exist_ok=True)
    _archive_stage_markers(flame_dir)
    session_id = uuid.uuid4().hex[:12]
    log = SessionLog(cfg.log_dir / f"{session_id}.jsonl")
    log.emit("start", task=task, effort=cfg.effort.value, model=cfg.model)
    backend = create_agent_backend(cfg, progress)

    original_task = task
    (flame_dir / "original.md").write_text(original_task + "\n", encoding="utf-8")
    cycles = budget.cycle_limit(cfg.effort, cfg.max_cycles)
    intake = preprocess.run_preprocess(
        backend, log, progress, original_task, flame_dir, cfg.effort
    )
    brief_text = intake.brief
    if intake.degraded:
        progress.note("preprocess degraded; plan uses original request")

    diagnosis = ""
    plan: Plan | None = None
    verify: VerifyResult | None = None
    last_text = ""
    round_plan_mtime: float | None = None

    for cycle in range(1, cycles + 1):
        cap = f"cycle {cycle}"
        log.emit("phase", phase="plan", cycle=cycle)
        progress.phase("plan", cap)
        plan = _run_plan(
            backend,
            log,
            brief_text,
            diagnosis,
            flame_dir,
            original_task,
            ask_use_jspace=budget.ask_use_jspace(cfg.effort),
        )
        plan.goal = original_task.strip()
        if not plan.summary.strip():
            plan.summary = stage_summary.plan_summary(
                {"summary": plan.summary, "approach": plan.approach}
            )
        if budget.ask_use_jspace(cfg.effort) and plan.use_jspace is None:
            plan.use_jspace = True
        elif not budget.ask_use_jspace(cfg.effort):
            plan.use_jspace = None
        _write_plan(flame_dir / "plan.json", plan)
        plan_mtime = (flame_dir / "plan.json").stat().st_mtime
        if round_plan_mtime is None:
            round_plan_mtime = plan_mtime
        skill = _act_skill(cfg.effort, plan)
        progress.note("goal: original (harness-forced)")
        if plan.degraded:
            progress.fail("plan degraded; act will run on the original request")
        if skill:
            progress.note(f"skill={skill}")
        elif cfg.effort is Effort.meld:
            progress.note("act=meld (panels → judge → selected panel writes)")
        elif cfg.effort is Effort.ledger and plan.use_jspace is False:
            progress.note("use_jspace=false; act without j-space")

        log.emit("phase", phase="act", cycle=cycle)
        progress.phase("act", cap)
        jspace = skills.jspace_dir()
        factgraph = skills.factgraph_dir()
        graph_seed_path = flame_dir / "graph_seed.json"
        graph_run_rel = ""
        if skill == "fact-graph":
            if factgraph is None:
                raise FlameError("fact-graph skill missing on disk")
            seed = prompts.build_graph_seed(
                original_task,
                plan,
                brief=brief_text,
                diagnosis=diagnosis,
            )
            graph_seed_path.write_text(
                json.dumps(seed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            graph_run_rel = _init_factgraph_board(
                workspace=cfg.workspace,
                factgraph=factgraph,
                seed_path=graph_seed_path,
                cycle=cycle,
            )
            (flame_dir / "graph_run.json").write_text(
                json.dumps({"run_dir": graph_run_rel}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            progress.note(f"fact-graph inited: {graph_run_rel}")
            # Fresh board → old graph-result.md must not shadow this cycle's RESULT.md.
            (flame_dir / "graph-result.md").unlink(missing_ok=True)
        else:
            graph_seed_path.unlink(missing_ok=True)
            (flame_dir / "graph_run.json").unlink(missing_ok=True)
        (flame_dir / "act_skill.json").write_text(
            json.dumps(
                {
                    "effort": cfg.effort.value,
                    "skill": skill,
                    "use_jspace": plan.use_jspace,
                    "jspace": str(jspace) if jspace else None,
                    "factgraph": str(factgraph) if factgraph else None,
                    "graph_seed": str(graph_seed_path.name) if skill == "fact-graph" else None,
                    "graph_run": graph_run_rel or None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        verify_before = _read_bytes(flame_dir / "verify.json")
        cycle_trace = evidence.ToolTrace()
        act_trace = evidence.ToolTrace()

        def _on_act_event(event: dict[str, Any]) -> None:
            evidence.collect_tool_event(event, act_trace)

        if budget.use_act_meld(cfg.effort):
            act, act_note = _run_meld_act(
                backend,
                log,
                progress,
                original_task,
                plan,
                flame_dir,
                workspace=cfg.workspace,
                on_event=_on_act_event,
            )
        else:
            act = backend.run(
                prompts.act_prompt(
                    original_task,
                    plan,
                    skill=skill,
                    jspace_dir=str(jspace) if jspace else "",
                    factgraph_dir=str(factgraph) if factgraph else "",
                    graph_run_dir=graph_run_rel,
                ),
                phase=Phase.act,
                force=True,
                mode=None,
                on_event=_on_act_event,
            )
            act_note = ""
        cycle_trace.absorb(act_trace)
        log.emit("agent_done", phase="act", error=act.is_error, code=act.returncode)
        if act.is_error and not act.timed_out:
            msg = act.text.strip() or f"act agent failed (exit {act.returncode})"
            progress.fail(msg)
            raise FlameError(msg)
        if act.timed_out:
            if not act_note:
                act_note = (
                    f"Act timed out after {cfg.timeout_sec}s (Flame watchdog). "
                    "Judge workspace artifacts (.jspace/, .fact-graph/, deliverables); "
                    "an incomplete agent reply is not proof the job is impossible. "
                    "Prefer retry=true if more cycles could finish from this state."
                )
            progress.fail(f"act timed out after {cfg.timeout_sec}s; handing partial work to verify")
            (flame_dir / "act_status.json").write_text(
                json.dumps(
                    {
                        "status": "timed_out",
                        "timeout_sec": cfg.timeout_sec,
                        "note": act_note,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            log.emit("act_timeout", timeout_sec=cfg.timeout_sec)
            act_output = act.text.strip() or act_note
        else:
            (flame_dir / "act_status.json").unlink(missing_ok=True)
            act_output = act.text.strip() or plan.goal

        schema_gaps = list(plan.schema_gaps)
        schema_gaps.extend(_restore_plan_if_mutated(flame_dir, plan))
        schema_gaps.extend(_restore_verify_if_mutated(flame_dir, verify_before))
        _finalize_act_json(
            flame_dir,
            cfg.workspace,
            act_text=act.text,
            timed_out=act.timed_out,
        )
        schema_gaps.extend(_canonical_act_json(flame_dir))

        log.emit("phase", phase="verify", cycle=cycle)
        progress.phase("verify", cap)
        verify = _run_verify(
            backend,
            log,
            original_task,
            plan,
            flame_dir,
            workspace=cfg.workspace,
            cycle_trace=cycle_trace,
            act_note=act_note,
            plan_mtime=round_plan_mtime,
            schema_gaps=schema_gaps,
        )
        last_text = act_output
        if verify.degraded:
            progress.fail("verify degraded; delivering act output")
            log.emit("finish", passed=False, cycles=cycle, reason="verify_degraded")
            return RunResult(
                output=act_output,
                passed=False,
                cycles=cycle,
                log_path=log.path,
                plan=plan,
                verify=verify,
            )
        progress.note(
            f"points_met={verify.points_met} aligned={verify.aligned} "
            f"evidence_ok={verify.evidence_ok} retry={verify.retry}"
        )
        if verify.passed:
            progress.note("checks: " + "; ".join(verify.checks[:3]))
            log.emit("finish", passed=True, cycles=cycle)
            progress.done(f"passed in {cycle} cycle(s)")
            return RunResult(
                output=act_output,
                passed=True,
                cycles=cycle,
                log_path=log.path,
                plan=plan,
                verify=verify,
            )

        progress.fail(verify.diagnosis or "verify rejected")
        if not verify.retry:
            log.emit("finish", passed=False, cycles=cycle, reason="no_retry")
            progress.fail("verify will not retry")
            return RunResult(
                output=verify.diagnosis or last_text,
                passed=False,
                cycles=cycle,
                log_path=log.path,
                plan=plan,
                verify=verify,
            )
        if cfg.effort is Effort.fast:
            progress.note("fast: one verify round; delivering act")
            log.emit("finish", passed=False, cycles=cycle, reason="fast_cap")
            return RunResult(
                output=last_text,
                passed=False,
                cycles=cycle,
                log_path=log.path,
                plan=plan,
                verify=verify,
            )

        diagnosis = prompts.correction_for_plan(verify)
        log.emit("replan", cycle=cycle, diagnosis=diagnosis)

    log.emit("finish", passed=False, cycles=cycles, reason="safety_cap")
    progress.fail(f"stopped after {cycles} cycle(s) (safety cap)")
    return RunResult(
        output=diagnosis or last_text,
        passed=False,
        cycles=cycles,
        log_path=log.path,
        plan=plan,
        verify=verify,
    )


def _act_skill(effort: Effort, plan: Plan) -> str | None:
    if budget.use_factgraph(effort):
        return "fact-graph"
    if budget.use_jspace(effort, plan.use_jspace):
        return "j-space"
    return None


def _run_meld_act(
    backend: AgentBackend,
    log: SessionLog,
    progress: Progress,
    original_task: str,
    plan: Plan,
    flame_dir: Path,
    *,
    workspace: Path,
    on_event: Any,
) -> tuple[AgentResult, str]:
    """Act-stage fusion: panels → judge picks winner → that panel writes answer.md."""
    progress.note("meld panels")
    jobs = [
        {
            "prompt": prompts.act_meld_panel_prompt(original_task, plan, role, desc),
            "phase": Phase.act,
            "force": False,
            "mode": "ask",
            "on_event": on_event,
        }
        for role, desc in prompts.MELD_ROLES
    ]
    panels = backend.run_parallel(jobs)
    ok: list[tuple[str, str]] = []
    for (role, _desc), panel in zip(prompts.MELD_ROLES, panels, strict=True):
        log.emit(
            "agent_done",
            phase="act",
            role=role,
            error=panel.is_error,
            code=panel.returncode,
            timed_out=panel.timed_out,
        )
        if panel.is_error or panel.timed_out or not panel.text.strip():
            continue
        ok.append((role, panel.text.strip()))
    if not ok:
        raise FlameError("act meld: no panel produced an answer")

    winner = ""
    judge_json = ""
    if len(ok) >= 2:
        progress.note("meld judge")
        blob = "\n\n".join(f"### {role}\n{text}" for role, text in ok)
        judge = backend.run(
            prompts.act_meld_judge_prompt(original_task, blob),
            phase=Phase.act,
            force=False,
            mode="ask",
            on_event=on_event,
        )
        log.emit(
            "agent_done",
            phase="act",
            role="judge",
            error=judge.is_error,
            code=judge.returncode,
        )
        payload = extract_json(judge.text) if not judge.is_error and not judge.timed_out else None
        if isinstance(payload, dict):
            payload = schema.strip_to_allowed(payload, schema.MELD_JUDGE_KEYS)
            pick = str(payload.get("winner") or "").strip()
            allowed = {role for role, _text in ok}
            if pick in allowed:
                winner = pick
                judge_json = json.dumps(payload, ensure_ascii=False, indent=2)
                (flame_dir / "meld-judge.json").write_text(judge_json + "\n", encoding="utf-8")
        if not winner:
            progress.fail("meld judge missing winner; selected panel writes from draft")
    if not winner:
        names = {role for role, _text in ok}
        winner = "primary_analyst" if "primary_analyst" in names else ok[0][0]
    draft = next(text for role, text in ok if role == winner)

    progress.note(f"meld finalizer ({winner})")
    final = backend.run(
        prompts.act_meld_finalizer_prompt(
            original_task,
            plan,
            role=winner,
            panel_answer=draft,
            judge_json=judge_json,
        ),
        phase=Phase.act,
        force=True,
        mode=None,
        on_event=on_event,
    )
    log.emit(
        "agent_done",
        phase="act",
        role="finalizer",
        error=final.is_error,
        code=final.returncode,
        timed_out=final.timed_out,
    )
    if final.is_error and not final.timed_out:
        progress.fail("meld finalizer failed; using selected panel draft")
        _write_answer_md(workspace, draft)
        return AgentResult(text=draft, is_error=False, returncode=0), ""
    if final.timed_out and not schema.answer_md_path(workspace):
        _write_answer_md(workspace, draft)
    return final, ""


def _init_factgraph_board(
    *,
    workspace: Path,
    factgraph: Path,
    seed_path: Path,
    cycle: int,
) -> str:
    """Harness-owned init so board goal/constraints cannot be swapped by act argv."""
    rel = f".fact-graph/runs/flame-act-c{cycle}"
    run_dir = workspace / rel
    if run_dir.exists():
        shutil.rmtree(run_dir)
    orch = factgraph / "scripts" / "orchestrator.py"
    cmd = [
        sys.executable,
        str(orch),
        "init",
        "--run-dir",
        str(run_dir),
        "--title",
        f"flame-act-c{cycle}",
        "--seed",
        str(seed_path),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise FlameError(f"fact-graph init failed: {detail}")
    return rel


def _orchestrator_script(factgraph: Path) -> Path:
    return factgraph / "scripts" / "orchestrator.py"


def _graph_hint(
    *,
    factgraph: Path,
    run_dir: Path,
    content: str,
    creator: str,
    workspace: Path,
) -> None:
    orch = _orchestrator_script(factgraph)
    cmd = [
        sys.executable,
        str(orch),
        "hint",
        "--run-dir",
        str(run_dir),
        "--content",
        content,
        "--creator",
        creator,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise FlameError(f"fact-graph hint failed: {detail}")


def _graph_reopen_if_completed(
    *,
    factgraph: Path,
    run_dir: Path,
    workspace: Path,
) -> None:
    board = _read_json_file(run_dir / "board.json")
    if not board or board.get("status") != "completed":
        return
    orch = _orchestrator_script(factgraph)
    cmd = [
        sys.executable,
        str(orch),
        "reopen",
        "--run-dir",
        str(run_dir),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise FlameError(f"fact-graph reopen failed: {detail}")


def _graph_run_orchestrator(
    *,
    factgraph: Path,
    run_dir: Path,
    workspace: Path,
    extra_budget: float,
) -> str:
    orch = _orchestrator_script(factgraph)
    cmd = [
        sys.executable,
        str(orch),
        "run",
        "--run-dir",
        str(run_dir),
        "--extra-budget",
        str(extra_budget),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise FlameError(f"fact-graph run failed: {detail}")
    text = (result.stdout or result.stderr or "").strip()
    for line in reversed(text.splitlines()):
        if line.startswith("run 结束: status="):
            return line.split("status=", 1)[-1].split()[0]
    board = _read_json_file(run_dir / "board.json")
    return str((board or {}).get("status") or "unknown")


def _read_graph_act_output(workspace: Path, run_dir: Path) -> str:
    rel = run_dir.relative_to(workspace) if run_dir.is_relative_to(workspace) else run_dir
    flame_result = workspace / ".flame" / "graph-result.md"
    if flame_result.is_file():
        return flame_result.read_text(encoding="utf-8").strip()
    result_md = run_dir / "RESULT.md"
    if result_md.is_file():
        text = result_md.read_text(encoding="utf-8").strip()
        flame_result.parent.mkdir(parents=True, exist_ok=True)
        flame_result.write_text(text + "\n", encoding="utf-8")
        return text
    return f"fact-graph run finished ({rel})"


def _run_plan(
    backend: AgentBackend,
    log: SessionLog,
    brief: str,
    diagnosis: str,
    flame_dir: Path,
    original_task: str,
    *,
    ask_use_jspace: bool,
) -> Plan:
    result = backend.run(
        prompts.plan_prompt(
            original_task,
            brief=brief,
            diagnosis=diagnosis,
            ask_use_jspace=ask_use_jspace,
        ),
        phase=Phase.plan,
        force=True,
        mode=None,
    )
    log.emit("agent_done", phase="plan", error=result.is_error, code=result.returncode)
    payload = _read_json_file(flame_dir / "plan.json") or extract_json(result.text)
    if payload is None:
        (flame_dir / "plan.raw.txt").write_text(result.text or result.stderr, encoding="utf-8")
        log.emit("plan_degraded", reason="no_json")
        return _stub_plan(original_task, ask_use_jspace=ask_use_jspace)
    plan = _plan_from(payload, ask_use_jspace=ask_use_jspace)
    plan.schema_gaps = schema.validate_plan_payload(
        payload, ask_use_jspace=ask_use_jspace
    )
    if not plan.summary.strip():
        plan.summary = stage_summary.plan_summary(payload if isinstance(payload, dict) else None)
    _write_plan(flame_dir / "plan.json", plan)
    return plan


def _plan_payload(plan: Plan) -> dict[str, Any]:
    dumped: dict[str, Any] = {
        "goal": plan.goal,
        "approach": plan.approach,
        "summary": plan.summary
        or stage_summary.plan_summary({"approach": plan.approach}),
        "constraints": plan.constraints,
        "verify_points": plan.verify_points,
    }
    if plan.use_jspace is not None:
        dumped["use_jspace"] = plan.use_jspace
    if plan.degraded:
        dumped["degraded"] = True
    return dumped


def _write_plan(path: Path, plan: Plan) -> None:
    path.write_text(
        json.dumps(_plan_payload(plan), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_verify(
    backend: AgentBackend,
    log: SessionLog,
    original_task: str,
    plan: Plan,
    flame_dir: Path,
    *,
    workspace: Path,
    cycle_trace: evidence.ToolTrace,
    act_note: str = "",
    require_evidence_touch: bool = True,
    plan_mtime: float | None = None,
    schema_gaps: list[str] | None = None,
) -> VerifyResult:
    verify_trace = evidence.ToolTrace()
    result = backend.run(
        prompts.verify_prompt(
            original_task,
            plan,
            act_note=act_note,
            tool_trace=evidence.render_trace_for_prompt(cycle_trace),
        ),
        phase=Phase.verify,
        force=True,
        mode=None,
        on_event=lambda event: evidence.collect_tool_event(event, verify_trace),
    )
    cycle_trace.absorb(verify_trace)
    evidence.write_trace(flame_dir / "tool_trace.json", cycle_trace)
    log.emit("agent_done", phase="verify", error=result.is_error, code=result.returncode)
    path = flame_dir / "verify.json"
    payload: dict[str, Any] | None = _read_json_file(path)
    if payload is None:
        payload = extract_json(result.text)
    if payload is None:
        _dump_verify_debug(flame_dir, result)
        return VerifyResult(
            passed=False,
            points_met=False,
            aligned=False,
            evidence_ok=False,
            retry=False,
            diagnosis=result.text.strip() or "verify produced no JSON artifact",
            degraded=True,
        )
    verify = _verify_from_payload(
        payload,
        workspace=workspace,
        trace=cycle_trace,
        fail_open_if_no_trace=bool(act_note),
        require_evidence_touch=require_evidence_touch,
        plan_mtime=plan_mtime,
        schema_gaps=schema_gaps,
    )
    _write_verify(path, verify)
    return verify


def _dump_verify_debug(flame_dir: Path, result: Any) -> None:
    """Persist raw verify output on degraded for post-mortem diagnosis."""
    try:
        lines = [
            f"returncode: {result.returncode}",
            f"is_error: {result.is_error}",
            f"text_len: {len(result.text or '')}",
            "",
            "--- stdout/text ---",
            (result.text or "(empty)").strip(),
        ]
        if hasattr(result, "stderr") and result.stderr:
            lines += ["", "--- stderr ---", result.stderr.strip()]
        (flame_dir / "verify_debug.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _verify_from_payload(
    payload: dict[str, Any],
    *,
    workspace: Path | None = None,
    trace: evidence.ToolTrace | None = None,
    fail_open_if_no_trace: bool = False,
    require_evidence_touch: bool = True,
    plan_mtime: float | None = None,
    schema_gaps: list[str] | None = None,
) -> VerifyResult:
    checks = _str_list(payload.get("checks"))
    drift = _str_list(payload.get("drift"))
    gaps = _str_list(payload.get("evidence_gaps"))
    format_gaps = schema.validate_verify_payload(payload) + list(schema_gaps or [])
    for gap in format_gaps:
        if gap not in gaps:
            gaps.append(gap)
    if "points_met" in payload:
        points_met = bool(payload.get("points_met"))
    else:
        points_met = bool(payload.get("passed"))
    aligned = True if "aligned" not in payload else bool(payload.get("aligned"))
    evidence_ok = True if "evidence_ok" not in payload else bool(payload.get("evidence_ok"))
    if points_met and not checks:
        evidence_ok = False
        if not gaps:
            gaps = ["points claimed met but no objective evidence handles in checks"]
    if points_met and evidence_ok and workspace is not None and trace is not None:
        audit = evidence.audit_checks(
            checks,
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=fail_open_if_no_trace,
            require_touch=require_evidence_touch,
        )
        for gap in audit.gaps:
            if gap not in gaps:
                gaps.append(gap)
        if not audit.ok:
            evidence_ok = False
    if workspace is not None and plan_mtime is not None:
        for gap in schema.audit_answer_vs_plan(workspace, plan_mtime=plan_mtime):
            if gap not in gaps:
                gaps.append(gap)
            if points_met:
                evidence_ok = False
    if format_gaps and points_met:
        evidence_ok = False
    passed = points_met and aligned and evidence_ok
    diagnosis = str(payload.get("diagnosis") or "")
    if not passed and not diagnosis:
        diagnosis = "verify rejected without diagnosis"
    if not evidence_ok and gaps and "evidence" not in diagnosis.lower():
        diagnosis = (diagnosis + "; " if diagnosis else "") + "evidence audit failed: " + gaps[0]
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        summary = stage_summary.verify_summary(
            {
                "passed": passed,
                "points_met": points_met,
                "aligned": aligned,
                "evidence_ok": evidence_ok,
                "checks": checks,
                "diagnosis": diagnosis,
            }
        )
    # Audit is part of verify. Decide retry only after all three legs settle.
    # retry=false is for "more cycles cannot help" on a finished content judgment;
    # evidence-only failure means verify is incomplete → keep cycling.
    if passed:
        retry = True
    elif points_met and aligned and not evidence_ok:
        retry = True
    else:
        retry = bool(payload["retry"]) if "retry" in payload else True
    return VerifyResult(
        passed=passed,
        points_met=points_met,
        aligned=aligned,
        evidence_ok=evidence_ok,
        retry=retry,
        checks=checks,
        drift=drift,
        evidence_gaps=gaps,
        diagnosis=diagnosis,
        summary=summary,
    )


def _write_verify(path: Path, verify: VerifyResult) -> None:
    path.write_text(
        json.dumps(
            {
                "passed": verify.passed,
                "points_met": verify.points_met,
                "aligned": verify.aligned,
                "evidence_ok": verify.evidence_ok,
                "retry": verify.retry,
                "summary": verify.summary
                or stage_summary.verify_summary(
                    {
                        "passed": verify.passed,
                        "points_met": verify.points_met,
                        "evidence_ok": verify.evidence_ok,
                        "checks": verify.checks,
                        "diagnosis": verify.diagnosis,
                    }
                ),
                "checks": verify.checks,
                "drift": verify.drift,
                "evidence_gaps": verify.evidence_gaps,
                "diagnosis": verify.diagnosis,
                "degraded": verify.degraded,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _finalize_act_json(
    flame_dir: Path,
    workspace: Path,
    *,
    act_text: str = "",
    timed_out: bool = False,
    graph_note: str = "",
) -> None:
    path = flame_dir / "act.json"
    payload: dict[str, Any] = _read_json_file(path) or {}
    if not str(payload.get("summary") or "").strip():
        payload["summary"] = stage_summary.synthesize_act_summary(
            workspace,
            act_text,
            timed_out=timed_out,
            graph_note=graph_note,
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_act_json(flame_dir: Path) -> list[str]:
    path = flame_dir / "act.json"
    payload = _read_json_file(path)
    if payload is None:
        return ["act.json missing"]
    gaps = schema.validate_act_payload(payload)
    cleaned = schema.strip_to_allowed(payload, schema.ACT_KEYS)
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return gaps


def _read_bytes(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_plan_if_mutated(flame_dir: Path, plan: Plan) -> list[str]:
    """Rewrite plan.json only if act changed it. A no-op write would bump mtime."""
    path = flame_dir / "plan.json"
    payload = _read_json_file(path)
    expected = _plan_payload(plan)
    gaps: list[str] = []
    if payload is None:
        gaps.append("plan.json missing after act")
        _write_plan(path, plan)
        return gaps
    extra = schema.extra_keys(payload, schema.PLAN_KEYS)
    if extra:
        gaps.append("plan.json extra keys after act: " + ", ".join(extra))
    elif payload != expected:
        gaps.append("plan.json was rewritten during act")
    if gaps:
        _write_plan(path, plan)
    return gaps


def _restore_verify_if_mutated(flame_dir: Path, before: bytes | None) -> list[str]:
    """Act must not create, delete, or rewrite verify.json."""
    path = flame_dir / "verify.json"
    after = _read_bytes(path)
    if before is None:
        if after is None:
            return []
        path.unlink(missing_ok=True)
        return ["verify.json was written during act"]
    if after == before:
        return []
    path.write_bytes(before)
    if after is None:
        return ["verify.json missing after act"]
    return ["verify.json was rewritten during act"]


def _write_answer_md(workspace: Path, text: str) -> None:
    body = str(text or "").strip()
    if not body:
        return
    (workspace / "answer.md").write_text(body + "\n", encoding="utf-8")


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _stub_plan(original: str, *, ask_use_jspace: bool) -> Plan:
    return Plan(
        goal=original.strip(),
        approach="Carry out the original user request.",
        constraints=[],
        verify_points=[],
        use_jspace=True if ask_use_jspace else None,
        degraded=True,
    )


def _plan_from(payload: dict[str, Any], *, ask_use_jspace: bool) -> Plan:
    # goal may be empty; harness forces plan.goal = original after parse.
    approach = payload.get("approach")
    if isinstance(approach, list):
        approach = "\n".join(str(item).strip() for item in approach if str(item).strip())
    else:
        approach = str(approach or "").strip()
    if not approach:
        legacy = _str_list(payload.get("milestones"))
        unknown = str(payload.get("unknown") or "").strip()
        approach = unknown or "\n".join(legacy)
    constraints = _str_list(payload.get("constraints"))
    verify_points = _str_list(payload.get("verify_points"))
    use_jspace: bool | None = None
    if ask_use_jspace and "use_jspace" in payload:
        use_jspace = bool(payload.get("use_jspace"))
    return Plan(
        goal=str(payload.get("goal") or "").strip(),
        approach=approach,
        constraints=constraints,
        verify_points=verify_points,
        use_jspace=use_jspace,
        summary=str(payload.get("summary") or "").strip(),
    )


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
