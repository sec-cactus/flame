from __future__ import annotations

import json

from flame.types import BRIEF_SCHEMA, Brief, Plan, VerifyResult


MELD_ROLES = (
    ("primary_analyst", "主分析：正面回答，给出主要结论和依据。"),
    ("critical_reviewer", "批判审查：找错误假设、逻辑跳跃、反例和风险。"),
    ("coverage_reviewer", "覆盖检查：问题有没有答全，边界和未知项。"),
)

# Non-act phases must not load Cursor skills; Flame attaches skills only on act.
_NO_SKILLS = (
    "Skill ban (this phase): Do not read, open, follow, or invoke any Cursor skill "
    "(j-space, fact-graph, or anything under ~/.cursor/skills* / skills-cursor). "
    "Do not create .jspace/ or .fact-graph/. Do not read any SKILL.md. "
    "If a host skill is already loaded, ignore it. Skills are for act only, and only "
    "when Flame attaches one."
)


def meld_panel_prompt(task: str, role: str, role_desc: str) -> str:
    return f"""[Flame phase: meld]
[Flame meld role: {role}]
You are one independent panel member in a local fusion pass. Other panels cannot see you.
Role: {role_desc}
{_NO_SKILLS}

User request:
{task}

Rules:
- Answer from the user request only. Do not read the repository. Do not edit files. Do not implement.
- Do not discuss the fusion process. Do not claim consensus with other models.
- Distinguish fact, inference, and what you cannot know.

Return a direct analysis. Plain text is fine.
"""


def meld_judge_prompt(task: str, panel_blob: str) -> str:
    return f"""[Flame phase: meld]
[Flame meld role: judge]
You are the Judge. Compare independent panel answers. Do not write the final user-facing solution.
Do not read the repository. Do not edit files.
{_NO_SKILLS}

User request:
{task}

Panel answers:
{panel_blob}

Write ONLY a JSON object:
{{
  "consensus": [{{"point": "...", "models": ["primary_analyst"], "caveat": ""}}],
  "contradictions": [{{"topic": "...", "positions": [{{"models": ["..."], "point": "..."}}], "status": "unresolved"}}],
  "unique_insights": [{{"point": "...", "models": ["..."]}}],
  "blind_spots": [{{"point": "...", "importance": "high"}}],
  "verification_needed": [{{"claim": "...", "required_evidence": "..."}}]
}}
Consensus is not external fact. Do not invent verification. Do not pick a decisive move. JSON only.
"""


def quadrants_prompt(task: str, judge_json: str = "") -> str:
    extra = ""
    if judge_json:
        extra = (
            "\nA prior meld Judge JSON follows. Use it as hypotheses, not as facts. "
            "Drop claims you cannot ground in the request or a read-only look at the repo.\n\n"
            f"{judge_json}\n"
        )
    return f"""[Flame phase: quadrants]
You fill Rumsfeld's four quadrants for this request. You are not the planner and not the executor.
Do not implement. Do not pick search. Do not pick a decisive move. Do not write success/failure factors.
{_NO_SKILLS}

User request:
{task}
{extra}
Read-only reconnaissance is allowed only to discover what belongs in the quadrants
(workspace / request artifacts only — not skills).

Write ONLY a JSON object:
{{
  "known_knowns": ["in the prompt; stated clearly"],
  "known_unknowns": ["we know we have not figured this out yet"],
  "unknown_knowns": ["so obvious the user would never write it down, but would recognize it"],
  "unknown_unknowns": ["the user did not know to ask; Blind Spot Pass lives here"]
}}
Keep each list short (at most 5). Empty list is valid. JSON only.
"""


def factors_prompt(task: str, quadrants_json: str, judge_json: str = "") -> str:
    extra = ""
    if judge_json:
        extra += f"\nMeld judge JSON (hypotheses, not facts):\n{judge_json}\n"
    extra += f"\nQuadrants (already filled; do not redo them):\n{quadrants_json}\n"
    return f"""[Flame phase: factors]
The quadrants are done. Now pick what the planner should care about this run.
Do not implement. Do not pick search. Do not rewrite the user request.
{_NO_SKILLS}

User request:
{task}
{extra}
Write ONLY a JSON object:
{{
  "success_factors": ["at most 3: what must be true for the original request to succeed"],
  "failure_factors": ["at most 3: premortem — how this would fail"],
  "decisive_move": "exactly one sentence: the load that, if wrong, fails the whole task",
  "summary": "1–2 plain sentences for the user: what preprocess concluded (no JSON, no path lists, ≤120 chars)"
}}
decisive_move must be a known unknown (already named in known_unknowns, or one you can now name because the quadrants made it speakable). It is NOT an unknown unknown — those stay in the table; they are not the attack order.
summary is user-facing; decisive_move is planner-facing (may overlap).
JSON only.
"""


