"""Laptop-only: turn an instrumented suite run into the selector's maps.

Stages (see plan T3):
  run     run the instrumented suite (tools.covctx, preimport, readaudit, classuse)
  flaky   run the plain suite three times and diff outcomes
  build   post-process a run directory into maps/*.bin, maps/flaky.txt, maps/NOTES.txt

    python tools/build_maps.py build --src SRC --run RUNDIR --tests TESTS.JSON --flaky A.xml B.xml C.xml --out maps

SRC is a checkout of sqlglot at the pinned commit (cwd of the run). Everything the
build writes is deterministic: sets are interned in order of first use over sorted keys.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import glob
import json
import marshal
import os
import re
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor

MAPS_VERSION = 1
_CTX_SPLIT = re.compile(r'(?<=\|run)\||(?<=\|setup)\||(?<=\|teardown)\||(?<=<import>)\|')
RUN_SQLGLOT = 0
RUN_ANY = 0


# --------------------------------------------------------------------------- tests


def load_tests(path: str):
    d = json.load(open(path))
    tests = d["tests"]
    ids = [None] * (max(t["number"] for t in tests) + 1)
    for t in tests:
        ids[t["number"]] = t["id"]
    return ids, {t["id"]: t["number"] for t in tests}


class Groups:
    """nodeid prefixes -> mask of numbered tests below them."""

    def __init__(self, ids):
        self.ids = ids
        self.by_prefix: dict[str, int] = {}
        for n, i in enumerate(ids):
            if i is None:
                continue
            parts = i.split("::")
            for k in range(1, len(parts)):
                p = "::".join(parts[:k])
                self.by_prefix[p] = self.by_prefix.get(p, 0) | (1 << n)

    def group(self, prefix: str) -> int:
        return self.by_prefix.get(prefix, 0)


# --------------------------------------------------------------------------- coverage


def read_coverage(run_dir: str, num_of: dict[str, int], groups: Groups):
    """-> (lines: {file: {line: mask}}, import_only: {file: set(line)}, unmapped: set(nodeid))"""
    from coverage.numbits import numbits_to_nums

    lines: dict[str, dict[int, int]] = {}
    imported: dict[str, set[int]] = {}
    unmapped: set[str] = set()
    global RUN_SQLGLOT, RUN_ANY
    RUN_SQLGLOT = 0  # mask of tests whose own |run context ran a line under sqlglot/
    RUN_ANY = 0  # mask of tests that have a |run context at all
    cmask: dict[str, int | None] = {}

    def ctx_mask(ctx: str):
        if ctx in cmask:
            return cmask[ctx]
        m: int | None
        # pool workers carry "<spawning test ctx>|<task test ctx>": the task's context wins
        parts = _CTX_SPLIT.split(ctx)
        ctx = parts[-1]
        if ctx in ("", "<import>"):
            m = None
        else:
            nodeid, _, phase = ctx.rpartition("|")
            if not nodeid:
                nodeid, phase = ctx, "run"
            if phase == "run":
                n = num_of.get(nodeid)
                m = (1 << n) if n is not None else 0
            else:  # setup / teardown: class (or module) wide
                if nodeid in num_of or groups.group(nodeid.rsplit("::", 1)[0]):
                    m = groups.group(nodeid.rsplit("::", 1)[0]) or (1 << num_of[nodeid])
                else:
                    m = 0
            if m == 0:
                unmapped.add(nodeid)
        cmask[ctx] = m
        return m

    for fn in sorted(glob.glob(os.path.join(run_dir, "cov", ".coverage.*"))):
        con = sqlite3.connect(fn)
        ctxs = dict(con.execute("select id, context from context"))
        files = dict(con.execute("select id, path from file"))
        for file_id, ctx_id, nb in con.execute("select file_id, context_id, numbits from line_bits"):
            path = files[file_id]
            m = ctx_mask(ctxs[ctx_id])
            lm = lines.setdefault(path, {})
            if m is None:
                imp = imported.setdefault(path, set())
                for ln in numbits_to_nums(nb):
                    imp.add(ln)
                    lm.setdefault(ln, 0)
            else:
                for ln in numbits_to_nums(nb):
                    lm[ln] = lm.get(ln, 0) | m
                if ctxs[ctx_id].endswith("|run"):
                    RUN_ANY |= m
                    if path.startswith("sqlglot/"):
                        RUN_SQLGLOT |= m
        con.close()
    return lines, imported, unmapped


# --------------------------------------------------------------------------- statements


def py_files(src: str):
    out = []
    for top in ("sqlglot", "tests", "sqlglotc"):
        for dp, dn, fns in os.walk(os.path.join(src, top)):
            dn.sort()
            for f in sorted(fns):
                if f.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(dp, f), src))
    return sorted(out)


class StmtBuilder(ast.NodeVisitor):
    """Collects non-overlapping statement records: (start, end, scope_id)."""

    def __init__(self):
        self.recs: list[tuple[int, int, int]] = []
        self.scopes: list[tuple] = []  # (kind, def_start, def_end, class_qualname)
        self._scope_ids: dict[tuple, int] = {}
        self.names: list[str] = []  # qualname stack: names; "<locals>" markers
        self.kind = "module"
        self.def_range = (0, 0)
        self.cls = ""

    def sid(self, kind, d0, d1, cls):
        key = (kind, d0, d1, cls)
        if key not in self._scope_ids:
            self._scope_ids[key] = len(self.scopes)
            self.scopes.append(key)
        return self._scope_ids[key]

    def add(self, start, end, kind=None, dr=None, cls=None):
        k = self.kind if kind is None else kind
        d = self.def_range if dr is None else dr
        c = self.cls if cls is None else cls
        self.recs.append((start, end, self.sid(k, d[0], d[1], c)))

    def body(self, stmts):
        for s in stmts:
            self.stmt(s)

    def stmt(self, n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            first = n.body[0].lineno
            hend = first - 1 if first > n.lineno else n.lineno
            hend = max(hend, n.lineno)
            if first == n.lineno:  # one-line def
                self.add(start, n.end_lineno, dr=(n.lineno, n.end_lineno))
                return
            self.add(start, hend, dr=(n.lineno, n.end_lineno))
            saved = (self.kind, self.def_range, list(self.names))
            self.names = self.names + [n.name, "<locals>"]
            self.kind = "function"
            self.def_range = (n.lineno, n.end_lineno)
            self.body(n.body)
            self.kind, self.def_range, self.names = saved
        elif isinstance(n, ast.ClassDef):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            first = n.body[0].lineno
            hend = first - 1 if first > n.lineno else n.lineno
            qual = ".".join(self.names + [n.name])
            if first == n.lineno:
                self.add(start, n.end_lineno, cls=qual)
                return
            self.add(start, max(hend, n.lineno), cls=qual)
            saved = (self.kind, self.cls, list(self.names))
            self.names = self.names + [n.name]
            self.kind, self.cls = "class", qual
            self.body(n.body)
            self.kind, self.cls, self.names = saved
        elif isinstance(n, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.Match)) or (
            hasattr(ast, "TryStar") and isinstance(n, ast.TryStar)
        ):
            self._compound(n)
        else:
            self.add(n.lineno, n.end_lineno)

    def _compound(self, n):
        subs = []  # lists of statements in order
        if isinstance(n, ast.Match):
            first_body = n.cases[0].pattern.lineno if n.cases else n.end_lineno
            self.add(n.lineno, max(n.lineno, first_body - 1 if first_body > n.lineno else n.lineno))
            for c in n.cases:
                b0 = c.body[0].lineno
                self.add(c.pattern.lineno, max(c.pattern.lineno, b0 - 1 if b0 > c.pattern.lineno else c.pattern.lineno))
                self.body(c.body)
            return
        first = n.body[0].lineno
        hend = first - 1 if first > n.lineno else n.lineno
        if first == n.lineno:  # `if x: y`
            hend = n.lineno
            self.add(n.lineno, n.lineno if len(n.body) == 0 else n.lineno)
        else:
            self.add(n.lineno, hend)
        self.body(n.body)
        if isinstance(n, ast.Try) or (hasattr(ast, "TryStar") and isinstance(n, ast.TryStar)):
            for h in n.handlers:
                b0 = h.body[0].lineno
                self.add(h.lineno, max(h.lineno, b0 - 1 if b0 > h.lineno else h.lineno))
                self.body(h.body)
            self.body(n.orelse)
            self.body(n.finalbody)
        else:
            self.body(getattr(n, "orelse", []))


def build_stmts(text: str):
    """-> (starts, ends, scope_ids, scopes) sorted by start, non-overlapping where possible."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    b = StmtBuilder()
    b.body(tree.body)
    recs = sorted(set(b.recs))
    # a one-line `if x: y` can produce a header record that overlaps its body line: keep the first
    out = []
    last_end = 0
    for s, e, sc in recs:
        if s <= last_end and out:
            continue
        out.append((s, e, sc))
        last_end = e
    return [r[0] for r in out], [r[1] for r in out], [r[2] for r in out], b.scopes


