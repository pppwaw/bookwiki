from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bookwiki.scheduler.config import load_config  # noqa: E402
from bookwiki.scheduler.lg_runner import run_pipeline  # noqa: E402


def book_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("book_dir", help="Path to books/<id>")
    return parser


def run_stage(book_dir: str, *, stop_after: str, resume: bool = True) -> None:
    cfg = load_config(book_dir)
    run_pipeline(cfg, stop_after=stop_after, resume=resume)
    print(f"stage complete: {stop_after}")
