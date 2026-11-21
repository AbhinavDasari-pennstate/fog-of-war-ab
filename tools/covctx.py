"""pytest plugin (map build only): coverage.py with the ctrace core and one dynamic context per test phase.

Replaces pytest-cov's controller (its xdist combine step trips on the empty
controller data file) with the same contexts: "<nodeid>|setup", "<nodeid>|run",
"<nodeid>|teardown". Anything outside a test phase runs in "" (import time and
collection) or "<import>" (set by tools.preimport). Every process writes its own
file $MAPS_OUT/cov/.coverage.<pid>. Loaded with `-p tools.covctx`.
"""

from __future__ import annotations

import os

import pytest

OUT = os.environ.get("MAPS_OUT", "")
_cov = None


def _new_cov(name: str):
    import coverage

    os.makedirs(os.path.join(OUT, "cov"), exist_ok=True)
    cov = coverage.Coverage(
        data_file=os.path.join(OUT, "cov", name),
        source=["sqlglot", "tests", "sqlglotc"],
        config_file=False,
        branch=False,
    )
    cov.config.relative_files = True
    return cov


FORK = bool(os.environ.get("MAPS_FORK"))


def child_start() -> None:
    """tools.forkrun: a forked child measures its own test with a fresh Coverage object."""
    global _cov
    _cov = _new_cov(f".coverage.t{os.getpid()}")
    _cov.start()


def child_stop() -> None:
    global _cov
    if _cov is not None:
        _cov.switch_context("")
        _cov.stop()
        _cov.save()
        _cov = None


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session) -> None:
    """Forked mode: the parent only measures import and collection; close its file before forking."""
    global _cov
    if FORK and _cov is not None:
        _cov.stop()
        _cov.save()
        _cov = None


def pytest_configure(config) -> None:
    global _cov
    is_controller = bool(getattr(config.option, "numprocesses", None)) and not hasattr(config, "workerinput")
    if is_controller or not OUT:
        return
    _cov = _new_cov(f".coverage.{os.getpid()}")
    _cov.start()
    # Child interpreters started during a test (spawn pools, subprocess) start coverage through
    # coverage's own .pth hook, in the same context as the test that started them.
    rc = os.path.join(OUT, "covrc")
    with open(rc, "w") as f:
        f.write(
            "[run]\ncore = ctrace\nparallel = True\nrelative_files = True\n"
            "source = sqlglot, tests, sqlglotc\n"
            f"data_file = {os.path.join(OUT, 'cov', '.coverage')}\n"
            "context = ${MAPS_CTX}\n"
        )
    _env["rc"] = rc
    rc_pool = os.path.join(OUT, "covrc_pool")
    with open(rc_pool, "w") as f:
        f.write(
            "[run]\ncore = ctrace\nparallel = True\nrelative_files = True\n"
            "source = sqlglot, tests, sqlglotc\n"
            f"data_file = {os.path.join(OUT, 'cov', '.coverage')}\n"
            "context = <import>\n"
        )
    _env["rc_pool"] = rc_pool
    _patch_pool()


_env: dict[str, str] = {}


def _patch_pool() -> None:
    """Tasks carry the submitting test's context into pool workers (see tools.covchild)."""
    import concurrent.futures.process as cfp

    from tools import covchild

    orig = cfp.ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = os.environ.get("MAPS_CTX")
        if not ctx:
            return orig(self, fn, *args, **kwargs)
        # workers are spawned inside submit: their start-up (imports) runs in "<import>", each task
        # then switches to the submitting test's context
        os.environ["COVERAGE_PROCESS_START"] = _env["rc_pool"]
        try:
            return orig(self, covchild.call, ctx, fn, *args, **kwargs)
        finally:
            os.environ["COVERAGE_PROCESS_START"] = _env["rc"]

    cfp.ProcessPoolExecutor.submit = submit  # type: ignore[method-assign]


def _sw(item, phase: str) -> None:
    if _cov is not None:
        ctx = f"{item.nodeid}|{phase}"
        _cov.switch_context(ctx)
        os.environ["MAPS_CTX"] = ctx
        os.environ["COVERAGE_PROCESS_START"] = _env["rc"]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item) -> None:
    _sw(item, "setup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item) -> None:
    _sw(item, "run")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item) -> None:
    _sw(item, "teardown")


@pytest.hookimpl(trylast=True)
def pytest_runtest_logfinish(nodeid, location) -> None:
    if _cov is not None:
        _cov.switch_context("")
        os.environ.pop("MAPS_CTX", None)
        os.environ.pop("COVERAGE_PROCESS_START", None)


def pytest_unconfigure(config) -> None:
    global _cov
    if _cov is not None:
        _cov.stop()
        _cov.save()
        _cov = None
