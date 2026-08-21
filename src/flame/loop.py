from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from flame import budget, evidence, preprocess, prompts, skills
from flame.backend import AgentBackend, extract_json
from flame.config import Config
from flame.log import SessionLog
from flame.progress import Progress
from flame.safety import SafetyDenied, deny_reason
from flame.types import Effort, Phase, Plan, RunResult, VerifyResult


class FlameError(RuntimeError):
    pass


def run(
    task: str,
    *,
    workspace: str | Path | None = None,
    effort: str | None = None,
    model: str | None = None,
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
    session_id = uuid.uuid4().hex[:12]
    log = SessionLog(cfg.log_dir / f"{session_id}.jsonl")
    log.emit("start", task=task, effort=cfg.effort.value, model=cfg.model)
    backend = AgentBackend(cfg, progress)

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
            ask_use_ledger=budget.ask_use_ledger(cfg.effort),
        )
        plan.goal = original_task.strip()
        if budget.ask_use_ledger(cfg.effort) and plan.use_ledger is None:
            plan.use_ledger = True
        elif not budget.ask_use_ledger(cfg.effort):
            plan.use_ledger = None
        _write_plan(flame_dir / "plan.json", plan)
        skill = _act_skill(cfg.effort, plan)
        progress.note("goal: original (harness-forced)")
        if plan.degraded:
            progress.fail("plan degraded; act will run on the original request")
        if skill:
            progress.note(f"skill={skill}")
        elif cfg.effort is Effort.high and plan.use_ledger is False:
            progress.note("use_ledger=false; act without j-space")

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
        else:
            graph_seed_path.unlink(missing_ok=True)
            (flame_dir / "graph_run.json").unlink(missing_ok=True)
        (flame_dir / "act_skill.json").write_text(
            json.dumps(
                {
                    "effort": cfg.effort.value,
                    "skill": skill,
                    "use_ledger": plan.use_ledger,
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
        cycle_trace = evidence.ToolTrace()
        act_trace = evidence.ToolTrace()
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
            on_event=lambda event: evidence.collect_tool_event(event, act_trace),
        )
        cycle_trace.absorb(act_trace)
        log.emit("agent_done", phase="act", error=act.is_error, code=act.returncode)
        act_note = ""
        if act.is_error and not act.timed_out:
            msg = act.text.strip() or f"act agent failed (exit {act.returncode})"
            progress.fail(msg)
            raise FlameError(msg)
        if act.timed_out:
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
    if budget.use_jspace(effort, plan.use_ledger):
        return "j-space"
    return None


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


def _run_plan(
    backend: AgentBackend,
    log: SessionLog,
    brief: str,
    diagnosis: str,
    flame_dir: Path,
    original_task: str,
    *,
    ask_use_ledger: bool,
) -> Plan:
    result = backend.run(
        prompts.plan_prompt(
            original_task,
            brief=brief,
            diagnosis=diagnosis,
            ask_use_ledger=ask_use_ledger,
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
        return _stub_plan(original_task, ask_use_ledger=ask_use_ledger)
    plan = _plan_from(payload, ask_use_ledger=ask_use_ledger)
    _write_plan(flame_dir / "plan.json", plan)
    return plan


def _write_plan(path: Path, plan: Plan) -> None:
    dumped: dict[str, Any] = {
        "goal": plan.goal,
        "approach": plan.approach,
        "constraints": plan.constraints,
        "verify_points": plan.verify_points,
    }
    if plan.use_ledger is not None:
        dumped["use_ledger"] = plan.use_ledger
    if plan.degraded:
        dumped["degraded"] = True
    path.write_text(json.dumps(dumped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    )
    _write_verify(path, verify)
    return verify


def _verify_from_payload(
    payload: dict[str, Any],
    *,
    workspace: Path | None = None,
    trace: evidence.ToolTrace | None = None,
    fail_open_if_no_trace: bool = False,
) -> VerifyResult:
    checks = _str_list(payload.get("checks"))
    drift = _str_list(payload.get("drift"))
    gaps = _str_list(payload.get("evidence_gaps"))
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
        )
        for gap in audit.gaps:
            if gap not in gaps:
                gaps.append(gap)
        if not audit.ok:
            evidence_ok = False
    passed = points_met and aligned and evidence_ok
    diagnosis = str(payload.get("diagnosis") or "")
    if not passed and not diagnosis:
        diagnosis = "verify rejected without diagnosis"
    if not evidence_ok and gaps and "evidence" not in diagnosis.lower():
        diagnosis = (diagnosis + "; " if diagnosis else "") + "evidence audit failed: " + gaps[0]
    retry = True if passed else bool(payload["retry"]) if "retry" in payload else True
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


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _stub_plan(original: str, *, ask_use_ledger: bool) -> Plan:
    return Plan(
        goal=original.strip(),
        approach="Carry out the original user request.",
        constraints=[],
        verify_points=[],
        use_ledger=True if ask_use_ledger else None,
        degraded=True,
    )


def _plan_from(payload: dict[str, Any], *, ask_use_ledger: bool) -> Plan:
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
    use_ledger: bool | None = None
    if ask_use_ledger:
        if "use_ledger" not in payload:
            use_ledger = True
        else:
            use_ledger = bool(payload.get("use_ledger"))
    return Plan(
        goal=str(payload.get("goal") or "").strip(),
        approach=approach,
        constraints=constraints,
        verify_points=verify_points,
        use_ledger=use_ledger,
    )


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
