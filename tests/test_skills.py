from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flame import prompts, skills
from flame.cli import main
from flame.types import Plan, SearchKind


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
            search=SearchKind.depth,
        )

    def test_fast_plan_omits_search(self) -> None:
        text = prompts.plan_prompt("task", ask_search=False)
        self.assertNotIn('"search"', text)
        self.assertIn("Skill ban (this phase)", text)

    def test_high_plan_asks_search(self) -> None:
        text = prompts.plan_prompt("task", ask_search=True)
        self.assertIn('"search": "depth"', text)
        self.assertIn("fact-graph", text)
        self.assertIn("majority-vote", text)
        self.assertIn("fear-missing", text)
        self.assertIn("Do not pick \"depth\" merely because", text)
        self.assertIn("Skill ban (this phase)", text)
        self.assertIn("do not open those skills now", text)
        self.assertIn("against that approach only", text)
        self.assertIn("decide approach first, then vote search on the approach", text)

    def test_non_act_phases_ban_skills(self) -> None:
        for text in (
            prompts.meld_panel_prompt("t", "primary_analyst", "desc"),
            prompts.meld_judge_prompt("t", "panels"),
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

    def test_factors_pick_known_unknown(self) -> None:
        text = prompts.factors_prompt("task", '{"known_unknowns": ["x"]}')
        self.assertIn("decisive_move", text)
        self.assertIn("NOT an unknown unknown", text)

    def test_plan_priority_original_verify_brief(self) -> None:
        brief = (
            '{"schema":"flame.brief.v1","judge":null,'
            '"quadrants":{"known_knowns":[],"known_unknowns":["x"],'
            '"unknown_knowns":[],"unknown_unknowns":[]},'
            '"success_factors":["s"],"failure_factors":["f"],'
            '"decisive_move":"fix the boundary"}'
        )
        text = prompts.plan_prompt(
            "user original",
            brief=brief,
            diagnosis="points_met=False; diagnosis: keep the original request",
            ask_search=False,
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
        self.assertIn("Goal: g", text)
        self.assertIn("Approach:", text)
        self.assertIn("Constraints:", text)
        self.assertNotIn("Unknown (attack first)", text)
        self.assertNotIn("Milestones:", text)
        self.assertIn("Skill ban (this act)", text)

    def test_verify_uses_original_and_points(self) -> None:
        text = prompts.verify_prompt("orig", self.plan)
        self.assertIn("orig", text)
        self.assertIn("v", text)
        self.assertNotIn("Working task:", text)
        self.assertIn("Skill ban (this phase)", text)

    def test_verify_act_timeout_note(self) -> None:
        text = prompts.verify_prompt(
            "orig",
            self.plan,
            act_note="Act timed out after 30s (Flame watchdog).",
        )
        self.assertIn("Act status", text)
        self.assertIn("timed out after 30s", text)
        self.assertIn("do not treat silence as infeasibility", text)

    def test_act_depth_mentions_jspace(self) -> None:
        text = prompts.act_prompt("task", self.plan, skill="j-space", jspace_dir="/tmp/j-space")
        self.assertIn("[Flame act skill: j-space]", text)
        self.assertIn("/tmp/j-space/SKILL.md", text)
        self.assertNotIn("[Flame act skill: fact-graph]", text)
        self.assertNotIn("Skill ban (this act)", text)
        self.assertNotIn("Skill ban (this phase)", text)

    def test_act_breadth_mentions_factgraph(self) -> None:
        self.plan.search = SearchKind.breadth
        text = prompts.act_prompt(
            "task",
            self.plan,
            skill="fact-graph",
            factgraph_dir="/tmp/fact-graph",
        )
        self.assertIn("[Flame act skill: fact-graph]", text)
        self.assertIn("FOREGROUND", text)
        self.assertIn("NOT Flame success", text)


if __name__ == "__main__":
    unittest.main()
