"""Loader for the committed maps (stdlib only; each .bin is zlib-compressed marshal).

    from maps.load import load
    m = load()                       # or load("/app/maps")
    m.lines["sqlglot/optimizer/eliminate_joins.py"][71]   # -> set id (0 = executed at import only)
    m.mask(set_id)                   # -> int bitmask over test numbers

Test numbers are the numbers in tests.json (bit n = test number n). `m.ids[n]` is the
test's node id, so a caller can check that /data/tests.json still agrees.

Tables (every value that is a "set" is an int bitmask, already resolved):
  lines      file -> {line: set id}      lines run by any test or at import; id 0 = import only
  sets       list of int bitmasks        sets[set_id]
  files      file -> mask                tests that ran any line of the file in a test context
  classes    (file, class qualname) -> mask   tests that ran any line inside the class body
  reads      path -> mask                tests that opened the (non-.py) file; setUpClass and
                                          collection-time reads count for the whole class/module
  srcreaders path -> mask                tests that read the text of a .py/sqlglotc file
  imports    file -> mask                tests that import the module, cold: static + runtime of their file, plus lazy imports (lazyimp.bin)
  names_class qualname -> mask           tests that instantiated the class or a subclass, or ran
                                          any line inside it (any file)
  names_attr  name -> mask               tests that ran any line that mentions the name, for names
                                          defined at module or class level (lazy-import fallback)
  stmts      file -> (starts, ends, scope_ids, scopes), see stmt()
  flaky      set of test numbers whose outcome differed across 3 clean runs
  importtime file -> set of lines executed at import/collection time (property, built on first use)
  unstable   file -> set of lines whose test set differed between the build runs
  ids        list, test number -> node id (check it against /data/tests.json)
"""

from __future__ import annotations

import bisect
import marshal
import os
import sys
import zlib

_DIR = os.path.dirname(os.path.abspath(__file__))


def _read(d: str, name: str):
    with open(os.path.join(d, name), "rb") as f:
        return marshal.loads(zlib.decompress(f.read()))


