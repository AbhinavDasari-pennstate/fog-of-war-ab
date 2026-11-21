"""V rule: which patches qualify for the dict-entry value narrowing (selector/valrule.py), and what the recorder
(tools/valmap_site/vm_runtime.py) counts as a read of a value."""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "valmap_site"))
import pyscope  # noqa: E402
import valrule  # noqa: E402

OLD = '''
TOP = {"a": 1, "b": (2, [3, 4]), "c": -5}

class Base:
    ESC = {"x": (16, 2), "u": (16, 4), "0": (8, 1)}
    MIXED = {"p": f(), "q": 1}
    SPREAD = {**TOP, "z": 1}
    DUP = {"k": 1, "k": 2}
    KEYS = {1: "one", 2: "two"}
    arg_types = {"this": True, "expression": False}

    class Inner:
        N = {"s": "text"}

    def f(self):
        return {"local": 1}

NAMED = {exp.A: 1, exp.B.C: "y"}
'''


def run(old, new):
    ot, nt = ast.parse(old), ast.parse(new)
    diffs = pyscope.ast_diff(ot, nt, [(i + 1, i + 1) for i in range(len(old.splitlines()))])
    return valrule.entries(ot, nt, diffs)


def test_value_constant_in_a_class_dict_qualifies():
    assert run(OLD, OLD.replace('"u": (16, 4)', '"u": (17, 4)')) == [(5, "'u'", False)]


def test_truth_flag_for_a_bool_flip_and_for_a_number_change():
    assert run(OLD, OLD.replace('"expression": False', '"expression": True'))[0][2] is True
    assert run(OLD, OLD.replace('"u": (16, 4)', '"u": (16, 5)'))[0][2] is False


def test_module_level_nested_tuple_and_list():
    assert run(OLD, OLD.replace("[3, 4]", "[3, 9]")) == [(2, "'b'", False)]
    assert run(OLD, OLD.replace('"c": -5', '"c": -6')) == [(2, "'c'", False)]


def test_nested_class_dict_qualifies():
    assert run(OLD, OLD.replace('"s": "text"', '"s": "texts"')) == [(13, "'s'", False)]


def test_two_entries_of_one_dict():
    got = run(OLD, OLD.replace('"x": (16, 2)', '"x": (16, 3)').replace('"0": (8, 1)', '"0": (8, 2)'))
    assert sorted(g[1] for g in got) == ["'0'", "'x'"]


def test_a_key_edit_does_not_qualify():
    assert run(OLD, OLD.replace('"u": (16, 4)', '"v": (16, 4)')) is None
    assert run(OLD, OLD.replace('1: "one"', '3: "one"')) is None


def test_a_call_entry_does_not_qualify_but_a_literal_entry_beside_it_does():
    assert run(OLD, OLD.replace('"p": f()', '"p": g()')) is None
    assert run(OLD, OLD.replace('"q": 1', '"q": 2')) == [(6, "'q'", False)]


def test_spread_entries_are_allowed_and_duplicate_keys_are_not():
    assert run(OLD, OLD.replace('"z": 1', '"z": 2')) == [(7, "'z'", False)]
    assert run(OLD, OLD.replace('"k": 2', '"k": 3')) is None


def test_keys_written_as_names_are_labelled_by_their_source_text():
    assert run(OLD, OLD.replace('exp.B.C: "y"', 'exp.B.C: "z"')) == [(18, "@exp.B.C", False)]
    assert run(OLD, OLD.replace("exp.A: 1", "exp.A: 2")) == [(18, "@exp.A", False)]
    assert run(OLD, OLD.replace("exp.A: 1", "exp.D: 1")) is None


def test_function_local_dict_is_not_a_site():
    assert run(OLD, OLD.replace('{"local": 1}', '{"local": 2}')) is None


def test_a_changed_shape_does_not_qualify():
    assert run(OLD, OLD.replace('"u": (16, 4)', '"u": (16, 4, 4)')) is None
    assert run(OLD, OLD.replace('"c": -5', '"c": 5')) is None
    assert run(OLD, OLD.replace('"a": 1', '"a": x')) is None


def test_a_bool_and_an_int_are_different_values():
    assert run("D = {'a': True}\n", "D = {'a': 1}\n") == [(1, "'a'", False)]


def test_site_nodes_names_the_statements():
    t = ast.parse(OLD)
    got = [(s.lineno, q, n) for s, q, n in valrule.site_nodes(t)]
    assert got == [(2, "", "TOP"), (5, "Base", "ESC"), (6, "Base", "MIXED"), (7, "Base", "SPREAD"), (9, "Base", "KEYS"),
                   (10, "Base", "arg_types"), (13, "Base.Inner", "N"), (18, "", "NAMED")]


# ---- the map side ------------------------------------------------------------------------------------------

