from __future__ import annotations

import io
import json
import os
import shutil
import stat
import unittest
from pathlib import Path

from flame.config import Config
from flame.loop import FlameError, run
from flame.progress import Progress
from flame.safety import SafetyDenied
from flame.types import Effort


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
        os.environ.pop("FLAME_FAKE_SEARCH", None)
        os.environ.pop("FLAME_FAKE_PREPROCESS_FAIL", None)
        os.environ.pop("FLAME_FAKE_PLAN_FAIL", None)
        os.environ.pop("FLAME_FAKE_VERIFY_FAIL", None)
        os.environ.pop("FLAME_FAKE_ACT_FAIL", None)
        os.environ.pop("FLAME_FAKE_ACT_TIMEOUT", None)
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
        self.assertIsNone(brief["judge"])
        self.assertTrue(brief["quadrants"]["known_unknowns"])
        self.assertTrue(brief["decisive_move"])
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertIsNone(skill["skill"])

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

    def test_high_depth_uses_jspace(self) -> None:
        workspace = self._workspace("jspace")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.high),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertEqual(skill["skill"], "j-space")
        self.assertEqual(skill["search"], "depth")
        self.assertIn("skill=j-space", buf.getvalue())
        plan = json.loads((workspace / ".flame" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["search"], "depth")

    def test_high_breadth_uses_factgraph(self) -> None:
        os.environ["FLAME_FAKE_SEARCH"] = "breadth"
        workspace = self._workspace("factgraph")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.max),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        skill = json.loads((workspace / ".flame" / "act_skill.json").read_text(encoding="utf-8"))
        self.assertEqual(skill["skill"], "fact-graph")
        self.assertEqual(skill["search"], "breadth")
        self.assertIn("skill=fact-graph", buf.getvalue())
        self.assertTrue(skill["factgraph"])

    def test_high_brief_without_meld(self) -> None:
        workspace = self._workspace("bsp")
        buf = io.StringIO()
        result = run(
            "make the done file",
            config=self._cfg(workspace, Effort.high),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        log = buf.getvalue()
        self.assertIn("▶ preprocess", log)
        self.assertIn("  · quadrants", log)
        self.assertIn("  · factors", log)
        self.assertNotIn("meld", log)
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        self.assertIsNone(brief["judge"])
        self.assertEqual(brief["decisive_move"], "whether the workspace accepts writes")
        self.assertLessEqual(len(brief["success_factors"]), 3)
        self.assertFalse((workspace / ".flame" / "task.md").is_file())
        self.assertFalse((workspace / ".flame" / "meld-judge.json").is_file())
        self.assertFalse((workspace / ".flame" / "brief.md").is_file())

    def test_max_meld_then_structured_brief(self) -> None:
        workspace = self._workspace("meld")
        buf = io.StringIO()
        result = run(
            "make the done file",
            config=self._cfg(workspace, Effort.max),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        log = buf.getvalue()
        self.assertIn("▶ preprocess", log)
        self.assertIn("meld", log)
        self.assertIn("  · quadrants", log)
        self.assertIn("  · factors", log)
        self.assertTrue((workspace / ".flame" / "meld-judge.json").is_file())
        brief = json.loads((workspace / ".flame" / "brief.json").read_text(encoding="utf-8"))
        self.assertIsInstance(brief["judge"], dict)
        self.assertIn("consensus", brief["judge"])
        self.assertNotIn("decisive_move", brief["judge"])
        self.assertIn("unknown_unknowns", brief["quadrants"])
        self.assertTrue(brief["decisive_move"])

    def test_preprocess_fail_degrades_to_original(self) -> None:
        os.environ["FLAME_FAKE_PREPROCESS_FAIL"] = "1"
        workspace = self._workspace("pre_fail")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.max),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertFalse((workspace / ".flame" / "brief.json").is_file())
        self.assertFalse((workspace / ".flame" / "meld-judge.json").is_file())
        self.assertIn("using original", buf.getvalue())
        self.assertTrue((workspace / ".flame" / "original.md").is_file())
        self.assertTrue((workspace / ".flame" / "plan.json").is_file())

    def test_max_does_not_spawn_refuter(self) -> None:
        workspace = self._workspace("no_refuter")
        buf = io.StringIO()
        result = run(
            "create done.txt",
            config=self._cfg(workspace, Effort.max),
            progress=Progress(stream=buf),
        )
        self.assertTrue(result.passed, result.output)
        self.assertNotIn("▶ refuter", buf.getvalue())
        self.assertEqual(result.cycles, 1)

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

    def test_search_aliases_ignore_algorithm_names(self) -> None:
        from flame.loop import _search_from
        from flame.types import SearchKind

        self.assertEqual(_search_from("depth"), SearchKind.depth)
        self.assertEqual(_search_from("breadth"), SearchKind.breadth)
        self.assertEqual(_search_from("wide"), SearchKind.breadth)
        self.assertIsNone(_search_from("bfs"))
        self.assertIsNone(_search_from("graph"))
        self.assertEqual(_search_from("dfs"), SearchKind.depth)

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
