"""Unified diff parsing and applying, stdlib only.

parse(patch_text) -> list[FileChange]; apply(original_text, change) -> new_text.
Both raise PatchError on anything unexpected; callers fall back to the widest answer.
"""

import re
from dataclasses import dataclass, field


class PatchError(Exception):
    pass


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text)
    no_newline: bool = False  # a "\ No newline at end of file" marker was seen


@dataclass
class FileChange:
    path_old: str
    path_new: str
    status: str  # modified | added | deleted | renamed | binary
    hunks: list[Hunk] = field(default_factory=list)


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


def apply(original: str, change: FileChange) -> str:
    """Apply a modified-file change to the original text."""
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
