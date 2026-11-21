"""pytest plugin (map build only): record which files each test opens.

An audit hook on "open" records repo-relative paths per current test.
 - reads while a test's setup phase runs (setUpClass, module fixtures) are credited
   to every test of that class (or module when there is no class)
 - reads while a test module is being collected are credited to every test of that module
 - reads of any .py file that is not import machinery go to the source-reader table
Results are dumped per process to $MAPS_OUT/readaudit-<pid>.json at session end.
Loaded with `-p tools.readaudit`. MAPS_ROOT = the sqlglot checkout.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.realpath(os.environ.get("MAPS_ROOT", os.getcwd()))
OUT = os.environ.get("MAPS_OUT", "")

_state = {"phase": None, "nodeid": None, "collect": None}
# key -> set of repo-relative paths; key kinds: "t:<nodeid>" (call/teardown),
# "g:<class-or-module nodeid>" (setup phase or collection)
reads: dict[str, set[str]] = {}
srcreads: dict[str, set[str]] = {}

_SKIP_FRAMES = ("importlib", "/coverage/", "assertion/rewrite", "linecache", "<frozen")
_active = False


def _norm(path) -> str | None:
    if isinstance(path, int):
        return None
    if isinstance(path, bytes):
        try:
            path = os.fsdecode(path)
        except Exception:
            return None
    if not isinstance(path, str):
        return None
    try:
        full = os.path.realpath(path)
    except Exception:
        return None
    if full == ROOT or not full.startswith(ROOT + os.sep):
        return None
    return os.path.relpath(full, ROOT)


def _machinery() -> bool:
    f = sys._getframe(2)
    n = 0
    while f is not None and n < 18:
        fn = f.f_code.co_filename
        for s in _SKIP_FRAMES:
            if s in fn:
                return True
        f = f.f_back
        n += 1
    return False


def _hook(event: str, args: tuple) -> None:
    if event != "open" or not _active:
        return
    rel = _norm(args[0])
    if rel is None:
        return
    mode = args[1] if len(args) > 1 else None
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return
    if "/__pycache__/" in "/" + rel or rel.endswith(".pyc"):
        return
    is_py = rel.endswith(".py")
    if is_py and _machinery():
        return
    if _state["collect"] is not None:
        key = "g:" + _state["collect"]
    elif _state["phase"] == "setup" and _state["nodeid"]:
        key = "g:" + _state["nodeid"].rsplit("::", 1)[0]
    elif _state["nodeid"]:
        key = "t:" + _state["nodeid"]
    else:
        key = "g:*"
    if is_py:
        srcreads.setdefault(key, set()).add(rel)
    else:
        reads.setdefault(key, set()).add(rel)


def pytest_configure(config) -> None:
    global _active
    sys.addaudithook(_hook)
    _active = True


@pytest.hookimpl(tryfirst=True)
def pytest_collectstart(collector) -> None:
    if type(collector).__name__ == "Module" or collector.__class__.__name__ == "Module":
        _state["collect"] = collector.nodeid


@pytest.hookimpl(trylast=True)
def pytest_collectreport(report) -> None:
    _state["collect"] = None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item) -> None:
    _state.update(phase="setup", nodeid=item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item) -> None:
    _state.update(phase="call", nodeid=item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item) -> None:
    # tearDownClass runs in the last test's teardown: treat like setup (class wide)
    _state.update(phase="setup", nodeid=item.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_runtest_logfinish(nodeid, location) -> None:
    _state.update(phase=None, nodeid=None)


def dump(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "reads": {k: sorted(v) for k, v in reads.items()},
        "srcreads": {k: sorted(v) for k, v in srcreads.items()},
    }
    with open(path, "w") as f:
        json.dump(data, f)


def pytest_sessionfinish(session) -> None:
    global _active
    _active = False
    if OUT:
        dump(os.path.join(OUT, f"readaudit-{os.getpid()}.json"))
