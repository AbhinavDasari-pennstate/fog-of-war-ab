"""Board probe A/B (not a real entry). v11 selection; commits of one category (BR_PROBE: O = other sqlglot code, F = fixture/test/multi-file) whose v11 cost is 0 < cost <= 3 s select nothing (missed = how many of them really fail). code = n_nonempty + 32*n_small + 1024*cost_small_halfsec + 131072*n_zero, offset 400000 (1500000 if base > 380000).
(P4 notes follow) v11 selection; constant-only commits with tests split: c1 def/lambda body (dropped -> missed = failing c1), c2 class/module level in a Call, c3 class/module plain data. code = c1_half + 128*c2_half + 16384*c3_half, offset 400000.\nP3: v11 selection; constant-only commits with tests dropped (missed = failing ones).\ncode = costA_halfsec + 512*costFixture_halfsec, offset 400000.\n(older notes follow) P2: v8.3 selection; on non-constant sqlglot code commits drop every test >= 2 s.
missed = commits where a dropped giant really fails. code = nG + 32*savedG_s + 4096*costFixture_s (offset 400000).
(old P1 notes follow)

Runs the v8.2 selector (selector/v82.py) unchanged, then:
- commits whose patch is a constant-only edit (only NUMBER/STRING/True/False/None tokens differ)
  and that v8.2 gives at least one test: select nothing, so the board's missed count = how many
  of them really fail;
- every other commit keeps the v8.2 selection, padded with one extra worker on some commits so
  the total simulated ms of the non-dropped commits equals OFFSET + code, where
  code = N + 128 * nA + 4096 * min(costA_s, 255)
  (N = commits, nA = constant-only commits dropped, costA_s = their v8.2 cost in whole seconds).
Decode: V = sim_ms - 300000 * missed; offset 400000 (or 1500000 if V >= 1500000).
"""
import io
import json
import os
import subprocess
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
HERE = Path(__file__).resolve().parent
tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
DUR = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
WORKER = 2000
MAXW = 8


def cost(workers):
    ws = [w for w in workers if w]
    return (max(sum(DUR[n] for n in w) for w in ws) + WORKER * len(ws)) if ws else 0


def toks(line):
    try:
        return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline)
                if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT)]
    except Exception:
        return None


LIT = {tokenize.NUMBER, tokenize.STRING}


def const_only(patch):
    files, minus, plus = set(), [], []
    for ln in patch.splitlines():
        if ln.startswith("+++ "):
            files.add(ln[4:].strip())
        elif ln.startswith("---") or ln.startswith("+++"):
            continue
        elif ln.startswith("-"):
            minus.append(ln[1:])
        elif ln.startswith("+"):
            plus.append(ln[1:])
    if len(files) != 1 or not next(iter(files)).endswith(".py") or "/tests/" in next(iter(files)) or next(iter(files)).startswith("b/tests"):
        return False
    if not minus or len(minus) != len(plus):
        return False
    changed = False
    for a, b in zip(minus, plus):
        ta, tb = toks(a), toks(b)
        if ta is None or tb is None or len(ta) != len(tb):
            return False
        for x, y in zip(ta, tb):
            if x == y:
                continue
            if x[0] != y[0]:
                return False
            if x[0] in LIT or (x[0] == tokenize.NAME and x[1] in ("True", "False", "None") and y[1] in ("True", "False", "None")):
                changed = True
                continue
            return False
    return changed


def subset(target, avail):
    """Exact subset of avail (test numbers) summing to target ms, greedy largest first."""
    pick, rem = [], target
    for n in sorted(avail, key=lambda n: -DUR[n]):
        d = DUR[n]
        if d <= rem and (d > 0 or rem == 0):
            pick.append(n)
            rem -= d
            if rem == 0:
                break
    if rem != 0:
        return None
    return pick or [min(avail, key=lambda n: DUR[n])] if target == 0 else pick


