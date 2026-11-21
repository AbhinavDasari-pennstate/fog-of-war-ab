import json
from pathlib import Path

import pytest

import main as selector_main
from conftest import FIXTURES, PIN, make_patch, needs_pin, tiny_tests

TESTS = tiny_tests(40)
ALL = {t["number"] for t in TESTS}


def flat(workers):
    return [n for w in workers for n in w]


def make_src(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def decide(tmp_path, rel, old, new):
    src = make_src(tmp_path, {rel: old})
    return selector_main.decide(make_patch(rel, old, new), src, TESTS)


def assert_full(workers):
    assert sorted(flat(workers)) == sorted(ALL)
    assert 1 <= len([w for w in workers if w]) <= 8


@needs_pin
def test_real_comment_patch_is_empty():
    patch = (FIXTURES / "9u5k.patch").read_text()
    assert selector_main.decide(patch, PIN, TESTS) == []


@needs_pin
def test_real_code_patch_runs_every_test_once():
    patch = (FIXTURES / "5y72.patch").read_text()
    assert_full(selector_main.decide(patch, PIN, TESTS))


@needs_pin
def test_real_fixture_patch_runs_everything():
    patch = (FIXTURES / "2vdj.patch").read_text()
    assert_full(selector_main.decide(patch, PIN, TESTS))


OLD = "def f(x):\n    return x + 1\n"


def test_blank_line_insert_is_empty(tmp_path):
    assert decide(tmp_path, "sqlglot/a.py", OLD, "def f(x):\n\n    return x + 1\n") == []


def test_code_change_is_full(tmp_path):
    assert_full(decide(tmp_path, "sqlglot/a.py", OLD, OLD.replace("+ 1", "+ 2")))


def test_comment_in_typing_is_not_empty(tmp_path):
    assert_full(decide(tmp_path, "sqlglot/typing/x.py", OLD, "# note\n" + OLD))


def test_comment_in_sqlglotc_is_not_empty(tmp_path):
    assert_full(decide(tmp_path, "sqlglotc/y.py", OLD, "# note\n" + OLD))


def test_hash_line_inside_string_is_not_empty(tmp_path):
    old = 'S = """\nselect 1\n"""\n'
    new = 'S = """\n# note\nselect 1\n"""\n'
    assert_full(decide(tmp_path, "sqlglot/a.py", old, new))


def test_docstring_edit_is_not_empty(tmp_path):
    old = 'def f():\n    """doc"""\n    return 1\n'
    new = 'def f():\n    """doc2"""\n    return 1\n'
    assert_full(decide(tmp_path, "sqlglot/a.py", old, new))


def test_syntax_error_is_full(tmp_path):
    assert_full(decide(tmp_path, "sqlglot/a.py", OLD, OLD + "def (:\n"))


def test_coding_cookie_is_full(tmp_path):
    assert_full(decide(tmp_path, "sqlglot/a.py", OLD, "# coding: latin-1\n" + OLD))


def test_non_py_file_is_full(tmp_path):
    assert_full(decide(tmp_path, "README.md", "a\n", "b\n"))


def test_comment_plus_code_in_two_files_is_full(tmp_path):
    src = make_src(tmp_path, {"sqlglot/a.py": OLD, "sqlglot/b.py": OLD})
    patch = make_patch("sqlglot/a.py", OLD, "# c\n" + OLD) + make_patch(
        "sqlglot/b.py", OLD, OLD.replace("+ 1", "+ 2")
    )
    assert_full(selector_main.decide(patch, src, TESTS))


def test_comments_in_two_files_is_empty(tmp_path):
    src = make_src(tmp_path, {"sqlglot/a.py": OLD, "sqlglot/b.py": OLD})
    patch = make_patch("sqlglot/a.py", OLD, "# c\n" + OLD) + make_patch(
        "sqlglot/b.py", OLD, OLD + "# d\n"
    )
    assert selector_main.decide(patch, src, TESTS) == []


def test_patch_that_does_not_apply_is_full(tmp_path):
    src = make_src(tmp_path, {"sqlglot/a.py": "totally different\n"})
    assert_full(selector_main.decide(make_patch("sqlglot/a.py", OLD, "# c\n" + OLD), src, TESTS))


def test_missing_source_file_is_full(tmp_path):
    assert_full(selector_main.decide(make_patch("sqlglot/gone.py", OLD, "# c\n" + OLD), tmp_path, TESTS))


def test_garbage_patch_is_full(tmp_path):
    assert_full(selector_main.decide("not a patch at all\n", tmp_path, TESTS))


def test_new_file_is_full(tmp_path):
    patch = (
        "diff --git a/sqlglot/new.py b/sqlglot/new.py\nnew file mode 100644\n"
        "index 0000000..1111111\n--- /dev/null\n+++ b/sqlglot/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    )
    assert_full(selector_main.decide(patch, tmp_path, TESTS))


def test_main_prints_only_answer_lines(tmp_path, capsys):
    data = tmp_path / "data"
    (data / "commits").mkdir(parents=True)
    (data / "tests.json").write_text(json.dumps({"tests": TESTS}))
    src = make_src(tmp_path / "src", {"sqlglot/a.py": OLD})
    (data / "commits" / "aaaa.patch").write_text(make_patch("sqlglot/a.py", OLD, "# c\n" + OLD))
    (data / "commits" / "bbbb.patch").write_text(
        make_patch("sqlglot/a.py", OLD, OLD.replace("+ 1", "+ 2"))
    )
    selector_main.main(data, src)
    out, err = capsys.readouterr()
    lines = [json.loads(l) for l in out.splitlines()]
    assert [l["commit"] for l in lines] == ["aaaa", "bbbb"]
    assert all(set(l) == {"commit", "workers"} for l in lines)
    assert lines[0]["workers"] == []
    assert sorted(flat(lines[1]["workers"])) == sorted(ALL)
    assert err.strip() != ""


def _run_main(tmp_path, capsys, patches: dict[str, bytes]):
    data = tmp_path / "data"
    (data / "commits").mkdir(parents=True)
    (data / "tests.json").write_text(json.dumps({"tests": TESTS}))
    src = make_src(tmp_path / "src", {"sqlglot/a.py": OLD, "sqlglot/parser.py": "x = 1\n# c\ny = 2\n"})
    for name, body in patches.items():
        (data / "commits" / f"{name}.patch").write_bytes(body)
    selector_main.main(data, src)
    out, _ = capsys.readouterr()
    return [json.loads(l) for l in out.splitlines()]


def test_non_utf8_patch_still_gives_one_valid_line_per_commit(tmp_path, capsys):
    bad = make_patch("sqlglot/a.py", OLD, "# caf\xe9\n" + OLD).encode("latin-1")
    good = make_patch("sqlglot/a.py", OLD, "# c\n" + OLD).encode()
    lines = _run_main(tmp_path, capsys, {"aaaa": bad, "bbbb": good})
    assert [l["commit"] for l in lines] == ["aaaa", "bbbb"]
    assert sorted(flat(lines[0]["workers"])) == sorted(ALL)
    assert lines[1]["workers"] == []


def test_exception_in_one_commit_still_prints_a_full_line(tmp_path, capsys, monkeypatch):
    real = selector_main.decide

    def boom(text, src, tests, *rest):
        if "boom" in text:
            raise RuntimeError("boom")
        return real(text, src, tests, *rest)

    monkeypatch.setattr(selector_main, "decide", boom)
    p = make_patch("sqlglot/a.py", OLD, "# boom\n" + OLD).encode()
    q = make_patch("sqlglot/a.py", OLD, "# c\n" + OLD).encode()
    lines = _run_main(tmp_path, capsys, {"aaaa": p, "bbbb": q})
    assert sorted(flat(lines[0]["workers"])) == sorted(ALL)
    assert lines[1]["workers"] == []


def test_lone_cr_in_added_line_is_not_empty(tmp_path, capsys):
    patch = (
        "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\nindex 1..2 100644\n"
        "--- a/sqlglot/parser.py\n+++ b/sqlglot/parser.py\n@@ -1,3 +1,3 @@\n x = 1\n"
        "-# c\n+# c\rraise_now()\n y = 2\n"
    ).encode()
    lines = _run_main(tmp_path, capsys, {"aaaa": patch})
    assert sorted(flat(lines[0]["workers"])) == sorted(ALL)


def test_stray_lines_after_a_complete_hunk_are_rejected():
    import diffparse
    patch = (
        "diff --git a/sqlglot/a.py b/sqlglot/a.py\n--- a/sqlglot/a.py\n+++ b/sqlglot/a.py\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n+raise_now()\n"
    )
    with pytest.raises(diffparse.PatchError):
        diffparse.parse(patch)