def plan_prompt(
    original_task: str,
    brief: str = "",
    diagnosis: str = "",
    *,
    ask_use_ledger: bool = False,
) -> str:
    job = original_task.strip()
    extra = f"\n[1 original — the job]\n{job}\n"
    if diagnosis:
        extra += (
            "\n[2 verify — empirical, this cycle]\n"
            "Previous cycle was rejected. Change the plan. Do not repeat the same approach "
            "unless verify says it still stands. "
            "Verify may not redefine the job; it may only say what is true about the last attempt "
            "and what to try next.\n"
            f"{diagnosis}\n"
        )
    brief_body = _brief_for_plan(brief)
    if brief_body:
        stale = (
            "This brief was written before any cycle. After verify, it is background only: "
            "landmines and defaults that 1 and 2 did not already settle. "
            "Do not re-anchor approach to the brief's decisive_move."
            if diagnosis
            else "Use only where original is silent. Fields below are labeled; do not invent a new job."
        )
        extra += f"\n[3 brief — hypotheses, lowest]\n{stale}\n{brief_body}\n"
    ledger_schema = ""
    ledger_rules = ""
    if ask_use_ledger:
        ledger_schema = ',\n  "use_ledger": true\n'
        ledger_rules = """
Also set use_ledger (required on this effort). Default true.
Harness will mount the j-space ledger skill on act when true.
Set use_ledger=false only when:
- this cycle's approach is short (few seams, one-shot checkable), or
- the approach is clearly complex / multi-path (do not dig a ledger into unresolved branching).
Omit or unclear → true. Do not open skills now.
"""
    unknown_rule = (
        "approach: lead with what verify said must change."
        if diagnosis
        else "approach: lead with brief.decisive_move if it still satisfies original; otherwise pick from original."
    )
    return f"""[Flame phase: plan]
You are the planner for Flame. Do not implement. Write two contracts: one for act, one for verify.
{_NO_SKILLS}

When inputs conflict, this order is strict:
1. original — defines success. Never shrink, swap, or clarify it away.
2. verify — if present, wins on facts about the last attempt and on what this cycle changes.
3. brief — optional, pre-act guesses. Fills gaps only. A missing brief is fine.
{unknown_rule}
Act contract: goal, approach, constraints{', and use_ledger' if ask_use_ledger else ''}.
Goal MUST be the original user request verbatim (copy field [1]). The harness overwrites goal to original after you write — do not invent a proxy success sentence.
Approach is how to attack this cycle, not a second job.
Verify contract: verify_points (min-fail checks of the user-visible work, typically answer.md). Do not put plan.json, act.json, JSON key order, or file mtimes in verify_points — the harness owns those.
{ledger_rules}
{extra}
Write ONLY `.flame/plan.json` (create `.flame/` if needed). Do not change any other files.
JSON schema (no extra keys):
{{
  "goal": "verbatim copy of the original user request",
  "approach": "how to get there this cycle; first sentence is the load that if wrong fails the task",
  "summary": "1–2 plain sentences for the user: this cycle's plan in everyday language (no JSON, ≤120 chars)",
  "constraints": ["must / must not, from original and brief"],
  "verify_points": ["concrete min-fail checks of the deliverable, ideally runnable"]{ledger_schema}}}
You may also print the same JSON. Do not use a separate planning UI instead of this file.
"""


def _brief_for_plan(brief: str) -> str:
    text = brief.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(payload, dict) or payload.get("schema") != BRIEF_SCHEMA:
        return text
    return Brief.from_dict(payload).render_for_plan() or text


