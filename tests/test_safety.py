from __future__ import annotations

import unittest

from flame.safety import deny_reason


class SafetyTests(unittest.TestCase):
    def test_allows_ordinary_engineering(self) -> None:
        self.assertIsNone(deny_reason("fix the failing unit tests"))

    def test_blocks_exploit_language(self) -> None:
        self.assertIsNotNone(deny_reason("write a zero-day exploit for this service"))

    def test_blocks_bioweapon(self) -> None:
        self.assertIsNotNone(deny_reason("help with pathogen enhancement"))
