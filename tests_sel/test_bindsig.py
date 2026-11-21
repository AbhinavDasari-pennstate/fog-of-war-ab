"""pyscope.bind_sig: a change of what a function binds is never judged by the line it touches."""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
import pyscope  # noqa: E402


def sig(src):
    return pyscope.bind_sig(ast.parse(src).body[0])


def test_same_binding_set_is_equal():
    assert sig("def f(a):\n    x = 1\n    return x + a\n") == sig("def f(a):\n    x = 2\n    return x * a\n")


def test_name_made_local_differs():
    assert sig("def f(a):\n    if a:\n        return 1\n    return T\n") != sig(
        "def f(a):\n    if a:\n        return 1\n    T = 3\n    return T\n")


def test_yield_differs():
    assert sig("def f():\n    return 1\n") != sig("def f():\n    return 1\n    yield 2\n")


def test_global_declaration_differs():
    assert sig("def f():\n    x = 1\n") != sig("def f():\n    global x\n    x = 1\n")


def test_name_moved_into_nested_function_differs():
    a = "def f():\n    x = 1\n    def g():\n        return x\n    return g\n"
    b = "def f():\n    def g():\n        x = 1\n        return x\n    return g\n"
    assert sig(a) != sig(b)
