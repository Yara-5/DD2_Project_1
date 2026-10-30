#!/usr/bin/env python3
"""Step 4 of the handout: order the transistors of every stage (Euler paths).

Reads network.json (synthesize.py) and writes layout_spec.json, the stage spec that
prepare_layout.py reads: the devices, with series chains possibly reordered, and one
NMOS row and one PMOS row as lists of diffusion segments ("dual trails") in column order.

The method follows R. L. Maziasz and J. P. Hayes, "Exact width and height
minimization of CMOS cells", DAC 1991 (cited as [MH91] below):
  * Graph model ([MH91] section 2.2, Fig. 1b): each stage's pull-down and pull-up
    are dual series-parallel graphs M and M^d; an edge is a transistor, a vertex a
    diffusion node. Leaf k of the pull-down tree and leaf k of the pull-up tree have
    the same gate, so they share one poly column.
  * Reordering ([MH91] section 2.2): series subgraphs may be reordered without
    changing the function. Every reordering M' of every stage is enumerated.
  * Dual trails ([MH91] section 2.2): a sequence of columns that is a trail in M and,
    with the same gates, a trail in M^d, can share diffusion along the whole row
    pair. A stage needs one diffusion break fewer than its number of dual trails, so
    width W = T + G + 1 ([MH91] section 2.1) is minimized by the fewest dual trails.
    [MH91]'s R-/HR-TrailTrace builds the covers bottom-up over a composition tree;
    for stages of at most a few transistors an exact memoized search over (columns
    placed, current node in M, current node in M^d) reaches the same minimum, and is
    what is implemented here.
  * Cell array ([MH91] section 3.1, assumptions 17-18): the transistors of each
    stage stay together, stages sit side by side, and neighbouring stages abut by
    sharing diffusion when they end and start on the same nodes in both rows
    (typically VGND and VPWR). Stage order and orientation are searched to minimize
    breaks, then wire length.
[MH91]'s height minimization (routing rows U and L) is not used: the HD frame fixes
the height, and prepare_layout.py routes inside it on li1.
Only one finger per transistor ('hd' sizing) is supported for now.
"""
import argparse
import itertools
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

SUPPLY = {'nmos': 'VGND', 'pmos': 'VPWR'}
MAX_REORDERINGS = 512     # per network of one stage; far above what 4 inputs need


# ---------------------------------------------------------------- one stage

def leaf_tree(t, counter):
    """network.json's {'op','children'} / {'gate'} tree, with leaf numbers in order."""
    if 'gate' in t:
        counter[0] += 1
        return ('g', counter[0] - 1, t['gate'])
    return ('s' if t['op'] == 'series' else 'p', tuple(leaf_tree(c, counter) for c in t['children']))


def dual(t):
    return t if t[0] == 'g' else ('p' if t[0] == 's' else 's', tuple(dual(c) for c in t[1]))


def reorderings(t):
    """Every tree with the children of series nodes permuted ([MH91] section 2.2)."""
    if t[0] == 'g':
        yield t
        return
    child_options = [list(reorderings(c)) for c in t[1]]
    for combo in itertools.product(*child_options):
        orders = itertools.permutations(combo) if t[0] == 's' else [combo]
        for order in orders:
            yield (t[0], tuple(order))


def edges(t, top, bottom, prefix, counter, out):
    """Graph M of one network: leaf -> (node, node) ([MH91] Fig. 1b)."""
    if t[0] == 'g':
        out[t[1]] = (top, bottom)
    elif t[0] == 'p':
        for c in t[1]:
            edges(c, top, bottom, prefix, counter, out)
    else:
        nodes = [top]
        for _ in t[1][1:]:
            counter[0] += 1
            nodes.append(f'{prefix}{counter[0]}')
        nodes.append(bottom)
        for i, c in enumerate(t[1]):
            edges(c, nodes[i], nodes[i + 1], prefix, counter, out)
    return out


