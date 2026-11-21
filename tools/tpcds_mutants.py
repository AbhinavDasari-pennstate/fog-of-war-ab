"""Real mutants of the pinned sqlglot that break test_execute_tpcds, for the selector's keep probes.

    python tools/tpcds_mutants.py list
    python tools/tpcds_mutants.py real <name|all> --src <pinned sqlglot> --python <venv python>

`real` copies the tree to a scratch dir, applies the mutant and runs the real
test_execute_tpcds there (about 25 s each); it prints PASS or FAIL. tests_sel/test_tpcds_probes.py
asserts that the selector keeps test 1106 for every mutant marked "fails" and records, for the others,
what the real test did.
"""

import difflib
import os
import shutil
import subprocess
import sys
import tempfile

# name -> (file, old text, new text, fails in the real test, where it acts)
MUTANTS = {
    "executor": (
        "sqlglot/executor/python.py",
        "            join_nulls = (None,) * (len(table.columns) - len(source_rows[0]) if source_rows else 0)\n",
        "            join_nulls = (None,) * (len(table.columns) - len(source_rows[0]) if not source_rows else 0)\n",
        True, "plan and executor (stage E)",
    ),
    "optimizer": (
        "sqlglot/optimizer/qualify_columns.py",
        "        if isinstance(selection, exp.Subquery):\n            if not selection.output_name:",
        "        if not isinstance(selection, exp.Subquery):\n            if not selection.output_name:",
        True, "optimizer (stage O)",
    ),
    "transpile": (
        "sqlglot/generators/duckdb.py",
        "        unnest = expression.args.get(\"unnest\")\n        if unnest:\n            return self.sql(\n                exp.replace_placeholders(",
        "        unnest = expression.args.get(\"unnest\")\n        if not unnest:\n            return self.sql(\n                exp.replace_placeholders(",
        True, "duckdb transpile (stage T)",
    ),
    "state": (
        "sqlglot/executor/__init__.py",
        "    result = PythonExecutor(tables=tables_).execute(plan)\n",
        "    result = PythonExecutor(tables=tables_).execute(plan)\n    for _t in tables_.mapping.values():\n        _t.rows.append(_t.rows[0])\n",
        True, "cross-query state: the query itself is right, the shared table grows for the next one",
    ),
    "classlevel": (
        "sqlglot/generators/python.py",
        "        exp.And: lambda self, e: f\"AND(lambda: {self.sql(e.left)}, lambda: {self.sql(e.right)})\",\n",
        "        exp.And: lambda self, e: f\"OR(lambda: {self.sql(e.left)}, lambda: {self.sql(e.right)})\",\n",
        True, "class-level table of the Python generator",
    ),
}


def patch_text(src, name):
    rel, old, new, _, _ = MUTANTS[name]
    text = open(os.path.join(src, rel), encoding="utf-8").read()
    assert text.count(old) == 1, (name, text.count(old))
    out = text.replace(old, new)
    diff = difflib.unified_diff(text.splitlines(True), out.splitlines(True), f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "".join(diff)


def real(src, python, name):
    rel, old, new, _, _ = MUTANTS[name]
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "tree")
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
        p = os.path.join(dst, rel)
        text = open(p, encoding="utf-8").read()
        open(p, "w", encoding="utf-8").write(text.replace(old, new))
        r = subprocess.run(
            [python, "-m", "pytest", "tests/test_executor.py", "-k", "test_execute_tpcds", "-x", "-q",
             "-p", "no:cacheprovider", "-o", "addopts=", "--no-header"],
            cwd=dst, capture_output=True, text=True, env=dict(os.environ, PYTHON_CPU_COUNT="2"),
        )
        return r.returncode, r.stdout[-400:]


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "list":
        for k, v in MUTANTS.items():
            print(k, v[0], v[4])
    else:
        src = sys.argv[sys.argv.index("--src") + 1]
        py = sys.argv[sys.argv.index("--python") + 1]
        names = list(MUTANTS) if sys.argv[2] == "all" else [sys.argv[2]]
        for n in names:
            code, tail = real(src, py, n)
            print(n, "PASS" if code == 0 else "FAIL", code, tail.strip().splitlines()[-1:] )
