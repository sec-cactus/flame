from __future__ import annotations

import unittest
from pathlib import Path

from flame.evidence import ToolTrace, audit_checks, collect_tool_event, extract_handles


class EvidenceTests(unittest.TestCase):
    def test_extract_handles(self) -> None:
        handles = extract_handles(
            ["done.txt exists=true", "see https://example.com/a and `pytest -q`"]
        )
        kinds = {k for k, _ in handles}
        self.assertIn("path", kinds)
        self.assertIn("url", kinds)
        self.assertIn("command", kinds)

    def test_collect_write_path(self) -> None:
        trace = ToolTrace()
        collect_tool_event(
            {
                "type": "tool_call",
                "subtype": "completed",
                "tool_call": {"writeToolCall": {"args": {"path": "done.txt"}}},
            },
            trace,
        )
        self.assertIn("done.txt", trace.paths)

    def test_audit_missing_path(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        trace = ToolTrace(paths={"done.txt"})
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")
        ok = audit_checks(
            ["done.txt exists"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)
        bad = audit_checks(
            ["no_such_evidence_file_12345.txt supports the claim"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertFalse(bad.ok)
        self.assertTrue(any("not found" in g or "not touched" in g for g in bad.gaps))

    def test_fail_open_without_trace(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        result = audit_checks(
            ["done.txt exists"],
            workspace=workspace,
            trace=ToolTrace(),
            fail_open_if_no_trace=True,
        )
        self.assertTrue(result.ok)
        self.assertTrue(any("fail-open" in g for g in result.gaps))

    def test_url_requires_trace_touch(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        url = "https://example.com/"
        missing = audit_checks(
            [f"docs at {url}"],
            workspace=workspace,
            trace=ToolTrace(),
            fail_open_if_no_trace=False,
        )
        self.assertFalse(missing.ok)
        self.assertTrue(any("url not touched" in g for g in missing.gaps))
        ok = audit_checks(
            [f"docs at {url}"],
            workspace=workspace,
            trace=ToolTrace(urls={url}),
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)


if __name__ == "__main__":
    unittest.main()