def make_val(tmp_path, **kw):
    import marshal
    import zlib

    d = {"v": 1, "sets": [0, 0b0110, 0b1000, 0b10000], "reads": {"m.py:5": {"'u'": 1, "'x'": 2}, "m.py:10": {"M:'expression'": 3}},
         "derived": {"m.py:6": ["'q'"], "m.py:9": ["*"], "m.py:10": []}, "sites": ["m.py:5", "m.py:6", "m.py:9", "m.py:10", "m.py:13"],
         "req": ["m.py:10"], "unrecorded": [7], "runs": 3}
    d.update(kw)
    (tmp_path / "valreads.bin").write_bytes(zlib.compress(marshal.dumps(d)))
    return valrule.Val(str(tmp_path))


def test_readers_union_with_unrecorded(tmp_path):
    v = make_val(tmp_path)
    assert v.readers("m.py", 5, "'u'") == 0b0110 | (1 << 7)
    assert v.readers("m.py", 5, "'0'") == 1 << 7  # nobody read it


def test_derived_key_or_whole_dict_and_unknown_site_fall_back(tmp_path):
    v = make_val(tmp_path)
    assert v.readers("m.py", 6, "'q'") is None
    assert v.readers("m.py", 9, "'one'") is None
    assert v.readers("m.py", 99, "'u'") is None


def test_req_site_adds_the_missing_arg_tests_only_when_the_truth_changes(tmp_path):
    v = make_val(tmp_path)
    assert v.readers("m.py", 10, "'expression'", False) == 1 << 7
    assert v.readers("m.py", 10, "'expression'", True) == 0b10000 | (1 << 7)


def test_no_map_file_means_today_rule(tmp_path):
    assert valrule.Val(str(tmp_path)).readers("m.py", 5, "'u'") is None


# ---- the recorder ------------------------------------------------------------------------------------------

@pytest.fixture
def rec(tmp_path, monkeypatch):
    import vm_runtime as rt

    log = []
    monkeypatch.setattr(rt, "_emit", lambda tag, site, key: log.append((tag, site, key)))
    monkeypatch.setattr(rt, "KEY", "tests/t.py::T::t1")
    monkeypatch.setattr(rt, "IMPORTING", 0)
    rt._SEEN.clear()
    d = rt._vw({"a": 1, "b": 2, "c": 3}, "m.py:1")
    log.clear()
    return d, log


def entries_read(log):
    return {k for t, s, k in log if t == "tests/t.py::T::t1"}


def test_item_and_get_read_only_that_entry(rec):
    d, log = rec
    d["a"]
    d.get("b")
    d.get("zz")
    assert entries_read(log) == {"'a'", "'b'"}


def test_keys_len_contains_iteration_read_no_value(rec):
    d, log = rec
    list(d)
    list(d.keys())
    len(d)
    "a" in d
    sorted(d)
    set(d)
    assert log == []


ALL = {"'a'", "'b'", "'c'"}


@pytest.mark.parametrize("op, want", [
    (lambda d: dict(d), ALL), (lambda d: {**d}, ALL), (lambda d: dict(**d), ALL), (lambda d: {}.update(d), ALL),
    (lambda d: d.copy(), {"*"}), (lambda d: list(d.items()), {"*"}), (lambda d: list(d.values()), {"*"}),
    (lambda d: d | {}, {"*"}), (lambda d: {} | d, {"*"}), (lambda d: d == {}, {"*"}), (lambda d: repr(d), {"*"}),
    (lambda d: str(d), {"*"}), (lambda d: __import__("copy").copy(d), {"*"}), (lambda d: __import__("copy").deepcopy(d), {"*"}),
    (lambda d: __import__("json").dumps(d), {"*"}), (lambda d: __import__("pickle").dumps(d), {"*"}),
    (lambda d: sorted(d.items()), {"*"}), (lambda d: d.popitem(), {"*"}),
    (lambda d: "%(a)s" % d, {"'a'"}), (lambda d: "{a}".format_map(d), {"'a'"}),
    (lambda d: max(d, key=d.get), ALL),
])
def test_every_way_to_read_values_is_seen(rec, op, want):
    d, log = rec
    op(d)
    got = entries_read(log)
    assert want <= got and got <= ALL | {"*"}


def test_a_read_during_import_or_outside_a_test_is_derived(rec, monkeypatch):
    import vm_runtime as rt

    d, log = rec
    monkeypatch.setattr(rt, "IMPORTING", 1)
    d["a"]
    monkeypatch.setattr(rt, "IMPORTING", 0)
    monkeypatch.setattr(rt, "KEY", None)
    rt._SEEN.clear()
    d["b"]
    assert [(t, k) for t, s, k in log] == [("D", "'a'"), ("D", "'b'")]


