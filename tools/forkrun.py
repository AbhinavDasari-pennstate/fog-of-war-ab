"""pytest plugin (map build only): run every test in a forked child of a warm, preimported parent.

Without it, anything a test builds lazily and caches (dialect registry entries, annotator tables,
class-level state) is credited to whichever test ran first in that worker process, so the per-test
line sets depend on scheduling. Here every test starts from the same state (the parent after
tools.preimport and collection), runs its whole protocol (class fixtures included) in a child,
and the child writes its own coverage, read-audit and class-use files. Outcomes are sent back to
the parent so pytest and xdist still see a normal run (subtest reports stay in the child).
Enabled with `-p tools.forkrun` together with tools.covctx / readaudit / classuse.
"""

from __future__ import annotations

import os
import pickle
import sys

import pytest


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(fd)
    return b"".join(chunks)


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

            signal.alarm(1500)  # a child stuck on a lock inherited from another thread dies; the parent reports it
            try:
                item.config.pluginmanager.unregister(name="workerinteractor")
            except Exception:
                pass
            from tools import classuse_core, covctx, readaudit

            covctx.child_start()
            reports = runtestprotocol(item, nextitem=None, log=False)
            hook = item.config.hook
            payload = pickle.dumps([hook.pytest_report_to_serializable(config=item.config, report=rep) for rep in reports])
            covctx.child_stop()
            out = os.environ.get("MAPS_OUT", "")
            if out:
                classuse_core.dump(os.path.join(out, f"classuse-t{os.getpid()}.json"))
                readaudit.dump(os.path.join(out, f"readaudit-t{os.getpid()}.json"))
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
    else:  # child died: report an error so the run is visibly wrong
        from _pytest.reports import TestReport

        rep = TestReport(
            nodeid=item.nodeid,
            location=item.location,
            keywords={},
            outcome="failed",
            longrepr=f"forked child died (status {status})",
            when="call",
        )
        ihook.pytest_runtest_logreport(report=rep)
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
