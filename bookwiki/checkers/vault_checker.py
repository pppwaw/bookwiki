from __future__ import annotations

from pathlib import Path


def vault_has_markdown(vault_dir: str | Path) -> bool:
    return any(Path(vault_dir).rglob("*.md"))
