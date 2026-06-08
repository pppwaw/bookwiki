"""Unit tests for the Phase 4 figure tools (``run_plot`` and friends)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bookwiki.generate.figures import (
    reuse_existing_figure,
    run_plot,
    scan_forbidden_code,
    verify_figure,
)

PLOT_CODE = (
    "import matplotlib.pyplot as plt\n"
    "fig, ax = plt.subplots()\n"
    "ax.plot([0, 1, 2], [0, 1, 4])\n"
    "ax.set_title('demo')\n"
)


# --------------------------------------------------------------------------- #
# AST blacklist (no subprocess, no matplotlib needed)
# --------------------------------------------------------------------------- #
def test_scan_blocks_network_import() -> None:
    violations = scan_forbidden_code("import requests\nrequests.get('http://x')\n")
    assert any("requests" in message for message in violations)


def test_scan_blocks_os_system_and_eval() -> None:
    assert scan_forbidden_code("import os\nos.system('rm -rf /')\n")
    assert scan_forbidden_code("eval('1+1')\n")
    assert scan_forbidden_code("__import__('socket')\n")


def test_scan_allows_plain_matplotlib() -> None:
    assert scan_forbidden_code(PLOT_CODE) == []


def test_scan_reports_syntax_error() -> None:
    violations = scan_forbidden_code("def broken(:\n")
    assert violations and "syntax error" in violations[0]


def test_run_plot_rejects_forbidden_code_before_execution(tmp_path: Path) -> None:
    result = run_plot(
        "import socket\n",
        output_path=tmp_path / "out.png",
        cache_dir=tmp_path / "cache",
        timeout_s=5,
    )
    assert result["ok"] is False
    assert "forbidden" in result["error"]
    assert not (tmp_path / "out.png").exists()


# --------------------------------------------------------------------------- #
# Subprocess execution (requires matplotlib)
# --------------------------------------------------------------------------- #
def test_run_plot_generates_png(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    output = tmp_path / "assets" / "fig.png"
    result = run_plot(
        PLOT_CODE,
        output_path=output,
        cache_dir=tmp_path / "cache",
        timeout_s=30,
    )
    assert result["ok"] is True, result["error"]
    assert result["cache_hit"] is False
    assert output.exists()
    assert verify_figure(output)["ok"] is True


def test_run_plot_caches_identical_code(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    cache_dir = tmp_path / "cache"
    first = run_plot(PLOT_CODE, output_path=tmp_path / "a.png", cache_dir=cache_dir, timeout_s=30)
    second = run_plot(PLOT_CODE, output_path=tmp_path / "b.png", cache_dir=cache_dir, timeout_s=30)
    assert first["ok"] and second["ok"]
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()


def test_run_plot_different_code_misses_cache(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    cache_dir = tmp_path / "cache"
    run_plot(PLOT_CODE, output_path=tmp_path / "a.png", cache_dir=cache_dir, timeout_s=30)
    other = run_plot(
        PLOT_CODE + "ax.set_xlabel('x')\n",
        output_path=tmp_path / "b.png",
        cache_dir=cache_dir,
        timeout_s=30,
    )
    assert other["cache_hit"] is False


def test_run_plot_times_out_on_infinite_loop(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    result = run_plot(
        "while True:\n    pass\n",
        output_path=tmp_path / "out.png",
        cache_dir=tmp_path / "cache",
        timeout_s=2,
    )
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert not (tmp_path / "out.png").exists()


# --------------------------------------------------------------------------- #
# reuse_existing_figure / verify_figure
# --------------------------------------------------------------------------- #
def test_reuse_existing_figure_hit_and_miss() -> None:
    figures = [{"id": "paper-p001-b001", "caption": "Tree"}]
    hit = reuse_existing_figure("paper-p001-b001", figures)
    assert hit["ok"] is True
    assert hit["caption"] == "Tree"
    miss = reuse_existing_figure("nope", figures)
    assert miss["ok"] is False


def test_verify_figure_rejects_missing_and_bad_format(tmp_path: Path) -> None:
    assert verify_figure(tmp_path / "missing.png")["ok"] is False
    bad = tmp_path / "note.txt"
    bad.write_text("x" * 200, encoding="utf-8")
    assert verify_figure(bad)["ok"] is False
