"""pytest plugin (weak-mutation map build only), harness C: the whole suite in ONE process, in collection order,
no fork, like the labeller. The recorder's per-site state is cleared before each test, so every site a test
reaches is credited to that test, with the process state the earlier tests left behind (a test that clears
Dialect._classes makes the next tests run the lazy re-registration code, which a forked run never reaches).

Load with `-p tools.wmseq`, with tools/weakmut_site first on PYTHONPATH and WM_OUT, WM_SRC, WM_TOOLS, WM_RES,
WM_TESTS set as for tools/wmfork.py, and run without xdist (-n 0). A result file <number>.mar is written only
when every report of the test, subtest reports included, passed.
"""

from __future__ import annotations

import json
import marshal
import os

import pytest

_NUM: dict[str, int] | None = None
_STATE = {"bad": set(), "k": 0}


def _numbers() -> dict[str, int]:
    global _NUM
    if _NUM is None:
        tests = json.load(open(os.environ["WM_TESTS"], encoding="utf-8"))["tests"]
        _NUM = {t["id"]: t["number"] for t in tests}
    return _NUM


def _collect(dirpath: str) -> dict:
    summ: dict[str, int] = {}
    for name in sorted(os.listdir(dirpath)):
        if not name.startswith("wm."):
            continue
        p = os.path.join(dirpath, name)
        with open(p) as fh:
            for line in fh:
                key, _, mask = line.rpartition(" ")
                summ[key] = summ.get(key, 0) | int(mask)
        os.remove(p)
    os.rmdir(dirpath)
    return summ


def pytest_runtest_logreport(report):
    if report.outcome != "passed":
        _STATE["bad"].add(report.nodeid)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    import wm_runtime as rt

    _STATE["k"] += 1
    base = os.environ.get("WM_BASE") or os.environ["WM_OUT"]
    os.environ["WM_BASE"] = base
    work = os.path.join(base, f"c{os.getpid()}-{_STATE['k']}")
    os.makedirs(work)
    os.environ["WM_OUT"] = work  # process pool workers and subprocesses of this test log here too
    rt.OUT = work
    if rt._fd[1] != -1:
        try:
            os.close(rt._fd[0])
        except OSError:
            pass
    rt._fd[1] = -1
    rt.SEEN.clear()
    _STATE["bad"].discard(item.nodeid)
    yield
    seen = dict(rt.SEEN)
    summ = _collect(work)
    for k, m in seen.items():
        summ[k] = summ.get(k, 0) | m
    num = _numbers().get(item.nodeid)
    if num is not None and item.nodeid not in _STATE["bad"]:
        res = os.environ["WM_RES"]
        tmp = os.path.join(res, f".{num}.tmp")
        with open(tmp, "wb") as f:
            f.write(marshal.dumps({"id": item.nodeid, "mask": summ}))
        os.replace(tmp, os.path.join(res, f"{num}.mar"))
    rt.SEEN.clear()
