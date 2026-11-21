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
    "ensure_tables": (
        "sqlglot/executor/table.py",
        "            result[table_name] = table\n",
        "            result[table_name] = Table(columns=table.columns, rows=table.rows[1:])\n",
        True, "stage O feeding E: the tables the executor gets lose a row (the optimized tree is untouched)",
    ),
    "find_tables": (
        "sqlglot/expressions/builders.py",
        "if isinstance(table, Table) and table.name and table.name not in scope.cte_sources\n",
        "if isinstance(table, Table) and table.name and table.name not in scope.cte_sources and table.name != \"item\"\n",
        True, "stage O feeding E: find_tables skips a table",
    ),
    "unbound": (
        "sqlglot/executor/table.py",
        "            rows = [tuple(row[name] for name in column_names) for row in table]\n",
        "            Table = [tuple(row[name] for name in column_names) for row in table]\n",
        True, "a statement no query reaches that makes Table local to the whole function (UnboundLocalError)",
    ),
    "constant_harmless": (
        "sqlglot/dialects/mysql.py",
        "    SAFE_DIVISION = True\n",
        "    SAFE_DIVISION = False\n",
        False, "class-level constant of a dialect no TPC-DS query uses: the real test still passes",
    ),
    "optimizer_noop": (
        "sqlglot/optimizer/annotate_types.py",
        "        if selects is None:\n",
        "        if selects == None:\n",
        False, "optimizer-only line that changes nothing: the real test passes, the replay runs O only",
    ),
    "seed_flaky": (
        "sqlglot/optimizer/eliminate_subqueries.py",
        "            if scope is cte_scope:\n",
        "            if not scope is cte_scope:\n",
        True, "optimizer-only line; query 53 fails under 7 of 10 PYTHONHASHSEEDs and passes under the others (synth-dev-1-0140)",
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