def test_sitecreation_is_logged_once(monkeypatch):
    import vm_runtime as rt

    log = []
    monkeypatch.setattr(rt, "_emit", lambda tag, site, key: log.append((tag, site, key)))
    monkeypatch.setattr(rt, "_CREATED", set())
    rt._vw({"a": 1}, "n.py:3")
    rt._vw({"a": 1}, "n.py:3")
    assert log == [("S", "n.py:3", "")]


# ---- the rule inside rules.select, on the real maps and the pinned source ---------------------------------------

from conftest import KIT, PIN, ROOT, make_patch, needs_kit, needs_pin  # noqa: E402


@pytest.fixture(scope="module")
def env():
    import json

    from maps.load import load
    import diffparse
    import rules

    sys.path.insert(0, str(ROOT))
    if not (ROOT / "maps" / "valreads.bin").exists():
        pytest.skip("no value-read map")
    tests = json.loads((KIT / "sample" / "tests.json").read_text("utf-8"))["tests"]
    return load(str(ROOT / "maps")), tests, diffparse, rules


def select(env, rel, old_s, new_s):
    maps, tests, diffparse, rules = env
    old = (PIN / rel).read_text("utf-8")
    assert old.count(old_s) == 1, (rel, old_s)
    got, why = rules.select(diffparse.parse(make_patch(rel, old, old.replace(old_s, new_s))), maps, tests, src_root=PIN)
    return got, why, {t["id"]: t["number"] for t in tests}


@needs_pin
@needs_kit
def test_unread_numeric_escape_selects_nothing(env):
    # sample commit 4d63: nobody tokenizes \u in a Snowflake string
    got, why, _ = select(env, "sqlglot/dialects/snowflake.py", '"u": (16, 4, 4, 0xFFFF), "0": (8, 1, 3, 0xFF)}', '"u": (17, 4, 4, 0xFFFF), "0": (8, 1, 3, 0xFF)}')
    assert why.startswith("V ") and got == set()


@needs_pin
@needs_kit
def test_read_numeric_escape_selects_its_readers(env):
    got, why, num = select(env, "sqlglot/dialects/bigquery.py", '"x": (16, 2, 2, 0xFF),\n            "X"', '"x": (17, 2, 2, 0xFF),\n            "X"')
    assert why.startswith("V ") and 0 < len(got) < 20


@needs_pin
@needs_kit
def test_optional_arg_made_required_keeps_the_tests_that_validated_an_instance_without_it(env):
    # a flip that changes bool(value) adds the tests whose validated instance had no value for the key
    maps, tests, diffparse, rules = env
    old_s = 'arg_types = {"this": True, "expression": False, "else_": False}'
    keep, why1, _ = select(env, "sqlglot/expressions/query.py", old_s, 'arg_types = {"this": 1, "expression": False, "else_": False}')
    flip, why2, _ = select(env, "sqlglot/expressions/query.py", old_s, 'arg_types = {"this": True, "expression": True, "else_": False}')
    assert why1.startswith("V ") and why2.startswith("V ")
    assert keep <= flip


def test_labels_for_named_keys_and_spread_sites(rec, monkeypatch):
    import vm_runtime as rt

    d, log = rec
    A, B = object(), object()
    base = rt._vw({"s": 1}, "m.py:5")
    log.clear()
    sub = rt._vw({**base, A: 1, B: 2}, "m.py:9", (A, B), ("@exp.A", "@exp.B"), True)
    # the spread read the base (derived or test read, per key), the new dict is labelled by source text
    assert {k for t, s, k in log if s == "m.py:5"} == {"'s'"}
    log.clear()
    sub[A]
    sub["s"]
    assert [(s, k) for t, s, k in log] == [("m.py:9", "@exp.A"), ("m.py:9", "?'s'")]


def test_two_spellings_of_one_key_make_the_site_unusable(monkeypatch):
    import vm_runtime as rt

    log = []
    monkeypatch.setattr(rt, "_emit", lambda tag, site, key: log.append((tag, site, key)))
    monkeypatch.setattr(rt, "_CREATED", set())
    A = object()
    rt._vw({A: 1}, "m.py:3", (A, A), ("@x.A", "@x.B"), False)
    assert ("B", "m.py:3", "") in log


# ---- string sets ---------------------------------------------------------------------------------------------

SET_OLD = '''
A = {"x", "y"}

class G:
    RESERVED = {"varchar", "select", "from"}
    OTHER = {1, 2}
    DUP = {"a", "a"}
'''


def set_run(old, new):
    ot, nt = ast.parse(old), ast.parse(new)
    diffs = pyscope.ast_diff(ot, nt, [(i + 1, i + 1) for i in range(len(old.splitlines()))])
    return valrule.set_entries(ot, nt, diffs)


