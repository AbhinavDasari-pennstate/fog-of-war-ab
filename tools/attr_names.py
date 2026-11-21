#!/usr/bin/env python3
"""Laptop only. List every name assigned directly in a class body of the pinned sqlglot (and tests).

    python tools/attr_names.py <sqlglot checkout> > names.json

The attribute-read recorder (tools/attrsite) rewrites a `.name` load only for these names: a patch
whose only change is a class-level assignment can only touch an attribute that is one of them
(a name outside the list makes the selector keep today's rule). Names starting with an underscore
are left out on purpose; the selector never narrows them.
"""
import ast
import json
import os
import sys


# read on every expression node: recording them would cost more than it can save, and no dialect class defines them
HOT = frozenset({"arg_types", "key"})


def class_assigned(tree):
    out = set()

    def stmts(body):
        for s in body:
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            out.add(n.id)
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
                out.add(s.target.id)
            elif isinstance(s, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for f in ("body", "orelse", "finalbody"):
                    stmts(getattr(s, f, []))
                for h in getattr(s, "handlers", []):
                    stmts(h.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stmts(node.body)
    return out


def main():
    src = sys.argv[1]
    names = set()
    for root in ("sqlglot",):
        for dp, dn, fn in os.walk(os.path.join(src, root)):
            for f in fn:
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    names |= class_assigned(ast.parse(open(p, "rb").read(), p))
    names = sorted(n for n in names if not n.startswith("_") and n not in HOT)
    json.dump(names, sys.stdout)


if __name__ == "__main__":
    main()
