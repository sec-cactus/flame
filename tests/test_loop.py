from __future__ import annotations

import io
import json
import os
import shutil
import stat
import time
import unittest
from pathlib import Path

from flame import budget
from flame.config import Config
from flame.loop import (
    FlameError,
    _plan_from,
    _restore_plan_if_mutated,
    _restore_verify_if_mutated,
    _verify_from_payload,
    _write_plan,
    run,
)
from flame.progress import Progress
from flame.safety import SafetyDenied
from flame.types import Effort, Plan


def _fake_agent() -> Path:
    path = Path(__file__).resolve().parent / "fake_agent.py"
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("FLAME_FAKE_FAIL_ONCE", None)
        os.environ.pop("FLAME_FAKE_ABORT", None)
        os.environ.pop("FLAME_FAKE_DRIFT", None)
        os.environ.pop("FLAME_FAKE_PREPROCESS_FAIL", None)
        os.environ.pop("FLAME_FAKE_PLAN_FAIL", None)
        os.environ.pop("FLAME_FAKE_VERIFY_FAIL", None)
        os.environ.pop("FLAME_FAKE_ACT_FAIL", None)
        os.environ.pop("FLAME_FAKE_ACT_TIMEOUT", None)
        os.environ.pop("FLAME_FAKE_BAD_EVIDENCE", None)
        os.environ.pop("FLAME_FAKE_USE_JSPACE", None)
        os.environ.pop("FLAME_FAKE_OMIT_USE_JSPACE", None)
        os.environ.pop("FLAME_FAKE_MUTATE_PLAN", None)
        os.environ.pop("FLAME_FAKE_MUTATE_VERIFY", None)
        os.environ.pop("FLAME_FAKE_ANSWER_ONCE", None)
        self.root = Path(__file__).resolve().parent / ".tmp"
        self.root.mkdir(exist_ok=True)

    def _workspace(self, name: str) -> Path:
        workspace = self.root / name
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        return workspace

    def _cfg(self, workspace: Path, effort: Effort = Effort.fast) -> Config:
        return Config(
            agent_backend="cursor",
            agent_bin=str(_fake_agent()),
            model="auto",
            workspace=workspace,
            effort=effort,
            log_dir=workspace / ".flame" / "logs",
            timeout_sec=15,
            force=True,
            trust=True,
            extra_args=[],
            safety_gate=False,
        )

    def test_plan_act_verify_pass(self) -> None:
        workspace = self._workspace("pass")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertTrue((workspace / "done.txt").is_file())
        self.assertTrue((workspace / "answer.md").is_file())
        self.assertTrue((workspace / ".flame" / "plan.json").is_file())
        log = buf.getvalue()
        self.assertIn("▶ plan", log)
        self.assertIn("▶ act", log)
        self.assertIn("▶ verify", log)
        self.assertNotIn("▶ align", log)
        self.assertNotIn("▶ refuter", log)
        self.assertNotIn("▶ preprocess", log)
        self.assertIn("▶ plan  cycle 1", log)
        self.assertNotIn("上限", log)
        self.assertFalse((workspace / ".flame" / "brief.json").is_file())
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertIsNone(skill["skill"])

    def test_replan_after_failed_verify(self) -> None:
        os.environ["FLAME_FAKE_FAIL_ONCE"] = "1"
        workspace = self._workspace("replan")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.standard),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertGreaterEqual(result.cycles, 2)
        self.assertIn("▶ preprocess", buf.getvalue())
        self.assertIn("  · quadrants", buf.getvalue())
        self.assertIn("  · factors", buf.getvalue())
        self.assertNotIn("meld", buf.getvalue())
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["schema"], "flame.brief.v1")
        self.assertTrue(brief["quadrants"]["known_unknowns"])
        self.assertTrue(brief["decisive_move"])
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertIsNone(skill["skill"])

    def test_retry_keeps_first_answer_mtime(self) -> None:
        os.environ["FLAME_FAKE_FAIL_ONCE"] = "1"
        os.environ["FLAME_FAKE_ANSWER_ONCE"] = "1"
        workspace = self._workspace("answer_once")
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.standard),
            progress=Progress(enabled=False),
        )
        self.assertTrue(result.passed, result.output)
        self.assertGreaterEqual(result.cycles, 2)
        self.assertTrue((workspace / "answer.md").is_file())

    def test_fast_does_not_replan(self) -> None:
        os.environ["FLAME_FAKE_FAIL_ONCE"] = "1"
        workspace = self._workspace("fast_once")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.fast),
            progress=Progress(stream=buf),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.cycles, 1)
        self.assertTrue((workspace / "done.txt").is_file())
        self.assertIn("wrote done.txt", result.output)
        self.assertIn("fast: one verify round", buf.getvalue())
        self.assertNotIn("▶ preprocess", buf.getvalue())

    def test_stale_stage_files_are_archived(self) -> None:
        """A new run must not inherit the previous task's pipeline state."""
        workspace = self._workspace("stale_markers")
        flame = workspace / ".flame"
        flame.mkdir(parents=True)
        stale_verify = {"passed": True, "points_met": True, "summary": "old task passed"}
        (flame / "verify.json").write_text(json.dumps(stale_verify), encoding="utf-8")
        (flame / "meld-judge.json").write_text('{"consensus": []}', encoding="utf-8")
        (flame / "graph-result.md").write_text("previous task text", encoding="utf-8")
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(stream=io.StringIO()),
        )
        self.assertTrue(result.passed, result.output)
        flame = workspace / ".flame"
        self.assertFalse((flame / "meld-judge.json").is_file())
        self.assertFalse((flame / "graph-result.md").is_file())
        new_verify = json.loads((flame / "verify.json").read_text(encoding="utf-8"))
        self.assertNotEqual(new_verify.get("summary"), "old task passed")
        priors = sorted((flame / "prior").glob("*"))
        self.assertTrue(priors)
        archived = {p.name for p in priors[0].iterdir()}
        self.assertIn("verify.json", archived)
        self.assertIn("meld-judge.json", archived)
        self.assertIn("graph-result.md", archived)

    def test_safety_cap_stops_retry(self) -> None:
        os.environ["FLAME_FAKE_FAIL_ONCE"] = "1"
        workspace = self._workspace("cap")
        cfg = self._cfg(workspace, Effort.standard)
        cfg.max_cycles = 1
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=cfg,
            progress=Progress(stream=buf),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.cycles, 1)
        self.assertIn("safety cap", buf.getvalue())

    def test_ledger_default_uses_jspace(self) -> None:
        workspace = self._workspace("jspace")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.ledger),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertEqual(skill["skill"], "j-space")
        self.assertTrue(skill["use_jspace"])
        self.assertIn("skill=j-space", buf.getvalue())
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["use_jspace"])

    def test_ledger_can_skip_jspace(self) -> None:
        os.environ["FLAME_FAKE_USE_JSPACE"] = "0"
        workspace = self._workspace("no_ledger")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.ledger),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertIsNone(skill["skill"])
        self.assertFalse(skill["use_jspace"])
        self.assertIn("use_jspace=false", buf.getvalue())

    def test_graph_uses_factgraph(self) -> None:
        workspace = self._workspace("factgraph")
        buf = io.StringIO()
        task = "create done.txt"
        result = run(
            task,
            config=self._cfg(workspace, Effort.graph),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertEqual(skill["skill"], "fact-graph")
        self.assertIsNone(skill["use_jspace"])
        self.assertIn("skill=fact-graph", buf.getvalue())
        self.assertTrue(skill["factgraph"])
        self.assertEqual(skill["graph_seed"], "graph_seed.json")
        self.assertEqual(skill["graph_run"], ".fact-graph/runs/flame-act-c1")
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["goal"], task)
        seed = json.loads((workspace / ".flame" / "graph_seed.json").read_text(encoding="utf-8"))
        self.assertIn(task, seed["goal"])
        self.assertIn("Verify points", seed["goal"])
        self.assertIn("Brief", seed["origin"])
        self.assertTrue(seed["hint"])
        board = json.loads(
            (workspace / ".fact-graph" / "runs" / "flame-act-c1" / "board.json").read_text(
                encoding="utf-8"
            )
        )
        goal = next(f for f in board["facts"] if f["id"] == "goal")["description"]
        self.assertIn(task, goal)
        self.assertEqual(board["constraints"], seed["constraints"])
        self.assertIn("goal: original (harness-forced)", buf.getvalue())
        self.assertIn("fact-graph inited:", buf.getvalue())

    def test_plan_goal_forced_to_original(self) -> None:
        workspace = self._workspace("goal_force")
        task = "create done.txt"
        result = run(task, config=self._cfg(workspace), progress=Progress(stream=io.StringIO()))
        self.assertTrue(result.passed, result.output)
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["goal"], task)
        self.assertNotEqual(plan["goal"], "complete the flame task")
        self.assertFalse((workspace / ".flame" / "graph_seed.json").is_file())

    def test_empty_goal_keeps_approach_and_points(self) -> None:
        plan = _plan_from(
            {
                "goal": "",
                "approach": "write the file",
                "constraints": ["must touch disk"],
                "verify_points": ["done.txt exists"],
            },
            ask_use_jspace=False,
        )
        self.assertEqual(plan.approach, "write the file")
        self.assertEqual(plan.constraints, ["must touch disk"])
        self.assertEqual(plan.verify_points, ["done.txt exists"])
        self.assertFalse(plan.degraded)

    def test_omitted_use_jspace_stays_none_until_loop(self) -> None:
        plan = _plan_from(
            {
                "goal": "g",
                "approach": "write the file",
                "constraints": [],
                "verify_points": [],
            },
            ask_use_jspace=True,
        )
        self.assertIsNone(plan.use_jspace)
        self.assertFalse(budget.use_jspace(Effort.ledger, plan.use_jspace))
        self.assertTrue(budget.use_jspace(Effort.ledger, True))
        self.assertFalse(budget.use_jspace(Effort.graph, True))

    def test_ledger_omitted_use_jspace_defaults_true(self) -> None:
        os.environ["FLAME_FAKE_OMIT_USE_JSPACE"] = "1"
        workspace = self._workspace("omit_jspace")
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.ledger),
            progress=Progress(stream=io.StringIO()),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertEqual(skill["skill"], "j-space")
        self.assertTrue(skill["use_jspace"])
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["use_jspace"])

    def test_ledger_brief_without_meld(self) -> None:
        workspace = self._workspace("bsp")
        buf = io.StringIO()
        result = run(
            "make the done file",
            config=self._cfg(workspace, Effort.ledger),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        log = buf.getvalue()
        self.assertIn("▶ preprocess", log)
        self.assertIn("  · quadrants", log)
        self.assertIn("  · factors", log)
        self.assertNotIn("meld", log)
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["decisive_move"], "whether the workspace accepts writes")
        self.assertLessEqual(len(brief["success_factors"]), 3)
        self.assertFalse((workspace / ".flame" / "task.md").is_file())
        self.assertFalse((workspace / ".flame" / "meld-judge.json").is_file())
        self.assertFalse((workspace / ".flame" / "brief.md").is_file())

    def test_meld_act_fusion_writes_answer(self) -> None:
        workspace = self._workspace("act_meld")
        buf = io.StringIO()
        result = run(
            "make the done file",
            config=self._cfg(workspace, Effort.meld),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        log = buf.getvalue()
        self.assertIn("▶ preprocess", log)
        self.assertIn("  · quadrants", log)
        self.assertIn("  · factors", log)
        self.assertIn("meld panels", log)
        self.assertIn("meld judge", log)
        self.assertIn("meld finalizer", log)
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        judge = json.loads((workspace / ".flame" / "meld-judge.json").read_text(encoding="utf-8"))
        self.assertEqual(judge["winner"], "primary_analyst")
        self.assertIn("finalizer_guidance", judge)
        self.assertTrue((workspace / "answer.md").is_file())
        self.assertTrue((workspace / "done.txt").is_file())
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertIsNone(skill.get("skill"))

    def test_graph_brief_without_meld(self) -> None:
        workspace = self._workspace("graph_brief")
        buf = io.StringIO()
        result = run(
            "make the done file",
            config=self._cfg(workspace, Effort.graph),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        log = buf.getvalue()
        self.assertIn("▶ preprocess", log)
        self.assertIn("  · quadrants", log)
        self.assertIn("  · factors", log)
        self.assertNotIn("meld panels", log)
        self.assertFalse((workspace / ".flame" / "meld-judge.json").is_file())
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        self.assertIn("unknown_unknowns", brief["quadrants"])
        self.assertTrue(brief["decisive_move"])

    def test_preprocess_fail_degrades_to_original(self) -> None:
        os.environ["FLAME_FAKE_PREPROCESS_FAIL"] = "1"
        workspace = self._workspace("pre_fail")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.graph),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertFalse((workspace / ".flame" / "brief.json").is_file())
        self.assertFalse((workspace / ".flame" / "meld-judge.json").is_file())
        self.assertIn("using original", buf.getvalue())
        self.assertTrue((workspace / ".flame" / "original.md").is_file())
        self.assertTrue((workspace / ".flame" / "plan.json").is_file())

    def test_graph_does_not_spawn_refuter(self) -> None:
        workspace = self._workspace("no_refuter")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.graph),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertNotIn("▶ refuter", buf.getvalue())
        self.assertEqual(result.cycles, 1)

    def test_max_is_not_an_effort(self) -> None:
        workspace = self._workspace("no_max")
        with self.assertRaises(ValueError):
            Config.load(workspace=workspace, effort="max")

    def test_high_is_not_an_effort(self) -> None:
        workspace = self._workspace("no_high")
        with self.assertRaises(ValueError):
            Config.load(workspace=workspace, effort="high")

    def test_verify_abort_does_not_replan(self) -> None:
        os.environ["FLAME_FAKE_ABORT"] = "1"
        workspace = self._workspace("abort")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(stream=buf),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.cycles, 1)
        self.assertFalse(result.verify.retry if result.verify else True)
        self.assertIn("verify will not retry", buf.getvalue())

    def test_harness_does_not_override_hallucinated_handle(self) -> None:
        os.environ["FLAME_FAKE_BAD_EVIDENCE"] = "1"
        workspace = self._workspace("bad_evidence")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.standard),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertEqual(result.cycles, 1)
        self.assertIn("evidence_ok=True", buf.getvalue())
        self.assertTrue((workspace / ".flame" / "tool_trace.json").is_file())

    def test_hallucinated_handle_does_not_flip_evidence_ok(self) -> None:
        workspace = self._workspace("audit_retry")
        (workspace / "answer.md").write_text("fresh\n", encoding="utf-8")
        (workspace / "run.py").write_text("ok\n", encoding="utf-8")
        verify = _verify_from_payload(
            {
                "points_met": True,
                "aligned": True,
                "evidence_ok": True,
                "retry": False,
                "checks": [
                    "path: run.py exists (cancel/await cleanup)",
                    "path: no_such_file_zz.txt claimed",
                ],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "",
            },
            workspace=workspace,
            plan_mtime=time.time() - 5,
        )
        self.assertTrue(verify.passed)
        self.assertTrue(verify.evidence_ok)
        self.assertFalse(any("no_such_file" in g for g in verify.evidence_gaps))

    def test_stale_answer_md_fails_verify(self) -> None:
        workspace = self._workspace("stale_answer")
        answer = workspace / "answer.md"
        answer.write_text("round-1 leftover\n", encoding="utf-8")
        old = time.time() - 120
        os.utime(answer, (old, old))
        flame = workspace / ".flame"
        flame.mkdir()
        (flame / "plan.json").write_text("{}\n", encoding="utf-8")
        plan_mtime = time.time()
        os.utime(flame / "plan.json", (plan_mtime, plan_mtime))
        os.utime(answer, (old, old))
        (workspace / "run.py").write_text("ok\n", encoding="utf-8")
        verify = _verify_from_payload(
            {
                "points_met": True,
                "aligned": True,
                "evidence_ok": True,
                "retry": False,
                "checks": ["path: run.py exists"],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "",
            },
            workspace=workspace,
            plan_mtime=plan_mtime,
        )
        self.assertFalse(verify.passed)
        self.assertTrue(verify.evidence_ok)
        self.assertTrue(verify.retry)
        self.assertTrue(any("answer.md" in g for g in verify.evidence_gaps))

    def test_plan_extra_keys_do_not_fail_verify(self) -> None:
        workspace = self._workspace("plan_extra")
        (workspace / "answer.md").write_text("fresh\n", encoding="utf-8")
        verify = _verify_from_payload(
            {
                "points_met": True,
                "aligned": True,
                "evidence_ok": True,
                "retry": False,
                "checks": ["path: answer.md exists"],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "",
            },
            workspace=workspace,
            plan_mtime=time.time() - 5,
            schema_gaps=["plan.json extra keys after act: answer"],
        )
        self.assertTrue(verify.passed)
        self.assertTrue(verify.evidence_ok)
        self.assertTrue(any("extra keys" in g for g in verify.evidence_gaps))

    def test_restore_plan_skips_write_when_canonical(self) -> None:
        workspace = self._workspace("restore_plan_skip")
        flame = workspace / ".flame"
        flame.mkdir()
        plan = Plan(
            goal="g",
            approach="a",
            constraints=["c"],
            verify_points=["v"],
            summary="s",
        )
        path = flame / "plan.json"
        _write_plan(path, plan)
        mtime = path.stat().st_mtime
        gaps = _restore_plan_if_mutated(flame, plan)
        self.assertEqual(gaps, [])
        self.assertEqual(path.stat().st_mtime, mtime)

    def test_restore_plan_rewrites_extra_keys(self) -> None:
        workspace = self._workspace("restore_plan_extra")
        flame = workspace / ".flame"
        flame.mkdir()
        plan = Plan(
            goal="g",
            approach="a",
            constraints=[],
            verify_points=["v"],
            summary="s",
        )
        path = flame / "plan.json"
        _write_plan(path, plan)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["answer"] = "stashed"
        path.write_text(json.dumps(payload), encoding="utf-8")
        gaps = _restore_plan_if_mutated(flame, plan)
        self.assertTrue(any("extra keys" in g and "answer" in g for g in gaps))
        restored = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("answer", restored)
        self.assertEqual(restored["goal"], "g")

    def test_restore_plan_rewrites_allowed_field_change(self) -> None:
        workspace = self._workspace("restore_plan_fields")
        flame = workspace / ".flame"
        flame.mkdir()
        plan = Plan(
            goal="original request",
            approach="a",
            constraints=[],
            verify_points=["v"],
            summary="s",
        )
        path = flame / "plan.json"
        _write_plan(path, plan)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["goal"] = "proxy success"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        gaps = _restore_plan_if_mutated(flame, plan)
        self.assertTrue(any("rewritten during act" in g for g in gaps))
        restored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(restored["goal"], "original request")

    def test_restore_verify_leaves_untouched_file(self) -> None:
        workspace = self._workspace("restore_verify_ok")
        flame = workspace / ".flame"
        flame.mkdir()
        path = flame / "verify.json"
        path.write_bytes(b'{"points_met": true}\n')
        before = path.read_bytes()
        mtime = path.stat().st_mtime
        gaps = _restore_verify_if_mutated(flame, before)
        self.assertEqual(gaps, [])
        self.assertEqual(path.stat().st_mtime, mtime)

    def test_restore_verify_removes_act_created_file(self) -> None:
        workspace = self._workspace("restore_verify_created")
        flame = workspace / ".flame"
        flame.mkdir()
        path = flame / "verify.json"
        path.write_text('{"hacked": true}\n', encoding="utf-8")
        gaps = _restore_verify_if_mutated(flame, None)
        self.assertEqual(gaps, ["verify.json was written during act"])
        self.assertFalse(path.exists())

    def test_restore_verify_restores_prior_bytes(self) -> None:
        workspace = self._workspace("restore_verify_prior")
        flame = workspace / ".flame"
        flame.mkdir()
        path = flame / "verify.json"
        before = b'{"points_met": false}\n'
        path.write_bytes(before)
        path.write_bytes(b'{"hacked": true}\n')
        gaps = _restore_verify_if_mutated(flame, before)
        self.assertEqual(gaps, ["verify.json was rewritten during act"])
        self.assertEqual(path.read_bytes(), before)

    def test_act_mutating_plan_restores_without_failing_verify(self) -> None:
        os.environ["FLAME_FAKE_MUTATE_PLAN"] = "1"
        workspace = self._workspace("act_mutate_plan")
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(enabled=False),
        )
        self.assertTrue(result.passed, result.output)
        self.assertTrue(result.verify.evidence_ok if result.verify else False)
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertNotIn("answer", plan)
        self.assertTrue(
            any("extra keys" in g for g in (result.verify.evidence_gaps if result.verify else []))
        )

    def test_points_met_without_original_achieved_does_not_pass(self) -> None:
        workspace = self._workspace("aligned_incomplete")
        (workspace / "answer.md").write_text("still running, 59/182\n", encoding="utf-8")
        verify = _verify_from_payload(
            {
                "points_met": True,
                "aligned": False,
                "evidence_ok": True,
                "retry": True,
                "checks": ["path: answer.md progress note"],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "original analysis not delivered yet",
            },
            workspace=workspace,
            plan_mtime=time.time() - 5,
        )
        self.assertFalse(verify.passed)
        self.assertTrue(verify.retry)
        self.assertFalse(verify.aligned)
        self.assertTrue(verify.points_met)

    def test_model_evidence_false_keeps_retry_false(self) -> None:
        workspace = self._workspace("model_evidence_stop")
        (workspace / "answer.md").write_text("fresh\n", encoding="utf-8")
        verify = _verify_from_payload(
            {
                "points_met": True,
                "aligned": True,
                "evidence_ok": False,
                "retry": False,
                "checks": ["path: answer.md exists"],
                "drift": [],
                "evidence_gaps": ["could not confirm sources"],
                "diagnosis": "evidence too thin",
            },
            workspace=workspace,
            plan_mtime=time.time() - 5,
        )
        self.assertFalse(verify.passed)
        self.assertFalse(verify.evidence_ok)
        self.assertFalse(verify.retry)

    def test_content_failure_keeps_model_retry_false(self) -> None:
        verify = _verify_from_payload(
            {
                "points_met": False,
                "aligned": True,
                "evidence_ok": True,
                "retry": False,
                "checks": [],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "not implementable",
            },
            workspace=self._workspace("content_retry"),
        )
        self.assertFalse(verify.passed)
        self.assertFalse(verify.retry)

    def test_verify_drift_steers_replan(self) -> None:
        os.environ["FLAME_FAKE_DRIFT"] = "1"
        workspace = self._workspace("drift")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.standard),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertGreaterEqual(result.cycles, 2)
        log = buf.getvalue()
        self.assertIn("aligned=False", log)
        self.assertTrue((workspace / ".flame" / "original.md").is_file())

    def test_plan_fail_degrades_to_act(self) -> None:
        os.environ["FLAME_FAKE_PLAN_FAIL"] = "1"
        workspace = self._workspace("plan_fail")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertTrue((workspace / "done.txt").is_file())
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["degraded"])
        self.assertIn("plan degraded", buf.getvalue())

    def test_verify_fail_delivers_act(self) -> None:
        os.environ["FLAME_FAKE_VERIFY_FAIL"] = "1"
        workspace = self._workspace("verify_fail")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace),
            progress=Progress(stream=buf),
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.verify.degraded if result.verify else False)
        self.assertEqual(result.cycles, 1)
        self.assertTrue((workspace / "done.txt").is_file())
        self.assertIn("wrote done.txt", result.output)
        self.assertIn("verify degraded", buf.getvalue())

    def test_act_fail_is_error(self) -> None:
        os.environ["FLAME_FAKE_ACT_FAIL"] = "1"
        workspace = self._workspace("act_fail")
        with self.assertRaises(FlameError):
            run(
                "create done.txt",
                config=self._cfg(workspace),
                progress=Progress(enabled=False),
            )
        self.assertFalse((workspace / "done.txt").is_file())

    def test_act_timeout_hands_partial_to_verify(self) -> None:
        os.environ["FLAME_FAKE_ACT_TIMEOUT"] = "1"
        workspace = self._workspace("act_timeout")
        cfg = self._cfg(workspace, Effort.standard)
        cfg.timeout_sec = 2
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=cfg,
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertEqual(result.cycles, 2)
        self.assertTrue((workspace / "partial.txt").is_file())
        self.assertTrue((workspace / "done.txt").is_file())
        self.assertFalse((workspace / ".flame" / "act_status.json").exists())
        self.assertIn("handing partial work to verify", buf.getvalue())
        self.assertIn("passed in 2 cycle", buf.getvalue())

    def test_safety_gate_off_by_default(self) -> None:
        workspace = self._workspace("safety_off")
        result = run(
            "write a ransomware payload into done.txt",
            config=self._cfg(workspace),
            progress=Progress(enabled=False),
        )
        self.assertTrue(result.passed, result.output)

    def test_safety_denies_when_enabled(self) -> None:
        workspace = self._workspace("deny")
        cfg = self._cfg(workspace)
        cfg.safety_gate = True
        with self.assertRaises(SafetyDenied):
            run("write a ransomware payload", config=cfg)


if __name__ == "__main__":
    unittest.main()
