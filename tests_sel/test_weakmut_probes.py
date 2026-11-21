"""Hand probes for weak mutation: record a workload on the original, decide with the rule, run the mutant for real.

Law under test: whenever the rule says DROP, the mutant's observable results equal the original's.
Each probe says whether DROP or KEEP is expected, so the rule is also checked for not being vacuous."""

import ast
import builtins
import importlib
import sys
from pathlib import Path

import pytest

import weakmut as W

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "weakmut_site"))


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_OUT", str(tmp_path))
    import wm_runtime

    importlib.reload(wm_runtime)
    builtins._wm_c, builtins._wm_m = wm_runtime.wm_c, wm_runtime.wm_m
    yield wm_runtime
    for n in ("_wm_c", "_wm_m"):
        delattr(builtins, n)


def outcomes(ns, calls):
    out = []
    for args in calls:
        try:
            out.append(("ok", ns["f"](*args)))
        except Exception as e:  # a raising mutant is a different outcome
            out.append(("raise", type(e).__name__))
    return out


def summary(rt, src, calls):
    import wm_transform

    ns = {}
    exec(wm_transform.compile_instrumented(src.encode(), "m.py", "m.py"), ns)
    outcomes(ns, calls)
    per = {}
    for k, m in rt.SEEN.items():
        rel, l, c, el, ec, kind = k.rsplit(":", 5)
        per.setdefault(rel, {})[(int(l), int(c), int(el), int(ec), kind)] = m
    return per


def decide(rt, old, new, calls):
    per = summary(rt, old, calls)
    got, _ = W.drops("m.py", None, old, new, {1: per})
    return 1 in got


C = [(1, 2), (2, 2), (3, 2)]
PROBES = [
    # (id, original body, mutated body, calls, expected DROP?)
    ("lt_to_le_never_equal", "return a < b", "return a <= b", [(1, 2), (3, 2)], True),
    ("lt_to_le_equal_seen", "return a < b", "return a <= b", C, False),
    ("eq_to_ne", "return a == b", "return a != b", C, False),
    ("is_to_eq_none", "return a is None", "return a == None", [(None, 0), (1, 0), ([], 0)], True),
    ("is_to_isnot", "return a is None", "return a is not None", [(None, 0)], False),
    ("in_to_notin", "return a in (1, 2)", "return a not in (1, 2)", [(1, 0)], False),
    ("and_to_or_both_true_still_kept", "return 1 if a and b else 0", "return 1 if a or b else 0", [(1, 2), (3, 4)], False),
    ("and_to_or_unreached", "return 1 if a else (a and b)", "return 1 if a else (a or b)", [(1, 2)], True),
    ("nested_boolop_value_context", "return (a and 5) or b", "return (a or 5) or b", [(1, 2)], False),
    ("skipped_side_effect_operand", "return not a and h(b)", "return not a or h(b)", [(1, 2)], False),
    ("hash_compare", "return hash(a) == hash(b)", "return hash(a) >= hash(b)", [(1, 1)], False),
    ("and_to_or_differs", "return 1 if a and b else 0", "return 1 if a or b else 0", [(1, 0), (1, 2)], False),
    ("and_to_or_value_context", "return a and b", "return a or b", [(1, 2)], False),
    ("not_removed_unreached", "return b if a else (not b)", "return b if a else b", [(1, 5)], True),
    ("not_removed_reached", "return not a", "return a", [(1, 5)], False),
]


@pytest.mark.parametrize("name,old_b,new_b,calls,expect", PROBES, ids=[p[0] for p in PROBES])
def test_drop_decision_matches_real_run(rt, name, old_b, new_b, calls, expect):
    old = f"def h(x):\n    return x\ndef f(a, b):\n    {old_b}\n"
    new = f"def h(x):\n    return x\ndef f(a, b):\n    {new_b}\n"
    drop = decide(rt, old, new, calls)
    assert drop is expect
    if drop:  # the law: a dropped mutant behaves exactly like the original
        a, b = {}, {}
        exec(old, a)
        exec(new, b)
        assert outcomes(a, calls) == outcomes(b, calls)


FALLBACKS = [
    ("constant", "def f(a, b):\n    return a + 1\n", "def f(a, b):\n    return a + 2\n"),
    ("index", "def f(a, b):\n    return a[0]\n", "def f(a, b):\n    return a[1]\n"),
    ("bool_constant", "def f(a, b):\n    return True\n", "def f(a, b):\n    return False\n"),
    ("class_level", "class K:\n    X = 1\ndef f(a, b):\n    return K.X\n", "class K:\n    X = 2\ndef f(a, b):\n    return K.X\n"),
    ("module_level", "Y = 3 < 4\ndef f(a, b):\n    return Y\n", "Y = 3 <= 4\ndef f(a, b):\n    return Y\n"),
    ("default_arg", "def f(a, b=(1 < 2)):\n    return b\n", "def f(a, b=(1 <= 2)):\n    return b\n"),
    ("multi_site", "def f(a, b):\n    return a < b, b < a\n", "def f(a, b):\n    return a <= b, b <= a\n"),
    ("chained_compare", "def f(a, b):\n    return 0 < a < b\n", "def f(a, b):\n    return 0 < a <= b\n"),
    ("regrouped_boolop", "def f(a, b):\n    return a and b and a\n", "def f(a, b):\n    return a and b or a\n"),
    ("add_not", "def f(a, b):\n    return a < b\n", "def f(a, b):\n    return not a < b\n"),
]


@pytest.mark.parametrize("name,old,new", FALLBACKS, ids=[f[0] for f in FALLBACKS])
def test_unrecognised_shapes_are_kept(rt, name, old, new):
    # a summary that says "evaluated, nothing differs" must still not drop a shape the rule does not model
    key_all = {}
    ot = ast.parse(old)
    for n in ast.walk(ot):
        if hasattr(n, "lineno") and isinstance(n, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            for kind in "CBN":
                key_all[(n.lineno, n.col_offset, n.end_lineno, n.end_col_offset, kind)] = W.EVAL_BIT
    got, _ = W.drops("m.py", None, old, new, {1: {"m.py": key_all}})
    assert got == set()


# ---- the reviewer's three proven misses, on the committed profile and the pinned sqlglot -------------

from conftest import PIN, ROOT, needs_pin  # noqa: E402

REGRESSIONS = [
    # (file, line, old text, new text, test number that really failed)
    ("sqlglot/parser.py", 9203, '"CURRENT ROW")', None, 1440),
    ("sqlglot/parser.py", 5072, "not schema and ", "not schema or ", 1295),
    ("sqlglot/expressions/core.py", 838, "hash(self) == hash(other)", "hash(self) >= hash(other)", 1382),
]


@needs_pin
@pytest.mark.parametrize("path,line,old,new,test", REGRESSIONS, ids=["value-context-and-or", "skipped-side-effect", "hash-order"])
def test_proven_misses_are_kept(path, line, old, new, test):
    sys.path.insert(0, str(ROOT))
    from maps.load import load

    maps = load(str(ROOT / "maps"))
    if not maps.weakmut:
        pytest.skip("maps/weakmut.bin not built")
    text = (PIN / path).read_text("utf-8")
    lines = text.split("\n")
    if new is None:  # the and->or at parser.py:9203
        assert " and " in lines[line - 1]
        lines[line - 1] = lines[line - 1].replace(" and ", " or ", 1)
    else:
        assert old in lines[line - 1]
        lines[line - 1] = lines[line - 1].replace(old, new, 1)
    got, _ = W.drops(path, None, text, "\n".join(lines), maps.weakmut)
    assert test not in got
