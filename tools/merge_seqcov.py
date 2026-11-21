#!/usr/bin/env python3
"""Laptop only. Union a sequential coverage pass into the committed line maps without renumbering any set id.

    python tools/merge_seqcov.py --maps maps --new <tools/build_maps.py output built from the original runs PLUS the
        sequential run (tools/run_seqcov.sh)> [--out maps]

The tables that tools/build_maps.py derives from per-test line coverage are replaced by the new build's values after
checking each is a superset of the committed one: lines.bin, files.bin, classes.bin, names.bin, unstable.bin. Set ids
of the committed pool stay as they are (new masks are appended to sets.bin), so tables the build does not own
(imports.bin with the lazy-import map, mentions, attribute maps) keep working. reads.bin, srcreaders.bin, stmts.bin,
importtime.bin and imports.bin are left as committed.
"""
import argparse
import marshal
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maps.load import load  # noqa: E402


def write(d, name, obj):
    with open(os.path.join(d, name), "wb") as f:
        f.write(zlib.compress(marshal.dumps(obj), 9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    out = a.out or a.maps
    cur, new = load(a.maps), load(a.new)
    pool = list(cur.sets)
    index = {m: i for i, m in enumerate(pool)}

    def sid(mask):
        i = index.get(mask)
        if i is None:
            i = index[mask] = len(pool)
            pool.append(mask)
        return i

    lines = {}
    added = 0
    for f in sorted(set(cur.lines) | set(new.lines)):
        lm = {}
        old = cur.lines.get(f, {})
        nw = new.lines.get(f, {})
        for ln in sorted(set(old) | set(nw)):
            mo = cur.sets[old[ln]] if ln in old else 0
            mn = new.sets[nw[ln]] if ln in nw else 0
            if mo & ~mn:
                sys.exit(f"{f}:{ln}: the new build lost tests of the committed line map")
            if mn != mo:
                added += 1
            lm[ln] = sid(mn) if ln in nw else old[ln]
        lines[f] = lm

    def table(name, old, nw):
        res = {}
        for k in set(old) | set(nw):
            mo, mn = old.get(k, 0), nw.get(k, 0)
            if mo & ~mn:
                sys.exit(f"{name} {k}: the new build lost tests")
            res[k] = sid(mn)
        return res

    files = table("files", cur.files, new.files)
    classes = table("classes", cur.classes, new.classes)
    names = {"class": table("names_class", cur.names_class, new.names_class),
             "attr": table("names_attr", cur.names_attr, new.names_attr)}
    unstable = {}
    for f in set(cur.unstable) | set(new.unstable):
        unstable[f] = sorted(cur.unstable.get(f, set()) | new.unstable.get(f, set()))
    write(out, "lines.bin", lines)
    write(out, "files.bin", files)
    write(out, "classes.bin", classes)
    write(out, "names.bin", names)
    write(out, "unstable.bin", unstable)
    write(out, "sets.bin", pool)
    print(f"lines gaining tests or new: {added}; sets {len(cur.sets)} -> {len(pool)}; "
          f"unstable lines {sum(len(v) for v in cur.unstable.values())} -> {sum(len(v) for v in unstable.values())}")


if __name__ == "__main__":
    main()
