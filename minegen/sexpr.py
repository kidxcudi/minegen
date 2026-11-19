"""Tiny MeTTa s-expression toolkit: parse, print, alpha-canonicalize, scan program output."""
import itertools
import re

_TOKEN = re.compile(r'\s*(\(|\)|"[^"]*"|[^\s()]+)')


class ParseError(ValueError):
    pass


def strip_comments(text):
    out = []
    for line in text.splitlines():
        i = line.find(";")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def tokenize(text):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            break
        yield m.group(1)
        pos = m.end()


def parse_all(text):
    """Parse text into a list of expressions. Atoms are str, expressions are tuples."""
    stack = [[]]
    for tok in tokenize(strip_comments(text)):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise ParseError("unbalanced ')'")
            done = tuple(stack.pop())
            stack[-1].append(done)
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ParseError("unbalanced '('")
    return stack[0]


def parse_one(text):
    items = parse_all(text)
    if len(items) != 1:
        raise ParseError(f"expected exactly one expression, got {len(items)}")
    return items[0]


def to_str(e):
    if isinstance(e, str):
        return e
    return "(" + " ".join(to_str(x) for x in e) + ")"


def is_var(x):
    return isinstance(x, str) and x.startswith("$")


def variables(e, acc=None):
    """Variables of an expression in order of first occurrence."""
    acc = [] if acc is None else acc
    if isinstance(e, str):
        if is_var(e) and e not in acc:
            acc.append(e)
    else:
        for x in e:
            variables(x, acc)
    return acc


def substitute(e, mapping):
    if isinstance(e, str):
        return mapping.get(e, e)
    return tuple(substitute(x, mapping) for x in e)


def _rename(e, mapping):
    if isinstance(e, str):
        if is_var(e):
            if e not in mapping:
                mapping[e] = f"$v{len(mapping)}"
            return mapping[e]
        return e
    return tuple(_rename(x, mapping) for x in e)


def is_conj(e):
    return isinstance(e, tuple) and len(e) > 0 and e[0] == ","


def clauses(pattern):
    return list(pattern[1:]) if is_conj(pattern) else [pattern]


def canon(e):
    """Alpha-canonical string. A conjunction (, c1 .. cn) is an unordered set of clauses."""
    if is_conj(e) and 2 <= len(e) <= 7:
        best = None
        for perm in itertools.permutations(e[1:]):
            s = to_str(_rename((",",) + perm, {}))
            if best is None or s < best:
                best = s
        return best
    return to_str(_rename(e, {}))


def find_forms(text, head):
    """Return every balanced '(head ...)' substring found in text."""
    forms, i, n = [], 0, len(text)
    start_pat = re.compile(r"\(\s*" + re.escape(head) + r"(?=[\s(])")
    while True:
        m = start_pat.search(text, i)
        if not m:
            break
        depth, j, in_str = 0, m.start(), False
        while j < n:
            c = text[j]
            if c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0:
            break
        forms.append(text[m.start(): j + 1])
        i = j + 1
    return forms
