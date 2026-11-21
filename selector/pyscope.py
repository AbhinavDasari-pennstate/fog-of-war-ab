"""Python-source helpers for the selector: AST equality, encoding cookies, changed names.

Stdlib only. Nothing here imports sqlglot.
"""

import ast
import re

CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_STR = re.compile(r"\"([^\"\\\n]*)\"|'([^'\\\n]*)'")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z_0-9]*)")
_CLASS = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z_0-9]*)")


def cookie(text: str) -> list:
    """PEP 263 encoding declaration on each of the first two lines (None when absent)."""
    out = []
    for line in text.split("\n")[:2]:
        m = CODING.match(line)
        out.append(m.group(1) if m else None)
    return out


def parse(text: str, path: str = "<src>"):
    return ast.parse(text, filename=path)


def dump(tree) -> str:
    return ast.dump(tree)


def compiles(tree, path: str = "<src>") -> bool:
    """True when the parsed tree also survives the compiler (catches 'return' outside a function)."""
    try:
        compile(tree, path, "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


def names_in(lines) -> set:
    """Identifiers and string-literal contents (whole and per word) found in source lines."""
    out: set = set()
    for line in lines:
        out.update(_IDENT.findall(line))
        for m in _STR.finditer(line):
            s = m.group(1) if m.group(1) is not None else m.group(2)
            if s:
                out.add(s)
                out.update(_IDENT.findall(s))
    return out


def def_name(line: str) -> str:
    m = _DEF.match(line)
    return m.group(1) if m else ""


def class_name(line: str) -> str:
    m = _CLASS.match(line)
    return m.group(1) if m else ""


def is_code_line(text: str) -> bool:
    s = text.strip()
    return bool(s) and not s.startswith("#")
