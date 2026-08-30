from __future__ import annotations

import os
import unittest
from pathlib import Path

from flame.schema import (
    answer_md_path,
    audit_answer_vs_plan,
    extra_keys,
    validate_plan_payload,
    validate_verify_payload,
    PLAN_KEYS,
)


class SchemaTests(unittest.TestCase):
    def test_plan_rejects_extra_keys(self) -> None:
        gaps = validate_plan_payload(
            {
                "goal": "g",
                "approach": "a",
                "constraints": [],
                "verify_points": [],
                "answer": "stashed",
            },
            ask_use_jspace=False,
        )
        self.assertTrue(any("extra keys" in g and "answer" in g for g in gaps))

    def test_plan_allows_known_keys(self) -> None:
        gaps = validate_plan_payload(
            {
                "goal": "g",
                "approach": "a",
                "summary": "s",
                "constraints": ["c"],
                "verify_points": ["v"],
            },
            ask_use_jspace=False,
        )
        self.assertEqual(gaps, [])

    def test_verify_rejects_extra_keys(self) -> None:
        gaps = validate_verify_payload(
            {
                "points_met": True,
                "aligned": True,
                "evidence_ok": True,
                "retry": True,
                "summary": "ok",
                "checks": ["path: done.txt"],
                "drift": [],
                "evidence_gaps": [],
                "diagnosis": "",
                "secret": 1,
            }
        )
        self.assertTrue(any("extra keys" in g and "secret" in g for g in gaps))

    def test_extra_keys_helper(self) -> None:
        self.assertEqual(extra_keys({"goal": "x", "foo": 1}, PLAN_KEYS), ["foo"])

    def test_answer_vs_plan_mtime(self) -> None:
        root = Path(__file__).resolve().parent / ".tmp" / "schema_mtime"
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir(parents=True)
        flame = root / ".flame"
        flame.mkdir()
        (flame / "plan.json").write_text("{}\n", encoding="utf-8")
        plan_mtime = (flame / "plan.json").stat().st_mtime
        leftover = root / "answer.md"
        leftover.write_text("old\n", encoding="utf-8")
        os.utime(leftover, (plan_mtime - 120, plan_mtime - 120))
        gaps = audit_answer_vs_plan(root, plan_mtime=plan_mtime)
        self.assertTrue(gaps)
        leftover.write_text("new\n", encoding="utf-8")
        os.utime(leftover, (plan_mtime + 1, plan_mtime + 1))
        self.assertEqual(audit_answer_vs_plan(root, plan_mtime=plan_mtime), [])

    def test_answer_prefers_newer_file(self) -> None:
        root = Path(__file__).resolve().parent / ".tmp" / "schema_newer_answer"
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir(parents=True)
        flame = root / ".flame"
        flame.mkdir()
        (flame / "plan.json").write_text("{}\n", encoding="utf-8")
        plan_mtime = (flame / "plan.json").stat().st_mtime
        stale = flame / "answer.md"
        stale.write_text("old flame\n", encoding="utf-8")
        os.utime(stale, (plan_mtime - 120, plan_mtime - 120))
        fresh = root / "answer.md"
        fresh.write_text("new root\n", encoding="utf-8")
        os.utime(fresh, (plan_mtime + 1, plan_mtime + 1))
        self.assertEqual(answer_md_path(root), fresh)
        self.assertEqual(audit_answer_vs_plan(root, plan_mtime=plan_mtime), [])


if __name__ == "__main__":
    unittest.main()
