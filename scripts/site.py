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
from bookwiki.integrator.markdown_renderers import normalize_mdx_math  # noqa: E402
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
PRESERVE_SITE_NAMES = {
    ".bookwiki",
    ".env.local",
    "node_modules",
    "tsconfig.tsbuildinfo",
}
SKIP_TEMPLATE_NAMES = {
    ".bookwiki",
    ".env.local",
    ".next",
    ".source",
    "node_modules",
    "tsconfig.tsbuildinfo",
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
    _normalize_site_mdx(target_docs)

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


def _normalize_site_mdx(content_dir: Path) -> None:
    for path in content_dir.rglob("*.mdx"):
        text = path.read_text(encoding="utf-8")
        normalized = normalize_mdx_math(text)
        if normalized != text:
            path.write_text(normalized, encoding="utf-8")


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


def _remove_path(path: Path) -> None:
    if path.is_dir():
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

    cfg = load_site_config(args.book_dir)
    site_dir = materialize_site(cfg)
    sync_site_env(site_dir)
    env = os.environ.copy()
    env["BOOKWIKI_SITE_LANGUAGE"] = cfg.language
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")

    subprocess.run(["pnpm", "install"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "build"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "start"], cwd=site_dir, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"site command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
