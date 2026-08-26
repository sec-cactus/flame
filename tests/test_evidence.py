from __future__ import annotations

import unittest
from pathlib import Path

from flame.evidence import (
    ToolTrace,
    audit_checks,
    collect_tool_event,
    extract_handles,
    extract_handles_from_check,
)


class EvidenceTests(unittest.TestCase):
    def test_extract_handles_explicit(self) -> None:
        handles = extract_handles(
            [
                "path: done.txt exists=true",
                "url: https://example.com/a and `pytest -q`",
            ]
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

    def test_collect_opencode_file_path(self) -> None:
        """OpenCode read/write/edit use filePath; trace must record it (normalized event)."""
        raw = {
            "type": "tool_use",
            "part": {
                "tool": "write",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/tmp/ws/done.txt", "content": "hello\n"},
                },
            },
        }
        from flame.agent_backends import normalize_opencode_event

        trace = ToolTrace()
        collect_tool_event(normalize_opencode_event(raw), trace)
        self.assertTrue(
            any(p.endswith("done.txt") for p in trace.paths),
            trace.paths,
        )

    def test_audit_missing_path(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        trace = ToolTrace(paths={"done.txt"})
        (workspace / "done.txt").write_text("ok\n", encoding="utf-8")
        ok = audit_checks(
            ["path: done.txt exists"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)
        bad = audit_checks(
            ["path: no_such_evidence_file_12345.txt supports the claim"],
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
            ["path: done.txt exists"],
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
            [f"url: {url}"],
            workspace=workspace,
            trace=ToolTrace(),
            fail_open_if_no_trace=False,
        )
        self.assertFalse(missing.ok)
        self.assertTrue(any("url not touched" in g for g in missing.gaps))
        ok = audit_checks(
            [f"url: {url}"],
            workspace=workspace,
            trace=ToolTrace(urls={url}),
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)

    def test_prose_without_path_label_is_ignored(self) -> None:
        handles = extract_handles_from_check(
            "path: run.py exists (Semaphore + BaseException cancel/await cleanup)"
        )
        self.assertEqual(handles, [("path", "run.py")])
        handles2 = extract_handles_from_check("signature tasks/max_concurrent -> None")
        self.assertEqual(handles2, [])

    def test_normalize_dot_flame_prefix(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        dot_flame = workspace / ".flame"
        dot_flame.mkdir(parents=True, exist_ok=True)
        (dot_flame / "answer.md").write_text("# ok\n", encoding="utf-8")
        trace = ToolTrace(paths={".flame/answer.md", "/work/.flame/answer.md"})
        ok = audit_checks(
            ["path: .flame/answer.md exists"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)

    def test_normalize_app_prefix(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        (workspace / "re.json").write_text("{}\n", encoding="utf-8")
        trace = ToolTrace(paths={"re.json"})
        ok = audit_checks(
            ["path: app/re.json matches oracle"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)

    def test_normalize_work_prefix(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        flame = workspace / ".flame"
        flame.mkdir(parents=True, exist_ok=True)
        (flame / "plan.json").write_text("{}\n", encoding="utf-8")
        trace = ToolTrace(paths={".flame/plan.json", "/work/.flame/plan.json"})
        ok = audit_checks(
            ["path: work/.flame/plan.json schema ok"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)

    def test_normalize_absolute_under_workspace(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        (workspace / "check.py").write_text("ok\n", encoding="utf-8")
        trace = ToolTrace(paths={"check.py"})
        ok = audit_checks(
            [f"path: {workspace / 'check.py'} exists"],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertTrue(ok.ok, ok.gaps)

    def test_each_check_must_have_explicit_handle(self) -> None:
        workspace = Path(__file__).resolve().parent / ".tmp_evidence"
        workspace.mkdir(exist_ok=True)
        (workspace / "re.json").write_text("{}\n", encoding="utf-8")
        trace = ToolTrace(paths={"re.json"})
        bad = audit_checks(
            [
                "path: re.json OK",
                "oracle passed with no explicit handle",
            ],
            workspace=workspace,
            trace=trace,
            fail_open_if_no_trace=False,
        )
        self.assertFalse(bad.ok)
        self.assertTrue(any("no explicit handle" in g for g in bad.gaps))


if __name__ == "__main__":
    unittest.main()
