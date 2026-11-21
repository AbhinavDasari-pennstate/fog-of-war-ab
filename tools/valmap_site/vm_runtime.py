"""Runtime of the value-read recorder (map build only, laptop). Injected into builtins by sitecustomize.

Every module-level or class-level assignment in the pinned sqlglot whose value is a dict display with
distinct constant keys is rewritten to `_vw({...}, "<path>:<line>")`, which builds a RecDict instead of a dict.
A RecDict records WHICH ENTRY VALUES are read:

  d[k], d.get(k), d.pop(k), d.setdefault(k)   the value of k
  d.items(), d.values(), d.copy(), d | x, x | d, d == x, repr(d), d.popitem(), dict(d), {**d}, f(**d), update(d), copy.copy(d)
                                              every value (a bulk read: the values are consumed in bulk)
  iteration, keys(), len(), `k in d`          nothing (keys are not values)

`__iter__` is overridden so that CPython's C fast paths for dict subclasses (dict(d), {**d}, update, f(**d),
PyDict_Merge) are NOT taken: without a custom tp_iter they read the storage directly and an override of
__getitem__ would never run. With it, they call keys() and then __getitem__ for each key, which records.
Code that still reads the storage raw (PyDict_Next on a non-exact dict) is rejected by CPython itself in the
cases that matter (str.maketrans, marshal) and fails the test, which the map builder then treats as unrecorded.

Two more kinds of line exist for the dicts that Expression subclasses call `arg_types` (see valrule.py):
  K = "R"      the values were read in bulk by `Expr.__init_subclass__` (sqlglot/expressions/core.py), the one place
               that turns them into `required_args = {k for k, v in cls.arg_types.items() if v}`; counted as "R"
               instead of "D" because the only effect of a value is whether its key is in required_args
  key "M:<k>"  Expression.error_messages ran on an instance of a class whose arg_types is this dict, and the
               instance had no value (None or an empty list) for key k: the one case where k being required matters

Lines go to $VM_OUT/vm.<pid>.log as "<K>\t<path>:<line>\t<key repr>" with key repr "*" for a bulk read:
  K = "D"      read while a module or class body was running, or outside any test: reaches every user
  K = nodeid   the test whose run read it
  K = "B"      the statement's key spellings do not map one to one onto the runtime keys: the site is unusable
  K = "T"      (key repr empty) this test started and finished its protocol (so its absence of reads means something)
"""

import os
import sys
import types

OUT = os.environ.get("VM_OUT", "")
KEY = os.environ.get("VM_KEY") or None  # the test a pool worker or child process works for
IMPORTING = 0
_SEEN = set()
_fd = [None, -1]
_busy = [False]


def _emit(tag, site, key):
    pid = os.getpid()
    if _fd[1] != pid:
        _fd[0] = os.open(os.path.join(OUT, f"vm.{pid}.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        _fd[1] = pid
    os.write(_fd[0], f"{tag}\t{site}\t{key}\n".encode())


def begin_test(nodeid):
    global KEY
    KEY = nodeid
    _SEEN.clear()
    os.environ["VM_KEY"] = nodeid
    _emit("T", nodeid, "")


def _note(rd, key, miss=False):
    site = rd._vs
    derived = IMPORTING > 0 or KEY is None
    tag = "D" if derived else KEY
    if derived and not miss and key == "*":
        f = sys._getframe(2)
        if f.f_code.co_name == "__init_subclass__" and f.f_code.co_filename.endswith("sqlglot/expressions/core.py"):
            tag = "R"
    ck = (tag, site, key if key == "*" or not isinstance(key, (dict, list, set)) else "*", miss)
    if ck in _SEEN:
        return
    _SEEN.add(ck)
    try:
        if key == "*" and not miss:
            r = "*"
        else:
            lb = rd._lb
            r = lb.get(key) if lb is not None else None
            if r is None:
                r = repr(key) if lb is None else "?" + repr(key)
            if miss:
                r = "M:" + r
    except Exception:
        r = "*"
    _emit(tag, site, r)


def _miss(self):
    d = type(self).arg_types
    site = getattr(d, "_vs", None)
    if site is None:
        return
    args = self.args
    for k in dict.keys(d):
        v = args.get(k)
        if v is None or (isinstance(v, list) and not v):
            _note(d, k, True)


def install_miss(mod):
    """Wrap every error_messages defined in sqlglot.expressions.core so that a validated instance's missing args are recorded."""
    done = set()
    for cls in list(vars(mod).values()):
        if isinstance(cls, type) and id(cls) not in done and "error_messages" in cls.__dict__:
            done.add(id(cls))

            def make(orig):
                def error_messages(self, args=None):
                    try:
                        _miss(self)
                    except Exception:
                        pass
                    return orig(self, args)

                error_messages.__wrapped__ = orig
                return error_messages

            setattr(cls, "error_messages", make(cls.__dict__["error_messages"]))


class RecDict(dict):
    __slots__ = ("_vs", "_lb")

    def __getitem__(self, k):
        if dict.__contains__(self, k):
            _note(self, k)
        return dict.__getitem__(self, k)

    def get(self, k, *d):
        if dict.__contains__(self, k):
            _note(self, k)
        return dict.get(self, k, *d)

    def pop(self, k, *d):
        if dict.__contains__(self, k):
            _note(self, k)
        return dict.pop(self, k, *d)

    def setdefault(self, k, *d):
        if dict.__contains__(self, k):
            _note(self, k)
        return dict.setdefault(self, k, *d)

    def popitem(self):
        _note(self, "*")
        return dict.popitem(self)

    def __iter__(self):  # keys only; being overridden is what turns off the C fast paths
        return dict.__iter__(self)

    def items(self):
        _note(self, "*")
        return dict.items(self)

    def values(self):
        _note(self, "*")
        return dict.values(self)

    def copy(self):
        _note(self, "*")
        return dict.copy(self)

    def __eq__(self, o):
        _note(self, "*")
        return dict.__eq__(self, o)

    def __ne__(self, o):
        _note(self, "*")
        return dict.__ne__(self, o)

    __hash__ = None

    def __repr__(self):
        _note(self, "*")
        return dict.__repr__(self)

    def __or__(self, o):
        _note(self, "*")
        return dict.__or__(self, o)

    def __ror__(self, o):
        _note(self, "*")
        return dict.__ror__(self, o)

    def __ior__(self, o):
        _note(self, "*")
        return dict.__ior__(self, o)

    def __reduce_ex__(self, protocol):
        _note(self, "*")
        return (dict, (dict(dict.items(self)),))

    def __reduce__(self):
        return self.__reduce_ex__(4)

    def __copy__(self):
        _note(self, "*")
        return dict.copy(self)

    def __deepcopy__(self, memo):
        _note(self, "*")
        import copy

        return copy.deepcopy(dict(dict.items(self)), memo)


_CREATED = set()


def _vw(d, site, keys=None, labels=None, spread=False):
    if site not in _CREATED:  # "S": this statement ran in this process (a site no run created is unknown to the map)
        _CREATED.add(site)
        _emit("S", site, "")
    r = RecDict(d)
    r._vs = site
    r._lb = None
    if keys is not None:  # keys written as names or attribute chains: their labels are the source text
        r._lb = dict(zip(keys, labels))
        if len(r._lb) != len(labels) or (not spread and len(d) != len(labels)):  # two spellings of one key, or a key collapsed: unusable
            _emit("B", site, "")
    return r
