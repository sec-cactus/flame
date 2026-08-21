from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _load_orch():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "flame"
        / "data"
        / "fact-graph"
        / "scripts"
        / "orchestrator.py"
    )
    name = f"factgraph_orch_{path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FactgraphLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.orch = _load_orch()

    def _board(self) -> dict:
        return self.orch.new_board("t", "origin text", "goal text", False, constraints="")

    def test_add_intent_forces_off_when_open_exists(self) -> None:
        board = self._board()
        first = self.orch.add_intent(board, ["origin"], "a", "reasoner", use_ledger=True)
        self.assertTrue(first["use_ledger"])
        second = self.orch.add_intent(board, ["origin"], "b", "reasoner", use_ledger=True)
        self.assertFalse(second["use_ledger"])

    def test_intent_allows_ledger_only_sole_open(self) -> None:
        board = self._board()
        a = self.orch.add_intent(board, ["origin"], "a", "reasoner", use_ledger=True)
        self.assertTrue(self.orch.intent_allows_ledger(board, a))
        self.orch.add_intent(board, ["origin"], "b", "reasoner", use_ledger=False)
        self.assertFalse(self.orch.intent_allows_ledger(board, a))

    def test_valid_reason_parses_use_ledger(self) -> None:
        PhaseResult = self.orch.PhaseResult
        result = PhaseResult(
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            parsed={
                "accepted": True,
                "data": {
                    "intent": {
                        "from": ["origin"],
                        "description": "pierce one path",
                        "use_ledger": True,
                    }
                },
            },
            elapsed=0.0,
            session_id=None,
        )
        outcome = self.orch.valid_reason(result, {"origin"})
        self.assertEqual(outcome[0], "intent")
        self.assertTrue(outcome[3])

    def test_ledger_root_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            root = self.orch.ledger_root_for(run_dir, "i001")
            self.assertEqual(root, (run_dir / "ledgers" / "i001").resolve())

    def test_init_from_seed_multiline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            seed_path = root / "graph_seed.json"
            seed_path.write_text(
                json.dumps(
                    {
                        "origin": "brief line\nsecond",
                        "goal": "do it\n\n## Verify points (min-fail)\n- a",
                        "constraints": "- must",
                        "hint": "pierce one path",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"
            ns = types.SimpleNamespace(
                run_dir=str(run_dir),
                title="t",
                origin="",
                goal="",
                constraints="",
                hint="",
                seed=str(seed_path),
                origin_file=None,
                goal_file=None,
                constraints_file=None,
                hint_file=None,
                config=None,
                no_bootstrap=True,
            )
            self.assertEqual(self.orch.cmd_init(ns), 0)
            board = self.orch.load_board(run_dir)
            goal = next(f for f in board["facts"] if f["id"] == "goal")["description"]
            origin = next(f for f in board["facts"] if f["id"] == "origin")["description"]
            self.assertIn("\n", goal)
            self.assertIn("Verify points", goal)
            self.assertEqual(origin, "brief line\nsecond")
            self.assertEqual(board["constraints"], "- must")
            self.assertEqual(board["hints"][0]["content"], "pierce one path")


if __name__ == "__main__":
    unittest.main()
