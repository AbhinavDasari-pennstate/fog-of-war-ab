"""pytest plugin (weak-mutation map build only): run every test in a forked child of a warm parent and
write that test's weak-mutation summary to $WM_RES/<test number>.mar.

Load with `-p tools.wmfork` (NOT with tools.preimport: a dialect loaded lazily by the first test that asks for it
must be loaded, and its sites recorded, by every test that asks for it), with tools/weakmut_site first on PYTHONPATH
(sitecustomize instruments sqlglot) and these variables set:
  WM_RES    directory for the per-test result files (one fresh directory per seed)
  WM_TESTS  path of tests.json (node id -> test number)
  WM_OUT    scratch directory for the recorder's event logs (per test sub directories are made inside)
  WM_PRE    optional, harness D: node id prefixes of state-changing tests to run first in the parent (see pytest_collection_finish)

Why fork: a test starts from the same state (the parent after collection), so a lazily cached
value is computed, and its sites recorded, in every test that needs it, whatever order the workers run in.
The child's summary starts from everything the parent had recorded (collection), so a site
that ran at import counts for every test. A result file is written only when every report of the test
passed; a failed, errored, skipped or timed out test has no file and is therefore never dropped.
"""

from __future__ import annotations

import json
import marshal
import os
import pickle
import sys

import pytest

_NUM: dict[str, int] | None = None


def _numbers() -> dict[str, int]:
    global _NUM
    if _NUM is None:
        tests = json.load(open(os.environ["WM_TESTS"], encoding="utf-8"))["tests"]
        _NUM = {t["id"]: t["number"] for t in tests}
    return _NUM


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(fd)
    return b"".join(chunks)


def _collect(dirpath: str) -> dict:
    """OR of every event log in dirpath (the child and any process pool workers it started); the files are removed."""
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


def pytest_collection_finish(session):
    """Harness D: WM_PRE=<node id prefix>[,<prefix>...] names tests that change process wide state (tests/test_dialect_entry_points.py
    clears Dialect._classes). They run here, in the parent and unrecorded, before any child is forked, so every
    recorded test starts from the state the suite has after them, and runs (and records) the lazy code that rebuilds it."""
    pre = [p for p in os.environ.get("WM_PRE", "").split(",") if p]
    if not pre:
        return
    from _pytest.runner import runtestprotocol

    for item in session.items:
        if any(item.nodeid.startswith(p) for p in pre):
            runtestprotocol(item, nextitem=None, log=False)


class _Watch:
    """Collects every non-passing report logged while the child runs the test (subtest failures included)."""

    def __init__(self):
        self.bad = []

    def pytest_runtest_logreport(self, report):
        if report.outcome != "passed":
            self.bad.append((report.nodeid, report.when, report.outcome))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            import signal

            signal.alarm(3000)
            try:
                item.config.pluginmanager.unregister(name="workerinteractor")
            except Exception:
                pass
            import wm_runtime as rt

            num = _numbers().get(item.nodeid)
            work = os.path.join(os.environ["WM_OUT"], f"t{os.getpid()}")
            os.makedirs(work)
            os.environ["WM_OUT"] = work  # process pool workers started by the test log here too
            rt.OUT = work
            rt._fd[1] = -1
            for key, mask in list(rt.SEEN.items()):  # what the parent recorded (import, collection) counts for this test
                rt._emit(key, mask)
            watch = _Watch()  # pytest 9 subtests report a failure through pytest_runtest_logreport, not in the returned list
            item.config.pluginmanager.register(watch, "wmwatch")
            reports = runtestprotocol(item, nextitem=None, log=False)
            hook = item.config.hook
            payload = pickle.dumps([hook.pytest_report_to_serializable(config=item.config, report=rep) for rep in reports])
            ok = all(rep.outcome == "passed" for rep in reports) and not watch.bad
            summ = _collect(work)
            if num is not None and ok:
                res = os.environ["WM_RES"]
                tmp = os.path.join(res, f".{num}.{os.getpid()}.tmp")
                with open(tmp, "wb") as f:
                    f.write(marshal.dumps({"id": item.nodeid, "mask": summ}))
                os.replace(tmp, os.path.join(res, f"{num}.mar"))
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
    _, status = os.waitpid(pid, 0)
    hook = item.config.hook
    if data:
        for d in pickle.loads(data):
            rep = hook.pytest_report_from_serializable(config=item.config, data=d)
            ihook.pytest_runtest_logreport(report=rep)
    else:
        from _pytest.reports import TestReport

        rep = TestReport(nodeid=item.nodeid, location=item.location, keywords={}, outcome="failed",
                         longrepr=f"forked child died (status {status})", when="call")
        ihook.pytest_runtest_logreport(report=rep)
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