class Maps:
    def __init__(self, d: str):
        self.dir = d
        self._mentions = None
        self.sets = _read(d, "sets.bin")
        sets = self.sets
        self.lines = _read(d, "lines.bin")
        self.ids = _read(d, "ids.bin")
        self.files = {k: sets[v] for k, v in _read(d, "files.bin").items()}
        self.classes = {k: sets[v] for k, v in _read(d, "classes.bin").items()}
        self.reads = {k: sets[v] for k, v in _read(d, "reads.bin").items()}
        self.srcreaders = {k: sets[v] for k, v in _read(d, "srcreaders.bin").items()}
        self.imports = {k: sets[v] for k, v in _read(d, "imports.bin").items()}
        # cold import sets per test, lazy imports included (tools/lazyimp.py): only ever adds tests
        lp = os.path.join(d, "lazyimp.bin")
        if os.path.exists(lp):
            for k, v in _read(d, "lazyimp.bin").items():
                self.imports[k] = self.imports.get(k, 0) | v
        names = _read(d, "names.bin")
        self.names_class = {k: sets[v] for k, v in names["class"].items()}
        self.names_attr = {k: sets[v] for k, v in names["attr"].items()}
        self.stmts = _read(d, "stmts.bin")
        self._importtime = _read(d, "importtime.bin")
                # lines whose test set differed between build runs (lazy or cached state): treat as wide
        self.unstable = {k: set(v) for k, v in _read(d, "unstable.bin").items()}
        num = {t: n for n, t in enumerate(self.ids) if t is not None}
        self._num = num
        self.flaky = set()
        p = os.path.join(d, "flaky.txt")
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line in num:
                        self.flaky.add(num[line])

    @property
    def seqcredit(self):
        """file -> lines that only the sequential and post-clear coverage passes credited to a test, every earlier run having them
        import-only (tools/seqcredit.py): the credit is the first importer's, not a dependency."""
        w = getattr(self, "_seqcredit", None)
        if w is None:
            w = {}
            try:
                if os.path.exists(os.path.join(self.dir, "seqcredit.bin")):
                    w = {k: set(v) for k, v in _read(self.dir, "seqcredit.bin").items()}
            except Exception:
                w = {}
            self._seqcredit = w
        return w

    @property
    def impexec(self):
        """(file -> modules, class qualname -> modules, file -> [(first line, last line, modules)], state) from impexec.bin (tools/impexec.py): the modules whose body, while it
        imports, ran a function of that file / a method of that class. state is "absent" (no file: nothing to add) or "bad" (damaged:
        the selector widens). A one-line edit of a table or function such a call reads changes what the importer does, or whether it
        imports at all; the tests that import the module (self.imports, lazy imports included) are the ones it can fail."""
        w = getattr(self, "_impexec", None)
        if w is None:
            w = ({}, {}, {}, "absent", frozenset(), None)
            try:
                if os.path.exists(os.path.join(self.dir, "impexec.bin")):
                    d = _read(self.dir, "impexec.bin")
                    w = (d["file"], d["class"], d["range"], "ok", frozenset(d.get("base", ())), d.get("probe"))
            except Exception:
                w = ({}, {}, {}, "bad", frozenset(), None)
            self._impexec = w
        return w

    @property
    def impfn(self):
        """(file -> [(first line, last line, modules)], frozenset of the files `import sqlglot` loads) from impfn.bin (tools/impfn.py), or None.

        The modules whose body (sqlglot or test module, or the pseudo module "<nomod>" for a call made by collection itself) ran the function
        or lambda at those lines while the repo imported or the tests were collected, over three hash seeds. None unless the file was recorded
        by THIS interpreter (sys.version is stored), with the collection pass and at least three distinct seeds: the caller then keeps the wide answer."""
        w = getattr(self, "_impfn", 0)
        if w == 0:
            w = None
            try:
                if os.path.exists(os.path.join(self.dir, "impfn.bin")):
                    d = _read(self.dir, "impfn.bin")
                    if d.get("v") == 1 and d.get("py") == sys.version and d.get("collect") and len(set(d.get("seeds", ()))) >= 3:
                        w = (d["range"], frozenset(d.get("base", ())))
            except Exception:
                w = None
            self._impfn = w
        return w

    @property
    def importtime(self):
        """file -> set of lines that ran at import or collection time (before any test). The test
        set of such a line is not the set of tests that depend on it: widen with the name/class/file maps."""
        if isinstance(self._importtime, dict) and not getattr(self, "_it_built", False):
            self._importtime = {k: set(v) for k, v in self._importtime.items()}
            self._it_built = True
        return self._importtime

    @property
    def mentions(self):
        """(files, {name: [file_index * 100000 + line]}, {(qualifier, name): [...]}): class- and module-level lines of sqlglot/ that
        mention a name defined at class or module level (its own definitions excluded)."""
        if self._mentions is None:
            p = os.path.join(self.dir, "mentions.bin")
            if os.path.exists(p):
                d = _read(self.dir, "mentions.bin")
                self._mentions = (d["files"], d["mentions"], d["qual"])
            else:
                self._mentions = ([], {}, {})
        return self._mentions

    @property
    def weakmut(self):
        """{test number: {path: {(line, col, end_line, end_col, kind): mask}}} from weakmut.bin (selector/weakmut.py);
        empty when the file is missing or a test id disagrees with ids.bin."""
        w = getattr(self, "_weakmut", None)
        if w is None:
            w = {}
            try:
                if os.path.exists(os.path.join(self.dir, "weakmut.bin")):
                    d = _read(self.dir, "weakmut.bin")
                    self.weakmut_rec = d.get("rec")
                    if d.get("fmt") == "sites":  # compact profile of many tests (selector/weakmut.py Profile)
                        import sys

                        sys.path.insert(0, os.path.join(os.path.dirname(self.dir), "selector"))
                        import weakmut as _W

                        ok = 0
                        for n, tid in d["ids"].items():
                            if n < len(self.ids) and self.ids[n] == tid:
                                ok |= 1 << n
                        w = _W.Profile(d["recorded"] & ok, d["sites"])
                    else:
                        for n, rec in d.get("tests", {}).items():
                            if n < len(self.ids) and self.ids[n] == rec["id"]:
                                w[n] = rec["sites"]
            except Exception:
                w = {}  # a damaged file only disables the rule
            self._weakmut = w
        return w

    def mask(self, set_id: int) -> int:
        return self.sets[set_id]

    def number(self, test_id: str):
        return self._num.get(test_id)

    def stmt(self, file: str, line: int):
        """Statement holding `line`: (start, end, kind, def_start, def_end, class_qualname) or None.

        kind is "function", "class" or "module" (the scope the statement runs in; a def or class
        header counts in the enclosing scope, with def_start/def_end or the class name of the
        thing it defines). def_start == 0 means not inside a function.
        """
        rec = self.stmts.get(file)
        if rec is None:
            return None
        starts, ends, sids, scopes = rec
        i = bisect.bisect_right(starts, line) - 1
        if i < 0 or ends[i] < line:
            return None
        kind, d0, d1, cls = scopes[sids[i]]
        return (starts[i], ends[i], kind, d0, d1, cls)

    def stmt_mask(self, file: str, line: int) -> int:
        """Union of the line sets of every line of the statement holding `line` (0 if unknown)."""
        s = self.stmt(file, line)
        lm = self.lines.get(file)
        if s is None or lm is None:
            return 0
        u = 0
        sets = self.sets
        for ln in range(s[0], s[1] + 1):
            sid = lm.get(ln)
            if sid:
                u |= sets[sid]
        return u


def load(d: str | None = None) -> Maps:
    return Maps(d or _DIR)
