#!/usr/bin/env python3
"""Audit a layered unique-completion candidate for rank-three obstructions.

This is a discovery artifact, not a claimed construction.  The generator makes
three pairs in each layer uniquely complete into the next layer.  It then adds
every other triple except the triples forbidden by the defect-one core.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import combinations
import sys


def make_bases(layer_count: int) -> tuple[int, set[int]]:
    n = 6 + 3 * layer_count
    A = {0, 1, 2}
    C = {3, 4, 5}
    layers = [tuple(range(6 + 3 * i, 9 + 3 * i)) for i in range(layer_count)]

    # pair -> its sole permitted completion
    special: dict[frozenset[int], int] = {
        frozenset((0, 3)): layers[0][0],
        frozenset((1, 4)): layers[0][1],
        frozenset((2, 5)): layers[0][2],
    }
    for left, right in zip(layers, layers[1:]):
        for pair, target in zip(combinations(left, 2), right):
            special[frozenset(pair)] = target
    for pair, target in zip(combinations(layers[-1], 2), (0, 1, 2)):
        special[frozenset(pair)] = target

    bases: set[int] = set()
    for triple in combinations(range(n), 3):
        T = set(triple)
        # Keep the induced system on the six core vertices equal to two
        # disjoint triples.  In particular A -> C has all exchanges fail.
        if T <= A | C and T not in (A, C):
            continue
        contained_special = [p for p in special if p <= T]
        if contained_special:
            if all((T - set(p)) == {special[p]} for p in contained_special):
                bases.add(sum(1 << x for x in T))
        else:
            bases.add(sum(1 << x for x in T))
    return n, bases


def induced_2k2(graph_edges: set[frozenset[int]], vertices: range):
    for four in combinations(vertices, 4):
        edges = [frozenset(e) for e in combinations(four, 2)]
        present = [e for e in edges if e in graph_edges]
        if len(present) == 2 and not (present[0] & present[1]):
            return four, present
    return None


def quick_audit(n: int, bases: set[int]) -> None:
    pair_completions: dict[frozenset[int], set[int]] = {}
    skeleton: set[frozenset[int]] = set()
    links = [set() for _ in range(n)]
    for b in bases:
        vs = [i for i in range(n) if b >> i & 1]
        for p in combinations(vs, 2):
            pair = frozenset(p)
            skeleton.add(pair)
            pair_completions.setdefault(pair, set()).add(next(x for x in vs if x not in pair))
        for x in vs:
            links[x].add(frozenset(y for y in vs if y != x))

    print("vertices", n, "bases", len(bases), "skeleton_complete", len(skeleton) == n * (n - 1) // 2)
    print("skeleton_2K2", induced_2k2(skeleton, range(n)))
    bad_links = [(x, induced_2k2(links[x], range(n))) for x in range(n)]
    bad_links = [(x, witness) for x, witness in bad_links if witness is not None]
    print("bad_link_count", len(bad_links), "witnesses", bad_links[:10])

    A = sum(1 << x for x in (0, 1, 2))
    C = sum(1 << x for x in (3, 4, 5))
    failures = 0
    for e in (0, 1, 2):
        if not any(((A & ~(1 << e)) | (1 << f)) in bases for f in (3, 4, 5)):
            failures += 1
    print("core_defect", Fraction(failures, 3))


def submasks(x: int):
    s = x
    while True:
        yield s
        if not s:
            return
        s = (s - 1) & x


def full_audit(n: int, bases: set[int]) -> None:
    independent = [False] * (1 << n)
    for x in range(1 << n):
        independent[x] = any(x & ~b == 0 for b in bases)

    def maximal_members(U: int, S: int) -> list[int]:
        available = U & ~S
        answer = []
        for X in submasks(available):
            if not independent[X | S]:
                continue
            if all(
                not independent[X | S | (1 << e)]
                for e in range(n)
                if available >> e & 1 and not X >> e & 1
            ):
                answer.append(X)
        return answer

    def defect_num_den(B: int, C: int, S: int) -> tuple[int, int]:
        difference = B & ~C
        denominator = difference.bit_count()
        if not denominator:
            return 0, 1
        failures = 0
        for e in range(n):
            if not difference >> e & 1:
                continue
            success = any(
                independent[((B & ~(1 << e)) | (1 << f)) | S]
                for f in range(n)
                if (C & ~B) >> f & 1
            )
            failures += not success
        return failures, denominator

    all_mask = (1 << n) - 1
    pure_count = 0
    maximum_whole = (0, 1)
    maximum_proper = (0, 1)
    bad_proper = None
    for U in range(1 << n):
        for S in submasks(U):
            if not independent[S]:
                continue
            maximal = maximal_members(U, S)
            if len({x.bit_count() for x in maximal}) != 1:
                continue
            pure_count += 1
            best = (0, 1)
            best_pair = (0, 0)
            for B in maximal:
                for C in maximal:
                    candidate = defect_num_den(B, C, S)
                    if candidate[0] * best[1] > best[0] * candidate[1]:
                        best = candidate
                        best_pair = (B, C)
            if U == all_mask and S == 0:
                maximum_whole = best
            else:
                if best[0] * maximum_proper[1] > maximum_proper[0] * best[1]:
                    maximum_proper = best
                if best[0] * 2 > best[1] and bad_proper is None:
                    bad_proper = (U, S, best, best_pair)
    print("pure_presentations", pure_count)
    print("whole_defect", Fraction(*maximum_whole))
    print("proper_defect", Fraction(*maximum_proper))
    print("first_bad_proper", bad_proper)


if __name__ == "__main__":
    layers = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    vertex_count, generated_bases = make_bases(layers)
    quick_audit(vertex_count, generated_bases)
    if vertex_count <= 12:
        full_audit(vertex_count, generated_bases)
