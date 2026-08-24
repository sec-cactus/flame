"""Unit tests for Cursor / OpenCode backend helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from flame.agent_backends import normalize_opencode_event
from flame.config import Config, normalize_agent_backend, resolve_runtime_model
from flame.types import Effort


class BackendConfigTests(unittest.TestCase):
    def test_normalize_backend(self) -> None:
        self.assertEqual(normalize_agent_backend("OpenCode"), "opencode")
        self.assertEqual(normalize_agent_backend(None), "cursor")
        with self.assertRaises(ValueError):
            normalize_agent_backend("codex")

    def test_resolve_opencode_model(self) -> None:
        cfg = Config(
            agent_backend="opencode",
            agent_bin="opencode",
            model="auto",
            workspace=Path("/tmp"),
            effort=Effort.fast,
            log_dir=Path("/tmp/logs"),
            timeout_sec=10,
            force=True,
            trust=True,
            extra_args=[],
        )
        self.assertEqual(resolve_runtime_model(cfg), "opencode-go/deepseek-v4-flash")
        cfg.model = "opencode-go/glm-5.3"
        self.assertEqual(resolve_runtime_model(cfg), "opencode-go/glm-5.3")

    def test_resolve_opencode_model_requires_slash(self) -> None:
        """A non-auto model without provider/ must error, not silently swap models."""
        cfg = Config(
            agent_backend="opencode",
            agent_bin="opencode",
            model="glm-5.3",
            workspace=Path("/tmp"),
            effort=Effort.fast,
            log_dir=Path("/tmp/logs"),
            timeout_sec=10,
            force=True,
            trust=True,
            extra_args=[],
        )
        with self.assertRaises(ValueError):
            resolve_runtime_model(cfg)

    def test_normalize_opencode_tool_event(self) -> None:
        raw = {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "echo hi"},
                },
            },
        }
        norm = normalize_opencode_event(raw)
        self.assertEqual(norm["type"], "tool_call")
        self.assertEqual(norm["subtype"], "completed")
        self.assertIn("bash", norm["tool_call"])


if __name__ == "__main__":
    unittest.main()
