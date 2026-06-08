"""Figure supplementation tools (Phase 4): reuse / plot / verify.

These are plain, LLM-agnostic helpers. ``run_plot`` executes LLM-written
matplotlib code on the host through three lightweight guardrails:

1. **AST blacklist** - reject forbidden imports/calls before anything runs.
2. **chdir to an isolated tempdir** - the child sees an empty working dir and a
   scrubbed environment (no inherited API keys), so it cannot read host files or
   exfiltrate secrets.
3. **wall-clock timeout + POSIX rlimits** - bound CPU time and output size so a
   runaway loop or disk bomb is killed.

Determinism (so the ``sha256(code)`` cache is meaningful): the child forces the
Agg backend, seeds ``numpy``/``random`` to 0, and locks the font to DejaVu Sans.

THREAT MODEL: BookWiki is a single-user local tool; the LLM is the user's own
paid (semi-trusted) DeepSeek/Kimi model and the output is rendered locally. This
is deliberately host execution, not a hardened multi-tenant sandbox. If BookWiki
is ever exposed as a multi-tenant web service, ``run_plot`` MUST be upgraded to a
real sandbox (Docker hardening / gVisor / E2B) and re-reviewed (OWASP ASI05).
"""

from __future__ import annotations

import ast
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:  # POSIX only; Windows falls back to the wall-clock timeout alone.
    import resource

    _HAS_RESOURCE = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAS_RESOURCE = False

# Modules a plotting snippet never legitimately needs and that open network /
# process / filesystem-escape surface. Library code (matplotlib, numpy) imports
# some of these internally - that is fine; only the user snippet is scanned.
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "ctypes",
        "shutil",
        "urllib",
        "requests",
        "httpx",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "pickle",
        "marshal",
        "pty",
        "multiprocessing",
        "socketserver",
        "importlib",
        "pathlib",
        "io",
        "glob",
        "tempfile",
    }
)
FORBIDDEN_CALLS: frozenset[str] = frozenset({"eval", "exec", "compile", "__import__", "open"})
FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {"__globals__", "__builtins__", "__subclasses__", "__bases__", "__mro__", "__import__"}
)
ALLOWED_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".svg"})

_MAX_STDERR_TAIL = 2000
_MAX_FIGURE_BYTES = 10 * 1024 * 1024


def scan_forbidden_code(code: str) -> list[str]:
    """Return a list of guardrail violations; empty means the code may run."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                violations.append(f"from {node.module} import")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in FORBIDDEN_CALLS:
                violations.append(f"call {name}()")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            violations.append(f"attribute {node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            violations.append(f"name {node.id}")

    return _dedupe(violations)


def run_plot(
    code: str,
    *,
    output_path: Path,
    cache_dir: Path,
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Execute matplotlib ``code`` in a guarded subprocess, writing ``output_path``.

    Returns ``{ok, image_path, cache_hit, error, stderr_tail}``. The cache key is
    ``sha256(code)``: identical code reuses the cached PNG, different code does
    not. Never raises - failures are best-effort and reported via ``ok=False``.
    """
    violations = scan_forbidden_code(code)
    if violations:
        return _fail("forbidden code: " + "; ".join(violations))

    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{digest}.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if cached.exists():
        shutil.copyfile(cached, output_path)
        return {
            "ok": True,
            "image_path": str(output_path),
            "cache_hit": True,
            "error": "",
            "stderr_tail": "",
        }

    with tempfile.TemporaryDirectory(prefix="bookwiki-plot-") as tmp:
        workdir = Path(tmp)
        figure_name = "figure.png"
        runner = workdir / "runner.py"
        runner.write_text(_runner_source(code, figure_name), encoding="utf-8")
        outcome = _exec_runner(runner, workdir, timeout_s)
        if not outcome["ok"]:
            return _fail(outcome["error"], stderr_tail=outcome["stderr_tail"])
        figure_path = workdir / figure_name
        if not figure_path.exists():
            return _fail(
                "plot produced no figure (call plt.savefig or leave an open figure)",
                stderr_tail=outcome["stderr_tail"],
            )
        shutil.copyfile(figure_path, cached)
        shutil.copyfile(figure_path, output_path)
        return {
            "ok": True,
            "image_path": str(output_path),
            "cache_hit": False,
            "error": "",
            "stderr_tail": outcome["stderr_tail"],
        }


