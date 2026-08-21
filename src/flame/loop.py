from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from flame import budget, preprocess, prompts, skills
from flame.backend import AgentBackend, extract_json
from flame.config import Config
from flame.log import SessionLog
from flame.progress import Progress
from flame.safety import SafetyDenied, deny_reason
from flame.types import Effort, Phase, Plan, RunResult, SearchKind, VerifyResult


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
            ask_search=budget.use_act_skills(cfg.effort),
        )
        if not budget.use_act_skills(cfg.effort):
            plan.search = None
        elif plan.search is None:
            plan.search = SearchKind.depth
        _write_plan(flame_dir / "plan.json", plan)
        skill = _act_skill(plan)
        progress.note(f"goal: {plan.goal}")
        if plan.degraded:
            progress.fail("plan degraded; act will run on the original request")
        if skill:
            progress.note(f"search={plan.search.value} skill={skill}")

        log.emit("phase", phase="act", cycle=cycle)
        progress.phase("act", cap)
        jspace = skills.jspace_dir()
        factgraph = skills.factgraph_dir()
        (flame_dir / "act_skill.json").write_text(
            json.dumps(
                {
                    "search": plan.search.value if plan.search else None,
                    "skill": skill,
                    "jspace": str(jspace) if jspace else None,
                    "factgraph": str(factgraph) if factgraph else None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        act = backend.run(
            prompts.act_prompt(
                original_task,
                plan,
                skill=skill,
                jspace_dir=str(jspace) if jspace else "",
                factgraph_dir=str(factgraph) if factgraph else "",
            ),
            phase=Phase.act,
            force=True,
            mode=None,
        )
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


def _act_skill(plan: Plan) -> str | None:
    if plan.search is SearchKind.breadth:
        return "fact-graph"
    if plan.search is SearchKind.depth:
        return "j-space"
    return None


def _run_plan(
    backend: AgentBackend,
    log: SessionLog,
    brief: str,
    diagnosis: str,
    flame_dir: Path,
    original_task: str,
    *,
    ask_search: bool,
) -> Plan:
    result = backend.run(
        prompts.plan_prompt(
            original_task,
            brief=brief,
            diagnosis=diagnosis,
            ask_search=ask_search,
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
        return _stub_plan(original_task, ask_search=ask_search)
    plan = _plan_from(payload, ask_search=ask_search)
    _write_plan(flame_dir / "plan.json", plan)
    return plan


def _write_plan(path: Path, plan: Plan) -> None:
    dumped: dict[str, Any] = {
        "goal": plan.goal,
        "approach": plan.approach,
        "constraints": plan.constraints,
        "verify_points": plan.verify_points,
    }
    if plan.search is not None:
        dumped["search"] = plan.search.value
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
    act_note: str = "",
) -> VerifyResult:
    result = backend.run(
        prompts.verify_prompt(original_task, plan, act_note=act_note),
        phase=Phase.verify,
        force=True,
        mode=None,
    )
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
    verify = _verify_from_payload(payload)
    _write_verify(path, verify)
    return verify


def _verify_from_payload(payload: dict[str, Any]) -> VerifyResult:
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
            gaps = ["points claimed met but no named command/file evidence"]
    passed = points_met and aligned and evidence_ok
    diagnosis = str(payload.get("diagnosis") or "")
    if not passed and not diagnosis:
        diagnosis = "verify rejected without diagnosis"
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


def _stub_plan(original: str, *, ask_search: bool) -> Plan:
    goal = original.strip().split("\n")[0][:200] or "carry out the original request"
    return Plan(
        goal=goal,
        approach="Carry out the original user request.",
        constraints=[],
        verify_points=[],
        search=SearchKind.depth if ask_search else None,
        degraded=True,
    )


def _plan_from(payload: dict[str, Any], *, ask_search: bool) -> Plan:
    goal = str(payload.get("goal") or "").strip()
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
    if not goal:
        return _stub_plan(approach or "carry out the original request", ask_search=ask_search)
    return Plan(
        goal=goal,
        approach=approach,
        constraints=constraints,
        verify_points=verify_points,
        search=_search_from(payload.get("search")),
    )


def _search_from(value: Any) -> SearchKind | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    # Only Flame search kinds. Do not map algorithm names (e.g. "bfs") — those are task topics.
    if raw in {"depth", "deep", "dfs", "linear"}:
        return SearchKind.depth
    if raw in {"breadth", "wide"}:
        return SearchKind.breadth
    return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
