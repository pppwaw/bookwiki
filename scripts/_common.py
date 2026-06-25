from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def book_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("book_dir", help="Path to books/<id>")
    return parser


def parse_list_arg(raw: list[str] | None) -> list[str]:
    values: list[str] = []
    for item in raw or []:
        values.extend(part.strip() for part in item.split(",") if part.strip())
    return values


def parse_chapters(raw: list[str] | None) -> list[str]:
    return parse_list_arg(raw)



