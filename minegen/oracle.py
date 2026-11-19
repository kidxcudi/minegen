"""Brute-force reference semantics for pattern support. Deliberately naive so it is easy to trust.

Support follows the repo's `counter`: the number of results of (match db pattern pattern).
For a conjunction (, c1 .. cn) that is the number of joint bindings (a join), so it can
exceed the support of each clause.
"""
import itertools

from .sexpr import (canon, clauses, is_conj, is_var, parse_all, substitute,
                    to_str, variables)


def load_atoms(text):
    return list(parse_all(text))


def match(pat, atom, b):
    if isinstance(pat, str):
        if is_var(pat):
            if pat in b:
                return b if b[pat] == atom else None
            nb = dict(b)
            nb[pat] = atom
            return nb
        return b if pat == atom else None
    if not isinstance(atom, tuple) or len(atom) != len(pat):
        return None
    for p, a in zip(pat, atom):
        b = match(p, a, b)
        if b is None:
            return None
    return b


def _count(cls, db, k, b):
    if k == len(cls):
        return 1
    total = 0
    for atom in db:
        nb = match(cls[k], atom, b)
        if nb is not None:
            total += _count(cls, db, k + 1, nb)
    return total


def support(pattern, db):
    """Duplicate atoms in db count with multiplicity (matches `counter` without dedup)."""
    return _count(clauses(pattern), db, 0, {})


def _set_partitions(items):
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in _set_partitions(rest):
        yield [[first]] + part
        for i in range(len(part)):
            yield part[:i] + [[first] + part[i]] + part[i + 1:]


def _is_flat_ground(a):
    return isinstance(a, tuple) and len(a) >= 2 and all(isinstance(x, str) and not is_var(x) for x in a)


def flat_patterns(db, minsup):
    """All flat single-clause patterns (Head t1..tk) with support >= minsup.
    Each ti is a constant seen in that column or a variable; variable sharing is enumerated.
    Fully ground patterns are included: a real run showed the miner reports them (support counts duplicates)."""
    shapes = {}
    for a in db:
        if _is_flat_ground(a):
            shapes.setdefault((a[0], len(a)), []).append(a)
    found = {}
    for (head, k), atoms in shapes.items():
        cols = [sorted({a[i] for a in atoms}) for i in range(k)]
        positions = list(range(1, k))
        for mask in itertools.product([False, True], repeat=len(positions)):  # True = variable
            var_pos = [p for p, m in zip(positions, mask) if m]
            const_pos = [p for p, m in zip(positions, mask) if not m]
            # an all-constant mask yields ground patterns; the miner reports them when their support reaches minsup
            for part in _set_partitions(var_pos):
                for consts in itertools.product(*[cols[p] for p in const_pos]):
                    t = [head] + [None] * (k - 1)
                    for p, c in zip(const_pos, consts):
                        t[p] = c
                    for vi, block in enumerate(part):
                        for p in block:
                            t[p] = f"$x{vi}"
                    pat = tuple(t)
                    sup = support(pat, db)
                    if sup >= minsup:
                        found[canon(pat)] = (pat, sup)
    return found


def _rename_apart(p, tag):
    return substitute(p, {v: f"${tag}{i}" for i, v in enumerate(variables(p))})


def _identifications(vp, vq):
    """All non-empty partial injective maps from Q's variables onto P's variables."""
    for k in range(1, min(len(vp), len(vq)) + 1):
        for qs in itertools.combinations(vq, k):
            for ps in itertools.permutations(vp, k):
                yield dict(zip(qs, ps))


def conj2_patterns(db, minsup, singles):
    """Two-clause conjunctions of frequent single-clause patterns that share at least one variable.
    Joins on several shared variables are included (a real run showed the miner reports them)."""
    pats = [p for p, _ in singles.values() if variables(p)]  # a clause with no variable cannot join
    found = {}
    for p, q in itertools.combinations_with_replacement(pats, 2):
        P, Q = _rename_apart(p, "a"), _rename_apart(q, "b")
        for mapping in _identifications(variables(P), variables(Q)):
            Q2 = substitute(Q, mapping)
            if P == Q2:  # literally the same clause twice: redundant
                continue
            pat = (",", P, Q2)
            sup = support(pat, db)
            if sup >= minsup:
                found[canon(pat)] = (pat, sup)
    return found


def expected_frequent(db, minsup, conj_exp, depth=0):
    """Oracle's expected miner output as {canon: (pattern, support)} plus the scope it covers.
    The miner's formatter returns EITHER the single-clause patterns (conj_exp False) OR the conjunctions
    (conj_exp True), never both; the repo's own golden output confirms it. Completeness is only claimed for
    flat data at depth 0."""
    flat = all(_is_flat_ground(a) for a in db)
    scope = {"completeness": flat and depth == 0}
    if not flat:
        return {}, scope
    singles = flat_patterns(db, minsup)
    if conj_exp and depth == 0:
        return conj2_patterns(db, minsup, singles), scope
    return singles, scope


def coverage_flags(db, minsup):
    """Which edge conditions does this dataset exercise? Computed from the oracle only."""
    flat = all(_is_flat_ground(a) for a in db)
    flags = {"boundary_at_minsup": False, "boundary_below_minsup": False, "self_link": False,
             "join_gain": False, "nothing_frequent": False, "single_atom": len(db) == 1,
             "nested": not flat}
    if not flat:
        return flags
    flags["self_link"] = any(len(set(a[1:])) < len(a) - 1 for a in db)
    low = flat_patterns(db, max(1, minsup - 1))
    flags["boundary_at_minsup"] = any(s == minsup for _, s in low.values())
    flags["boundary_below_minsup"] = any(s == minsup - 1 for _, s in low.values()) if minsup > 1 else False
    exp = flat_patterns(db, minsup)
    flags["nothing_frequent"] = len(exp) == 0
    base = flat_patterns(db, 1)
    for _, (p, s) in conj2_patterns(db, 1, base).items():
        c1, c2 = p[1], p[2]
        if s > max(support(c1, db), support(c2, db)):
            flags["join_gain"] = True
            break
    return flags