from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_pnpm_in_check(monkeypatch):
    """Default the test environment to "pnpm unavailable" so ``check_node``'s site type-check /
    build never shells out to a real ``pnpm`` (which would run an actual ``next build`` and hang
    a unit test). Tests that specifically exercise the site check re-mock
    ``bookwiki.pipeline.nodes.shutil.which`` to return a pnpm path, overriding this within the same
    function-scoped monkeypatch. This restores the CI assumption (no pnpm on PATH) on dev machines
    that happen to have pnpm installed.
    """
    import bookwiki.pipeline.nodes as nodes

    real_which = nodes.shutil.which
    monkeypatch.setattr(
        nodes.shutil,
        "which",
        lambda name, *a, **k: None if name == "pnpm" else real_which(name, *a, **k),
    )
