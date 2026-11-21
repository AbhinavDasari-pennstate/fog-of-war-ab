"""pytest plugin (map build only): run every test in a forked child of a warm parent, for the attribute-read recorder.

Same protocol as tools.forkrun (each test, class fixtures included, runs in its own forked child so lazily
cached state is rebuilt, and recorded, by every test that needs it) without coverage. The child tells
tools/attr_site/attr_runtime which test its reads belong to. Used with `-p tools.attrfork -p tools.preimport
--assert=plain` and tools/attr_site first on PYTHONPATH.
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

            signal.alarm(1500)
            try:
                item.config.pluginmanager.unregister(name="workerinteractor")
            except Exception:
                pass
            rt = sys.modules["attr_runtime"]
            rt.begin_test(item.nodeid)
            reports = runtestprotocol(item, nextitem=None, log=False)
            hook = item.config.hook
            payload = pickle.dumps([hook.pytest_report_to_serializable(config=item.config, report=rep) for rep in reports])
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
