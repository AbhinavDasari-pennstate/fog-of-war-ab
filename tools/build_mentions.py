#!/usr/bin/env python3
"""Build maps/mentions.bin: for each name defined at class or module level (a key of names_attr),
the class-level and module-level source lines of sqlglot/ that mention it (definitions of the
same name excluded). Function-body lines are left out unless they ran at import time: tests that ran the others are
in names_attr already. Pinned-commit map; run on the laptop.

    python3 tools/build_mentions.py --src ../sqlglot-pin --maps maps
"""

import argparse
import marshal
import os
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "selector"))
from maps.load import load  # noqa: E402
import pyscope  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--maps", default=str(ROOT / "maps"))
    a = ap.parse_args()
    m = load(a.maps)
    names = set(m.names_attr)
    files = sorted(m.stmts)
    out: dict[str, list[int]] = {}
    fidx = {f: i for i, f in enumerate(files)}
    for f in files:
        if not f.startswith("sqlglot/"):
            continue
        lines = (Path(a.src) / f).read_text("utf-8").split("\n")
        for ln, text in enumerate(lines, 1):
            s = m.stmt(f, ln)
            if s is None or (s[2] == "function" and ln not in m.importtime.get(f, ())):
                continue
            for tok in pyscope.names_in([text]) & names:
                if re.match(r"^\s*" + re.escape(tok) + r"\s*(?::|=(?!=))", text):
                    continue
                out.setdefault(tok, []).append(fidx[f] * 100000 + ln)
    blob = zlib.compress(marshal.dumps({"files": files, "mentions": out}), 9)
    Path(a.maps, "mentions.bin").write_bytes(blob)
    os.chmod(Path(a.maps, "mentions.bin"), 0o644)
    print(f"{len(out)} names, {sum(map(len, out.values()))} mentions, {len(blob)} bytes")


if __name__ == "__main__":
    main()
