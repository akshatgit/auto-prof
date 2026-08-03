#!/usr/bin/env python3
"""Discovery audit for a guarded alternating-cycle candidate.

The six core vertices form an alternating cycle.  Each cycle edge uniquely
completes to a guard, and each core--guard pair points to the next core vertex.
Thus, once all guards are present, any one core vertex forces the whole core.
The remaining three cross pairs seed h, g, and b_0.  The pair hg is completed
only by core vertices.
"""

from itertools import combinations
import sys

from layer_family_audit import induced_2k2, quick_audit


def make_bases(cycle_length: int):
    # Core order a0,c0,a1,c1,a2,c2.
    core_cycle = (0, 3, 1, 4, 2, 5)
    A, C = {0, 1, 2}, {3, 4, 5}
    guards = tuple(range(6, 12))
    h, g = 12, 13
    b = tuple(14 + 2 * i for i in range(cycle_length))
    t = tuple(15 + 2 * i for i in range(cycle_length))
    n = 14 + 2 * cycle_length

    special = {}
    for i, guard in enumerate(guards):
        x, y = core_cycle[i], core_cycle[(i + 1) % 6]
        special[frozenset((x, y))] = guard
        special[frozenset((x, guard))] = y
    remaining_cross = [
        frozenset((a, c))
        for a in A for c in C
        if frozenset((a, c)) not in special
    ]
    assert len(remaining_cross) == 3
    for pair, target in zip(remaining_cross, (h, g, b[0])):
        special[pair] = target
    h_seed = remaining_cross[0]
    g_seed = remaining_cross[1]

    for i in range(cycle_length):
        special[frozenset((h, b[i]))] = t[i]
        special[frozenset((g, t[i]))] = b[(i + 1) % cycle_length]

    bases = set()
    external = set(range(6, n))
    for triple in combinations(range(n), 3):
        T = set(triple)
        if T <= A | C and T not in (A, C):
            continue
        if h in T and g in T and T <= external:
            continue
        if len(T & set(b)) == 2 and T & set(h_seed):
            continue
        if len(T & set(t)) == 2 and T & set(g_seed):
            continue
        if T <= set(b) or T <= set(t):
            continue
        contained = [p for p in special if p <= T]
        if contained:
            if all(T - set(p) == {special[p]} for p in contained):
                bases.add(sum(1 << x for x in T))
        else:
            bases.add(sum(1 << x for x in T))
    return n, bases, special, (set(core_cycle), set(guards), h, g, set(b), set(t))


def bad_pair_closure_report(n, bases, special, parts):
    core, guards, h, g, B, T = parts
    bad = []
    exposed = []
    for p in bases:
        for q in bases:
            if p & q:
                continue
            failures = [
                e for e in range(n) if p >> e & 1
                and not any(
                    q >> f & 1 and ((p & ~(1 << e)) | (1 << f)) in bases
                    for f in range(n)
                )
            ]
            if len(failures) < 2:
                continue
            bad.append((p, q))
            closure = {x for x in range(n) if (p | q) >> x & 1}
            changed = True
            while changed:
                changed = False
                for pair, target in special.items():
                    if pair <= closure and target not in closure:
                        closure.add(target)
                        changed = True
            if not (core <= closure or {h, g} <= closure):
                exposed.append((p, q, closure))
    print("bad_ordered_disjoint", len(bad), "unprotected", len(exposed))
    print("first_unprotected", exposed[:1])


if __name__ == "__main__":
    length = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    args = make_bases(length)
    quick_audit(args[0], args[1])
    bad_pair_closure_report(*args)
