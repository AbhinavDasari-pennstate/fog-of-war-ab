"""Recorder for the weak-mutation map build (laptop only). Injected into builtins by sitecustomize.

Every event that adds a bit to a site's summary is appended to $WM_OUT/wm.<pid>.log as "key mask",
so nothing depends on atexit (pool workers and forked children exit with os._exit).
"""

import os
import operator

OPFN = (operator.lt, operator.le, operator.gt, operator.ge, operator.eq, operator.ne,
        operator.is_, operator.is_not, lambda a, b: a in b, lambda a, b: a not in b)
_SAFE_IN = (list, tuple, set, frozenset, dict)
EVAL = 1 << 10
OUT = os.environ.get("WM_OUT", "")
SEEN = {}
_fd = [None, -1]


def _emit(key, mask):
    pid = os.getpid()
    if _fd[1] != pid:
        _fd[0] = os.open(os.path.join(OUT, f"wm.{pid}.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        _fd[1] = pid
    os.write(_fd[0], f"{key} {mask}\n".encode())


def _note(key, bits):
    m = SEEN.get(key, 0)
    if bits & ~m:
        m |= bits
        SEEN[key] = m
        _emit(key, m)


_SCALARS = (type(None), bool, int, float, str, bytes)
_CMP = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
_TYPE_SAFE = {}
HEAP = 1 << 9
import enum as _enum


def _builtin_cmp(t):
    """True when every comparison dunder of t is implemented by a builtin type (no sqlglot code runs)."""
    ok = _TYPE_SAFE.get(t)
    if ok is None:
        ok = True
        for name in _CMP + ("__hash__",):
            for k in t.__mro__:
                if name in k.__dict__:
                    # static (non heap) builtin types, or the enum module's own pure methods
                    if (k.__flags__ & HEAP) and not (k.__module__ == "enum" and getattr(_enum, k.__name__, None) is k):
                        ok = False
                    break
        _TYPE_SAFE[t] = ok
    return ok


def _sf(x):
    """Comparing x with another safe value runs no user code and changes no state (rule c)."""
    t = type(x)
    if t in _SCALARS or t is type:
        return True
    if t in (tuple, list, frozenset, set):
        return len(x) <= 64 and all(type(e) in _SCALARS for e in x)
    if t is dict:
        return len(x) <= 64 and all(type(k) in _SCALARS and type(v) in _SCALARS for k, v in x.items())
    return _builtin_cmp(t)


def _pair_safe(a, b):
    """A scalar against any static builtin type never reaches user code (`[x] == None`, `{} < 3`)."""
    ta, tb = type(a), type(b)
    return (ta in _SCALARS and not tb.__flags__ & HEAP) or (tb in _SCALARS and not ta.__flags__ & HEAP)


def _alt(opi, a, b, r):
    m = 0
    safe = _pair_safe(a, b) or (_sf(a) and _sf(b))
    for j in range(10):
        if j == opi:
            continue
        if not safe or (j >= 8 and not (type(b) in (str, bytes, range) or (type(b) in _SAFE_IN and _sf(b)))):
            m |= 1 << j
            continue
        try:
            v = OPFN[j](a, b)
        except BaseException:
            m |= 1 << j
            continue
        if v is not r:
            m |= 1 << j
    return m


_busy = [False]
ALL = (1 << 10) - 1


def wm_m(key):
    """Marks the site as reached, BEFORE its operands run (an operand that raises still counts as ran). Returns False."""
    if not SEEN.get(key, 0) & EVAL:
        _note(key, EVAL)
    return False


def wm_c(key, opi, taint, a, b):
    r = OPFN[opi](a, b)
    if _busy[0]:
        return r
    if taint:  # an operand may come from hash()/id(): its order alternatives are process dependent
        _note(key, EVAL | ALL)
        return r
    _busy[0] = True
    try:
        bits = EVAL | _alt(opi, a, b, r)
    except BaseException:
        bits = EVAL | ALL
    finally:
        _busy[0] = False
    m = SEEN.get(key, 0)
    if bits & ~m:
        _note(key, bits)
    return r
