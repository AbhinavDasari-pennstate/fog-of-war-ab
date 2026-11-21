#!/usr/bin/env python3
"""Hand probe: apply one textual mutation to a scratch copy of sqlglot, run one test for real, restore.

    python tools/weakmut_probe.py --src <scratch copy> --py <python> --test <node id> --file sqlglot/x.py --line N --old "is None" --new "== None"
Prints RESULT passed|failed and pytest's last line. Also asks the selector's rule what it would do.
"""
import argparse
import ast
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "selector"))
import weakmut as W  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    for k in ("src", "py", "test", "file", "old", "new", "weakmut"):
        ap.add_argument("--" + k, required=(k != "weakmut"))
    ap.add_argument("--line", type=int, required=True)
    ap.add_argument("--number", type=int)
    a = ap.parse_args()
    path = os.path.join(a.src, a.file)
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    assert lines[a.line - 1].count(a.old) == 1, lines[a.line - 1]
    lines[a.line - 1] = lines[a.line - 1].replace(a.old, a.new)
    mutated = "\n".join(lines)
    verdict = "n/a"
    if a.weakmut and a.number is not None:
        import marshal, zlib
        d = marshal.loads(zlib.decompress(open(a.weakmut, "rb").read()))
        summ = {n: r["sites"] for n, r in d["tests"].items()}
        got, note = W.drops(a.file, None, text, mutated, summ)
        verdict = "DROP" if a.number in got else "KEEP"
        print("selector verdict for", a.number, verdict, note)
    open(path, "w", encoding="utf-8").write(mutated)
    try:
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=a.src, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([a.py, "-m", "pytest", a.test, "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-x"],
                           cwd=a.src, env=env, capture_output=True, text=True)
    finally:
        open(path, "w", encoding="utf-8").write(text)
    last = [l for l in r.stdout.splitlines() if l.strip()][-1:]
    print("RESULT", "passed" if r.returncode == 0 else "failed", "|", last, "| verdict", verdict)


if __name__ == "__main__":
    main()
