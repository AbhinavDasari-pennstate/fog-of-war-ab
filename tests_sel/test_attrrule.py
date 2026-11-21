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
