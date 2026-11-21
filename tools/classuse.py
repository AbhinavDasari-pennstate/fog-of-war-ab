"""pytest plugin (map build only): record which sqlglot classes each test instantiated.

Thin wrapper over tools.classuse_core: wraps __init__ of every class defined in a sqlglot module
(after tools.preimport imported them all) and sets the current test key from pytest hooks.
Setup-phase use (setUpClass) is credited to the whole class group.
Results: $MAPS_OUT/classuse-<pid>.json {key: [module:qualname, ...]}; pool workers write
classuse-child-<pid>.json through tools.covchild.
Loaded with `-p tools.classuse`.
"""

from __future__ import annotations

import os

import pytest

from tools import classuse_core as core

OUT = os.environ.get("MAPS_OUT", "")


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session) -> None:
    core.install()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item) -> None:
    core.set_key("g:" + item.nodeid.rsplit("::", 1)[0])


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item) -> None:
    core.set_key("t:" + item.nodeid)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item) -> None:
    core.set_key("g:" + item.nodeid.rsplit("::", 1)[0])


@pytest.hookimpl(trylast=True)
def pytest_runtest_logfinish(nodeid, location) -> None:
    core.set_key("g:*")


def pytest_sessionfinish(session) -> None:
    if OUT:
        core.dump(os.path.join(OUT, f"classuse-{os.getpid()}.json"))
