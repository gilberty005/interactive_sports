from __future__ import annotations

from pathlib import Path


def load_system_prompt(prompt_path: str) -> str:
    path = Path(prompt_path)
    return path.read_text(encoding="utf-8").strip()

