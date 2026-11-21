"""Loaded in every interpreter that has this directory on PYTHONPATH and ATTR_OUT set (map build only).

Rewrites sqlglot and tests modules on import (attribute reads become recorded reads) and marks every
module body execution as "import time" for the recorder. Bytecode caches are bypassed.
"""
import os

if os.environ.get("ATTR_OUT") and os.environ.get("ATTR_SRC"):
    import builtins
    import importlib.machinery
    import sys

    sys.dont_write_bytecode = True
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _here)
    import attr_runtime as _rt
    import attr_transform as _tr

    for _n in ("_ar", "_arg", "_arh", "_ard", "_arv"):
        setattr(builtins, _n, getattr(_rt, _n))
    _SRC = os.path.realpath(os.environ["ATTR_SRC"])
    _ROOTS = (os.path.join(_SRC, "sqlglot") + os.sep, os.path.join(_SRC, "tests") + os.sep)

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            return _tr.compile_instrumented(self.get_data(path), path, _rt.NAMES)

        def exec_module(self, module):
            _rt.IMPORTING += 1
            try:
                super().exec_module(module)
            finally:
                _rt.IMPORTING -= 1

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
