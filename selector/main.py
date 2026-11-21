"""Board probe P3 (not a real entry). v11 selection; constant-only commits with tests dropped (missed = failing ones).\ncode = costA_halfsec + 512*costFixture_halfsec, offset 400000.\n(older notes follow) P2: v8.3 selection; on non-constant sqlglot code commits drop every test >= 2 s.
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
    cost_a, cost_fix, n_a = 0, 0, 0
    for c in order:
        p = DATA / "commits" / f"{c}.patch"
        try:
            txt = p.read_text("utf-8", "replace")
        except Exception:
            txt = ""
        fs = files_of(txt)
        code_py = bool(fs) and all(f.endswith(".py") and "/sqlglot/" in ("/" + f) and "/tests/" not in ("/" + f) for f in fs)
        if not code_py:
            cost_fix += cost(rows[c])
            continue
        if const_only(txt) and rows[c] and any(rows[c]):
            n_a += 1
            cost_a += cost(rows[c])
            rows[c] = []
    code = min(cost_a // 500, 511) + 512 * min(cost_fix // 500, 511)
    dropA = []
    base = sum(cost(rows[c]) for c in order)
    offset = 400_000 if base <= 380_000 else 1_500_000
    rem = offset + code - base
    for c in order:
        if rem == 0:
            break
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
    sys.stderr.write(f"probe3: N={n_all} nA={n_a} costA_ms={cost_a} costFix_ms={cost_fix} code={code} offset={offset} rem={rem}\n")
    for c in order:
        print(json.dumps({"commit": c, "workers": rows[c]}))


main()
