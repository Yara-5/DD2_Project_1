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
  * HD reference (handout Table 4): for a cell in hd_reference.REFERENCE_CELLS whose
    function equals the installed HD cell's under some pin map, the spec names that cell
    and the map; among placements as good as the best in breaks and repeated nets, the
    first whose circuit is exactly HD's (series order included) is taken.
  * Folded transistors ('stack' sizing, handout step 4: "order folded fingers to share
    diffusion as well (Tutorial 3, section 8)"): a transistor of f fingers is f
    parallel copies of its edge. Where a leaf has more fingers in one network than in
    the other (NOR3 under 'stack': one NMOS and three PMOS fingers per input), a
    column holds a finger in one row only. Inside that row's diffusion strip the
    column carries the node on (its poly stops at the contact pad, as in Tutorial 3's
    folded NOR3, whose NMOS row is stretched under the PMOS fingers); outside it the
    row has no diffusion. finger_cover() is the exact search for such columns: fewest
    columns plus breaks, then the least diffusion under one-row columns, then the
    fewest strip ends off the supplies.
[MH91]'s height minimization (routing rows U and L) is not used: the HD frame fixes
the height, and prepare_layout.py routes inside it on li1.
"""
import argparse
import itertools
import json
import sys
from functools import lru_cache
from pathlib import Path

import hd_reference

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
            trails[-1].append((leaf, sn, en, sp, ep, True, True))
        return trails

    cost = go(0, None, None)[0]
    # [MH91] keeps every cover type that can lead to an optimal layout; likewise every
    # minimum cover is returned here, so routability can choose among them.
    return cost, [as_trails(plan) for plan in all_best(0, None, None, [max_covers])]


def finger_cover(n_edges, p_edges, fn, fp, max_covers=16):
    """Columns for a stage whose leaves have fn[leaf] NMOS and fp[leaf] PMOS fingers.

    A column places one finger of a leaf in both rows, or in one row only. A row's
    state in the current segment is: not started (None), open on a node, or closed
    ('#'). Inside an open strip a one-row column leaves the other row's node as it is
    (diffusion runs under it); before or after the strip that row has no diffusion
    there. A new segment (a diffusion break) restarts both rows. Cost: (columns +
    breaks, one-row columns inside a strip, strip ends off the supplies).
    """
    leaves = sorted(n_edges)
    start = tuple((fn[l], fp[l]) for l in leaves)
    CLOSED = '#'

    def off(node, kind):
        return (0, 0, 0 if node in (None, CLOSED, SUPPLY[kind]) else 1)

    def row_step(state, edge, take, kind):
        """(record, new state, extra cost) options for one row at one column."""
        if take:
            a, b = edge
            if state == CLOSED:
                return []
            if state is None:
                return [((a, b, True), b, off(a, kind)), ((b, a, True), a, off(b, kind))]
            if state in (a, b):
                other = b if state == a else a
                return [((state, other, True), other, (0, 0, 0))]
            return []
        if state is None or state == CLOSED:
            return [((None, None, False), state, (0, 0, 0))]
        # Open strip: carry the node on under this column, or end the strip before it.
        return [((state, state, False), state, (0, 1, 0)), ((None, None, False), CLOSED, off(state, kind))]

    def add(*costs):
        return tuple(map(sum, zip(*costs)))

    @lru_cache(maxsize=None)
    def go(rem, ns, ps, used):
        if all(r == (0, 0) for r in rem):
            return add(off(ns, 'nmos'), off(ps, 'pmos')), ()
        best = None
        for i, leaf in enumerate(leaves):
            rn, rp = rem[i]
            for take_n, take_p in ((True, True), (True, False), (False, True)):
                if (take_n and rn == 0) or (take_p and rp == 0):
                    continue
                left = rem[:i] + ((rn - take_n, rp - take_p),) + rem[i + 1:]
                for rec_n, nxt_n, cn in row_step(ns, n_edges[leaf], take_n, 'nmos'):
                    for rec_p, nxt_p, cp in row_step(ps, p_edges[leaf], take_p, 'pmos'):
                        cost, plan = go(left, nxt_n, nxt_p, True)
                        total = add(cost, (1, 0, 0), cn, cp)
                        col = (leaf, rec_n[0], rec_n[1], rec_p[0], rec_p[1], rec_n[2], rec_p[2])
                        cand = (total, ((col, False),) + plan)
                        if best is None or cand[0] < best[0]:
                            best = cand
        if used:
            # A diffusion break: both rows start again.
            cost, plan = go(rem, None, None, False)
            total = add(cost, (1, 0, 0), off(ns, 'nmos'), off(ps, 'pmos'))
            if plan:
                cand = (total, ((plan[0][0], True),) + plan[1:])
                if best is None or cand[0] < best[0]:
                    best = cand
        return best

    cost, plan = go(start, None, None, False)
    trails = []
    for col, new in plan:
        if new or not trails:
            trails.append([])
        trails[-1].append(col)
    return cost, [trails]


def row_repeats(nodes_by_row, signals=frozenset()):
    """Routing difficulty of a diffusion order: a net that appears again later in the
    same row must be joined by an li1 wire along that row. That wire is cheap if
    nothing between the two appearances has a contact (1). A supply contact in between
    is merged into its rail away from the band, so the wire passes on the band side
    (1). In the 0.65 um NMOS row a signal contact in between blocks it (4): that net
    leaves the row upwards and no track fits below its landing. In the 1.0 um PMOS row
    a wire can pass a signal contact on the rail side, but not if a VPWR contact also
    lies between (4). `signals` are the nets that get contacts besides the supplies
    and the repeated nets themselves (stage outputs and the cell output).
    """
    total = 0
    for kind, nodes in nodes_by_row.items():
        nodes = [n for n in nodes if n is not None]
        counts = {}
        for n in nodes:
            counts[n] = counts.get(n, 0) + 1
        contacted = {n for n, c in counts.items() if c > 1} | set(signals) | {SUPPLY[kind]}
        last = {}
        for i, n in enumerate(nodes):
            if n != SUPPLY[kind] and n in last:
                between = [m for m in nodes[last[n] + 1:i] if m in contacted and m != n]
                supply = SUPPLY[kind] in between
                other = any(m != SUPPLY[kind] for m in between)
                if kind == 'nmos':
                    total += 4 if other else 1
                else:
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


def joins(left, right, from_index, to_index):
    """Whether one row lets two columns share diffusion: the same node on both sides,
    or no diffusion on either (a strip is never reopened inside a segment)."""
    return left[to_index] == right[from_index]


def stage_options(stage, keep=6, fingers=None):
    """The best dual-trail covers of one stage over all its reorderings. fingers:
    {leaf: (NMOS fingers, PMOS fingers)} when the stage has folded transistors."""
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
            if fingers:
                cost, covers = finger_cover(n_edges, p_edges, {k: v[0] for k, v in fingers.items()},
                                            {k: v[1] for k, v in fingers.items()})
            else:
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
    return [[(l, en, sn, ep, sp, hn, hp) for l, sn, en, sp, ep, hn, hp in reversed(t)] for t in reversed(trails)]


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
                if joins(last, trail[0], 1, 2) and joins(last, trail[0], 3, 4):
                    segments[-1].extend(cols)
                    continue
            segments.append(cols)
    return segments


def wiring(segments, stages, gate_of, cell_output):
    """Total horizontal span of every net's pins, in half-column slots."""
    pins, slot = {}, 0
    for seg in segments:
        for i, (stage, (leaf, sn, en, sp, ep, *_)) in enumerate(seg):
            if i == 0:
                for net in (sn, sp):
                    if net is not None:
                        pins.setdefault(net, []).append(slot)
            pins.setdefault(gate_of[stage, leaf], []).append(slot + 1)
            for net in (en, ep):
                if net is not None:
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
    """Rows as diffusion segments. A column without a transistor in a row has None for
    its device; its nodes on both sides are equal (diffusion carried on) or None (no
    diffusion there). Fingers of one transistor get the ids of its finger devices in
    column order."""
    rows = {'nmos': [], 'pmos': []}
    used = {}
    for seg in segments:
        for kind, (fi, ti, hi) in (('nmos', (1, 2, 5)), ('pmos', (3, 4, 6))):
            cols = [col for _, col in seg]
            devices = []
            for stage, col in seg:
                if not col[hi]:
                    devices.append(None)
                    continue
                ids = device_id[stage, col[0], kind]
                n = used.get(ids[0], 0)
                used[ids[0]] = n + 1
                devices.append(ids[n])
            # The node between columns i-1 and i: whichever side has diffusion there
            # (both agree when both do); None where neither has.
            nodes = []
            for i in range(len(cols) + 1):
                left = cols[i - 1][ti] if i > 0 else None
                right = cols[i][fi] if i < len(cols) else None
                nodes.append(left if left is not None else right)
            rows[kind].append({'devices': devices, 'nodes': nodes})
    return rows


def build_layout_spec(network, hd=None):
    """The layout spec; hd is {'liberty': text, 'spice': text} of the installed HD library,
    or None to name no reference."""
    stages = network['stages']
    by_stage = {}
    for d in network['devices']:
        by_stage.setdefault(d['stage'], []).append(d)
    gate_of = {(st['output'], d['leaf']): d['gate'] for st in stages for d in by_stage[st['output']]}
    # A transistor of f fingers is f devices of W/f in parallel (ids <id>_f1 ... <id>_ff).
    finger_ids = {d['id']: [d['id']] if d['fingers'] == 1 else [f"{d['id']}_f{j + 1}" for j in range(d['fingers'])]
                  for d in network['devices']}
    device_id = {(d['stage'], d['leaf'], d['type']): finger_ids[d['id']] for d in network['devices']}

    def stage_fingers(st):
        ds = by_stage[st['output']]
        if all(d['fingers'] == 1 for d in ds):
            return None
        count = {(d['leaf'], d['type']): d['fingers'] for d in ds}
        return {leaf: (count[leaf, 'nmos'], count[leaf, 'pmos']) for leaf, _ in count}
    options = [stage_options(st, fingers=stage_fingers(st)) for st in stages]
    placements = place_stages(stages, options, gate_of, network['output'])
    if not placements:
        raise ValueError('no placement found')

    def devices_of(seq):
        """The devices with the nodes of this placement's reorderings."""
        chosen = {stages[s]['output']: option for s, option, _ in seq}
        out = []
        for d in network['devices']:
            _, _, n_edges, p_edges = chosen[d['stage']]
            a, b = (n_edges if d['type'] == 'nmos' else p_edges)[d['leaf']]
            if d['fingers'] == 1:
                out.append(dict(d, drain=a, source=b))
                continue
            w = round(d['w_um'] / d['fingers'], 6)
            for fid in finger_ids[d['id']]:
                out.append(dict(d, id=fid, drain=a, source=b, w_um=w, fingers=1, finger_of=d['id']))
        return out

    # The installed HD cell of the same name, when its function equals the request's under
    # some map of the pins (hd_reference.py). Maps that keep the most pin names are tried
    # first; for each, the first placement as good as the best in breaks and repeated
    # nets whose circuit is exactly HD's (series order included) is taken, so that LVS
    # against the HD cell can match. Otherwise the best placement and the first map stay.
    ports = [*network['inputs'], 'VGND', 'VNB', 'VPB', 'VPWR', network['output']]
    short = network['name'].split('__', 1)[-1]
    reference, maps, pick, same = None, [], 0, False
    if hd and short in hd_reference.REFERENCE_CELLS:
        reference = hd_reference.hd_cell(hd['liberty'], hd['spice'], short)
        if reference:
            maps = hd_reference.pin_maps(network['inputs'], network['output'],
                                         int(network['truth_table'], 16), reference)
            maps.sort(key=lambda m: -sum(k == v for k, v in m.items()))
        if not maps:
            reference = None
    if reference:
        best = placements[0][0][:2]
        equal = [i for i, (cost, _, _) in enumerate(placements) if cost[:2] == best]
        for m in maps:
            found = next((i for i in equal if hd_reference.same_circuit(
                hd_reference.renamed([dict(d, w=d['w_um'], l=d['l_um']) for d in devices_of(placements[i][1])], m),
                reference['devices'], reference['ports'])), None)
            if found is not None:
                pick, same, maps = found, True, [m]
                break
    (gaps, repeats, wire), seq, segments = placements[pick]
    devices = devices_of(seq)
    chosen = {stages[s]['output']: option for s, option, _ in seq}

    def same_graphs(seq_other):
        return all(chosen[stages[s]['output']] is o for s, o, _ in seq_other)

    spec = {
        'schema': 'dd2.layout-spec.v1', 'name': network['name'], 'request_id': network['request_id'],
        'inputs': network['inputs'], 'output': network['output'], 'truth_table': network['truth_table'],
        'sizing': network['sizing'], 'devices': devices,
        'euler_paths': euler_paths(segments, device_id),
        # Other placements with the same transistor graphs, for prepare_layout.py to try
        # in order when li1 routing fails on the first.
        'alternative_euler_paths': [euler_paths(segs, device_id) for i, (_, sq, segs) in enumerate(placements)
                                    if i != pick and same_graphs(sq)],
        'placement': {
            'stage_order': [stages[s]['output'] for s, _, _ in seq],
            'mirrored': [stages[s]['output'] for s, _, r in seq if r],
            'dual_trails_per_stage': {st['output']: len(o[0][1]) for st, o in zip(stages, options)},
            'diffusion_segments': len(segments), 'diffusion_breaks': gaps,
            'columns': sum(len(s) for s in segments), 'repeated_nets_in_rows': repeats,
            'folded': any(d['fingers'] != 1 for d in network['devices']),
            'wiring_slots': wire,
            'method': 'dual-trail cover with series reordering, after Maziasz and Hayes, DAC 1991',
        },
    }
    if reference:
        # Layout pins keep the request's names; verify_lvs.py renames them for LVS against HD.
        spec['hd_reference'] = reference['name']
        spec['hd_pin_mapping'] = maps[0]
        spec['hd_same_circuit'] = same
    return spec


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('network', type=Path, help='cells/<name>/network.json')
    ap.add_argument('--out', type=Path, help='default: layout_spec.json beside the network')
    ap.add_argument('--hd-spice', type=Path, help='PDK sky130_fd_sc_hd.spice (with --hd-liberty)')
    ap.add_argument('--hd-liberty', type=Path,
                    help='PDK sky130_fd_sc_hd__tt_025C_1v80.lib: with --hd-spice, names the installed '
                         'cell to compare with when its function matches the request')
    args = ap.parse_args()
    try:
        if bool(args.hd_spice) != bool(args.hd_liberty):
            raise ValueError('--hd-spice and --hd-liberty go together')
        hd = args.hd_spice and {'spice': args.hd_spice.read_text(encoding='utf-8', errors='replace'),
                                'liberty': args.hd_liberty.read_text(encoding='utf-8', errors='replace')}
        network = json.loads(args.network.read_text(encoding='utf-8'))
        spec = build_layout_spec(network, hd)
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
        print(f"    {kind}: " + ' | '.join(' '.join(f"{n or '-'} [{d or '-'}]" for n, d in zip(seg['nodes'], seg['devices']))
                                           + ' ' + (seg['nodes'][-1] or '-') for seg in spec['euler_paths'][kind]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
