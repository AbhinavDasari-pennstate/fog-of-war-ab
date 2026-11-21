"""Loaded in every interpreter that has this directory on PYTHONPATH and WM_OUT set (map build only)."""
import os

if os.environ.get("WM_OUT") and os.environ.get("WM_SRC"):
    import builtins
    import importlib.machinery
    import sys

    sys.dont_write_bytecode = True
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _here)
    sys.path.insert(0, os.environ["WM_TOOLS"])
    sys.path.insert(0, os.path.join(os.environ["WM_TOOLS"], "selector"))
    import wm_runtime as _rt
    import wm_transform as _tr

    builtins._wm_c = _rt.wm_c
    builtins._wm_b = _rt.wm_b
    builtins._wm_n = _rt.wm_n
    _SRC = os.path.realpath(os.environ["WM_SRC"])

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            data = self.get_data(path)
            rel = os.path.relpath(os.path.realpath(path), _SRC)
            return _tr.compile_instrumented(data, path, rel)

    class _Finder:
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                real = os.path.realpath(spec.origin or "")
                if real.startswith(os.path.join(_SRC, "sqlglot") + os.sep):
                    spec.loader = _Loader(spec.loader.name, spec.loader.path)
            return spec

    sys.meta_path.insert(0, _Finder)
