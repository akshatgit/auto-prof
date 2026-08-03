#!/usr/bin/env python3
"""Definition-driven exact audit of the nine-element obstruction B9."""

from fractions import Fraction


n = 9
ALL = (1 << n) - 1


def mask(*xs):
    return sum(1 << x for x in xs)


bases = {mask(0, 1, 2), mask(3, 4, 5)}
for z in (6, 7):
    for p in range(6):
        for q in range(p + 1, 6):
            if z == 6 and (p, q) == (0, 3):
                continue
            if z == 7 and (p, q) == (1, 4):
                continue
            bases.add(mask(z, p, q))
for k in range(5):
    bases.add(mask(6, 8, k))
    bases.add(mask(7, 8, k))
bases.add(mask(6, 7, 8))

independent = [any(x & ~b == 0 for b in bases) for x in range(1 << n)]


def submasks(x):
    s = x
    while True:
        yield s
        if s == 0:
            return
        s = (s - 1) & x


def maximal_members(U, S):
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


def defect_num_den(B, C, S):
    difference = B & ~C
    denominator = difference.bit_count()
    if denominator == 0:
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


pure_count = 0
whole = (0, 1)
proper = (0, 1)
bad_proper = 0
for U in range(1 << n):
    for S in submasks(U):
        if not independent[S]:
            continue
        maximal = maximal_members(U, S)
        if len({X.bit_count() for X in maximal}) != 1:
            continue
        pure_count += 1
        value = (0, 1)
        for B in maximal:
            for C in maximal:
                candidate = defect_num_den(B, C, S)
                if candidate[0] * value[1] > value[0] * candidate[1]:
                    value = candidate
        if U == ALL and S == 0:
            whole = value
        else:
            if value[0] * proper[1] > proper[0] * value[1]:
                proper = value
            bad_proper += value[0] * 2 > value[1]

print("basis count:", len(bases))
print("pure presentations:", pure_count)
print("whole defect:", Fraction(*whole))
print("maximum proper defect:", Fraction(*proper))
print("bad proper presentations:", bad_proper)