def act_prompt(
    task: str,
    plan: Plan,
    *,
    skill: str | None = None,
    jspace_dir: str = "",
    factgraph_dir: str = "",
    graph_run_dir: str = "",
) -> str:
    constraints = "\n".join(f"- {c}" for c in plan.constraints) or "- (none)"
    body = f"""[Flame phase: act]
You are the executor for Flame. Carry out this cycle. Do not stop at a proposal — apply the changes.
Do not delete the .flame/ directory. Do not rewrite the user request.

Original user request (= plan.goal, harness-forced):
{task}

Approach:
{plan.approach or "(none — do the original request)"}
Constraints:
{constraints}
Do not create or modify `.flame/plan.json` or `.flame/verify.json`. Those files belong to plan and verify; if the plan is wrong, leave it for the next plan cycle.
The user's reply belongs in `answer.md`.
Write or replace `answer.md` (workspace root or `.flame/answer.md`) this round.
The UI displays that file and nothing else as the answer.
"""
    if skill == "j-space":
        body += _jspace_act_addendum(jspace_dir)
    elif skill == "fact-graph":
        body += _factgraph_act_addendum(
            factgraph_dir,
            graph_run_dir=graph_run_dir or ".fact-graph/runs/flame-act",
        )
    else:
        body += (
            "\nSkill ban (this act): No Flame skill attached. Do not read or follow "
            "j-space, fact-graph, or other Cursor skills; do not create .jspace/ or "
            ".fact-graph/.\n"
        )
    body += """
Before you finish, write `.flame/act.json` (create `.flame/` if needed):
{
  "summary": "1–2 plain sentences for the user: what you delivered this cycle (no JSON, no path lists, ≤120 chars)",
  "deliverables": ["workspace-relative files the user should read, e.g. answer.md"]
}
Keep doing the work in the repo; act.json is only the user-facing recap.
"""
    return body


def render_graph_origin(*, brief: str = "", diagnosis: str = "") -> str:
    """Board origin = 现状 from preprocess brief + prior verify (not the user job)."""
    parts: list[str] = []
    brief_body = _brief_for_plan(brief)
    if brief_body:
        parts.append(
            "## Brief (preprocess; hypotheses, not facts)\n" + brief_body
        )
    if diagnosis.strip():
        parts.append("## Last verify (empirical)\n" + diagnosis.strip())
    if not parts:
        return (
            "No preprocess or prior verify. Discover workspace state via "
            "bootstrap/explore; do not invent a substitute job."
        )
    return "\n\n".join(parts)


def render_graph_goal(original: str, verify_points: list[str]) -> str:
    """Board goal = original plus verify_points as min-fail acceptance in-goal."""
    text = original.strip()
    points = [str(p).strip() for p in verify_points if str(p).strip()]
    if not points:
        return text
    lines = "\n".join(f"- {p}" for p in points)
    return f"{text}\n\n## Verify points (min-fail)\n{lines}"


def render_graph_constraints(constraints: list[str]) -> str:
    items = [str(c).strip() for c in constraints if str(c).strip()]
    return "\n".join(f"- {c}" for c in items)


def build_graph_seed(
    original: str,
    plan: Plan,
    *,
    brief: str = "",
    diagnosis: str = "",
) -> dict[str, str]:
    return {
        "origin": render_graph_origin(brief=brief, diagnosis=diagnosis),
        "goal": render_graph_goal(original, plan.verify_points),
        "constraints": render_graph_constraints(plan.constraints),
        "hint": (plan.approach or "").strip(),
    }


def _jspace_act_addendum(jspace_dir: str) -> str:
    path = jspace_dir or "(j-space skill not found on disk; use the host j-space skill if loaded)"
    return f"""
[Flame act skill: j-space]
Effort=high and plan.use_ledger=true. Before implementing, read `{path}/SKILL.md` and follow it.
This is a `loop` pass: keep a `.jspace/` ledger in this workspace if the controller is available
(`python3 {path}/scripts/jspace.py` when that file exists).
Hold the goal through the mechanical work. Do not stop at notes — deliver the plan.
A later verify phase is the judge; you still implement and leave evidence in the repo.
"""


def _factgraph_act_addendum(
    factgraph_dir: str,
    *,
    graph_run_dir: str,
) -> str:
    path = factgraph_dir or "(fact-graph skill missing)"
    orch = f"{path}/scripts/orchestrator.py" if factgraph_dir else "orchestrator.py"
    return f"""
[Flame act skill: fact-graph]
Effort=max: open the fact-graph. Follow `{path}/SKILL.md`. You are the control session.
Flame harness already inited the board from `.flame/graph_seed.json` at `{graph_run_dir}`
(origin=现状, goal=original+verify_points, constraints=plan.constraints, hint=approach).
Do **not** re-init or change origin/goal/constraints.

Run the orchestrator in the FOREGROUND and wait until it exits — do not background it.
Flame's verify phase starts as soon as you return; a running graph is a failed act.
Board complete is exploration progress only — Flame verify is the QA judge against the original request.

```bash
python3 {orch} run --run-dir {graph_run_dir}
```

Rules:
- Orchestrator is the only writer of board.json. Further human course-correction uses `hint`.
- Independent explore workers may run in parallel; do not have them edit the same files.
- When the orchestrator exits, apply any deliverable to this workspace. Copy or summarize
  `{graph_run_dir}/RESULT.md` into `.flame/graph-result.md`.
- Board `complete` is NOT Flame success. Leave artifacts so verify can check the original request.
"""