import ast as _ast
import re as _re
SRC = Path(os.environ.get("BR_SRC", "/app/sqlglot-src"))


def const_category(txt):
    """1 = literal inside a def/lambda body, 2 = class/module level inside a Call, 3 = class/module level plain data."""
    try:
        f = next(ln[6:].strip() for ln in txt.splitlines() if ln.startswith("+++ b/"))
        old = None
        cur = None
        for ln in txt.splitlines():
            m = _re.match(r"@@ -(\d+)", ln)
            if m:
                cur = int(m.group(1))
                continue
            if cur is None or ln.startswith("---") or ln.startswith("+++"):
                continue
            if ln.startswith("-"):
                old = cur
                break
            if ln.startswith("+"):
                continue
            cur += 1
        tree = _ast.parse((SRC / f).read_text("utf-8"))
        best = None
        for node in _ast.walk(tree):
            lo, hi = getattr(node, "lineno", None), getattr(node, "end_lineno", None)
            if lo is None or not (lo <= old <= hi):
                continue
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.Lambda)):
                if isinstance(node, _ast.Lambda) or old > node.lineno:
                    return 1
        for node in _ast.walk(tree):
            lo, hi = getattr(node, "lineno", None), getattr(node, "end_lineno", None)
            if lo is not None and lo <= old <= hi and isinstance(node, _ast.Call):
                return 2
        return 3
    except Exception:
        return 3


