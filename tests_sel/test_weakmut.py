"""Weak mutation (R10): recognising the mutation, the drop decision, and the instrumenter's semantics."""

import ast
import os
import sys
from pathlib import Path

import pytest

import weakmut as W

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "weakmut_site"))


def mut(old, new):
    ot, nt = ast.parse(old), ast.parse(new)
    m = W.find_mutation(ot, nt)
    return None if m is None else (m[0], W.classify(ot, *m))


def test_is_to_eq_inside_a_function_is_a_compare_site():
    a, cl = mut("def f(x):\n    return x is None\n", "def f(x):\n    return x == None\n")
    assert cl == ("C", 4) and a.lineno == 2


def test_module_level_and_class_level_edits_are_not_sites():
    assert mut("x = a is b\n", "x = a == b\n")[1] is None
    assert mut("class C:\n    y = a is b\n", "class C:\n    y = a == b\n")[1] is None


def test_default_argument_is_not_a_site():
    assert mut("def f(x=(a is b)):\n    pass\n", "def f(x=(a == b)):\n    pass\n")[1] is None


def test_class_inside_function_resets_to_not_a_site():
    old = "def f():\n    class C:\n        y = a is b\n"
    assert mut(old, old.replace("is", "=="))[1] is None


def test_two_edits_or_regrouping_are_not_recognised():
    assert mut("def f(a,b,c):\n    return a < b, b < c\n", "def f(a,b,c):\n    return a <= b, b <= c\n") is None
    # `a and b and c` -> `a and b or c` regroups the tree: no single operator differs
    assert mut("def f(a,b,c):\n    return a and b and c\n", "def f(a,b,c):\n    return a and b or c\n") is None


def test_chained_compare_and_constants_are_not_sites():
    assert mut("def f(a,b,c):\n    return a < b < c\n", "def f(a,b,c):\n    return a < b <= c\n")[1] is None
    assert mut("def f():\n    return 1\n", "def f():\n    return 2\n")[1] is None


def test_not_removal_and_boolop_swap_are_recognised():
    assert mut("def f(a):\n    return not a\n", "def f(a):\n    return a\n")[1] == ("N", None)
    assert mut("def f(a,b):\n    return a and b\n", "def f(a,b):\n    return a or b\n")[1] == ("B", None)


def test_droppable_rules():
    bit = 1 << 4
    assert W.droppable("C", 4, 0)  # never evaluated
    assert W.droppable("C", 4, W.EVAL_BIT)  # evaluated, no alternative differed
    assert not W.droppable("C", 4, W.EVAL_BIT | bit)
    assert W.droppable("C", 5, W.EVAL_BIT | bit)  # a different alternative differed, not this one
    assert not W.droppable("B", None, W.EVAL_BIT | W.ALT_BIT)
    assert W.droppable("N", None, 0) and not W.droppable("N", None, W.EVAL_BIT)


def test_drops_uses_the_summary_and_the_source_readers():
    old = "def f(x):\n    return x is None\n"
    new = old.replace("is", "==")
    key = (2, 11, 2, 22, "C")
    tree = ast.parse(old)
    node = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)][0]
    key = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, "C")
    p = "sqlglot/x.py"
    same = {7: {p: {key: W.EVAL_BIT}}, 8: {p: {key: W.EVAL_BIT | 1 << 4}}, 9: {}}
    got, note = W.drops(p, None, old, new, same)
    assert got == {7, 9}  # 9 has no entry for the site: it never ran
    got, _ = W.drops(p, None, old, new, same, srcreaders=1 << 7)
    assert got == {9}


# ---- the instrumenter keeps program behaviour and records the right bits -----------------------

@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_OUT", str(tmp_path))
    import importlib

    import wm_runtime

    importlib.reload(wm_runtime)
    import builtins

    builtins._wm_c, builtins._wm_b, builtins._wm_n = wm_runtime.wm_c, wm_runtime.wm_b, wm_runtime.wm_n
    yield wm_runtime
    for n in ("_wm_c", "_wm_b", "_wm_n"):
        delattr(builtins, n)


def run_instrumented(src, rt):
    import wm_transform

    code = wm_transform.compile_instrumented(src.encode(), "m.py", "m.py")
    ns = {}
    exec(code, ns)
    return ns


