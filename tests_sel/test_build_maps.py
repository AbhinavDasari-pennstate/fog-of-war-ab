"""Unit tests for the statement table and context parsing in tools/build_maps.py."""

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import build_maps as bm  # noqa: E402

SRC = textwrap.dedent(
    '''\
    import os

    X = {
        "a": 1,
        "b": 2,
    }


    class A:
        K = 3

        def f(self, x=4):
            y = x + 1
            if y > 2:
                return (
                    y
                    and x
                )
            return 0


    def g():
        return 1
    '''
)


def stmt_at(src: str, line: int):
    starts, ends, sids, scopes = bm.build_stmts(src)
    import bisect

    i = bisect.bisect_right(starts, line) - 1
    assert i >= 0 and ends[i] >= line, (line, starts, ends)
    kind, d0, d1, cls = scopes[sids[i]]
    return starts[i], ends[i], kind, d0, d1, cls


def test_module_dict_is_one_statement():
    assert stmt_at(SRC, 5)[:3] == (3, 6, "module")


def test_class_body_and_header():
    assert stmt_at(SRC, 10)[2:] == ("class", 0, 0, "A")  # K = 3
    assert stmt_at(SRC, 9)[2:] == ("module", 0, 0, "A")  # class A: header


def test_def_header_is_class_scope_with_its_own_range():
    kind, d0, d1, cls = stmt_at(SRC, 12)[2:]
    assert (kind, cls) == ("class", "A") and (d0, d1) == (12, 19)


def test_multiline_return_is_one_function_statement():
    s = stmt_at(SRC, 16)
    assert s[:3] == (15, 18, "function") and s[5] == "A"


def test_context_parsing_keeps_task_context():
    assert bm._CTX_SPLIT.split("a.py::T::t1|run|b.py::T::t2|run")[-1] == "b.py::T::t2|run"
    assert bm._CTX_SPLIT.split("<import>|b.py::T::t2|run")[-1] == "b.py::T::t2|run"
    assert bm._CTX_SPLIT.split("a.py::T::t1|setup")[-1] == "a.py::T::t1|setup"
