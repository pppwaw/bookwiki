from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the BookWiki Next.js demo site.")
    parser.add_argument("book_dir")
    args = parser.parse_args()

    site_dir = ROOT / "site-template"
    env = os.environ.copy()
    env["BOOKWIKI_BOOK_DIR"] = str(Path(args.book_dir).resolve())
    subprocess.run(["pnpm", "dev"], cwd=site_dir, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"site command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
