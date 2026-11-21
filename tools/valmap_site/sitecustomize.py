"""Loaded in every interpreter that has this directory on PYTHONPATH and VM_OUT set (map build only).

Rewrites eligible dict constants of the pinned sqlglot on import (see vm_transform) and marks every module body
execution as "import time" for the recorder. Bytecode caches are bypassed.
"""
import os

if os.environ.get("VM_OUT") and os.environ.get("VM_SRC"):
    import builtins
    import importlib.machinery
    import sys

    sys.dont_write_bytecode = True
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _here)
    import vm_runtime as _rt
    import vm_transform as _tr

    builtins._vw = _rt._vw
    for _n in ("_sw", "_mem", "_bulk", "_gets", "_sd", "_sv"):
        setattr(builtins, _n, getattr(_rt, _n))
    _SETNAMES = frozenset()
    if os.environ.get("VM_SETNAMES"):
        import json

        with open(os.environ["VM_SETNAMES"]) as _f:
            _SETNAMES = frozenset(json.load(_f))
    _SRC = os.path.realpath(os.environ["VM_SRC"])
    _ROOTS = (os.path.join(_SRC, "sqlglot") + os.sep, os.path.join(_SRC, "tests") + os.sep)

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            rel = os.path.relpath(os.path.realpath(path), _SRC).replace(os.sep, "/")
            return _tr.compile_instrumented(self.get_data(path), path, rel, _SETNAMES)

        def exec_module(self, module):
            _rt.IMPORTING += 1
            try:
                super().exec_module(module)
            finally:
                _rt.IMPORTING -= 1
            if module.__name__ == "sqlglot.expressions.core":
                _rt.install_miss(module)
            elif module.__name__ == "sqlglot.time":
                _rt.install_time(module)

    class _Finder:
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if name not in ("sqlglot", "tests") and not name.startswith(("sqlglot.", "tests.")):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                origin = os.path.realpath(spec.origin or "")
                if origin.startswith(_ROOTS):
                    spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

    sys.meta_path.insert(0, _Finder)
