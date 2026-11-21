#!/usr/bin/env python3
"""Laptop only. List the names of the string sets the value-read recorder tracks.

    python tools/vm_names.py <sqlglot checkout> > setnames.json

A name qualifies when some module- or class-level assignment of the pinned sqlglot binds it to a set display of string
constants (valrule.set_nodes) and no `import ... as name` renames it (a renamed import would hide its loads).
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "selector"))
import valrule  # noqa: E402

src = sys.argv[1]
names, aliased = set(), set()
for root in ("sqlglot", "tests"):
    for dp, _, fn in os.walk(os.path.join(src, root)):
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            tree = ast.parse(open(p, "rb").read(), p)
            if root == "sqlglot":
                names |= {n for _, _, n in valrule.set_nodes(tree)}
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        if a.asname:
                            aliased.add(a.name.split(".")[-1])
json.dump(sorted(names - aliased), sys.stdout)