def test_set_word_rename_names_both_words():
    assert set_run(SET_OLD, SET_OLD.replace('"varchar"', '"zarchar"')) == [(5, ["'varchar'", "'zarchar'"])]
    assert set_run(SET_OLD, SET_OLD.replace('"x"', '"w"')) == [(2, ["'x'", "'w'"])]


def test_set_edit_outside_eligible_sets_does_not_qualify():
    assert set_run(SET_OLD, SET_OLD.replace("{1, 2}", "{1, 3}")) is None
    assert set_run(SET_OLD, SET_OLD.replace('"a", "a"', '"a", "b"')) == [(7, ["'a'", "'b'"])]


def test_set_sites():
    assert [(s.lineno, q, n) for s, q, n in valrule.set_nodes(ast.parse(SET_OLD))] == [(2, "", "A"), (5, "G", "RESERVED"), (7, "G", "DUP")]


def test_set_readers(tmp_path):
    v = make_val(tmp_path, sites=["m.py:5", "m.py:2"], reads={"m.py:5": {"'varchar'": 1, "*": 3}}, derived={"m.py:2": ["'x'"]})
    assert v.set_readers("m.py", 5, ["'varchar'", "'zarchar'"]) == 0b10110 | (1 << 7)
    assert v.set_readers("m.py", 5, ["'select'"]) == 0b10000 | (1 << 7)
    assert v.set_readers("m.py", 2, ["'x'", "'w'"]) is None  # a derived word
    assert v.set_readers("m.py", 9, ["'x'"]) is None  # unknown set


@pytest.fixture
def setrec(monkeypatch):
    import vm_runtime as rt

    log = []
    monkeypatch.setattr(rt, "_emit", lambda tag, site, key: log.append((tag, site, key)))
    monkeypatch.setattr(rt, "KEY", "tests/t.py::T::t1")
    monkeypatch.setattr(rt, "IMPORTING", 0)
    rt._SEEN.clear()
    s = rt._sw({"aa", "bb"}, "m.py:1")
    log.clear()
    return rt, s, log


def test_membership_records_the_word_and_nothing_else(setrec):
    rt, s, log = setrec
    assert rt._mem("aa", s) is True and rt._mem("zz", s) is False
    assert type(s) is set
    assert [(k) for t, site, k in log] == ["'aa'", "'zz'"]


def test_every_other_load_is_a_whole_read(setrec):
    rt, s, log = setrec
    rt._bulk(s)
    assert log[-1][2] == "*"
    log.clear()
    rt._SEEN.clear()
    class C:
        pass
    C.N = s
    rt._gets(C, "N")
    assert log[-1][2] == "*"
    log.clear()
    rt._SEEN.clear()
    rt._sd(C).get("N")
    assert log[-1][2] == "*"
    log.clear()
    rt._SEEN.clear()
    list(rt._sv(C).items())
    assert log[-1][2] == "*"


def test_transform_rewrites_loads_and_membership():
    import vm_transform as vt

    src = "def f(self, x):\n    a = x in self.S\n    b = x not in S\n    c = self.S | y\n    d = getattr(self, 'S')\n    e = vars(self)\n    g = self.__dict__\n"
    tree = vt.rewrite_set_loads(ast.parse(src), {"S"})
    out = ast.unparse(tree)
    assert "_mem(x, self.S)" in out and "not _mem(x, S)" in out and "_bulk(self.S) | y" in out
    assert "_gets(self" in out and "_sv(self)" in out and "_sd(self)" in out


def test_format_time_calls_are_recorded_by_string(monkeypatch):
    import types

    import vm_runtime as rt

    log = []
    monkeypatch.setattr(rt, "_emit", lambda tag, site, key: log.append((tag, site, key)))
    monkeypatch.setattr(rt, "KEY", "tests/t.py::T::t1")
    monkeypatch.setattr(rt, "IMPORTING", 0)
    rt._SEEN.clear()
    mod = types.SimpleNamespace(format_time=lambda s, m, t=None: s.upper())
    rt.install_time(mod)
    assert mod.format_time("%Y", {}) == "%Y".upper()
    assert log == [("tests/t.py::T::t1", "FT", "'%Y'")]


def test_time_mapping_tokens_pick_the_format_strings_that_contain_them(tmp_path):
    v = make_val(tmp_path, sites=["m.py:5"], reads={"FT": {"'%Y-%m'": 1, "'dd'": 2}, "m.py:5": {}}, derived={}, req2=["m.py:5"], strict=["%mstrict"])
    assert v.readers("m.py", 5, "'Y'", False, frozenset({"%Y", "%R"}), 1 << 9) == 0b0110 | (1 << 7) | (1 << 9)
    assert v.readers("m.py", 5, "'Y'", False, frozenset({"zz"}), 0) == 1 << 7  # no recorded string contains it
    assert v.readers("m.py", 5, "'Y'", False, frozenset(), 0) is None
