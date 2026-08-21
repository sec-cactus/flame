from __future__ import annotations

import unittest

from flame.backend import describe_tool, extract_json


class ExtractJsonTests(unittest.TestCase):
    def test_plain_object(self) -> None:
        data = extract_json('{"ok": true, "n": 1}')
        self.assertEqual(data, {"ok": True, "n": 1})

    def test_fenced_object(self) -> None:
        text = """here\n```json\n{"goal": "x", "unknown": "y"}\n```\n"""
        self.assertEqual(extract_json(text)["goal"], "x")

    def test_embedded_object(self) -> None:
        text = 'noise before {"passed": false, "checks": ["a"]} trailing'
        self.assertEqual(extract_json(text)["passed"], False)


class DescribeToolTests(unittest.TestCase):
    def test_read_path(self) -> None:
        event = {"tool_call": {"readToolCall": {"args": {"path": "README.md"}}}}
        self.assertEqual(describe_tool(event), "read README.md")

    def test_function_fallback(self) -> None:
        event = {"tool_call": {"function": {"name": "shell", "arguments": "ls"}}}
        self.assertIn("shell", describe_tool(event))
