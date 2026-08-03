#!/usr/bin/env python3
"""Dense alternating certificate cycle with anchors outside the bad core."""

from itertools import combinations
import sys

from layer_family_audit import full_audit, quick_audit


def make_bases(cycle_length: int) -> tuple[int, set[int]]:
    # Core A=012, C=345; external anchors h=6, g=7.
    n = 8 + 2 * cycle_length
    A, C = {0, 1, 2}, {3, 4, 5}
    h, g = 6, 7
    b = tuple(8 + 2 * i for i in range(cycle_length))
    t = tuple(9 + 2 * i for i in range(cycle_length))
    special: dict[frozenset[int], int] = {
        frozenset((0, 3)): h,
        frozenset((1, 4)): g,
        frozenset((2, 5)): b[0],
    }
    for i in range(cycle_length):
        special[frozenset((h, b[i]))] = t[i]
        special[frozenset((g, t[i]))] = b[(i + 1) % cycle_length]

    bases: set[int] = set()
    for triple in combinations(range(n), 3):
        T = set(triple)
        # The induced core is precisely the two disjoint bases A and C.
        if T <= A | C and T not in (A, C):
            continue
        # Forced by the leaf edge 3--h in L_0 (and symmetrically): h cannot
        # meet a b_i there because {h,b_i} is a special pair, so no b_i b_j
        # edge may be present in L_0 or L_3.  The t-side is analogous.
        if len(T & set(b)) == 2 and T & {0, 3}:
            continue
        if len(T & set(t)) == 2 and T & {1, 4}:
            continue
        # Three vertices from the same side would form a bad basis against a
        # basis containing its corresponding split-link anchor once n >= 3.
        if T <= set(b) or T <= set(t):
            continue
        contained = [p for p in special if p <= T]
        if contained:
            if all(T - set(p) == {special[p]} for p in contained):
                bases.add(sum(1 << x for x in T))
        else:
            bases.add(sum(1 << x for x in T))
    return n, bases


if __name__ == "__main__":
    length = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    vertex_count, generated_bases = make_bases(length)
    quick_audit(vertex_count, generated_bases)
    if vertex_count <= 12:
        full_audit(vertex_count, generated_bases)
