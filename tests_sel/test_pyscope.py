import ast

import pyscope


def diff(old, new, ranges):
    return pyscope.ast_diff(pyscope.parse(old), pyscope.parse(new), ranges)


def test_comment_and_blank_edits_leave_no_difference():
    old = "x = 1\ny = 2\n"
    assert diff(old, "x = 1\n# note\n\ny = 2\n", [(1, 2)]) == []


def test_operator_swap_is_a_hard_difference():
    got = diff("a = b and c\n", "a = b or c\n", [(1, 1)])
    assert got and all(lam is None for _, lam in got)


def test_edit_inside_a_lambda_body_is_deferred_to_that_lambda():
    old = "T = {'a': lambda s: (\n    s.f(1) if s.x else s.g()\n)}\n"
    new = old.replace("if s.x", "if not s.x")
    got = diff(old, new, [(2, 2)])
    assert got and all(lam is not None and lam.lineno == 1 for _, lam in got)


def test_lambda_default_argument_is_not_deferred():
    got = diff("T = lambda s, k=1: s\n", "T = lambda s, k=2: s\n", [(1, 1)])
    assert got and all(lam is None for _, lam in got)


def test_decorator_change_is_seen_even_though_it_sits_above_the_def_line():
    old = "class A:\n    @staticmethod\n    def f():\n        pass\n"
    new = old.replace("staticmethod", "classmethod")
    assert diff(old, new, [(2, 2)])


def test_one_vs_true_constants_differ():
    assert diff("x = 1\n", "x = True\n", [(1, 1)])


def test_inserted_statement_changes_the_list_length():
    assert diff("a = 1\nb = 2\n", "a = 1\nc = 3\nb = 2\n", [(1, 2)])


def test_compiles_catches_compile_time_errors_the_parser_accepts():
    tree = ast.parse("return 1\n")
    assert not pyscope.compiles(tree)
    assert pyscope.compiles(ast.parse("def f():\n    return 1\n"))


def test_default_only_names_the_def_whose_literal_default_changed():
    old = "def f(a, b=1, *, c=None):\n    return a\n\ndef g(x=-1):\n    return x\n"
    new = old.replace("b=1", "b=2").replace("x=-1", "x=0")
    got = pyscope.default_only(pyscope.parse(old), pyscope.parse(new))
    assert [f.name for f in got] == ["f", "g"]


def test_default_only_refuses_anything_else():
    old = "def f(a, b=1):\n    return a\n"
    for new in ("def f(a, b=1):\n    return b\n",           # body changed too
                "def f(a, b=g()):\n    return a\n",         # default is no longer a literal
                "def f(a, b=1, c=2):\n    return a\n",      # a default was added
                "def f(a, b):\n    return a\n",             # a default was removed
                "def f(a, b=1):\n    return a\n"):          # nothing changed
        assert pyscope.default_only(pyscope.parse(old), pyscope.parse(new)) is None


def test_default_only_leaves_the_trees_as_found():
    old, new = pyscope.parse("def f(b=1):\n    pass\n"), pyscope.parse("def f(b=2):\n    pass\n")
    before = (pyscope.dump(old), pyscope.dump(new))
    pyscope.default_only(old, new)
    assert (pyscope.dump(old), pyscope.dump(new)) == before
