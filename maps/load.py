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
  imports    file -> mask                tests whose test file imports the module (static + runtime)
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
