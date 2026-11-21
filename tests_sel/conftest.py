"""Shared helpers: selector/ on sys.path, a patch builder and a tiny tests.json."""

import difflib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selector"))
sys.path.insert(0, str(ROOT / "tools"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The pinned sqlglot checkout used by tests that need real source files.
PIN = Path(os.environ.get("BR_SRC", str(ROOT.parent / "sqlglot-pin")))


def make_patch(path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" + "".join(diff)


def tiny_tests(count: int = 40) -> list[dict]:
    return [
        {"number": n, "id": f"tests/t.py::T::t{n}", "file": "tests/t.py",
         "seconds": round(0.05 + (n % 7) * 0.4, 3)}
        for n in range(count)
    ]


KIT = ROOT.parent / "kits" / "rwx" / "x"
needs_pin = pytest.mark.skipif(
    not (PIN / "sqlglot").is_dir(), reason=f"pinned sqlglot checkout not found at {PIN} (set BR_SRC)"
)
needs_kit = pytest.mark.skipif(
    not (KIT / "score.py").is_file() or not (ROOT.parent / "baseline" / "output.jsonl").is_file(),
    reason=f"organiser kit or baseline output not found next to the repo ({KIT})",
)
