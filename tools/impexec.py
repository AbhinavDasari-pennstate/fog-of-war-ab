#!/usr/bin/env python3
"""Import-time execution map (map build only): which module bodies run which sqlglot code while they import?

    PYTHONPATH=<pinned sqlglot checkout> python tools/impexec.py --src <checkout> --out maps/impexec.bin

A module body can call into other sqlglot code (a parse_one or transpile at import, a registry decorator, a
metaclass hook). A one-line edit of a table or a function those calls read then changes what the importer does,
or whether it imports at all. The coverage maps cannot see it: the map build preimports every module in the parent
process, so those lines have no test context, and a test that imports the module lazily (Dialect.get_or_raise)
is not credited with the classes and names the import used.

This recorder imports every sqlglot module in one process with sys.setprofile. For each Python function call
whose caller chain has a module body (code name <module>) on the stack, it credits the innermost such module M:
  file[F]    -> {M}   F's functions ran under M's body
  range[F]   -> {(first line, last line): {M}}   the function (or lambda) code at those lines of F ran under M's body
  klass[Q]   -> {M}   a method of class Q (or a subclass of it, through the first argument's MRO) ran under M's body
The selector (rules.py, _import_time) widens an edit of class- or module-level data to the tests that import
each such M (maps/imports.bin plus lazyimp.bin: cold, lazy imports included).

Output: zlib(marshal({"file": {rel file: sorted modules}, "class": {qualname: sorted modules}, "range": {rel file: [(lo, hi, modules)]}})),
modules as rel files.
"""

from __future__ import annotations

import argparse
import importlib
import marshal
import os
import pkgutil
import sys
import zlib

CO_OPTIMIZED = 0x1
META = not os.environ.get("IMPEXEC_NO_META")  # IMPEXEC_NO_META=1: do not credit metaclass __new__ hooks to the classes being built (narrower, less sound)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src = os.path.realpath(a.src)
    pre = src + os.sep
    sys.path.insert(0, src)
    fcache: dict[str, str | None] = {}

    def rel(fn: str):
        r = fcache.get(fn, 0)
        if r == 0:
            r = None
            if fn.startswith(pre) and fn.endswith(".py"):
                r = fn[len(pre):]
            fcache[fn] = r
        return r

    mods: list[str] = []  # stack of module rel files whose body runs
    stack: list[tuple[bool, bool]] = []  # per live python frame: (is repo module body, pushed)
    byfile: dict[str, set] = {}
    byclass: dict[str, set] = {}
    byrange: dict[str, dict] = {}

    def prof(frame, event, arg):
        if event == "call":
            code = frame.f_code
            r = rel(code.co_filename)
            if r is None:
                stack.append(False)
                return
            if code.co_name == "<module>":
                mods.append(r)
                stack.append(True)
                return
            stack.append(False)
            if mods and code.co_flags & CO_OPTIMIZED:
                m = mods[-1]
                byfile.setdefault(r, set()).add(m)
                key = (code.co_firstlineno, max([ln for _, _, ln in code.co_lines() if ln] or [code.co_firstlineno]))
                byrange.setdefault(r, {}).setdefault(key, set()).add(m)
                if code.co_argcount:
                    try:
                        loc = frame.f_locals
                        o = loc.get(code.co_varnames[0])
                        k = o if isinstance(o, type) else type(o)
                        for c in k.__mro__:
                            if getattr(c, "__module__", "").startswith("sqlglot"):
                                byclass.setdefault(c.__qualname__, set()).add(m)
                        if META and code.co_name == "__new__" and isinstance(o, type) and issubclass(o, type):
                            # a metaclass hook: the class being built (its name, and the MRO of its bases) is what it reads
                            for b in loc.get(code.co_varnames[2], ()) if code.co_argcount > 2 else ():
                                for c in getattr(b, "__mro__", ()):
                                    if getattr(c, "__module__", "").startswith("sqlglot"):
                                        byclass.setdefault(c.__qualname__, set()).add(m)
                            nm = loc.get(code.co_varnames[1]) if code.co_argcount > 1 else None
                            if isinstance(nm, str):
                                byclass.setdefault(nm, set()).add(m)
                    except Exception:
                        pass
        elif event == "return":
            if stack and stack.pop():
                mods.pop()

    sys.setprofile(prof)
    try:
        import sqlglot  # noqa: F401

        base = sorted(rel(getattr(mm, "__file__", "") or "") for mm in list(sys.modules.values()) if rel(getattr(mm, "__file__", "") or ""))

        for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if mod.name.rsplit(".", 1)[-1] == "__main__":
                continue
            try:
                importlib.import_module(mod.name)
            except Exception as exc:
                print("skip", mod.name, type(exc).__name__, file=sys.stderr)
        from sqlglot import dialects

        for name in list(getattr(dialects, "DIALECTS", [])):
            try:
                importlib.import_module("sqlglot.dialects." + name.lower())
            except Exception:
                pass
        for attr in dir(dialects):
            if attr[:1].isupper():
                try:
                    getattr(dialects, attr)
                except Exception:
                    pass
    finally:
        sys.setprofile(None)
    out = {
        "file": {k: sorted(v) for k, v in sorted(byfile.items())},
        "class": {k: sorted(v) for k, v in sorted(byclass.items())},
        "base": base,
        "range": {f: sorted((lo, hi, sorted(v)) for (lo, hi), v in d.items()) for f, d in sorted(byrange.items())},
    }
    with open(a.out, "wb") as f:
        f.write(zlib.compress(marshal.dumps(out), 9))
    allm = set().union(*byfile.values()) if byfile else set()
    print(f"files {len(byfile)} classes {len(byclass)} modules-with-import-time-calls {len(allm)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
