"""pytest plugin (map build only): which repo modules does each test import, cold?

Run one test file per pytest process, with `-p tools.lazyimp`. After collection (only that file's
imports are loaded) the plugin forks one child per test; the child records every repo module that
appears in sys.modules during the test and was not there before it. Imports that a test triggers
lazily (the dialect registry, importlib.import_module with a computed name) are invisible to static
analysis and to the preimported coverage runs, yet a change that breaks such a module fails exactly
those tests.

Output: $LAZY_OUT/<file with / replaced by __>.json = {"collect": [module files], "tests": {nodeid: [module files]}}.
Module files are paths relative to $LAZY_ROOT (the pinned checkout). Loaded with `-p tools.lazyimp`.
"""

from __future__ import annotations

import json
import os
import pickle
import sys

import pytest

ROOT = os.environ.get("LAZY_ROOT", os.getcwd())
OUT = os.environ.get("LAZY_OUT", "")
_collect: list[str] = []
_tests: dict[str, list[str]] = {}


def _repo_files() -> set[str]:
    out = set()
    pre = ROOT + os.sep
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.startswith(pre) and f.endswith(".py"):
            out.add(os.path.relpath(f, ROOT))
    return out


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(fd)
    return b"".join(chunks)


def pytest_collection_finish(session) -> None:
    _collect[:] = sorted(_repo_files())


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    before = _repo_files()
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            import signal

            signal.alarm(1500)
            try:
                item.config.pluginmanager.unregister(name="workerinteractor")
            except Exception:
                pass
            reports = runtestprotocol(item, nextitem=None, log=False)
            hook = item.config.hook
            payload = pickle.dumps(
                (
                    [hook.pytest_report_to_serializable(config=item.config, report=rep) for rep in reports],
                    sorted(_repo_files() - before),
                )
            )
            with os.fdopen(w, "wb") as f:
                f.write(payload)
        except BaseException:
            import traceback

            traceback.print_exc()
            code = 3
        finally:
            os._exit(code)
    os.close(w)
    data = _read_all(r)
    os.waitpid(pid, 0)
    hook = item.config.hook
    if data:
        reps, mods = pickle.loads(data)
        _tests[item.nodeid] = mods
        for d in reps:
            ihook.pytest_runtest_logreport(report=hook.pytest_report_from_serializable(config=item.config, data=d))
    else:
        _tests[item.nodeid] = ["<child died>"]
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def pytest_sessionfinish(session) -> None:
    if not OUT:
        return
    files = sorted({i.nodeid.partition("::")[0] for i in session.items})
    name = "__".join(files).replace("/", "__") or "none"
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name[:150] + ".json"), "w") as f:
        json.dump({"collect": _collect, "tests": _tests}, f)
