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
    assert W.droppable("B", None, 0) and not W.droppable("B", None, W.EVAL_BIT)  # and/or: only a site that never ran
    assert W.droppable("N", None, 0) and not W.droppable("N", None, W.EVAL_BIT)
    assert not W.droppable("C", 4, W.EVAL_BIT | W.UNKNOWN_BIT) and not W.droppable("C", 4, W.UNKNOWN_BIT)


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

    builtins._wm_c, builtins._wm_m = wm_runtime.wm_c, wm_runtime.wm_m
    yield wm_runtime
    for n in ("_wm_c", "_wm_m"):
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


def test_boolop_records_only_that_it_ran_and_does_so_before_its_operands(rt):
    ns = run_instrumented("def f(a, g):\n    if a and g():\n        return 1\n    return 0\n", rt)
    assert not bits(rt, "B")
    assert ns["f"](0, lambda: 1) == 0
    assert list(bits(rt, "B").values()) == [W.EVAL_BIT]

    def boom():
        raise ValueError

    ns = run_instrumented("def f(a, g):\n    return a < g()\n", rt)
    rt.SEEN.clear()
    with pytest.raises(ValueError):
        ns["f"](1, boom)
    assert any(m & W.EVAL_BIT for m in rt.SEEN.values())  # an operand that raises still counts as ran


def test_hash_operands_set_every_bit(rt):
    ns = run_instrumented("def f(a, b):\n    return hash(a) == hash(b)\n", rt)
    ns["f"](1, 1)
    (mask,) = bits(rt, "C").values()
    assert mask & 0x3FF == 0x3FF


def test_not_records_only_evaluation(rt):
    ns = run_instrumented("def f(a, b):\n    return a and not str(b)\n", rt)
    ns["f"](0, 1)
    assert not bits(rt, "N")  # `a` is falsy: the `not` operand never ran
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


def test_container_against_none_is_safe_even_with_user_elements(rt):
    ns = run_instrumented(
        "class E:\n    calls = 0\n    def __eq__(self, o):\n        E.calls += 1\n        return True\n"
        "    __hash__ = None\n"
        "def f(a, b):\n    return a is b\n", rt)
    ns["f"]([ns["E"]()], None)
    ns["f"]({1: ns["E"]()}, None)
    assert ns["E"].calls == 0
    (mask,) = bits(rt, "C").values()
    assert not mask & (1 << 4)  # list == None agrees with `is`, without calling any element


# ---- the rule on the committed maps and the organiser sample -------------------------------------
from conftest import KIT, PIN, ROOT, needs_kit, needs_pin  # noqa: E402

GIANTS = {1490, 1106, 1224, 1107, 1123}


@pytest.fixture(scope="module")
def real():
    import json

    sys.path.insert(0, str(ROOT))
    from maps.load import load

    maps = load(str(ROOT / "maps"))
    if not maps.weakmut:
        pytest.skip("maps/weakmut.bin not built")
    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((KIT / "sample" / "answers.json").read_text("utf-8"))["commits"]
    return maps, tests, answers


def sample_select(real, cid, weak=True):
    import diffparse
    import rules

    maps, tests, _ = real
    full = maps.weakmut
    maps._weakmut = full if weak else {}
    try:
        text = (KIT / "sample" / "commits" / f"{cid}.patch").read_bytes().decode("utf-8")
        return rules.select(diffparse.parse(text), maps, tests, src_root=PIN)
    finally:
        maps._weakmut = full


@needs_pin
@needs_kit
def test_sample_is_to_eq_commits_drop_the_tpcds_giant(real):
    for cid in ("2haz", "66ks"):  # `is` -> `==` on an enum member / on None
        on, why = sample_select(real, cid)
        off, _ = sample_select(real, cid, weak=False)
        assert 1106 in off and 1106 not in on, why


@needs_pin
@needs_kit
def test_sample_never_drops_a_test_that_really_fails(real):
    _, _, answers = real
    for cid, a in answers.items():
        on, _ = sample_select(real, cid)
        assert set(a["failing"]) <= on, cid


@needs_pin
@needs_kit
def test_a_mutation_a_giant_feels_keeps_the_giant(real):
    on, _ = sample_select(real, "3utz")  # `and` -> `or`, tpch and tpcds really fail
    assert {1106, 1107} <= on


# ---- compact whole-suite profile (format 3) ---------------------------------------------------------

def _profile_case():
    old = "def f(x):\n    return x is None\n"
    new = old.replace("is", "==")
    node = [n for n in ast.walk(ast.parse(old)) if isinstance(n, ast.Compare)][0]
    key = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, "C")
    return old, new, key


def test_profile_drops_match_the_dict_form_and_keep_unrecorded_tests():
    old, new, key = _profile_case()
    p = "sqlglot/x.py"
    # tests 7: ran, == agrees; 8: ran, == differs; 9: recorded, never ran here; 10: not recorded at all; 11: unknown
    prof = W.Profile(
        (1 << 7) | (1 << 8) | (1 << 9) | (1 << 11),
        {p: {key: [(W.EVAL_BIT, 1 << 7), (W.EVAL_BIT | 1 << 4, 1 << 8), (W.EVAL_BIT | W.UNKNOWN_BIT, 1 << 11)]}},
    )
    got, _ = W.drops(p, None, old, new, prof)
    assert got == {7, 9}  # 10 was never recorded: never dropped; 8 differs; 11 unknown
    got, _ = W.drops(p, None, old, new, prof, srcreaders=1 << 7)
    assert got == {9}


def test_profile_never_drops_and_or_not_sites_that_ran():
    p = "sqlglot/x.py"
    old, new = "def f(a, b):\n    return a and b\n", "def f(a, b):\n    return a or b\n"
    node = [n for n in ast.walk(ast.parse(old)) if isinstance(n, ast.BoolOp)][0]
    key = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, "B")
    prof = W.Profile((1 << 3) | (1 << 4), {p: {key: [(W.EVAL_BIT, 1 << 3)]}})
    got, _ = W.drops(p, None, old, new, prof)
    assert got == {4}  # 3 ran the site; 4 never did


def test_empty_profile_is_false():
    assert not W.Profile(0, {})
