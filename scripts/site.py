from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from ._common import book_arg_parser  # type: ignore[import-not-found] # noqa: E402
except ImportError:  # pragma: no cover - direct script execution
    from _common import book_arg_parser  # type: ignore[no-redef] # noqa: E402
from bookwiki.scheduler.config import BookConfig, load_config  # noqa: E402

TEMPLATE_DIR = ROOT / "site-template"
PRESERVE_SITE_NAMES = {".bookwiki", ".next", ".source", "node_modules", "tsconfig.tsbuildinfo"}
SKIP_TEMPLATE_NAMES = {"node_modules", ".next", ".source", ".bookwiki", "tsconfig.tsbuildinfo"}


def load_site_config(book_dir: str | Path) -> BookConfig:
    cfg = load_config(book_dir)
    if not cfg.content_dir.exists():
        alternate = ROOT / "books" / Path(book_dir).name
        hint = ""
        if alternate.exists() and (alternate / "content" / "docs").exists():
            hint = f" Did you mean `{alternate}`?"
        raise FileNotFoundError(f"Book content directory not found: {cfg.content_dir}.{hint}")
    return cfg


def materialize_site(book: BookConfig | str | Path) -> Path:
    cfg = book if isinstance(book, BookConfig) else load_site_config(book)

    site_dir = cfg.site_dir
    site_dir.mkdir(parents=True, exist_ok=True)

    for child in site_dir.iterdir():
        if child.name in PRESERVE_SITE_NAMES:
            continue
        _remove_path(child)

    for child in TEMPLATE_DIR.iterdir():
        if child.name in SKIP_TEMPLATE_NAMES:
            continue
        target = site_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, ignore=shutil.ignore_patterns(*SKIP_TEMPLATE_NAMES))
        else:
            shutil.copy2(child, target)

    target_docs = site_dir / "content" / "docs"
    if target_docs.exists():
        _remove_path(target_docs)
    target_docs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cfg.content_dir, target_docs)

    return site_dir


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main() -> None:
    parser = book_arg_parser("Start the BookWiki Next.js demo site.")
    args = parser.parse_args()

    cfg = load_site_config(args.book_dir)
    site_dir = materialize_site(cfg)
    env = os.environ.copy()
    env["BOOKWIKI_SITE_LANGUAGE"] = cfg.language

    subprocess.run(["pnpm", "install"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "dev"], cwd=site_dir, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"site command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
