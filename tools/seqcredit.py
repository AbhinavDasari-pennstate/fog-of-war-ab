#!/usr/bin/env python3
"""Laptop only. List the lines the sequential and post-clear coverage passes credited although every earlier run had
them import-only (present with an empty test set): the credit goes to whichever test imported the module first, so it says nothing about
who depends on the line. Written to maps/seqcredit.bin as {file: sorted lines}; selector/rules.py ignores these
lines when it takes the line-coverage union of an edited lambda body.

    python tools/seqcredit.py --old <maps dir before the passes (git show ba1e74e:maps/...)> --new maps [--out maps/seqcredit.bin]
"""
import argparse
import marshal
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maps.load import load  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    out = {}
    for f, lm in new.lines.items():
        ol = old.lines.get(f, {})
        bad = sorted(ln for ln, sid in lm.items() if new.sets[sid] and ln in ol and not old.sets[ol[ln]])
        if bad:
            out[f] = bad
    path = a.out or os.path.join(a.new, "seqcredit.bin")
    with open(path, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(out), 9))
    print(f"{sum(len(v) for v in out.values())} lines in {len(out)} files -> {path}")


if __name__ == "__main__":
    main()
