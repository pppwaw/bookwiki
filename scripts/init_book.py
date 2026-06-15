from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bookwiki.scheduler.config import default_config, save_config  # noqa: E402
from bookwiki.utils.files import copy_file, ensure_dir  # noqa: E402

BOOK_NOTES_TEMPLATE = """# Book Notes

"""


def init_book(book_dir: Path, source: Path | None = None, title: str | None = None) -> None:
    cfg = default_config(book_dir, title=title)
    for path in [
        cfg.input_dir,
        cfg.work_dir / "logs",
        cfg.work_dir / ".cache",
        cfg.content_dir,
        cfg.site_dir / ".bookwiki",
    ]:
        ensure_dir(path)
    save_config(cfg)
    if not cfg.notes_file.exists():
        cfg.notes_file.write_text(BOOK_NOTES_TEMPLATE, encoding="utf-8")

    if source is not None:
        copy_file(source, cfg.input_dir / source.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a BookWiki book directory skeleton.")
    parser.add_argument("book_dir")
    parser.add_argument("--source", help="Source file to copy into book/input")
    parser.add_argument("--title", help="Book title")
    args = parser.parse_args()

    init_book(Path(args.book_dir), Path(args.source) if args.source else None, args.title)
    print(f"initialized book: {args.book_dir}")


if __name__ == "__main__":
    main()