def reuse_existing_figure(figure_ref: str, source_figures: list[dict[str, str]]) -> dict[str, Any]:
    """Resolve a reference to a figure already extracted from the source PDF."""
    for figure in source_figures:
        if figure.get("id") == figure_ref:
            return {
                "ok": True,
                "figure_ref": figure_ref,
                "caption": figure.get("caption", ""),
                "error": "",
            }
    return {
        "ok": False,
        "figure_ref": figure_ref,
        "caption": "",
        "error": f"unknown source figure {figure_ref!r}",
    }


def verify_figure(image_path: str | Path) -> dict[str, Any]:
    """Basic usability check: file exists, known format, sane size."""
    path = Path(image_path)
    if not path.exists():
        return {"ok": False, "error": "file does not exist"}
    if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        return {"ok": False, "error": f"unsupported image format {path.suffix!r}"}
    size = path.stat().st_size
    if size < 100:
        return {"ok": False, "error": "image file is suspiciously small"}
    if size > _MAX_FIGURE_BYTES:
        return {"ok": False, "error": "image file is too large"}
    return {"ok": True, "error": ""}


# --------------------------------------------------------------------------- #
# Generated-figure asset helpers
# --------------------------------------------------------------------------- #
def generated_asset_relpath(chapter_id: str, figure_ref: str) -> str:
    """Path (relative to ``book_dir``) for a generated figure's PNG.

    Lives under ``work/assets/...`` so the existing site asset copy step
    (``work/assets`` -> ``site/public/bookwiki-assets``) picks it up unchanged.
    """
    return f"work/assets/generated/{_safe_stem(chapter_id)}/{_safe_stem(figure_ref)}.png"


def public_asset_url(asset_relpath: str) -> str:
    """Map a ``work/assets/...`` path to its served ``/bookwiki-assets/...`` URL."""
    normalized = asset_relpath.replace("\\", "/")
    prefix = "work/assets/"
    if normalized.startswith(prefix):
        return "/bookwiki-assets/" + normalized.removeprefix(prefix)
    return normalized


def build_book_figure_tag(figure_ref: str, *, src: str, caption: str = "") -> str:
    """Render a canonical self-closing ``<BookFigure/>`` tag for a generated figure."""
    attrs = [("id", figure_ref), ("src", src)]
    if caption:
        attrs.append(("caption", caption))
    rendered = " ".join(f'{name}="{html.escape(value, quote=True)}"' for name, value in attrs)
    return f"<BookFigure {rendered} />"


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^\w.-]+", "-", str(value).strip(), flags=re.UNICODE)
    stem = re.sub(r"-{2,}", "-", stem).strip("-.")
    return stem or "figure"


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _runner_source(user_code: str, figure_name: str) -> str:
    prelude = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import numpy as np\n"
        "import random\n"
        "np.random.seed(0)\n"
        "random.seed(0)\n"
        "matplotlib.rcParams['font.family'] = 'DejaVu Sans'\n"
    )
    epilogue = (
        "\nimport matplotlib.pyplot as _bw_plt\n"
        "if _bw_plt.get_fignums():\n"
        f"    _bw_plt.savefig({figure_name!r}, dpi=120, bbox_inches='tight')\n"
    )
    return f"{prelude}\n{user_code}\n{epilogue}"


def _exec_runner(runner: Path, workdir: Path, timeout_s: int) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workdir),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(workdir),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    try:
        proc = subprocess.run(  # noqa: S603 - inputs are guarded; see module docstring
            [sys.executable, runner.name],
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            preexec_fn=_limit_resources(timeout_s) if _HAS_RESOURCE else None,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"plot timed out after {timeout_s}s", "stderr_tail": ""}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"plot exited with code {proc.returncode}",
            "stderr_tail": _tail(proc.stderr),
        }
    return {"ok": True, "error": "", "stderr_tail": _tail(proc.stderr)}


def _limit_resources(timeout_s: int):  # pragma: no cover - runs in the child process
    def _apply() -> None:
        cpu = max(timeout_s, 1) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        fsize = _MAX_FIGURE_BYTES
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass

    return _apply


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _tail(text: str) -> str:
    text = text or ""
    return text[-_MAX_STDERR_TAIL:]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _fail(error: str, *, stderr_tail: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "image_path": None,
        "cache_hit": False,
        "error": error,
        "stderr_tail": stderr_tail,
    }
