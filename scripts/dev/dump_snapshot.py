"""Golden-snapshot exporter for pipeline regression checks.

Emits a single deterministic text snapshot covering three areas of a built book:

1. Vault content  -- every ``*.mdx`` / ``*.md`` under ``content/docs``
2. Agent results  -- every ``*.json`` under ``work/agent_results`` (key-normalised)
3. SQLite index   -- schema + full row dump of the stable content tables

The output is intentionally free of timestamps, durations and absolute paths so
that two runs of the same pipeline (e.g. before and after the LangGraph
migration) diff to nothing when behaviour is unchanged. Compare with::

    diff -ru gold/baseline.snapshot gold/current.snapshot
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

VAULT_GLOBS: tuple[str, ...] = ("*.mdx", "*.md")
SNAPSHOT_TABLES: tuple[str, ...] = (
    "pages",
    "chunks",
    "quiz_items",
    "card_items",
    "source_refs",
)


def dump_snapshot(book_dir: Path) -> str:
    sections = [
        _vault_section(book_dir),
        _agent_results_section(book_dir),
        _sqlite_section(book_dir),
    ]
    return "\n".join(sections) + "\n"


def _vault_section(book_dir: Path) -> str:
    content_dir = book_dir / "content" / "docs"
    lines = ["===== SECTION: vault =====", f"root: {_rel(content_dir, book_dir)}"]
    for path in _iter_vault_files(content_dir):
        rel = _rel(path, content_dir)
        body = path.read_text(encoding="utf-8")
        lines.append(f"----- FILE: {rel} -----")
        lines.append(body.rstrip("\n"))
    if len(lines) == 2:
        lines.append("(no vault files)")
    return "\n".join(lines)


def _iter_vault_files(content_dir: Path) -> list[Path]:
    if not content_dir.exists():
        return []
    seen: set[Path] = set()
    for pattern in VAULT_GLOBS:
        seen.update(content_dir.rglob(pattern))
    return sorted(seen, key=lambda item: _rel(item, content_dir))


def _agent_results_section(book_dir: Path) -> str:
    results_dir = book_dir / "work" / "agent_results"
    lines = ["===== SECTION: agent_results =====", f"root: {_rel(results_dir, book_dir)}"]
    files = sorted(results_dir.glob("*.json")) if results_dir.exists() else []
    for path in files:
        lines.append(f"----- FILE: {path.name} -----")
        lines.append(_normalise_json(path.read_text(encoding="utf-8")))
    if not files:
        lines.append("(no agent results)")
    return "\n".join(lines)


def _sqlite_section(book_dir: Path) -> str:
    db_path = book_dir / "site" / ".bookwiki" / "bookwiki.sqlite"
    lines = ["===== SECTION: sqlite ====="]
    if not db_path.exists():
        lines.append("(no sqlite index)")
        return "\n".join(lines)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        lines.append("--- schema ---")
        lines.extend(_schema_lines(conn))
        for table in SNAPSHOT_TABLES:
            lines.append(f"--- table: {table} ---")
            lines.extend(_table_lines(conn, table))
    return "\n".join(lines)


def _schema_lines(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'fts_chunks_%'
        ORDER BY type, name
        """
    ).fetchall()
    return [f"{row['type']} {row['name']}: {_squash(row['sql'])}" for row in rows]


def _table_lines(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if not columns:
        return ["(missing table)"]
    order = ", ".join(f'"{col}"' for col in columns)
    rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall()  # noqa: S608
    out = [f"rows: {len(rows)}", f"columns: {','.join(columns)}"]
    out.extend(
        json.dumps({col: row[col] for col in columns}, ensure_ascii=False, sort_keys=True)
        for row in rows
    )
    return out


def _normalise_json(raw: str) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw.rstrip("\n")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _squash(sql: str | None) -> str:
    if not sql:
        return ""
    return " ".join(sql.split())


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump a deterministic book snapshot.")
    parser.add_argument("book_dir", help="Path to the built book directory")
    parser.add_argument("-o", "--output", help="Write snapshot here instead of stdout")
    args = parser.parse_args()

    snapshot = dump_snapshot(Path(args.book_dir))
    if args.output:
        Path(args.output).write_text(snapshot, encoding="utf-8")
        print(f"snapshot written: {args.output}")
    else:
        print(snapshot, end="")


if __name__ == "__main__":
    main()
