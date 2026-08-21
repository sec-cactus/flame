from __future__ import annotations

import re

# Lightweight deny-list. This is not a classifier model.
_DENY_PATTERNS = [
    r"\b(exploit|zero[- ]day|0day|rce|privilege escalation)\b",
    r"\b(malware|ransomware|keylogger|botnet|rootkit)\b",
    r"\b(unauthorized (access|intrusion)|sql injection|xss payload)\b",
    r"(未授权入侵|漏洞利用|木马|勒索软件)",
    r"\b(bioweapon|pathogen enhancement|reverse genetics|gain[- ]of[- ]function)\b",
    r"(生物武器|病原增强|增益功能)",
    r"\b(chemical weapon|nerve agent|synthesis of sarin)\b",
    r"(化学武器|神经毒剂)",
    r"(child sexual|csam|child porn|未成年.*(性|色情))",
]


def deny_reason(task: str) -> str | None:
    text = task.lower()
    for pattern in _DENY_PATTERNS:
        if re.search(pattern, task, flags=re.IGNORECASE) or re.search(
            pattern, text, flags=re.IGNORECASE
        ):
            return "request blocked by flame safety gate"
    return None


class SafetyDenied(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
