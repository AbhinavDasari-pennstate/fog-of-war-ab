"""Runtime of the attribute-read recorder (map build only, laptop). Injected into builtins by sitecustomize.

Every rewritten `.name` load, getattr/hasattr call and `cls.__dict__` access of the pinned sqlglot (and its
tests) calls into here. A read is recorded as (module:Qualname, name) for EVERY sqlglot class in the MRO of
the class the lookup started from, so an edit of C.name is matched by the tests that read `name` through C or
through any subclass of C (whichever class finally defines it).

Lines go to $ATTR_OUT/attr.<pid>.log as "<K>\t<module:Qualname>\t<name>":
  K = "D"   derived: read while a module body or class body was executing (class creation, metaclass,
            __init_subclass__, import-time tables) or outside any test (collection): a change of that
            attribute reaches every user of the class.
  K = nodeid  the test whose run read it.
"""

import os
import sys
import types

OUT = os.environ.get("ATTR_OUT", "")
_names_file = os.environ.get("ATTR_NAMES", "")
NAMES = frozenset()
if _names_file:
    import json

    with open(_names_file) as _f:
        NAMES = frozenset(json.load(_f))

KEY = os.environ.get("ATTR_KEY") or None  # the test a pool worker or child process works for
IMPORTING = 0
_SEEN_T = set()  # (class object, name) already recorded for the current test
_SEEN_D = set()  # same for derived reads
_ANC = {}  # class -> tuple of "module:Qualname" for every sqlglot class in its MRO
_fd = [None, -1]
_ModuleType = types.ModuleType
_busy = [False]


def _emit(tag, qual, name):
    pid = os.getpid()
    if _fd[1] != pid:
        _fd[0] = os.open(os.path.join(OUT, f"attr.{pid}.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        _fd[1] = pid
    os.write(_fd[0], f"{tag}\t{qual}\t{name}\n".encode())


def _anc(k):
    a = _ANC.get(k)
    if a is None:
        out = []
        try:
            mro = k.__mro__
        except Exception:
            mro = ()
        for c in mro:
            mod = getattr(c, "__module__", "") or ""
            if mod == "sqlglot" or mod.startswith("sqlglot."):
                out.append(f"{mod}:{c.__qualname__}")
        a = _ANC[k] = tuple(out)
    return a


def begin_test(nodeid):
    """Called in the forked child of one test: its reads belong to that test from here on."""
    global KEY
    KEY = nodeid
    _SEEN_T.clear()
    os.environ["ATTR_KEY"] = nodeid


def _note(o, n):
    if type(o) is _ModuleType:
        return
    if type(o) is super:
        try:
            k = o.__self_class__
        except Exception:
            return
    elif isinstance(o, type):
        k = o
    else:
        k = type(o)
    derived = IMPORTING > 0 or KEY is None
    seen = _SEEN_D if derived else _SEEN_T
    ck = (k, n)
    if ck in seen:
        return
    seen.add(ck)
    tag = "D" if derived else KEY
    for q in _anc(k):
        _emit(tag, q, n)


def _ar(o, n):
    try:
        v = getattr(o, n)
    except AttributeError:
        _note(o, n)
        raise
    _note(o, n)
    return v


def _arg(o, n, *d):
    if len(d) > 1:
        return getattr(o, n, *d)
    v = getattr(o, n, *d)
    if isinstance(n, str) and n in NAMES:
        _note(o, n)
    return v


def _arh(o, n):
    r = hasattr(o, n)
    if isinstance(n, str) and n in NAMES:
        _note(o, n)
    return r


def _ard(o):
    """`o.__dict__`: for a class, a view that records the keys looked at (a bulk read records the class itself)."""
    d = o.__dict__
    if isinstance(o, type):
        return _Dict(o, d)
    return d


def _arv(o=None, *rest):
    if rest or o is None:
        return vars(o, *rest)
    return _ard(o) if isinstance(o, type) else vars(o)


class _Dict:
    """Read-through view of a class __dict__ that records the names looked up in it."""

    __slots__ = ("_k", "_d")

    def __init__(self, k, d):
        self._k, self._d = k, d

    def _rec(self, name):
        if isinstance(name, str) and name in NAMES:
            _note(self._k, name)

    def _all(self):
        for name in self._d:
            if isinstance(name, str) and name in NAMES:
                _note(self._k, name)

    def get(self, name, *default):
        self._rec(name)
        return self._d.get(name, *default)

    def __getitem__(self, name):
        self._rec(name)
        return self._d[name]

    def __contains__(self, name):
        self._rec(name)
        return name in self._d

    def __iter__(self):
        self._all()
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def keys(self):
        self._all()
        return self._d.keys()

    def values(self):
        self._all()
        return self._d.values()

    def items(self):
        self._all()
        return self._d.items()

    def copy(self):
        self._all()
        return self._d.copy()

    def __eq__(self, other):
        self._all()
        return self._d == other

    __hash__ = None

    def __repr__(self):
        self._all()
        return repr(self._d)

    def __or__(self, other):
        self._all()
        return self._d | other

    def __ror__(self, other):
        self._all()
        return other | self._d

    def __getattr__(self, name):
        self._all()
        return getattr(self._d, name)
