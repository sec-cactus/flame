from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flame import prompts, skills
from flame.cli import main
from flame.types import Plan


class SkillPathTests(unittest.TestCase):
    def test_factgraph_is_bundled(self) -> None:
        root = skills.factgraph_dir()
        self.assertIsNotNone(root)
        assert root is not None
        self.assertTrue((root / "SKILL.md").is_file())
        self.assertTrue((root / "scripts" / "orchestrator.py").is_file())

    def test_jspace_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake = Path(raw) / "j-space"
            fake.mkdir()
            (fake / "SKILL.md").write_text("# j-space\n", encoding="utf-8")
            with patch.dict(os.environ, {"FLAME_JSPACE": str(fake)}):
                self.assertEqual(skills.jspace_dir(), fake)

    def test_jspace_env_missing_does_not_fall_through(self) -> None:
        with patch.dict(os.environ, {"FLAME_JSPACE": "/no/such/j-space"}):
            self.assertIsNone(skills.jspace_dir())


class SkillsCliTests(unittest.TestCase):
    def test_skills_prints_factgraph(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["skills"])
        self.assertEqual(code, 0)
        self.assertIn("fact-graph", buf.getvalue())
        self.assertIn("j-space", buf.getvalue())


class PromptSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = Plan(
            goal="g",
            approach="a",
            constraints=["c"],
            verify_points=["v"],
        )

    def test_fast_plan_omits_use_jspace(self) -> None:
        text = prompts.plan_prompt("task", ask_use_jspace=False)
        self.assertNotIn("use_jspace", text)
        self.assertIn("Skill ban (this phase)", text)

    def test_ledger_plan_asks_use_jspace(self) -> None:
        text = prompts.plan_prompt("task", ask_use_jspace=True)
        self.assertIn('"use_jspace": true', text)
        self.assertIn("Set use_jspace=false only when", text)
        self.assertIn("multi-path", text)
        self.assertIn("Skill ban (this phase)", text)
        self.assertNotIn("majority-vote", text)
        self.assertNotIn('"search"', text)

    def test_non_act_phases_ban_skills(self) -> None:
        for text in (
            prompts.quadrants_prompt("t"),
            prompts.factors_prompt("t", "{}"),
            prompts.plan_prompt("t"),
            prompts.verify_prompt("t", self.plan),
        ):
            self.assertIn("Skill ban (this phase)", text)
            self.assertIn("Do not read any SKILL.md", text)

    def test_quadrants_are_not_the_decisive_move(self) -> None:
        text = prompts.quadrants_prompt("task")
        self.assertIn("unknown_unknowns", text)
        self.assertIn("Do not pick a decisive move", text)
        self.assertNotIn("meld Judge", text)
        self.assertNotIn("Meld judge", text)

    def test_factors_pick_known_unknown(self) -> None:
        text = prompts.factors_prompt("task", '{"known_unknowns": ["x"]}')
        self.assertIn("decisive_move", text)
        self.assertIn("NOT an unknown unknown", text)
        self.assertNotIn("Meld judge", text)

    def test_plan_priority_original_verify_brief(self) -> None:
        brief = (
            '{"schema":"flame.brief.v1",'
            '"quadrants":{"known_knowns":[],"known_unknowns":["x"],'
            '"unknown_knowns":[],"unknown_unknowns":[]},'
            '"success_factors":["s"],"failure_factors":["f"],'
            '"decisive_move":"fix the boundary"}'
        )
        text = prompts.plan_prompt(
            "user original",
            brief=brief,
            diagnosis="points_met=False; diagnosis: keep the original request",
            ask_use_jspace=False,
        )
        self.assertLess(text.index("[1 original"), text.index("[2 verify"))
        self.assertLess(text.index("[2 verify"), text.index("[3 brief"))
        self.assertIn("original — defines success", text)
        self.assertIn("Do not re-anchor approach", text)
        self.assertIn("user original", text)
        self.assertIn("decisive_move: fix the boundary", text)
        self.assertIn("success_factors:", text)
        self.assertNotIn('"schema": "flame.brief.v1"', text)

    def test_act_uses_goal_approach_constraints(self) -> None:
        text = prompts.act_prompt("orig", self.plan)
        self.assertIn("Original user request (= plan.goal, harness-forced):", text)
        self.assertIn("orig", text)
        self.assertIn("Approach:", text)
        self.assertIn("Constraints:", text)
        self.assertNotIn("Unknown (attack first)", text)
        self.assertNotIn("Milestones:", text)
        self.assertIn("Skill ban (this act)", text)
        self.assertIn("Do not create or modify `.flame/plan.json` or `.flame/verify.json`", text)
        self.assertNotIn("Do not add keys to `.flame/plan.json`", text)
        self.assertIn("verbatim copy of the original", prompts.plan_prompt("orig"))
        self.assertIn("Do not put plan.json", prompts.plan_prompt("orig"))

    def test_verify_uses_original_and_points(self) -> None:
        text = prompts.verify_prompt("orig", self.plan)
        self.assertIn("orig", text)
        self.assertIn("v", text)
        self.assertNotIn("Working task:", text)
        self.assertIn("Skill ban (this phase)", text)
        self.assertIn("do not use file mtimes as verify_points", text)
        self.assertNotIn("older than this cycle's `.flame/plan.json`", text)

    def test_verify_act_timeout_note(self) -> None:
        text = prompts.verify_prompt(
            "orig",
            self.plan,
            act_note="Act timed out after 30s (Flame watchdog).",
        )
        self.assertIn("Act status", text)
        self.assertIn("timed out after 30s", text)
        self.assertIn("do not treat silence as infeasibility", text)

    def test_act_jspace_mentions_effort_ledger(self) -> None:
        text = prompts.act_prompt("task", self.plan, skill="j-space", jspace_dir="/tmp/j-space")
        self.assertIn("[Flame act skill: j-space]", text)
        self.assertIn("Effort=ledger", text)
        self.assertIn("/tmp/j-space/SKILL.md", text)
        self.assertNotIn("[Flame act skill: fact-graph]", text)
        self.assertNotIn("Skill ban (this act)", text)
        self.assertNotIn("Skill ban (this phase)", text)

    def test_act_factgraph_mentions_effort_graph(self) -> None:
        text = prompts.act_prompt(
            "task",
            self.plan,
            skill="fact-graph",
            factgraph_dir="/tmp/fact-graph",
            graph_run_dir=".fact-graph/runs/flame-act-c1",
        )
        self.assertIn("[Flame act skill: fact-graph]", text)
        self.assertIn("Effort=graph", text)
        self.assertIn("FOREGROUND", text)
        self.assertIn("NOT Flame success", text)
        self.assertIn("run --run-dir .fact-graph/runs/flame-act-c1", text)
        self.assertNotIn(" init ", text)
        self.assertIn("Do **not** re-init", text)

    def test_act_meld_prompts(self) -> None:
        panel = prompts.act_meld_panel_prompt("t", self.plan, "primary_analyst", "desc")
        self.assertIn("[Flame phase: act]", panel)
        self.assertIn("[Flame meld role: primary_analyst]", panel)
        self.assertIn("Do not write answer.md", panel)
        self.assertIn("Skill ban (this act)", panel)
        judge = prompts.act_meld_judge_prompt("t", "### primary_analyst\nhi")
        self.assertIn('"winner"', judge)
        self.assertIn("finalizer_guidance", judge)
        self.assertNotIn("[Flame phase: meld]", judge)
        fin = prompts.act_meld_finalizer_prompt(
            "t",
            self.plan,
            role="primary_analyst",
            panel_answer="draft",
            judge_json='{"winner":"primary_analyst"}',
        )
        self.assertIn("[Flame meld role: finalizer]", fin)
        self.assertIn("[Flame meld selected: primary_analyst]", fin)
        self.assertIn("answer.md", fin)

    def test_graph_seed_roles(self) -> None:
        seed = prompts.build_graph_seed(
            "do the job",
            self.plan,
            brief="",
            diagnosis="",
        )
        self.assertEqual(seed["goal"].split("\n", 1)[0], "do the job")
        self.assertIn("- v", seed["goal"])
        self.assertEqual(seed["constraints"], "- c")
        self.assertEqual(seed["hint"], "a")
        self.assertIn("No preprocess", seed["origin"])


if __name__ == "__main__":
    unittest.main()
