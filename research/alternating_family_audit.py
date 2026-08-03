#!/usr/bin/env python3
"""Audit the dense completion of an alternating certificate cycle.

Special pairs are
    {1,4} -> b_0,
    {0,b_i} -> t_i,
    {3,t_i} -> b_(i+1).
Every non-special triple is included except a mixed triple wholly inside the
six-vertex defect-one core.  This tests the repeatable split-link pattern.
"""

from fractions import Fraction
from itertools import combinations
import sys

from layer_family_audit import full_audit, induced_2k2, quick_audit


def make_bases(cycle_length: int) -> tuple[int, set[int]]:
    n = 6 + 2 * cycle_length
    A, C = {0, 1, 2}, {3, 4, 5}
    b = tuple(6 + 2 * i for i in range(cycle_length))
    t = tuple(7 + 2 * i for i in range(cycle_length))
    special: dict[frozenset[int], int] = {frozenset((1, 4)): b[0]}
    for i in range(cycle_length):
        special[frozenset((0, b[i]))] = t[i]
        special[frozenset((3, t[i]))] = b[(i + 1) % cycle_length]

    bases: set[int] = set()
    for triple in combinations(range(n), 3):
        T = set(triple)
        # Make both core bases rigid: each of their internal pairs is completed
        # only by the third core vertex.  This kills the proper bad restriction
        # obtained by replacing a missing core vertex with b_0.
        if (len(T & A) >= 2 or len(T & C) >= 2) and T not in (A, C):
            continue
        contained = [p for p in special if p <= T]
        if contained:
            if all(T - set(p) == {special[p]} for p in contained):
                bases.add(sum(1 << x for x in T))
        else:
            bases.add(sum(1 << x for x in T))
    return n, bases


if __name__ == "__main__":
    length = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    vertex_count, generated_bases = make_bases(length)
    quick_audit(vertex_count, generated_bases)
    if vertex_count <= 12:
        full_audit(vertex_count, generated_bases)
