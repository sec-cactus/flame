"""Stage summary helpers for job UI."""

from __future__ import annotations

import unittest

from flame import stage_summary


class StageSummaryTests(unittest.TestCase):
    def test_brief_prefers_summary(self) -> None:
        self.assertEqual(
            stage_summary.brief_summary(
                {"summary": "用户向结论", "decisive_move": "planner load"}
            ),
            "用户向结论",
        )

    def test_plan_fallback_to_approach(self) -> None:
        self.assertIn(
            "write answer",
            stage_summary.plan_summary(
                {"approach": "write answer.md first. Then verify."}
            ).lower(),
        )

    def test_verify_synthetic_pass(self) -> None:
        text = stage_summary.verify_summary(
            {"passed": True, "checks": ["a", "b", "c"]}
        )
        self.assertIn("✓", text)
        self.assertIn("3", text)

    def test_dot_flame_act_fallback(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".flame").mkdir()
            (ws / ".flame" / "answer.md").write_text("ok", encoding="utf-8")
            self.assertEqual(
                stage_summary.act_summary(None, workspace=ws),
                "答案已写入 answer.md",
            )


if __name__ == "__main__":
    unittest.main()