def main():
    r = subprocess.run([sys.executable, "-S", "-E", str(HERE / "v11.py")], capture_output=True)
    sys.stderr.write(r.stderr.decode("utf-8", "replace")[-2000:])
    rows = {}
    order = []
    for line in r.stdout.decode("utf-8").splitlines():
        row = json.loads(line)
        rows[row["commit"]] = [[int(n) for n in w] for w in row["workers"]]
        order.append(row["commit"])
    n_all = len(order)
    def files_of(txt):
        return {ln[4:].strip() for ln in txt.splitlines() if ln.startswith("+++ ")}
    MODE = os.environ.get("BR_PROBE", "C")
    SMALL = 3000
    GIANT = 2000
    dropped = set()
    ID2N = {t["id"]: t["number"] for t in tests}
    info = {}
    for c in order:
        p = DATA / "commits" / f"{c}.patch"
        try:
            txt = p.read_text("utf-8", "replace")
        except Exception:
            txt = ""
        fs = files_of(txt)
        code_py = bool(fs) and all(f.endswith(".py") and "/sqlglot/" in ("/" + f) and "/tests/" not in ("/" + f) for f in fs)
        if code_py and const_only(txt):
            cat = "A"
        elif code_py:
            cat = "O"
        else:
            cat = "F"
        info[c] = (cat, txt, fs)

    def strip_giants(ws):
        out = [[n for n in w if DUR[n] < GIANT] for w in ws]
        return [w for w in out if w]

    def test_methods(txt, fs):
        """Test numbers of the test methods that hold every changed line of a patch that only touches tests/*.py files, or None."""
        want = []
        cur_file, old, lines = None, None, {}
        for ln in txt.splitlines():
            if ln.startswith("+++ "):
                cur_file = ln[4:].strip()
                cur_file = cur_file[2:] if cur_file.startswith("b/") else cur_file
                lines.setdefault(cur_file, set())
                old = None
                continue
            if ln.startswith("--- "):
                continue
            m = _re.match(r"@@ -(\d+)(?:,(\d+))? \+", ln)
            if m:
                old = int(m.group(1))
                if m.group(2) == "0":
                    old += 1
                continue
            if old is None or cur_file is None:
                continue
            if ln.startswith("-"):
                lines[cur_file].add(old)
                old += 1
            elif ln.startswith("+"):
                lines[cur_file].add(old)
            elif ln.startswith("\\"):
                continue
            else:
                old += 1
        for f, ls in lines.items():
            if not ls:
                return None
            tree = _ast.parse((SRC / f).read_text("utf-8"))
            spans = []
            for cls in tree.body:
                if isinstance(cls, _ast.ClassDef):
                    for fn in cls.body:
                        if isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                            lo = fn.body[0].lineno
                            spans.append((lo, fn.end_lineno, f"{f}::{cls.name}::{fn.name}"))
            for l in ls:
                hit = [i for lo, hi, i in spans if lo <= l <= hi]
                if len(hit) != 1 or hit[0] not in ID2N:
                    return None
                want.append(ID2N[hit[0]])
        return sorted(set(want)) or None

    if MODE == "C":
        st = {"A": [0, 0], "F": [0, 0]}
        for c in order:
            cat, txt, fs = info[c]
            k = cost(rows[c])
            if k == 0:
                continue
            if cat == "A" and const_category(txt) != 1:
                key = "A"
            elif cat == "F" and k > SMALL:
                key = "F"
            else:
                continue
            new = strip_giants(rows[c])
            if cost(new) != k:
                st[key][0] += 1
                st[key][1] += k - cost(new)
                rows[c] = new
                dropped.add(c)
        sec = lambda ms: min((ms + 500) // 1000, 127)
        code = min(st["A"][0], 7) + 8 * sec(st["A"][1]) + 1024 * min(st["F"][0], 7) + 8192 * sec(st["F"][1])
        note = f"st={st}"
    else:  # "D"
        n_py, n_narrow, saved, n_multi, n_fix = 0, 0, 0, 0, 0
        for c in order:
            cat, txt, fs = info[c]
            k = cost(rows[c])
            if cat != "F" or k == 0:
                continue
            names = [f[2:] if f.startswith("b/") else f for f in fs]
            py_only = all(f.startswith("tests/") and f.endswith(".py") for f in names)
            if k > SMALL:
                if any(f.startswith("sqlglot/") for f in names):
                    n_multi += 1
                elif not any(f.endswith(".py") for f in names):
                    n_fix += 1
            if py_only:
                n_py += 1
                try:
                    want = test_methods(txt, fs)
                except Exception:
                    want = None
                if want:
                    new = [want]
                    if cost(new) < k:
                        n_narrow += 1
                        saved += k - cost(new)
                        rows[c] = new
                        dropped.add(c)
        sec = lambda ms: min((ms + 500) // 1000, 127)
        code = min(n_py, 7) + 8 * min(n_narrow, 7) + 64 * sec(saved) + 8192 * min(n_multi, 7) + 65536 * min(n_fix, 7)
        note = f"n_py={n_py} n_narrow={n_narrow} saved_ms={saved} n_multi={n_multi} n_fix={n_fix}"
    base = sum(cost(rows[c]) for c in order)
    offset = 400_000 if base <= 380_000 else 1_500_000
    rem = offset + code - base
    for c in order:
        if rem == 0:
            break
        if c in dropped:
            continue
        ws = [w for w in rows[c] if w]
        if len(ws) >= MAXW:
            continue
        m = max((sum(DUR[n] for n in w) for w in ws), default=0)
        used = {n for w in ws for n in w}
        avail = [n for n in DUR if n not in used]
        inc_max = WORKER + max(0, sum(DUR[n] for n in avail) - m - 1000)
        inc = min(rem, inc_max)
        if 0 < rem - inc < WORKER:
            inc = rem - WORKER
        if inc < WORKER:
            continue
        L = m + (inc - WORKER)
        pick = subset(L, avail)
        if not pick:
            continue
        new = ws + [pick]
        if cost(new) - cost(ws) != inc:
            continue
        rows[c] = new
        rem -= inc
    sys.stderr.write(f"probe {MODE}: {note} changed={sorted(dropped)} code={code} offset={offset} base={base} rem={rem}\n")
    for c in order:
        print(json.dumps({"commit": c, "workers": rows[c]}))


main()
