"""pytest plugin (map build only): import every sqlglot module and dialect class before collection.

Import-time lines (module and class bodies) then run once, under the coverage
context "<import>", instead of being credited to whichever test happens to load a
module first. Loaded with `-p tools.preimport`.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def _switch(ctx: str) -> None:
    try:
        import coverage

        cov = coverage.Coverage.current()
        if cov is not None:
            cov.switch_context(ctx)
    except Exception:
        pass


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session) -> None:
    _switch("<import>")
    try:
        import sqlglot

        for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if mod.name.rsplit(".", 1)[-1] in ("__main__",):
                continue
            try:
                importlib.import_module(mod.name)
            except Exception:
                # sqlglotrs / optional pieces: not fatal for the map
                pass
        from sqlglot import dialects

        for name in list(getattr(dialects, "DIALECTS", [])):
            try:
                importlib.import_module("sqlglot.dialects." + name.lower())
            except Exception:
                pass
        # touch every public dialect class (lazy __getattr__ in sqlglot.dialects)
        for attr in dir(dialects):
            if attr[:1].isupper():
                try:
                    getattr(dialects, attr)
                except Exception:
                    pass
    finally:
        _switch("")
