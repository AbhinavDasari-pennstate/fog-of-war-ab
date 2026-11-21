"""Unified diff parsing and applying, stdlib only.

parse(patch_text) -> list[FileChange]; apply(original_text, change) -> new_text.
Both raise PatchError on anything unexpected; callers fall back to the widest answer.
"""

import os
import re


class PatchError(Exception):
    pass


class _Record:
    """What @dataclass gave these two classes (init, eq, repr) without importing dataclasses, which pulls in inspect,
    typing and dis: tens of milliseconds of start-up CPU for a batch that parses a few small patches."""

    _FIELDS = ()
    __hash__ = None

    def __eq__(self, other):
        if other.__class__ is not self.__class__:
            return NotImplemented
        return all(getattr(self, f) == getattr(other, f) for f in self._FIELDS)

    def __repr__(self):
        return f"{self.__class__.__qualname__}(" + ", ".join(f"{f}={getattr(self, f)!r}" for f in self._FIELDS) + ")"


class Hunk(_Record):
    _FIELDS = ("old_start", "old_count", "new_start", "new_count", "lines", "no_newline")

    def __init__(self, old_start, old_count, new_start, new_count, lines=None, no_newline=False):
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines = [] if lines is None else lines  # (" " | "-" | "+", text)
        self.no_newline = no_newline  # a "\ No newline at end of file" marker was seen


class FileChange(_Record):
    _FIELDS = ("path_old", "path_new", "status", "hunks")

    def __init__(self, path_old, path_new, status, hunks=None):
        self.path_old = path_old
        self.path_new = path_new
        self.status = status  # modified | added | deleted | renamed | binary
        self.hunks = [] if hunks is None else hunks


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADER = re.compile(r"^diff --git (\S+) (\S+)$")


def _strip_prefix(name: str) -> str:
    name = name.split("\t")[0].strip()
    return name[2:] if name.startswith(("a/", "b/")) else name


def _count(text: str | None) -> int:
    return 1 if text is None else int(text)


def parse(patch_text: str) -> list[FileChange]:
    lines = patch_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    changes: list[FileChange] = []
    cur: FileChange | None = None
    hunk: Hunk | None = None
    for line in lines:
        if hunk is not None:
            seen_old = sum(1 for op, _ in hunk.lines if op in " -")
            seen_new = sum(1 for op, _ in hunk.lines if op in " +")
            if line.startswith("\\"):
                hunk.no_newline = True
                continue
            if seen_old < hunk.old_count or seen_new < hunk.new_count:
                if line[:1] not in (" ", "-", "+"):
                    if line == "":  # a blank context line whose space was stripped
                        hunk.lines.append((" ", ""))
                        continue
                    raise PatchError("hunk ended early")
                hunk.lines.append((line[0], line[1:]))
                continue
            hunk = None
        if line[:1] in (" ", "-", "+") and not (line.startswith(("--- ", "+++ ")) and cur and not cur.hunks):
            raise PatchError("diff line outside a hunk")
        m = _HEADER.match(line)
        if m:
            cur = FileChange(_strip_prefix(m.group(1)), _strip_prefix(m.group(2)), "modified")
            changes.append(cur)
            continue
        if cur is None:
            if line.strip():
                raise PatchError("text before the first diff header")
            continue
        m = _HUNK.match(line)
        if m:
            hunk = Hunk(int(m.group(1)), _count(m.group(2)), int(m.group(3)), _count(m.group(4)))
            cur.hunks.append(hunk)
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from ") or line.startswith("rename to "):
            cur.status = "renamed"
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.status = "binary"
        elif line.startswith("--- "):
            if line[4:].startswith("/dev/null"):
                cur.status = "added"
            else:
                cur.path_old = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            if line[4:].startswith("/dev/null"):
                cur.status = "deleted"
            else:
                cur.path_new = _strip_prefix(line[4:])
    if not changes:
        raise PatchError("no file changes found")
    return changes


_APPLIED: dict = {}  # (id(original), id(change)) -> (original, change, patched text)
_READ: dict = {}  # path -> ((mtime_ns, size), text)
_MEMO_MAX = 8


def read_text(path) -> str:
    """The UTF-8 text of a file, read again only when its size or modification time changed: the same source file is
    wanted by several rules of one run, and opening a 300 KB file costs about as much as parsing a small patch."""
    p = os.fspath(path)
    st = os.stat(p)
    key = (st.st_mtime_ns, st.st_size)
    hit = _READ.get(p)
    if hit is not None and hit[0] == key:
        return hit[1]
    with open(p, "rb") as fh:
        text = fh.read().decode("utf-8")
    if len(_READ) >= _MEMO_MAX:
        _READ.pop(next(iter(_READ)))
    _READ[p] = (key, text)
    return text


def apply(original: str, change: FileChange) -> str:
    """Apply a modified-file change to the original text (remembered for the same two objects: rules, the weak
    mutation rule and the TPC-DS scope each ask for the same patched file)."""
    key = (id(original), id(change))
    hit = _APPLIED.get(key)
    if hit is not None and hit[0] is original and hit[1] is change:
        return hit[2]
    out = _apply(original, change)
    if len(_APPLIED) >= _MEMO_MAX:
        _APPLIED.pop(next(iter(_APPLIED)))
    _APPLIED[key] = (original, change, out)
    return out


def _apply(original: str, change: FileChange) -> str:
    if change.status != "modified" or not change.hunks:
        raise PatchError(f"cannot apply a {change.status} change")
    if any(h.no_newline for h in change.hunks) or not original.endswith("\n"):
        raise PatchError("end-of-file newline handling not supported")
    src = original.split("\n")[:-1]
    out: list[str] = []
    pos = 0
    for h in change.hunks:
        start = h.old_start - 1 if h.old_count else h.old_start
        if start < pos or start > len(src):
            raise PatchError("hunks overlap or start past the end")
        out.extend(src[pos:start])
        pos = start
        for op, text in h.lines:
            if op == "+":
                out.append(text)
                continue
            if pos >= len(src) or src[pos] != text:
                raise PatchError(f"context mismatch at line {pos + 1}")
            pos += 1
            if op == " ":
                out.append(text)
    out.extend(src[pos:])
    return "\n".join(out) + "\n"
