"""Class-use recorder core (no pytest import, so spawned pool workers can use it).

install() wraps __init__ of every class defined in a sqlglot module; set_key() says which
test the next instantiations belong to; dump() writes {key: [module:qualname, ...]}.
For an instance, every sqlglot class in its MRO is credited, so a class counts as used when
it or a subclass was instantiated.
"""

from __future__ import annotations

import functools
import json
import os
import sys

_key = "g:*"
_use: dict[str, set[str]] = {}
_seen: set = set()
_wrapped_modules = -1


def set_key(key: str) -> None:
    global _key
    _key = key


def _record(cls) -> None:
    ks = (_key, cls)
    if ks in _seen:
        return
    _seen.add(ks)
    bucket = _use.setdefault(_key, set())
    for k in cls.__mro__:
        if (getattr(k, "__module__", "") or "").startswith("sqlglot"):
            bucket.add(f"{k.__module__}:{k.__qualname__}")


def _wrap(cls) -> None:
    init = cls.__dict__.get("__init__")
    if init is None or not callable(init) or getattr(init, "_classuse", False):
        return

    @functools.wraps(init)
    def __init__(self, *a, **k):
        _record(type(self))
        return init(self, *a, **k)

    __init__._classuse = True  # type: ignore[attr-defined]
    try:
        cls.__init__ = __init__
    except (TypeError, AttributeError):
        pass


def _walk(cls, seen: set) -> None:
    if cls in seen:
        return
    seen.add(cls)
    _wrap(cls)
    try:
        subs = cls.__subclasses__()
    except TypeError:
        subs = type.__subclasses__(cls)
    for s in subs:
        _walk(s, seen)


def install() -> None:
    global _wrapped_modules
    if _wrapped_modules == len(sys.modules):
        return
    _wrapped_modules = len(sys.modules)
    seen: set = set()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if isinstance(obj, type) and (obj.__module__ or "").startswith("sqlglot"):
                _walk(obj, seen)


def dump(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({k: sorted(v) for k, v in _use.items()}, f)


def key_for(ctx: str) -> str:
    """coverage context 'nodeid|phase' -> classuse key."""
    nodeid, _, phase = ctx.rpartition("|")
    if phase == "run":
        return "t:" + nodeid
    return "g:" + nodeid.rsplit("::", 1)[0]
