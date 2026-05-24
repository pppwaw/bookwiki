from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_run_script():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("bookwiki_run_script", SCRIPTS / "run.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_force_from(argv: list[str]) -> str | None:
    run_script = load_run_script()
    parser: argparse.ArgumentParser = run_script.build_parser()
    args = parser.parse_args(argv)
    return run_script.resolve_force_from(args, parser)


def test_run_cli_uses_explicit_from_force_pair() -> None:
    assert parse_force_from(["books/mini", "--from", "integrate", "--force"]) == "integrate"


def test_run_cli_rejects_from_without_force() -> None:
    with pytest.raises(SystemExit):
        parse_force_from(["books/mini", "--from", "integrate"])


def test_run_cli_rejects_force_without_from() -> None:
    with pytest.raises(SystemExit):
        parse_force_from(["books/mini", "--force"])


def test_run_cli_does_not_keep_force_from_alias() -> None:
    run_script = load_run_script()
    parser: argparse.ArgumentParser = run_script.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["books/mini", "--force-from", "integrate"])
