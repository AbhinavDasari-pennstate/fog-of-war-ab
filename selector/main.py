"""v0 selector: provable no-ops answer [], everything else runs the whole suite packed.

A commit answers [] only when every changed file is a modified .py file whose AST
(docstrings included) is unchanged, outside sqlglot/typing/ and sqlglotc/, whose
text is read by tests. Any doubt (parse failure, patch that does not apply, other
file types) runs every test over the best worker count.

stdout carries answer lines only; reasons go to stderr.
"""

import ast
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diffparse  # noqa: E402
import pack  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/app/sqlglot-src"))
SOURCE_READERS = ("sqlglot/typing/", "sqlglotc/")
CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _cookie(text: str) -> list[str | None]:
    """The PEP 263 encoding declaration on each of the first two lines, if any."""
    out: list[str | None] = []
    for line in text.split("\n")[:2]:
        m = CODING.match(line)
        out.append(m.group(1) if m else None)
    return out


def _is_noop(change: "diffparse.FileChange", src_root: Path) -> tuple[bool, str]:
    path = change.path_new
    if change.status != "modified" or change.path_old != path:
        return False, f"{path}: {change.status}"
    if not path.endswith(".py"):
        return False, f"{path}: not python"
    if path.startswith(SOURCE_READERS):
        return False, f"{path}: source read by tests"
    if any("\r" in text for h in change.hunks for _, text in h.lines):
        return False, f"{path}: carriage return in a changed hunk"
    old = (src_root / path).read_bytes().decode("utf-8")
    new = diffparse.apply(old, change)
    if _cookie(old) != _cookie(new):
        return False, f"{path}: encoding declaration changed"
    same = ast.dump(ast.parse(old, type_comments=True)) == ast.dump(
        ast.parse(new, type_comments=True)
    )
    return same, f"{path}: ast {'identical' if same else 'changed'}"


_FULL: dict[int, list[list[int]]] = {}


def _full(tests: list[dict]) -> list[list[int]]:
    """Every test packed; computed once per tests list."""
    durations = {t["number"]: round(t["seconds"] * 1000 + 1e-9) for t in tests}
    key = hash(tuple(durations.items()))
    if key not in _FULL:
        _FULL[key] = pack.pack(durations)
    return [list(w) for w in _FULL[key]]


def decide(patch_text: str, src_root: Path, tests: list[dict]) -> list[list[int]]:
    """Workers for one commit."""
    try:
        changes = diffparse.parse(patch_text)
        for change in changes:
            ok, why = _is_noop(change, src_root)
            print(f"  {why}", file=sys.stderr)
            if not ok:
                return _full(tests)
        return []
    except Exception as exc:  # any doubt means the widest answer
        print(f"  fallback to all tests: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _full(tests)


def main(data: Path = DATA, src_root: Path = SRC) -> None:
    tests = json.loads((data / "tests.json").read_text(encoding="utf-8"))["tests"]
    for patch in sorted((data / "commits").glob("*.patch")):
        print(f"{patch.stem}:", file=sys.stderr)
        try:
            workers = decide(patch.read_bytes().decode("utf-8"), src_root, tests)
        except Exception as exc:  # one full-suite line per commit, whatever happens
            print(f"  fallback to all tests: {type(exc).__name__}: {exc}", file=sys.stderr)
            workers = _full(tests)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"v0 done: {len(tests)} tests known", file=sys.stderr)


if __name__ == "__main__":
    main()
