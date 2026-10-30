#!/usr/bin/env python3
"""Quine-McCluskey two-level minimization (step 2 of the handout, first half).

A function of n inputs is given by its truth table (bit r is the output for row r,
input 0 being the least significant bit of r, as request_reader.py defines it).
  1. prime_implicants(): merge minterms that differ in one bit, repeatedly; a term
     that never merges is prime. A term is (value, mask): mask bits are the
     eliminated inputs, value holds the other inputs' required values.
  2. minimum_covers(): Petrick's method on the prime-implicant chart gives every
     cover with the fewest terms, then the fewest literals. Essential primes are the
     ones that alone cover some minterm.
Exact for the four inputs this project allows. Standard library only.
"""
import argparse
import json
import sys
from itertools import combinations


def popcount(value):
    return bin(value).count('1')


def covers(term, minterm):
    value, mask = term
    return (minterm & ~mask) == value


def prime_implicants(minterms):
    current = {(m, 0) for m in minterms}
    primes = set()
    while current:
        merged, used = set(), set()
        for a, b in combinations(sorted(current), 2):
            diff = a[0] ^ b[0]
            # Same eliminated inputs, and exactly one other input differs.
            if a[1] == b[1] and diff and not diff & (diff - 1):
                merged.add((a[0] & ~diff, a[1] | diff))
                used.update((a, b))
        primes |= current - used
        current = merged
    return sorted(primes)


def literal_count(term, n):
    return n - popcount(term[1])


def minimum_covers(primes, minterms, n):
    """All covers with the fewest terms, then the fewest literals (Petrick)."""
    chart = {m: [p for p in primes if covers(p, m)] for m in minterms}
    essential = sorted({chart[m][0] for m in minterms if len(chart[m]) == 1})
    products = {frozenset()}
    for m in sorted(minterms, key=lambda m: len(chart[m])):
        products = {s | {p} for s in products for p in chart[m]}
        # Absorption: X + XY = X keeps only the minimal sets.
        products = {s for s in products if not any(o < s for o in products)}
    def cost(cover):
        return (len(cover), sum(literal_count(p, n) for p in cover))
    best = min(map(cost, products), default=(0, 0))
    found = sorted((sorted(c) for c in products if cost(c) == best))
    return essential, found


def term_cube(term, n):
    """The literals of a term as (input index, polarity); polarity 1 is the true input."""
    value, mask = term
    return frozenset((i, (value >> i) & 1) for i in range(n) if not (mask >> i) & 1)


def term_text(term, names):
    cube = sorted(term_cube(term, len(names)))
    if not cube:
        return '1'
    return ' & '.join(names[i] if pol else '!' + names[i] for i, pol in cube)


def cover_text(cover, names):
    if not cover:
        return '0'
    return ' | '.join(term_text(t, names) if len(cover) == 1 or literal_count(t, len(names)) < 2
                      else f'({term_text(t, names)})' for t in cover)


def minimize(truth_table, names):
    """Minimal sum-of-products data for one truth table."""
    n = len(names)
    minterms = [r for r in range(1 << n) if (truth_table >> r) & 1]
    primes = prime_implicants(minterms)
    essential, best = minimum_covers(primes, minterms, n)
    return {
        'minterms': minterms,
        'prime_implicants': [term_text(p, names) for p in primes],
        'essential_prime_implicants': [term_text(p, names) for p in essential],
        'minimum_covers': [cover_text(c, names) for c in best],
        'terms': len(best[0]) if best else 0,
        'literals': sum(literal_count(p, n) for p in best[0]) if best else 0,
        '_covers': [[term_cube(p, n) for p in c] for c in best],
    }


def main():
    from request_reader import RequestError, validate_request
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('request', help='A cell request, as request_reader.py reads it')
    args = ap.parse_args()
    try:
        request = validate_request(json.load(open(args.request, encoding='utf-8')))
    except (OSError, ValueError, RequestError) as exc:
        print(f'FAIL: {exc}')
        return 1
    names, table = request['inputs'], request['truth_table_value']
    full = (1 << (1 << len(names))) - 1
    for label, t in (('f', table), ('!f', full & ~table)):
        result = minimize(t, names)
        print(f"{label}: {' ; '.join(result['minimum_covers'])}   "
              f"(primes: {', '.join(result['prime_implicants'])})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
