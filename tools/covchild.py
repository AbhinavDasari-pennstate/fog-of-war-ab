"""Runs inside spawned pool workers (map build only).

The parent's covctx plugin wraps ProcessPoolExecutor.submit so every task arrives here with the
coverage context of the test that submitted it. Workers outlive tests (TestOptimizer keeps one pool
for the whole class), so the context must switch per task, not per process.
"""

from __future__ import annotations

import atexit
import os

_registered = False


def call(ctx: str, fn, *args, **kwargs):
    global _registered
    try:
        import coverage

        cov = coverage.Coverage.current()
        if cov is not None:
            cov.switch_context(ctx)
    except Exception:
        pass
    out = os.environ.get("MAPS_OUT")
    if out:
        from tools import classuse_core

        classuse_core.install()
        classuse_core.set_key(classuse_core.key_for(ctx))
        if not _registered:
            _registered = True
            atexit.register(classuse_core.dump, os.path.join(out, f"classuse-child-{os.getpid()}.json"))
    return fn(*args, **kwargs)