def bits(rt, kind):
    return {k: v for k, v in rt.SEEN.items() if k.endswith(":" + kind)}


def test_compare_summary_marks_alternatives_that_differ(rt):
    ns = run_instrumented("def f(a, b):\n    return a < b\n", rt)
    assert ns["f"](1, 2) is True
    (mask,) = bits(rt, "C").values()
    assert mask & W.EVAL_BIT
    assert not mask & (1 << 1)  # <= agrees with < on 1,2
    assert not mask & (1 << 5)  # != also gives True on 1,2
    # the equal case makes <= differ
    ns["f"](2, 2)
    (mask,) = bits(rt, "C").values()
    assert mask & (1 << 1)


def test_is_vs_eq_agree_on_singletons_and_differ_on_equal_copies(rt):
    ns = run_instrumented("def f(a, b):\n    return a is b\n", rt)
    ns["f"](None, None)
    (mask,) = bits(rt, "C").values()
    assert not mask & (1 << 4)
    ns["f"]([1], [1])
    (mask,) = bits(rt, "C").values()
    assert mask & (1 << 4)


def test_raising_alternative_counts_as_different(rt):
    ns = run_instrumented("def f(a, b):\n    return a == b\n", rt)
    ns["f"](None, None)
    (mask,) = bits(rt, "C").values()
    assert mask & (1 << 0)  # None < None raises TypeError


def test_boolop_truth_context_and_short_circuit(rt):
    ns = run_instrumented("def f(a, b):\n    if a and b:\n        return 1\n    return 0\n", rt)
    assert ns["f"](1, 1) == 1
    (mask,) = bits(rt, "B").values()
    assert not mask & W.ALT_BIT  # both truthy: or agrees on truth value
    assert ns["f"](1, 0) == 0
    (mask,) = bits(rt, "B").values()
    assert mask & W.ALT_BIT


def test_boolop_unevaluable_operand_is_conservative(rt):
    ns = run_instrumented("def f(a, g):\n    if a and g():\n        return 1\n    return 0\n", rt)
    assert ns["f"](0, lambda: 1) == 0  # `or` would call g(): not pure, so flagged
    (mask,) = bits(rt, "B").values()
    assert mask & W.ALT_BIT


def test_not_records_only_evaluation(rt):
    ns = run_instrumented("def f(a, b):\n    return a and not str(b)\n", rt)
    ns["f"](0, 1)
    assert not bits(rt, "N")
    ns["f"](1, 1)
    assert list(bits(rt, "N").values()) == [W.EVAL_BIT]


def test_reentrant_eq_does_not_recurse(rt):
    ns = run_instrumented(
        "class E:\n    def __eq__(self, o):\n        return self is o or (type(o) is E)\n"
        "def f(a, b):\n    return a is b\n", rt)
    e1, e2 = ns["E"](), ns["E"]()
    assert ns["f"](e1, e2) is False


def test_custom_eq_is_never_called_by_the_recorder(rt):
    ns = run_instrumented(
        "class E:\n    calls = 0\n    def __eq__(self, o):\n        E.calls += 1\n        return True\n"
        "    def __hash__(self):\n        E.calls += 1\n        return 1\n"
        "def f(a, b):\n    return a is b\n", rt)
    e = ns["E"]()
    assert ns["f"](e, None) is False
    assert ns["E"].calls == 0  # the recorder did not run user __eq__/__hash__ (no state change, no caches)
    (mask,) = bits(rt, "C").values()
    assert mask & (1 << 4)  # unknown behaviour is treated as different, so the site is never dropped


def test_enum_and_default_objects_are_safe_to_compare(rt):
    ns = run_instrumented("import enum\nclass K(enum.Enum):\n    A = 1\n    B = 2\ndef f(a, b):\n    return a is b\n", rt)
    ns["f"](ns["K"].A, ns["K"].A)
    ns["f"](ns["K"].A, ns["K"].B)
    (mask,) = bits(rt, "C").values()
    assert not mask & (1 << 4)  # == agrees with is on enum members
    ns["f"](object(), None)
    (mask,) = bits(rt, "C").values()
    assert not mask & (1 << 4)
