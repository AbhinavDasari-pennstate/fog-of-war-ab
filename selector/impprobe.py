"""Import probe (run as a child: python -S -E impprobe.py, job as JSON on stdin).

Imports the patched tree (the overlay of tpcds_core serves the new text of each patched file) the way a whole
pytest session ends up having it: `import sqlglot`, then every module of the package, then every dialect by name.
Prints one JSON line: {"base": [rel files of the sqlglot modules `import sqlglot` loaded], "nclasses": len(Dialect._classes),
"failed": [[name, [rel files of the sqlglot frames in the traceback]]]}.
The selector (rules.Ctx.probe) uses it to decide exactly which edits make an import raise, instead of assuming it.
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpcds_core as core  # noqa: E402


def _files(exc, src):
    """The file of the innermost module body the traceback passes through (the module that raised while it imported; the modules
    above it only import it, and every test that loads them loads it), else every sqlglot file in the traceback."""
    pre = os.path.realpath(src) + os.sep
    mods, allf = [], set()
    for fs in traceback.extract_tb(exc.__traceback__):
        fn = os.path.realpath(fs.filename)
        if fn.startswith(pre):
            allf.add(fn[len(pre):])
            if fs.name == "<module>":
                mods.append(fn[len(pre):])
    return [mods[-1]] if mods else sorted(allf)


def _loaded(src):
    pre = os.path.realpath(src) + os.sep
    out = set()
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and os.path.realpath(f).startswith(pre) and f.endswith(".py"):
            out.add(os.path.realpath(f)[len(pre):])
    return sorted(out)


def main():
    job = json.loads(sys.stdin.read())
    src = job["src"]
    res = {"base": [], "nclasses": -1, "failed": []}
    core.setup_path(src, job.get("patched"))
    try:
        import sqlglot  # noqa: F401
    except BaseException as exc:  # noqa: BLE001
        res["failed"].append(["sqlglot", _files(exc, src) or ["sqlglot/__init__.py"]])
        print(json.dumps(res))
        return
    res["base"] = _loaded(src)
    try:
        from sqlglot.dialects.dialect import Dialect

        res["nclasses"] = len(Dialect._classes)
    except BaseException as exc:  # noqa: BLE001
        res["failed"].append(["dialect", _files(exc, src) or ["sqlglot/dialects/dialect.py"]])
    import pkgutil

    names = sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."))
    for n in names:
        try:
            __import__(n)
        except BaseException as exc:  # noqa: BLE001
            res["failed"].append([n, _files(exc, src)])
    try:
        from sqlglot.dialects.dialect import Dialect

        for name in sorted(Dialect.classes) if hasattr(Dialect, "classes") else []:
            try:
                Dialect.get_or_raise(name)
            except BaseException as exc:  # noqa: BLE001
                res["failed"].append(["dialect:" + str(name), _files(exc, src)])
    except BaseException as exc:  # noqa: BLE001
        res["failed"].append(["dialect", _files(exc, src) or ["sqlglot/dialects/dialect.py"]])
    print(json.dumps(res))


if __name__ == "__main__":
    main()