def verify_prompt(
    original_task: str,
    plan: Plan,
    *,
    act_note: str = "",
    tool_trace: str = "",
) -> str:
    points = "\n".join(f"- {c}" for c in plan.verify_points) or "- (none listed; judge the original request only)"
    status = ""
    if act_note.strip():
        status = f"\nAct status (from harness, not the model):\n{act_note.strip()}\n"
    trace_block = ""
    if tool_trace.strip():
        trace_block = (
            "\nTool handles touched this cycle (harness audit source; prefer citing these):\n"
            f"{tool_trace.strip()}\n"
        )
    return f"""[Flame phase: verify]
You are the only judge. You control whether the loop finishes, replans, or stops.
Do not implement new features. You may run commands to check.
{_NO_SKILLS}

Two contracts only:
1. Original user request — did the work satisfy this, or only a proxy?
2. verify_points — actually run min-fail checks. A check that must fail if the work is wrong.

This cycle must have written or replaced answer.md (workspace root or `.flame/answer.md`).
Judge that file's content against the original request. The harness already checks it was written this round; do not use file mtimes as verify_points, and do not fail because live answer.md is older than live plan.json.
Do not fail on plan.json key order or the goal field — the harness writes those.

Every positive claim needs an objective evidence handle: a workspace path, a URL, or a `command` that was actually run. Prefer handles from the tool trace below when present. If you cannot cite a real handle, it is unsupported — do not invent files or links.
In each checks[] line, cite handles explicitly: `path: re.json …`, `url: …`, or a backtick command. Use workspace-relative paths only (not host absolute paths or container-only prefixes like app/ unless that directory exists).
The harness audits that cited handles exist and were touched this cycle; it does not re-run your work.
retry=true means continue; retry=false means stop burning budget.
The harness finishes verify with an evidence audit of checks (exist + touched this cycle). That audit is part of verify — not a side gate.
Set retry=false only when points/alignment already fail and more cycles cannot help.
If evidence is the weak leg, leave retry=true so the next plan can fix handles.
{status}{trace_block}
Original user request:
{original_task}

Verify points:
{points}

Write .flame/verify.json with this schema and no extra keys:
{{
  "points_met": true,
  "aligned": true,
  "evidence_ok": true,
  "retry": true,
  "summary": "1–2 plain sentences for the user: pass/fail verdict in everyday language (no JSON, ≤120 chars)",
  "checks": ["path: done.txt exists=true; or `python3 -m pytest` ran clean"],
  "drift": ["how this diverged from the original request, or empty"],
  "evidence_gaps": ["unsupported claims, or empty"],
  "diagnosis": "empty if done; otherwise what the next plan must change"
}}
passed is implied: points_met AND aligned AND evidence_ok.
summary is user-facing; diagnosis is for the next plan (may be more technical).
If verify_points is empty, judge only the original request.
If you cannot judge at all, write nothing — the harness will deliver the act output.
Set retry=false only when points/alignment already fail and more cycles cannot help
(contradictory ask, missing user-only info, same failure would recur). Evidence gaps alone → leave retry=true.
If act timed out, judge leftover workspace artifacts; do not treat silence as infeasibility.
"""


def correction_for_plan(verify: VerifyResult) -> str:
    drift = "; ".join(verify.drift) or "(none)"
    gaps = "; ".join(verify.evidence_gaps) or "(none)"
    checks = "; ".join(verify.checks[:6]) or "(none)"
    return (
        f"points_met={verify.points_met}; checks: {checks}\n"
        f"aligned={verify.aligned}; drift: {drift}\n"
        f"evidence_ok={verify.evidence_ok}; gaps: {gaps}\n"
        f"diagnosis: {verify.diagnosis or '(none)'}"
    )
