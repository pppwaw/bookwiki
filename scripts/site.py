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
SITE_ENV_KEYS = (
    "BOOKWIKI_CHAT_API_KEY",
    "BOOKWIKI_CHAT_BASE_URL",
    "BOOKWIKI_CHAT_MODEL",
    "BOOKWIKI_EVALUATE_API_KEY",
    "BOOKWIKI_EVALUATE_BASE_URL",
    "BOOKWIKI_EVALUATE_MODEL",
)
# site is a persistent workspace: keep installed deps (node_modules) and build caches (.next,
# .source) across re-scaffold so ``pnpm build`` stays incremental, and keep ``content`` — the
# single source of truth rendered by ``integrate_node`` — from being wiped or overwritten here.
PRESERVE_SITE_NAMES = {
    ".bookwiki",
    ".env.local",
    "tsconfig.tsbuildinfo",
    "node_modules",
    ".next",
    ".source",
    "content",
}
SKIP_TEMPLATE_NAMES = {
    ".bookwiki",
    ".env.local",
    ".next",
    ".source",
    "node_modules",
    "tsconfig.tsbuildinfo",
    "content",
}


def load_site_config(book_dir: str | Path) -> BookConfig:
    cfg = load_config(book_dir)
    if not cfg.content_dir.exists():
        alternate = ROOT / "books" / Path(book_dir).name
        hint = ""
        if alternate.exists() and (alternate / "content" / "docs").exists():
            hint = f" Did you mean `{alternate}`?"
        raise FileNotFoundError(f"Book content directory not found: {cfg.content_dir}.{hint}")
    return cfg


def scaffold_site_template(book: BookConfig | str | Path) -> Path:
    """Lay the Next.js site framework into ``site_dir`` without touching ``content/docs``.

    ``content/docs`` is the single source of truth, rendered straight into the site by
    ``integrate_node`` (which also normalizes math). This only (re)installs the framework files,
    public assets, and the concept graph. Idempotent; preserves deps and build caches and the
    rendered ``content`` (see PRESERVE_SITE_NAMES / SKIP_TEMPLATE_NAMES).
    """
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
            shutil.copytree(
                child,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*SKIP_TEMPLATE_NAMES),
            )
        else:
            shutil.copy2(child, target)

    source_assets = cfg.work_dir / "assets"
    target_assets = site_dir / "public" / "bookwiki-assets"
    if target_assets.exists():
        _remove_path(target_assets)
    if source_assets.exists():
        target_assets.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_assets, target_assets)

    # The homepage force-directed concept graph fetches `/concept-graph.json`,
    # emitted by `integrate_node` into `work/`. Serve it from `public/`.
    source_graph = cfg.work_dir / "concept-graph.json"
    target_graph = site_dir / "public" / "concept-graph.json"
    if target_graph.exists():
        _remove_path(target_graph)
    if source_graph.exists():
        target_graph.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_graph, target_graph)

    return site_dir


# Transitional alias until call sites migrate (removed in the site.py main slimming step).
materialize_site = scaffold_site_template


def sync_site_env(site_dir: Path) -> Path | None:
    values = _site_env_values()
    env_path = site_dir / ".env.local"
    existing = _env_keys(env_path)
    additions = [(key, value) for key, value in values.items() if key not in existing]

    if not additions:
        return env_path if env_path.exists() else None

    lines: list[str] = []
    if env_path.exists():
        current = env_path.read_text(encoding="utf-8")
        lines.append(current.rstrip("\n"))
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)

    lines.extend(f"{key}={value}" for key, value in additions)
    env_path.write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")
    return env_path


def sync_public_book_id(site_dir: Path, book_id: str) -> Path:
    """Inject ``NEXT_PUBLIC_BOOK_ID`` into the site ``.env.local``.

    The book id namespaces all client-side localStorage keys (highlights, chat
    history) so two books served on the same ``localhost:<port>`` origin never
    share state. Idempotent: an existing line is replaced, not duplicated.
    """
    env_path = site_dir / ".env.local"
    line = f"NEXT_PUBLIC_BOOK_ID={book_id}"
    if env_path.exists():
        out: list[str] = []
        replaced = False
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            if raw_line.strip().removeprefix("export ").lstrip().startswith("NEXT_PUBLIC_BOOK_ID="):
                out.append(line)
                replaced = True
            else:
                out.append(raw_line)
        if not replaced:
            out.append(line)
        env_path.write_text("\n".join(item for item in out if item) + "\n", encoding="utf-8")
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(line + "\n", encoding="utf-8")
    return env_path


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _site_env_values() -> dict[str, str]:
    values = _read_site_values_from_dotenv()
    for key in SITE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            values[key] = value
    return values


def _read_site_values_from_dotenv() -> dict[str, str]:
    dotenv_path = _default_dotenv_path()
    if dotenv_path is None:
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key in SITE_ENV_KEYS and value:
            values[key] = value
    return values


def _default_dotenv_path() -> Path | None:
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidate = parent / ".env"
        if candidate.exists():
            return candidate
    repo_candidate = ROOT / ".env"
    return repo_candidate if repo_candidate.exists() else None


def _env_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is not None:
            keys.add(parsed[0])
    return keys


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").lstrip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if key not in SITE_ENV_KEYS:
        return None
    return key, _parse_env_value(value.strip())


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value.split(" #", 1)[0].strip()


def main() -> None:
    parser = book_arg_parser("Start the BookWiki Next.js demo site.")
    args = parser.parse_args()

    # load_site_config requires content/docs to exist — i.e. the pipeline (integrate) has rendered
    # the single source of truth into the site already. main only frames it and serves.
    cfg = load_site_config(args.book_dir)
    site_dir = scaffold_site_template(cfg)
    sync_site_env(site_dir)
    sync_public_book_id(site_dir, cfg.book_id)
    env = os.environ.copy()
    env["BOOKWIKI_SITE_LANGUAGE"] = cfg.language
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")

    if not (site_dir / "node_modules").exists():
        subprocess.run(["pnpm", "install"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "build"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "start"], cwd=site_dir, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"site command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
