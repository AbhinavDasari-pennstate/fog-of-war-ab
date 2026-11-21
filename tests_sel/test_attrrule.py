"""L2 rule: which patches qualify for the attribute-read narrowing (selector/attrrule.py)."""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
import attrrule  # noqa: E402

OLD = '''
from x import exp

class Dialect:
    FLAG = True

class MySQL(Dialect):
    SAFE = True
    NAMES = {"a": 1}
    Generator = MyGen

    class Tokenizer(Base):
        QUOTES = ["'"]

    def f(self):
        return 1
'''


def run(old, new):
    return attrrule.find(ast.parse(old), ast.parse(new), old)


def test_literal_flip_qualifies():
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = False")) == (8, "MySQL", "SAFE")


def test_nested_class_qualname():
    assert run(OLD, OLD.replace("[\"'\"]", "['\"']"))[1:] == ("MySQL.Tokenizer", "QUOTES")


def test_generator_none_is_a_simple_value():
    # find() qualifies it; the derived table (class creation reads Generator) is what keeps it wide
    assert run(OLD, OLD.replace("Generator = MyGen", "Generator = None"))[1:] == ("MySQL", "Generator")


def test_name_not_in_old_text_does_not_qualify():
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = Nope")) is None


def test_call_or_lambda_value_does_not_qualify():
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = f()")) is None
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = lambda: 1")) is None


def test_two_changes_do_not_qualify():
    new = OLD.replace("SAFE = True", "SAFE = False").replace("FLAG = True", "FLAG = False")
    assert run(OLD, new) is None


def test_function_edit_does_not_qualify():
    assert run(OLD, OLD.replace("return 1", "return 2")) is None


def test_private_and_module_level_do_not_qualify():
    assert run("class A:\n    _X = 1\n", "class A:\n    _X = 2\n") is None
    assert run("X = 1\n", "X = 2\n") is None


def test_bases_change_does_not_qualify():
    assert run(OLD, OLD.replace("class MySQL(Dialect)", "class MySQL(Other)")) is None


def test_module_of():
    assert attrrule.module_of("sqlglot/dialects/mysql.py") == "sqlglot.dialects.mysql"
    assert attrrule.module_of("sqlglot/dialects/__init__.py") == "sqlglot.dialects"


def test_bare_name_use_in_the_class_body_does_not_qualify():
    # the value flows into another statement at class creation (reviewer m1, m19, m21)
    old = "class A:\n    BASE = {'a': 1}\n    FULL = {**BASE, 'b': 2}\n"
    assert run(old, old.replace("'a': 1", "'a': 2")) is None
    old = "class A:\n    N = {'a': 'x'}\n    INV = {v: k for k, v in N.items()}\n"
    assert run(old, old.replace("'x'", "'y'")) is None
    old = "class A:\n    N = 1\n    def f(self, x=N):\n        pass\n"
    assert run(old, old.replace("N = 1", "N = 2")) is None
    old = "class A:\n    N = 1\n    if N:\n        M = 2\n"
    assert run(old, old.replace("N = 1", "N = 0")) is None


def test_own_name_inside_its_own_value_is_not_a_bare_use():
    old = "class A:\n    N = {'a': 1}\n    M = 3\n"
    assert run(old, old.replace("'a': 1", "'a': 2")) is not None


def test_value_that_raises_at_import_does_not_qualify():
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = {[]}")) is None
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = {[1]: 2}")) is None
    assert run(OLD, OLD.replace("SAFE = True", "SAFE = {1, 2}")) is not None
    assert run(OLD, OLD.replace("NAMES = {\"a\": 1}", "NAMES = {**Dialect.X, \"a\": 2}")) is None  # Dialect.X not in old text