def min_dual_trail_cover(n_edges, p_edges, max_covers=64):
    """Fewest dual trails covering both graphs, then fewest trail ends off the supplies.

    Exact: a memoized search over (columns used, current node in the pull-down,
    current node in the pull-up). Each step either extends the current dual trail by
    a column whose two transistors both touch the current nodes, or starts a new one.
    Ends on VGND/VPWR are preferred because there neighbouring stages can abut
    ([MH91] section 3.1).
    """
    leaves = sorted(n_edges)
    full = (1 << len(leaves)) - 1

    def off_supply(n, p):
        # A neighbour can abut only at an end that is on the supply in both rows.
        return 0 if (n, p) == (SUPPLY['nmos'], SUPPLY['pmos']) else 1

    @lru_cache(maxsize=None)
    def go(mask, cn, cp):
        if mask == full:
            return (0, off_supply(cn, cp)), ()
        best = None
        if cn is not None:
            for i, leaf in enumerate(leaves):
                if (mask >> i) & 1:
                    continue
                (a, b), (c, d) = n_edges[leaf], p_edges[leaf]
                if cn in (a, b) and cp in (c, d):
                    nn, np_ = (b if cn == a else a), (d if cp == c else c)
                    cost, plan = go(mask | 1 << i, nn, np_)
                    cand = (cost, ((leaf, cn, nn, cp, np_, False),) + plan)
                    if best is None or cand[0] < best[0]:
                        best = cand
        ending = off_supply(cn, cp) if cn is not None else 0
        for i, leaf in enumerate(leaves):
            if (mask >> i) & 1:
                continue
            (a, b), (c, d) = n_edges[leaf], p_edges[leaf]
            for sn, en in ((a, b), (b, a)):
                for sp, ep in ((c, d), (d, c)):
                    cost, plan = go(mask | 1 << i, en, ep)
                    total = (cost[0] + 1, cost[1] + ending + off_supply(sn, sp))
                    cand = (total, ((leaf, sn, en, sp, ep, True),) + plan)
                    if best is None or cand[0] < best[0]:
                        best = cand
        return best

    def moves(mask, cn, cp):
        """(step, next state, cost of the step) for every way to place one more column."""
        out = []
        if cn is not None:
            for i, leaf in enumerate(leaves):
                if not (mask >> i) & 1:
                    (a, b), (c, d) = n_edges[leaf], p_edges[leaf]
                    if cn in (a, b) and cp in (c, d):
                        nn, np_ = (b if cn == a else a), (d if cp == c else c)
                        out.append(((leaf, cn, nn, cp, np_, False), (mask | 1 << i, nn, np_), (0, 0)))
        ending = off_supply(cn, cp) if cn is not None else 0
        for i, leaf in enumerate(leaves):
            if not (mask >> i) & 1:
                (a, b), (c, d) = n_edges[leaf], p_edges[leaf]
                for sn, en in ((a, b), (b, a)):
                    for sp, ep in ((c, d), (d, c)):
                        out.append(((leaf, sn, en, sp, ep, True), (mask | 1 << i, en, ep),
                                    (1, ending + off_supply(sn, sp))))
        return out

    def all_best(mask, cn, cp, budget):
        """Every plan that reaches the optimum from this state (at most `budget`)."""
        if mask == full:
            yield ()
            return
        target = go(mask, cn, cp)[0]
        for step, state, (dt, dp) in moves(mask, cn, cp):
            sub = go(*state)[0]
            if (sub[0] + dt, sub[1] + dp) == target:
                for rest in all_best(*state, budget):
                    yield (step,) + rest
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return

    def as_trails(plan):
        trails = []
        for leaf, sn, en, sp, ep, new in plan:
            if new:
                trails.append([])
            trails[-1].append((leaf, sn, en, sp, ep))
        return trails

    cost = go(0, None, None)[0]
    # [MH91] keeps every cover type that can lead to an optimal layout; likewise every
    # minimum cover is returned here, so routability can choose among them.
    return cost, [as_trails(plan) for plan in all_best(0, None, None, [max_covers])]


def row_repeats(nodes_by_row, signals=frozenset()):
    """Routing difficulty of a diffusion order: a net that appears again later in the
    same row must be joined by an li1 wire along that row. That wire is cheap if
    nothing between the two appearances has a contact (1); in the 0.65 um NMOS row a
    contact of another net in between blocks it (4); in the 1.0 um PMOS row a wire can
    pass a low signal contact or a high VPWR contact, but not both (4 if both lie
    between, else 1). `signals` are the nets that get contacts besides the supplies
    and the repeated nets themselves (stage outputs and the cell output).
    """
    total = 0
    for kind, nodes in nodes_by_row.items():
        counts = {}
        for n in nodes:
            counts[n] = counts.get(n, 0) + 1
        contacted = {n for n, c in counts.items() if c > 1} | set(signals) | {SUPPLY[kind]}
        last = {}
        for i, n in enumerate(nodes):
            if n != SUPPLY[kind] and n in last:
                between = [m for m in nodes[last[n] + 1:i] if m in contacted and m != n]
                if not between:
                    total += 1
                elif kind == 'nmos':
                    total += 4
                else:
                    supply = SUPPLY[kind] in between
                    other = any(m != SUPPLY[kind] for m in between)
                    total += 4 if supply and other else 1
            last[n] = i
    return total


def cover_nodes(trails):
    """Diffusion nodes of a cover, per row, as a layout would show them."""
    rows = {'nmos': [], 'pmos': []}
    for t in trails:
        rows['nmos'] += [t[0][1]] + [c[2] for c in t]
        rows['pmos'] += [t[0][3]] + [c[4] for c in t]
    return rows


