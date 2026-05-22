from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bookwiki.scheduler.config import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the BookWiki Next.js demo site.")
    parser.add_argument("book_dir")
    args = parser.parse_args()

    cfg = load_config(args.book_dir)
    site_dir = ROOT / "site-template"
    env = os.environ.copy()
    env["BOOKWIKI_BOOK_DIR"] = str(cfg.book_dir.resolve())
    env["BOOKWIKI_SITE_LANGUAGE"] = cfg.language
    subprocess.run(["pnpm", "dev"], cwd=site_dir, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"site command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
