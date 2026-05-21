from __future__ import annotations

SUSPICIOUS_PHRASES = ["ignore previous instructions", "system prompt", "developer message"]


def suspicious_instruction_phrases(markdown: str) -> list[str]:
    lower = markdown.lower()
    return [phrase for phrase in SUSPICIOUS_PHRASES if phrase in lower]
