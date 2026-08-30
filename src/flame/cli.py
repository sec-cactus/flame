from __future__ import annotations

import argparse
import os
import sys
from importlib.metadata import PackageNotFoundError, version

from flame.loop import FlameError, continue_run, run
from flame.safety import SafetyDenied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flame", description="Flame agent harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a task through plan-act-verify")
    run_p.add_argument("task", nargs="+", help="task prompt")
    run_p.add_argument("--effort", choices=["fast", "standard", "ledger", "meld", "graph"], default=None)
    run_p.add_argument("--workspace", default=None)
    run_p.add_argument("--model", default=None, help="agent model (cursor: auto; opencode: provider/model)")
    run_p.add_argument(
        "--agent-backend",
        choices=["cursor", "opencode"],
        default=None,
        dest="agent_backend",
        help="agent CLI backend (default: cursor, or FLAME_AGENT_BACKEND)",
    )
    run_p.add_argument("--agent-bin", default=None, dest="agent_bin")
    run_p.add_argument("--no-force", action="store_true", help="do not pass --force to agent")
    run_p.add_argument(
        "--safety",
        action="store_true",
        help="enable Flame keyword safety gate (off by default; agent LLM decides refuse/degrade)",
    )

    cont_p = sub.add_parser(
        "continue",
        help="graph: hint + resume fact-graph from .flame/graph_run.json, then verify",
    )
    cont_p.add_argument("task", nargs="+", help="follow-up instruction (written as graph hint)")
    cont_p.add_argument("--workspace", default=None)
    cont_p.add_argument("--model", default=None)
    cont_p.add_argument(
        "--agent-backend",
        choices=["cursor", "opencode"],
        default=None,
        dest="agent_backend",
    )
    cont_p.add_argument("--agent-bin", default=None, dest="agent_bin")
    cont_p.add_argument("--no-force", action="store_true")
    cont_p.add_argument(
        "--extra-budget",
        type=float,
        default=None,
        help="seconds added to fact-graph wallclock budget (default: 900)",
    )

    sub.add_parser("skills", help="print resolved j-space / fact-graph paths")
    sub.add_parser("version", help="print version")

    args = parser.parse_args(argv)
    if args.cmd == "version":
        print(_version())
        return 0
    if args.cmd == "skills":
        return _print_skills()

    task = " ".join(args.task).strip()
    if not task:
        parser.error("empty task")
    runner = continue_run if args.cmd == "continue" else run
    run_kwargs: dict = {
        "task": task,
        "workspace": args.workspace,
        "model": args.model,
        "agent_backend": args.agent_backend,
        "agent_bin": args.agent_bin,
        "force": False if args.no_force else None,
    }
    if args.cmd == "run":
        run_kwargs["effort"] = args.effort
        run_kwargs["safety_gate"] = True if args.safety else None
    else:
        run_kwargs["extra_budget"] = args.extra_budget
    try:
        result = runner(**run_kwargs)
    except SafetyDenied as err:
        print(f"flame: {err.reason}", file=sys.stderr)
        return 2
    except (FlameError, FileNotFoundError, ValueError) as err:
        print(f"flame: {err}", file=sys.stderr)
        return 1

    if result.output:
        print(result.output)
    return 0 if result.passed else 3


def _print_skills() -> int:
    from flame import skills

    jspace = skills.jspace_dir()
    factgraph = skills.factgraph_dir()
    print(f"j-space     {jspace or 'MISSING'}")
    print(f"fact-graph  {factgraph or 'MISSING'}")
    if jspace is None:
        override = os.environ.get("FLAME_JSPACE", "").strip()
        print("j-space is not bundled. Install it, then re-run `flame skills`.", file=sys.stderr)
        print("See docs/SKILLS.md", file=sys.stderr)
        if override:
            print(f"FLAME_JSPACE={override} (no SKILL.md there)", file=sys.stderr)
        else:
            print("looked at:", file=sys.stderr)
            for path in skills.default_jspace_candidates():
                print(f"  {path}", file=sys.stderr)
            print("or set FLAME_JSPACE to the j-space directory", file=sys.stderr)
    return 0 if factgraph is not None else 1


def _version() -> str:
    try:
        return version("flame")
    except PackageNotFoundError:
        return "0.1.0"


if __name__ == "__main__":
    raise SystemExit(main())