def stage_options(stage, keep=6):
    """The best dual-trail covers of one stage over all its reorderings."""
    counter = [0]
    pdn = leaf_tree(stage['pull_down'], counter)
    pun = dual(pdn)
    out = stage['output']
    n_variants = list(itertools.islice(reorderings(pdn), MAX_REORDERINGS))
    p_variants = list(itertools.islice(reorderings(pun), MAX_REORDERINGS))
    found = {}
    for nt in n_variants:
        n_edges = edges(nt, out, 'VGND', f'{out}_d', [0], {})
        for pt in p_variants:
            p_edges = edges(pt, 'VPWR', out, f'{out}_u', [0], {})
            cost, covers = min_dual_trail_cover(n_edges, p_edges)
            for trails in covers:
                key = tuple(tuple(t) for t in trails)
                if key not in found:
                    found[key] = (cost, trails, n_edges, p_edges)
    # Fewest dual trails first ([MH91]), then the easiest to route (repeated nets in a
    # row), then fewest ends off the supplies (abutment, scored again across stages).
    signals = {out}
    ranked = sorted(found.values(),
                    key=lambda v: (v[0][0], row_repeats(cover_nodes(v[1]), signals), v[0][1], repr(v[1])))
    return ranked[:keep]


# ---------------------------------------------------------------- the cell array

def oriented(option, reverse):
    """A stage's trails, as columns (leaf, n_from, n_to, p_from, p_to), maybe mirrored."""
    trails = option[1]
    if not reverse:
        return [list(t) for t in trails]
    return [[(l, en, sn, ep, sp) for l, sn, en, sp, ep in reversed(t)] for t in reversed(trails)]


def assemble(sequence, stages):
    """Join the stages' trails left to right into diffusion segments.

    Consecutive trails share diffusion when the left one ends on the same node in
    both rows as the right one starts ([MH91] section 3.1: abutment); otherwise a
    diffusion break separates them.
    """
    segments = []
    for s, option, reverse in sequence:
        for trail in oriented(option, reverse):
            cols = [(stages[s]['output'], col) for col in trail]
            if segments:
                last = segments[-1][-1][1]
                if last[2] == trail[0][1] and last[4] == trail[0][3]:
                    segments[-1].extend(cols)
                    continue
            segments.append(cols)
    return segments


def wiring(segments, stages, gate_of, cell_output):
    """Total horizontal span of every net's pins, in half-column slots."""
    pins, slot = {}, 0
    for seg in segments:
        for i, (stage, (leaf, sn, en, sp, ep)) in enumerate(seg):
            if i == 0:
                for net in (sn, sp):
                    pins.setdefault(net, []).append(slot)
            pins.setdefault(gate_of[stage, leaf], []).append(slot + 1)
            for net in (en, ep):
                pins.setdefault(net, []).append(slot + 2)
            slot += 2
        slot += 2                              # a diffusion break
    return sum(max(v) - min(v) for net, v in pins.items() if net not in ('VGND', 'VPWR'))


def place_stages(stages, options, gate_of, cell_output, beam=400, keep=8):
    """Order and orient the stages: fewest breaks, then least wiring."""
    def cost(seq):
        segs = assemble(seq, stages)
        nodes = cover_nodes([[col for _, col in seg] for seg in segs])
        return (len(segs) - 1, row_repeats(nodes, signals), wiring(segs, stages, gate_of, cell_output))

    signals = frozenset(st['output'] for st in stages)
    choices = {s: [(o, r) for o in options[s] for r in (False, True)] for s in range(len(stages))}
    if len(stages) <= 4:
        candidates = []
        for order in itertools.permutations(range(len(stages))):
            for picks in itertools.product(*(choices[s] for s in order)):
                seq = [(s, o, r) for s, (o, r) in zip(order, picks)]
                candidates.append((cost(seq), seq))
    else:
        # Beam search over left-to-right prefixes for larger cells.
        beams = [((0, 0), [])]
        for _ in range(len(stages)):
            grown = []
            for _, seq in beams:
                used = {s for s, _, _ in seq}
                for s in range(len(stages)):
                    if s not in used:
                        for o, r in choices[s]:
                            nxt = seq + [(s, o, r)]
                            grown.append((cost(nxt), nxt))
            grown.sort(key=lambda c: c[0])
            beams = grown[:beam]
        candidates = beams
    candidates.sort(key=lambda c: c[0])
    out, seen = [], set()
    for c, seq in candidates:
        segs = assemble(seq, stages)
        key = repr([[col for _, col in seg] for seg in segs])
        if key not in seen:
            seen.add(key)
            out.append((c, seq, segs))
        if len(out) == keep:
            break
    return out


# ---------------------------------------------------------------- output

