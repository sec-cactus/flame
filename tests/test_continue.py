from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flame.loop import continue_run
from flame.progress import Progress
from flame.types import Effort
from tests.test_loop import LoopTests, _fake_agent


class ContinueRunTests(LoopTests):
    def _seed_graph_workspace(self, name: str) -> Path:
        workspace = self._workspace(name)
        flame = workspace / ".flame"
        flame.mkdir(parents=True, exist_ok=True)
        run_rel = ".fact-graph/runs/flame-act-c1"
        run_dir = workspace / run_rel
        run_dir.mkdir(parents=True)
        (flame / "graph_run.json").write_text(
            json.dumps({"run_dir": run_rel}) + "\n",
            encoding="utf-8",
        )
        (flame / "plan.json").write_text(
            json.dumps(
                {
                    "goal": "prior task",
                    "approach": "explore",
                    "constraints": [],
                    "verify_points": ["done.txt exists"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        board = {
            "title": "t",
            "status": "stopped",
            "bootstrap_enabled": False,
            "created_at": "2026-01-01T00:00:00Z",
            "completed_at": None,
            "completion": None,
            "constraints": "",
            "facts": [
                {"id": "origin", "description": "brief", "source": "init", "created_at": "t"},
                {"id": "goal", "description": "goal", "source": "init", "created_at": "t"},
            ],
            "intents": [],
            "hints": [],
            "reason_rounds": 0,
            "inbox_merged": 0,
        }
        (run_dir / "board.json").write_text(json.dumps(board) + "\n", encoding="utf-8")
        (run_dir / "config.toml").write_text(
            "max_workers = 1\ninterval = 1\nmax_reason_rounds = 1\n"
            "max_facts = 5\nwallclock_budget = 60\nprompt_group = 'mock'\n",
            encoding="utf-8",
        )
        # Stale handoff from the previous task must not shadow this run's RESULT.md.
        (flame / "graph-result.md").write_text(
            "STALE previous task text\n", encoding="utf-8"
        )
        return workspace

    def test_continue_run_hint_and_verify(self) -> None:
        workspace = self._seed_graph_workspace("continue_ok")
        cfg = self._cfg(workspace, Effort.graph)
        run_dir = workspace / ".fact-graph" / "runs" / "flame-act-c1"

        def fake_subprocess(cmd, **kwargs):
            proc = mock.Mock()
            proc.returncode = 0
            if "hint" in cmd:
                proc.stdout = "hint ok\n"
            elif "reopen" in cmd:
                proc.stdout = "reopen ok\n"
            elif "run" in cmd:
                proc.stdout = "run 结束: status=stopped\n"
                (workspace / "done.txt").write_text("ok\n", encoding="utf-8")
                (run_dir / "RESULT.md").write_text(
                    "FRESH continue result\n", encoding="utf-8"
                )
            else:
                proc.stdout = ""
            proc.stderr = ""
            return proc

        with mock.patch("flame.loop.subprocess.run", side_effect=fake_subprocess):
            result = continue_run(
                "please extend the deliverable",
                config=cfg,
                progress=Progress(stream=io.StringIO()),
            )
        self.assertTrue(result.passed, result.output)
        self.assertTrue((workspace / "done.txt").is_file())
        self.assertIn("FRESH continue result", result.output)
        self.assertNotIn("STALE previous task text", result.output)
        inbox = workspace / ".fact-graph" / "runs" / "flame-act-c1" / "inbox.jsonl"
        self.assertFalse(inbox.is_file())  # hint via subprocess mock, not real file

    def test_continue_requires_graph_run(self) -> None:
        workspace = self._workspace("continue_missing")
        cfg = self._cfg(workspace, Effort.graph)
        with self.assertRaises(Exception):
            continue_run("nope", config=cfg)


class OrchestratorReopenTests(unittest.TestCase):
    def test_reopen_completed(self) -> None:
        from tests.test_factgraph_ledger import _load_orch

        orch = _load_orch()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            board = {
                "title": "t",
                "status": "completed",
                "bootstrap_enabled": False,
                "created_at": "t",
                "completed_at": "t",
                "completion": {"description": "done"},
                "constraints": "",
                "facts": [],
                "intents": [],
                "hints": [],
                "reason_rounds": 0,
                "inbox_merged": 0,
            }
            (run_dir / "board.json").write_text(json.dumps(board) + "\n", encoding="utf-8")
            ns = mock.Mock(
                run_dir=str(run_dir),
                content="follow up",
                creator="proceed",
            )
            self.assertEqual(orch.cmd_reopen(ns), 0)
            saved = json.loads((run_dir / "board.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "active")
            self.assertIsNone(saved["completed_at"])


if __name__ == "__main__":
    unittest.main()
