#!/usr/bin/env python3
"""Stand-in for Cursor `agent` CLI. Used only by Flame tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--print", action="store_true")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--stream-partial-output", action="store_true")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--trust", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--mode", default=None)
    parser.add_argument("prompt", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    prompt_parts = list(args.prompt)
    if prompt_parts and prompt_parts[0] == "--":
        prompt_parts = prompt_parts[1:]
    prompt = " ".join(prompt_parts)
    workspace = Path(args.workspace)
    flame_dir = workspace / ".flame"
    flame_dir.mkdir(parents=True, exist_ok=True)

    phase = "unknown"
    for name in ("quadrants", "factors", "meld", "plan", "act", "verify"):
        if f"[Flame phase: {name}]" in prompt:
            phase = name
            break

    emit(
        {
            "type": "system",
            "subtype": "init",
            "model": args.model,
            "session_id": "fake-session",
            "cwd": str(workspace),
        }
    )
    emit(
        {
            "type": "tool_call",
            "subtype": "started",
            "tool_call": {"readToolCall": {"args": {"path": "README.md"}}},
        }
    )
    emit(
        {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "readToolCall": {
                    "args": {"path": "README.md"},
                    "result": {"success": {"totalLines": 1}},
                }
            },
        }
    )

    if os.environ.get("FLAME_FAKE_PREPROCESS_FAIL") == "1" and phase in {
        "meld",
        "quadrants",
        "factors",
    }:
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "duration_ms": 1,
                "result": "",
                "session_id": "fake-session",
            }
        )
        return 1

    if phase == "act" and os.environ.get("FLAME_FAKE_ACT_TIMEOUT") == "1":
        stamp = flame_dir / "act_timeout_once"
        if not stamp.exists():
            stamp.write_text("1\n", encoding="utf-8")
            (workspace / "partial.txt").write_text("half\n", encoding="utf-8")
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "partial progress before timeout"}],
                    },
                }
            )
            import time

            time.sleep(9999)

    if (
        (phase == "plan" and os.environ.get("FLAME_FAKE_PLAN_FAIL") == "1")
        or (phase == "verify" and os.environ.get("FLAME_FAKE_VERIFY_FAIL") == "1")
        or (phase == "act" and os.environ.get("FLAME_FAKE_ACT_FAIL") == "1")
    ):
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "duration_ms": 1,
                "result": f"{phase} failed",
                "session_id": "fake-session",
            }
        )
        return 1

    result = _handle(phase, prompt, workspace, flame_dir, force=args.force)
    emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": result[:200]}],
            },
        }
    )
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 5,
            "result": result,
            "session_id": "fake-session",
        }
    )
    return 0


def _handle(phase: str, prompt: str, workspace: Path, flame_dir: Path, *, force: bool) -> str:
    if phase == "quadrants":
        return json.dumps(
            {
                "known_knowns": ["create a done.txt signal"],
                "known_unknowns": ["whether the workspace accepts writes"],
                "unknown_knowns": ["utf-8 text files are enough"],
                "unknown_unknowns": ["whether a later verify phase will require evidence"],
            }
        )
    if phase == "factors":
        return json.dumps(
            {
                "success_factors": ["done.txt exists"],
                "failure_factors": ["act never writes"],
                "decisive_move": "whether the workspace accepts writes",
                "summary": "关键在 workspace 能否写入 done.txt",
            }
        )
    if phase == "meld":
        if "[Flame meld role: judge]" in prompt:
            return json.dumps(
                {
                    "consensus": [{"point": "deliverable is a file", "models": ["primary_analyst"]}],
                    "contradictions": [],
                    "unique_insights": [],
                    "blind_spots": [{"point": "format of done.txt", "importance": "medium"}],
                    "verification_needed": [],
                }
            )
        role = "panel"
        for name in ("primary_analyst", "critical_reviewer", "coverage_reviewer"):
            if f"[Flame meld role: {name}]" in prompt:
                role = name
                break
        return f"{role}: the task is to leave a verify signal in the workspace."
    if phase == "plan":
        return json.dumps(
            {
                "goal": "complete the flame task",
                "approach": "write done.txt first to prove the workspace accepts writes",
                "summary": "先写 done.txt 证明 workspace 可写",
                "constraints": ["do not delete .flame"],
                "verify_points": ["done.txt exists"],
                **(
                    {"use_ledger": os.environ.get("FLAME_FAKE_USE_LEDGER", "1") != "0"}
                    if "use_ledger" in prompt
                    else {}
                ),
            }
        )
    if phase == "act":
        if force:
            (workspace / "done.txt").write_text("ok\n", encoding="utf-8")
            (flame_dir / "act.json").write_text(
                json.dumps(
                    {
                        "summary": "已创建 done.txt",
                        "deliverables": ["done.txt"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            emit(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "tool_call": {
                        "writeToolCall": {
                            "args": {"path": "done.txt"},
                            "result": {"success": {"linesCreated": 1}},
                        }
                    },
                }
            )
        return "wrote done.txt"
    if phase == "verify":
        fail_once = os.environ.get("FLAME_FAKE_FAIL_ONCE") == "1"
        abort = os.environ.get("FLAME_FAKE_ABORT") == "1"
        drift = os.environ.get("FLAME_FAKE_DRIFT") == "1"
        bad_evidence = os.environ.get("FLAME_FAKE_BAD_EVIDENCE") == "1"
        stamp = flame_dir / "verify_attempt"
        points_met = (workspace / "done.txt").is_file()
        aligned = True
        evidence_ok = True
        diagnosis = ""
        retry = True
        drift_list: list[str] = []
        checks = [f"path: done.txt exists={ (workspace / 'done.txt').is_file() }"]
        if abort:
            points_met = False
            retry = False
            diagnosis = "task is not implementable"
        elif drift and not stamp.is_file():
            stamp.write_text("1\n", encoding="utf-8")
            aligned = False
            drift_list = ["plan optimized a proxy instead of the original request"]
            diagnosis = "satisfy the original user request; do not stop at a convenient verify point"
        elif fail_once and not stamp.is_file():
            stamp.write_text("1\n", encoding="utf-8")
            points_met = False
            diagnosis = "first-pass fake failure"
        elif bad_evidence and not stamp.is_file():
            stamp.write_text("1\n", encoding="utf-8")
            checks = ["path: no_such_evidence_file_12345.txt supports the claim"]
            retry = False  # model thinks success; harness audit must still force retry
        passed = points_met and aligned and evidence_ok
        payload = {
            "points_met": points_met,
            "aligned": aligned,
            "evidence_ok": evidence_ok,
            "retry": retry,
            "summary": "✓ 验收通过" if passed else diagnosis or "✗ 验收未通过",
            "checks": checks,
            "drift": drift_list,
            "evidence_gaps": [],
            "diagnosis": diagnosis,
            "passed": passed,
        }
        (flame_dir / "verify.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return json.dumps(payload)
    return json.dumps({"note": "unrecognized phase", "prompt": prompt[:80]})


if __name__ == "__main__":
    raise SystemExit(main())