# --------------------------------------------------------------------------- imports


def module_name(rel: str) -> str:
    m = rel[:-3].replace(os.sep, ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


def static_imports(src: str, files: list[str]):
    """file -> set of repo files it imports directly (any depth in the AST, incl. function-level)."""
    mod_to_file = {module_name(f): f for f in files}
    pkgs = {module_name(f) for f in files if f.endswith("__init__.py")}
    edges: dict[str, set[str]] = {}

    def resolve(name: str) -> list[str]:
        """module + all parent packages that exist as files"""
        out = []
        parts = name.split(".")
        for k in range(1, len(parts) + 1):
            f = mod_to_file.get(".".join(parts[:k]))
            if f:
                out.append(f)
        return out

    for f in files:
        try:
            tree = ast.parse(open(os.path.join(src, f), encoding="utf-8").read())
        except Exception:
            edges[f] = set()
            continue
        mod = module_name(f)
        pkg = mod if f.endswith("__init__.py") else mod.rpartition(".")[0]
        e: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    e.update(resolve(a.name))
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base_parts = pkg.split(".") if pkg else []
                    base_parts = base_parts[: len(base_parts) - (n.level - 1)] if n.level > 1 else base_parts
                    base = ".".join(base_parts + ([n.module] if n.module else []))
                else:
                    base = n.module or ""
                if base:
                    e.update(resolve(base))
                for a in n.names:
                    if a.name != "*":
                        e.update(resolve(base + "." + a.name if base else a.name))
        edges[f] = e
    return edges


def closure(start: str, edges: dict[str, set[str]]) -> set[str]:
    seen = {start}
    todo = [start]
    while todo:
        f = todo.pop()
        for g in edges.get(f, ()):
            if g not in seen:
                seen.add(g)
                todo.append(g)
    return seen


def runtime_imports(src: str, test_files: list[str], python: str, env: dict) -> dict[str, set[str]]:
    code = (
        "import sys, importlib, json, os\n"
        "root = os.getcwd()\n"
        "importlib.import_module(sys.argv[1])\n"
        "out = []\n"
        "for m in list(sys.modules.values()):\n"
        "    f = getattr(m, '__file__', None)\n"
        "    if f and f.startswith(root + os.sep) and f.endswith('.py'):\n"
        "        out.append(os.path.relpath(f, root))\n"
        "print(json.dumps(sorted(out)))\n"
    )

    def one(tf):
        r = subprocess.run(
            [python, "-c", code, module_name(tf)],
            cwd=src,
            env={**env, "PYTHONPATH": src},
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode != 0:
            return tf, None
        return tf, set(json.loads(r.stdout.strip().splitlines()[-1]))

    out = {}
    with ThreadPoolExecutor(6) as ex:
        for tf, s in ex.map(one, test_files):
            if s is not None:
                out[tf] = s
    return out


# --------------------------------------------------------------------------- flaky


def junit_outcomes(path: str, ids: list[str]) -> dict[str, str]:
    res: dict[str, str] = {}
    tree = ET.parse(path)
    cases: dict[tuple[str, str], str] = {}
    for tc in tree.getroot().iter("testcase"):
        key = (tc.get("classname", ""), tc.get("name", ""))
        o = "pass"
        for ch in tc:
            if ch.tag in ("failure", "error"):
                o = "fail"
            elif ch.tag == "skipped" and o == "pass":
                o = "skip"
        prev = cases.get(key)
        cases[key] = "fail" if "fail" in (prev, o) else (prev or o)
    by_class: dict[str, list[tuple[str, str]]] = {}
    for (cn, nm), o in cases.items():
        by_class.setdefault(cn, []).append((nm, o))
    for i in ids:
        if i is None:
            continue
        f, _, rest = i.partition("::")
        cls, _, name = rest.rpartition("::")
        cn = f[:-3].replace("/", ".") + ("." + cls if cls else "")
        o = "missing"
        for nm, oo in by_class.get(cn, []):
            if nm == name or nm.startswith(name + " ["):
                o = "fail" if "fail" in (o, oo) else (oo if o in ("missing", "pass") else o)
        res[i] = o
    return res


# --------------------------------------------------------------------------- main build


class SetPool:
    def __init__(self):
        self.sets: list[int] = [0]
        self.idx: dict[int, int] = {0: 0}

    def intern(self, mask: int) -> int:
        i = self.idx.get(mask)
        if i is None:
            i = len(self.sets)
            self.sets.append(mask)
            self.idx[mask] = i
        return i


def group_members(key: str, groups: Groups, num_of: dict[str, int]) -> int:
    """readaudit/classuse key -> mask. 't:<nodeid>' one test; 'g:<prefix>' every test under it."""
    kind, _, val = key.partition(":")
    if kind == "t":
        n = num_of.get(val)
        return (1 << n) if n is not None else 0
    if val == "*":
        return 0
    return groups.group(val)


def dump(path: str, obj) -> int:
    data = zlib.compress(marshal.dumps(obj), 9)
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o644)
    return len(data)


IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def defined_names(tree: ast.AST) -> set[str]:
    """Names defined at module or class level: assign targets, def/class names, dict keys."""
    out: set[str] = set()

    def key_name(k):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            return k.value
        if isinstance(k, ast.Attribute):
            return k.attr
        if isinstance(k, ast.Name):
            return k.id
        return None

    def targets(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                targets(e)

    def dict_keys(v):
        for d in ast.walk(v):
            if isinstance(d, ast.Dict):
                for k in d.keys:
                    nm = key_name(k) if k is not None else None
                    if nm and len(nm) < 64:
                        out.add(nm)

    def scan(body):
        for s in body:
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    targets(t)
                dict_keys(s.value)
            elif isinstance(s, ast.AnnAssign):
                targets(s.target)
                if s.value is not None:
                    dict_keys(s.value)
            elif isinstance(s, ast.AugAssign):
                targets(s.target)
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(s.name)
                if isinstance(s, ast.ClassDef):
                    scan(s.body)
            elif isinstance(s, (ast.If, ast.Try, ast.With)):
                scan(s.body)
                scan(getattr(s, "orelse", []))
                for h in getattr(s, "handlers", []):
                    scan(h.body)
                scan(getattr(s, "finalbody", []))

    scan(tree.body)  # type: ignore[attr-defined]
    return {n for n in out if IDENT.fullmatch(n)}


def build(args) -> None:
    src = os.path.abspath(args.src)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    notes: list[str] = []
    ids, num_of = load_tests(args.tests)
    groups = Groups(ids)
    ntests = sum(1 for i in ids if i is not None)

    cov_lines: dict[str, dict[int, int]] = {}
    unmapped: set[str] = set()
    per_run: list[dict[str, dict[int, int]]] = []
    run_sqlglot = 0
    run_any = 0
    imported_all: dict[str, set[int]] = {}
    for rd in args.run:
        cl, imp, um = read_coverage(rd, num_of, groups)
        for f, ls in imp.items():
            imported_all.setdefault(f, set()).update(ls)
        per_run.append(cl)
        unmapped |= um
        run_sqlglot |= RUN_SQLGLOT
        run_any |= RUN_ANY
        for f, lm in cl.items():
            dst = cov_lines.setdefault(f, {})
            for ln, m in lm.items():
                dst[ln] = dst.get(ln, 0) | m
    # lines whose test set differs between runs: lazily built or cached state, or scheduling effects
    unstable: dict[str, list[int]] = {}
    if len(per_run) > 1:
        for f in sorted(cov_lines):
            if not (f.startswith("sqlglot/") or f.startswith("tests/")):
                continue
            bad = []
            for ln in sorted(cov_lines[f]):
                vals = {r.get(f, {}).get(ln) for r in per_run}
                if len(vals) > 1:
                    bad.append(ln)
            if bad:
                unstable[f] = bad
    pool = SetPool()

    # lines.bin, files.bin
    lines_bin: dict[str, dict[int, int]] = {}
    files_bin: dict[str, int] = {}
    for f in sorted(cov_lines):
        if not (f.startswith("sqlglot/") or f.startswith("tests/") or f.startswith("sqlglotc/")):
            continue
        lm = cov_lines[f]
        lines_bin[f] = {ln: pool.intern(lm[ln]) for ln in sorted(lm)}
        u = 0
        for m in lm.values():
            u |= m
        files_bin[f] = pool.intern(u)

    # stmts.bin
    allpy = py_files(src)
    stmts_bin: dict[str, tuple] = {}
    class_ranges: dict[str, list[tuple[str, int, int]]] = {}
    for f in allpy:
        text = open(os.path.join(src, f), encoding="utf-8").read()
        r = build_stmts(text)
        if r is None:
            notes.append(f"stmts: syntax error in {f}")
            continue
        stmts_bin[f] = (r[0], r[1], r[2], r[3])
        # class ranges for classes.bin
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        cr: list[tuple[str, int, int]] = []

        def walk(node, names):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    q = ".".join(names + [ch.name])
                    cr.append((q, ch.lineno, ch.end_lineno))
                    walk(ch, names + [ch.name])
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(ch, names + [ch.name, "<locals>"])
                else:
                    walk(ch, names)

        walk(tree, [])
        class_ranges[f] = cr

    # classes.bin: (file, qualname) -> tests that executed a line inside the class (test contexts)
    classes_bin: dict[tuple[str, str], int] = {}
    for f, cr in class_ranges.items():
        lm = cov_lines.get(f)
        if not lm:
            continue
        for q, a, b in cr:
            u = 0
            for ln, m in lm.items():
                if a <= ln <= b:
                    u |= m
            if u:
                classes_bin[(f, q)] = pool.intern(u)

    # reads.bin and srcreaders.bin
    reads: dict[str, int] = {}
    srcreads: dict[str, int] = {}
    classuse_by_key: dict[str, set[str]] = {}
    for fn in sorted(fn for rd in args.run for fn in glob.glob(os.path.join(rd, "readaudit-*.json"))):
        d = json.load(open(fn))
        for key, paths in d["reads"].items():
            m = group_members(key, groups, num_of)
            if m:
                for p in paths:
                    reads[p] = reads.get(p, 0) | m
        for key, paths in d["srcreads"].items():
            m = group_members(key, groups, num_of)
            if m:
                for p in paths:
                    srcreads[p] = srcreads.get(p, 0) | m
    for fn in sorted(fn for rd in args.run for fn in glob.glob(os.path.join(rd, "classuse-*.json"))):
        d = json.load(open(fn))
        for key, qs in d.items():
            classuse_by_key.setdefault(key, set()).update(qs)
    # static source-reader rules (plan R7b)
    def tests_in(prefix: str) -> int:
        return groups.group(prefix)

    typing_tests = tests_in("tests/test_typing.py")
    for f in allpy:
        if f.startswith("sqlglot/typing/"):
            srcreads[f] = srcreads.get(f, 0) | typing_tests
        if f.startswith("sqlglotc/"):
            srcreads[f] = srcreads.get(f, 0) | tests_in("tests/test_sqlglotc.py")
    srcreads["sqlglot/dialects/__init__.py"] = (
        srcreads.get("sqlglot/dialects/__init__.py", 0)
        | tests_in("tests/test_dialect_imports.py")
        | tests_in("tests/test_dialect_entry_points.py")
    )
    # sqlglotc non-py files (setup.py read by test_sqlglotc is .py; other files via reads)
    reads_bin = {p: pool.intern(m) for p, m in sorted(reads.items())}
    srcreaders_bin = {p: pool.intern(m) for p, m in sorted(srcreads.items())}

    # names.bin
    qual_mask: dict[str, int] = {}
    for key, qs in classuse_by_key.items():
        m = group_members(key, groups, num_of)
        if not m:
            continue
        for mq in qs:
            q = mq.partition(":")[2]
            qual_mask[q] = qual_mask.get(q, 0) | m
    for (f, q), sid in classes_bin.items():
        if f.startswith("sqlglot/"):
            qual_mask[q] = qual_mask.get(q, 0) | pool.sets[sid]
    # attribute / dict-key names defined at module or class level in sqlglot/
    defined: set[str] = set()
    for f in allpy:
        if f.startswith("sqlglot/"):
            try:
                defined |= defined_names(ast.parse(open(os.path.join(src, f), encoding="utf-8").read()))
            except SyntaxError:
                pass
    name_mask: dict[str, int] = {}
    for f, lm in cov_lines.items():
        if not f.startswith("sqlglot/"):
            continue
        lines_text = open(os.path.join(src, f), encoding="utf-8").read().split("\n")
        for ln, m in lm.items():
            if not m or ln > len(lines_text):
                continue
            for tok in set(IDENT.findall(lines_text[ln - 1])):
                if tok in defined:
                    name_mask[tok] = name_mask.get(tok, 0) | m
    # also credit test files' lines? (names used only by tests): tests/ lines naming a sqlglot constant
    for f, lm in cov_lines.items():
        if not f.startswith("tests/"):
            continue
        try:
            lines_text = open(os.path.join(src, f), encoding="utf-8").read().split("\n")
        except OSError:
            continue
        for ln, m in lm.items():
            if not m or ln > len(lines_text):
                continue
            for tok in set(IDENT.findall(lines_text[ln - 1])):
                if tok in defined:
                    name_mask[tok] = name_mask.get(tok, 0) | m
    names_bin = {
        "class": {q: pool.intern(m) for q, m in sorted(qual_mask.items())},
        "attr": {n: pool.intern(m) for n, m in sorted(name_mask.items())},
    }

    # imports.bin
    edges = static_imports(src, allpy)
    test_files = sorted({i.partition("::")[0] for i in ids if i})
    rt = runtime_imports(src, test_files, args.python, {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
    imp_mask: dict[str, int] = {}
    for tf in test_files:
        cl = closure(tf, edges) | rt.get(tf, set())
        m = groups.group(tf)
        for g in cl:
            imp_mask[g] = imp_mask.get(g, 0) | m
    imports_bin = {g: pool.intern(m) for g, m in sorted(imp_mask.items())}
    if len(rt) != len(test_files):
        notes.append(f"imports: runtime import snapshot failed for {sorted(set(test_files) - set(rt))}")

    # flaky
    flaky: list[str] = []
    always_fail: list[str] = []
    if args.flaky:
        outs = [junit_outcomes(p, ids) for p in args.flaky]
        for i in ids:
            if i is None:
                continue
            vals = [o[i] for o in outs]
            if len(set(vals)) > 1:
                flaky.append(i)
            elif vals[0] == "fail":
                always_fail.append(i)
    with open(os.path.join(out_dir, "flaky.txt"), "w") as f:
        f.write("\n".join(sorted(flaky)) + ("\n" if flaky else ""))
    os.chmod(os.path.join(out_dir, "flaky.txt"), 0o644)

    sizes = {
        "lines.bin": dump(os.path.join(out_dir, "lines.bin"), lines_bin),
        "sets.bin": dump(os.path.join(out_dir, "sets.bin"), pool.sets),
        "files.bin": dump(os.path.join(out_dir, "files.bin"), files_bin),
        "classes.bin": dump(os.path.join(out_dir, "classes.bin"), classes_bin),
        "reads.bin": dump(os.path.join(out_dir, "reads.bin"), reads_bin),
        "names.bin": dump(os.path.join(out_dir, "names.bin"), names_bin),
        "srcreaders.bin": dump(os.path.join(out_dir, "srcreaders.bin"), srcreaders_bin),
        "imports.bin": dump(os.path.join(out_dir, "imports.bin"), imports_bin),
        "stmts.bin": dump(os.path.join(out_dir, "stmts.bin"), stmts_bin),
        "ids.bin": dump(os.path.join(out_dir, "ids.bin"), ids),
        "unstable.bin": dump(os.path.join(out_dir, "unstable.bin"), unstable),
        "importtime.bin": dump(
            os.path.join(out_dir, "importtime.bin"),
            {f: sorted(v) for f, v in sorted(imported_all.items()) if f.startswith(("sqlglot/", "tests/"))},
        ),
    }

    # NOTES
    union_all = 0
    for m in pool.sets:
        union_all |= m
    empty = [ids[n] for n in range(len(ids)) if ids[n] and not (run_sqlglot >> n) & 1]
    ran_ctx = sum(1 for n in range(len(ids)) if ids[n] and (run_any >> n) & 1)
    ran = 0
    lines_out = [
        f"maps v{MAPS_VERSION}",
        f"sqlglot pinned: eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        f"tests in tests.json: {ntests}",
        f"tests with a run context in the instrumented run: {ran_ctx} of {ntests}",
        f"runs merged (union of test sets per line): {len(args.run)}",
        f"distinct sets: {len(pool.sets)}",
        f"unstable lines (test set differs between runs): {sum(len(v) for v in unstable.values())} in {len(unstable)} files",
        f"files with line data: {len(lines_bin)}; covered lines: {sum(len(v) for v in lines_bin.values())}",
        "sizes (bytes): " + ", ".join(f"{k}={v}" for k, v in sizes.items()),
        f"total bytes: {sum(sizes.values())}",
        f"flaky tests (differing outcome across {len(args.flaky or [])} clean runs): {len(flaky)}",
    ]
    lines_out += [f"  flaky {t}" for t in sorted(flaky)]
    lines_out.append(f"tests failing in all clean runs on this machine: {len(always_fail)}")
    lines_out += [f"  always-fail {t}" for t in always_fail]
    lines_out.append(f"tests with empty coverage (own run context ran no line under sqlglot/): {len(empty)}")
    lines_out += [f"  empty {t}" for t in empty]
    lines_out.append(f"coverage contexts whose test is not in tests.json: {len(unmapped)}")
    lines_out += [f"  unmapped {t}" for t in sorted(unmapped)[:40]]
    lines_out += notes
    if args.extra:
        lines_out += open(args.extra).read().rstrip("\n").split("\n")
    with open(os.path.join(out_dir, "NOTES.txt"), "w") as f:
        f.write("\n".join(lines_out) + "\n")
    os.chmod(os.path.join(out_dir, "NOTES.txt"), 0o644)
    print("\n".join(lines_out[:12]))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--src", required=True)
    b.add_argument("--run", required=True, nargs="+")
    b.add_argument("--tests", required=True)
    b.add_argument("--flaky", nargs="*", default=[])
    b.add_argument("--out", required=True)
    b.add_argument("--extra", default="", help="text file appended to NOTES.txt")
    b.add_argument("--python", default=sys.executable)
    args = ap.parse_args()
    if args.cmd == "build":
        build(args)


if __name__ == "__main__":
    main()
