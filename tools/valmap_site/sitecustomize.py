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
    _SRC = os.path.realpath(os.environ["VM_SRC"])
    _ROOT = os.path.join(_SRC, "sqlglot") + os.sep

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            rel = os.path.relpath(os.path.realpath(path), _SRC).replace(os.sep, "/")
            return _tr.compile_instrumented(self.get_data(path), path, rel)

        def exec_module(self, module):
            _rt.IMPORTING += 1
            try:
                super().exec_module(module)
            finally:
                _rt.IMPORTING -= 1
            if module.__name__ == "sqlglot.expressions.core":
                _rt.install_miss(module)

    class _Finder:
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                origin = os.path.realpath(spec.origin or "")
                if origin.startswith(_ROOT):
                    spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

    sys.meta_path.insert(0, _Finder)