def euler_paths(segments, device_id):
    rows = {'nmos': [], 'pmos': []}
    for seg in segments:
        for kind, (fi, ti) in (('nmos', (1, 2)), ('pmos', (3, 4))):
            cols = [col for _, col in seg]
            rows[kind].append({
                'devices': [device_id[stage, leaf, kind] for stage, (leaf, *_ ) in seg],
                'nodes': [cols[0][fi]] + [c[ti] for c in cols],
            })
    return rows


def hd_reference(name, ports, spice_path):
    """The installed HD cell with this cell's function name and exactly its ports, if any."""
    if not spice_path:
        return None
    short = name.split('__', 1)[-1]
    match = re.search(rf'(?im)^\.subckt\s+sky130_fd_sc_hd__{re.escape(short)}\s+(.*)$',
                      Path(spice_path).read_text(encoding='utf-8', errors='replace'))
    if match and match[1].split() == ports:
        return f'sky130_fd_sc_hd__{short}'
    return None


def build_layout_spec(network, hd_spice=None):
    if any(d['fingers'] != 1 for d in network['devices']):
        raise ValueError("folded transistors (sizing 'stack') are not supported by the layout step yet")
    stages = network['stages']
    by_stage = {}
    for d in network['devices']:
        by_stage.setdefault(d['stage'], []).append(d)
    gate_of = {(st['output'], d['leaf']): d['gate'] for st in stages for d in by_stage[st['output']]}
    device_id = {(d['stage'], d['leaf'], d['type']): d['id'] for d in network['devices']}
    options = [stage_options(st) for st in stages]
    placements = place_stages(stages, options, gate_of, network['output'])
    if not placements:
        raise ValueError('no placement found')

    # Devices of the first placement's reorderings: nodes come from its graphs.
    (gaps, repeats, wire), seq, segments = placements[0]
    chosen = {stages[s]['output']: option for s, option, _ in seq}
    devices = []
    for d in network['devices']:
        _, _, n_edges, p_edges = chosen[d['stage']]
        a, b = (n_edges if d['type'] == 'nmos' else p_edges)[d['leaf']]
        devices.append(dict(d, drain=a, source=b))

    def same_graphs(seq_other):
        return all(chosen[stages[s]['output']] is o for s, o, _ in seq_other)

    ports = [*network['inputs'], 'VGND', 'VNB', 'VPB', 'VPWR', network['output']]
    spec = {
        'schema': 'dd2.layout-spec.v1', 'name': network['name'], 'request_id': network['request_id'],
        'inputs': network['inputs'], 'output': network['output'], 'truth_table': network['truth_table'],
        'sizing': network['sizing'], 'devices': devices,
        'euler_paths': euler_paths(segments, device_id),
        # Other placements with the same transistor graphs, for prepare_layout.py to try
        # in order when li1 routing fails on the first.
        'alternative_euler_paths': [euler_paths(segs, device_id) for _, sq, segs in placements[1:]
                                    if same_graphs(sq)],
        'placement': {
            'stage_order': [stages[s]['output'] for s, _, _ in seq],
            'mirrored': [stages[s]['output'] for s, _, r in seq if r],
            'dual_trails_per_stage': {st['output']: len(o[0][1]) for st, o in zip(stages, options)},
            'diffusion_segments': len(segments), 'diffusion_breaks': gaps,
            'columns': sum(len(s) for s in segments), 'repeated_nets_in_rows': repeats,
            'wiring_slots': wire,
            'method': 'dual-trail cover with series reordering, after Maziasz and Hayes, DAC 1991',
        },
    }
    reference = hd_reference(network['name'], ports, hd_spice)
    if reference:
        spec['hd_reference'] = reference
    return spec


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('network', type=Path, help='cells/<name>/network.json')
    ap.add_argument('--out', type=Path, help='default: layout_spec.json beside the network')
    ap.add_argument('--hd-spice', type=Path,
                    help='PDK sky130_fd_sc_hd.spice: names the installed cell to compare with, if its ports match')
    args = ap.parse_args()
    try:
        network = json.loads(args.network.read_text(encoding='utf-8'))
        spec = build_layout_spec(network, args.hd_spice)
    except (OSError, KeyError, ValueError) as exc:
        print(f'FAIL: {exc}')
        return 1
    out = args.out or args.network.with_name('layout_spec.json')
    out.write_text(json.dumps(spec, indent=2) + '\n', encoding='utf-8')
    p = spec['placement']
    print(f"{spec['name']}: {p['columns']} columns, {p['diffusion_segments']} diffusion segment"
          f"{'s' if p['diffusion_segments'] != 1 else ''} ({p['diffusion_breaks']} breaks), "
          f"stage order {' '.join(p['stage_order'])}")
    for kind in ('nmos', 'pmos'):
        print(f"    {kind}: " + ' | '.join(' '.join(f"{n} [{d}]" for n, d in zip(seg['nodes'], seg['devices']))
                                           + ' ' + seg['nodes'][-1] for seg in spec['euler_paths'][kind]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
