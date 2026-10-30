#!/usr/bin/env python3
"""Steps 2 and 3 of the handout: from a request to a sized static CMOS network.

  1. Minimize: Quine-McCluskey (qm.py) on f and on !f gives every minimum
     sum-of-products of each.
  2. Formulas: each minimum SOP of f, and each minimum SOP of !f turned into a
     product-of-sums of f by De Morgan, is factored algebraically (pull a literal
     shared by several terms out, recursively) into AND/OR trees of literals.
  3. Stages: a static CMOS stage computes out = !G, where G is an AND/OR tree of its
     gate signals (AND = series in the pull-down, the pull-up is the dual). A stage
     may have at most 3 transistors in series in either network. For a formula
     tree T of f, a dynamic program chooses which subtrees become stages of their
     own ("cuts"). Inverting a stage's whole tree by De Morgan swaps AND/OR and
     complements every leaf, so each cut flips the polarity every literal below it
     needs. A cut subtree is either built in the flipped form (no inverter) or in
     the same form followed by an inverter. A literal needed in its complemented
     form costs one shared input inverter per input. The root stage is built so it
     outputs f directly, or outputs !f followed by an output inverter.
  4. Choice: fewest inverters, then fewest transistors, then fewest stages, over
     all formulas (--objective transistors puts transistors first).
  5. Sizing: 'hd' gives every NMOS the frame's x1 width (0.65 um) and every PMOS
     1.00 um; 'stack' multiplies the output stage's widths by each transistor's
     series depth (inner stages and inverters stay x1). Widths, L and models come
     from plan_hd.json; fingers = ceil(W / the frame's diffusion height).
Writes network.json (devices in the stage-spec format switch_level_check.py
reads), network.spice and network_schematic.png into --outdir.
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from qm import minimize
from request_reader import RequestError, validate_request

LIMIT_SERIES = 3
SUPPLIES = ('VGND', 'VNB', 'VPB', 'VPWR')


# ---------------------------------------------------------------- formula trees
# ('lit', input index, polarity) with polarity 1 for the true input, or
# ('and' | 'or', (children...)), flattened and sorted so equal trees are equal.

def lit(v, pol):
    return ('lit', v, pol)


def make(op, children):
    flat = set()
    for c in children:
        flat.update(c[1] if c[0] == op else (c,))
    if len(flat) == 1:
        return flat.pop()
    return (op, tuple(sorted(flat, key=repr)))


def leaves(t):
    return 1 if t[0] == 'lit' else sum(leaves(c) for c in t[1])


def dual_complement(t):
    """!t by De Morgan: AND <-> OR, every literal complemented."""
    if t[0] == 'lit':
        return lit(t[1], 1 - t[2])
    return make('or' if t[0] == 'and' else 'and', [dual_complement(c) for c in t[1]])


def evaluate(t, row):
    if t[0] == 'lit':
        return ((row >> t[1]) & 1) == t[2]
    values = (evaluate(c, row) for c in t[1])
    return all(values) if t[0] == 'and' else any(values)


def truth_table(t, n):
    return sum(1 << r for r in range(1 << n) if evaluate(t, r))


def text(t, names, top=True):
    if t[0] == 'lit':
        return names[t[1]] if t[2] else '!' + names[t[1]]
    body = (' & ' if t[0] == 'and' else ' | ').join(text(c, names, False) for c in t[1])
    return body if top else f'({body})'


def factorings(cubes, memo, limit=24):
    """Algebraic factorings of a set of cubes (frozensets of literals)."""
    key = frozenset(cubes)
    if key in memo:
        return memo[key]
    cubes = sorted(key, key=sorted)
    found = {make('or', [make('and', [lit(*l) for l in c]) for c in cubes])}
    counts = Counter(l for c in cubes for l in c)
    for l, k in sorted(counts.items()):
        if k < 2:
            continue
        with_l = [c - {l} for c in cubes if l in c]
        rest = [c for c in cubes if l not in c]
        if any(not c for c in with_l):
            continue
        for q in factorings(with_l, memo, limit):
            head = make('and', [lit(*l), q])
            for r in (factorings(rest, memo, limit) if rest else [None]):
                found.add(head if r is None else make('or', [head, r]))
    memo[key] = sorted(found, key=lambda t: (leaves(t), repr(t)))[:limit]
    return memo[key]


def formula_candidates(f_min, nf_min):
    """Factored trees of f from its minimum SOPs and from De Morgan of !f's."""
    memo, out = {}, {}
    for cover in f_min['_covers']:
        for t in factorings(cover, memo):
            out.setdefault(t, 'SOP of f')
    for cover in nf_min['_covers']:
        for t in factorings(cover, memo):
            out.setdefault(dual_complement(t), 'POS of f (De Morgan of the SOP of !f)')
    return out


# ---------------------------------------------------------------- stage search
# Within one stage, a subtree is described by (sa, so, mask):
#   sa  transistors in series through its AND chains (the pull-down's series count
#       when the stage is built as written, the pull-up's when built as the dual),
#   so  the same through its OR chains,
#   mask the inputs that some leaf needs complemented.
# Each key keeps its best cost (inverter stages, transistors, stages) and a plan.

def partitions(items, max_blocks):
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for p in partitions(rest, max_blocks):
        for i in range(len(p)):
            yield p[:i] + [[first] + p[i]] + p[i + 1:]
        if len(p) < max_blocks:
            yield [[first]] + p


class StageSearch:
    def __init__(self, inverter_first=True):
        # Costs are (inverter stages, transistors, stages); the objective orders them.
        self.order = (lambda c: c[:3]) if inverter_first else (lambda c: (c[1], c[0], c[2]))
        self.inline_memo, self.stage_memo = {}, {}

    def put(self, table, key, value):
        old = table.get(key)
        if old is None or self.order(value) < self.order(old):
            table[key] = value

    def inline(self, node, p):
        """Ways to build `node` inside a stage of parity p (p=1: built as the dual)."""
        key = (node, p)
        if key not in self.inline_memo:
            table = dict(self.body(node, p))
            if node[0] != 'lit':
                for k, v in self.cut(node, p).items():
                    self.put(table, k, v)
            self.inline_memo[key] = table
        return self.inline_memo[key]

    def body(self, node, p):
        """`node` spread inside the stage, without making it a stage of its own."""
        if node[0] == 'lit':
            needed = node[2] if p == 0 else 1 - node[2]
            mask = 0 if needed else 1 << node[1]
            return {(1, 1, mask): (0, 2, 0, ('lit', node[1], needed))}
        op, children = node
        table = {}
        for blocks in partitions(list(children), LIMIT_SERIES):
            if len(blocks) < 2:
                continue
            # A block of several children becomes a new stage (same operator).
            options = [self.inline(b[0], p) if len(b) == 1 else self.cut(make(op, b), p)
                       for b in blocks]
            acc = {(0, 0, 0): (0, 0, 0, ())}
            for opt in options:
                nxt = {}
                for (sa, so, m), (i1, t1, s1, plans) in acc.items():
                    for (sb, sob, mb), (i2, t2, s2, plan) in opt.items():
                        if op == 'and':
                            k = (sa + sb, max(so, sob), m | mb)
                        else:
                            k = (max(sa, sb), so + sob, m | mb)
                        if k[0] <= LIMIT_SERIES and k[1] <= LIMIT_SERIES:
                            self.put(nxt, k, (i1 + i2, t1 + t2, s1 + s2, plans + (plan,)))
                acc = nxt
            for k, (i, t, s, plans) in acc.items():
                self.put(table, k, (i, t, s, (op, plans)))
        return table

    def cut(self, node, p):
        """`node` as one gate signal of the current stage, built by its own stage."""
        table = {}
        # Flipped: the child stage is built as the opposite form, so it outputs
        # exactly the polarity this stage needs, with no inverter.
        for mask, (i, t, s, plan) in self.stage(node, 1 - p).items():
            self.put(table, (1, 1, mask), (i, t + 2, s, ('cut', 'flip', plan)))
        # Same form, then an inverter: literals below keep their polarity.
        for mask, (i, t, s, plan) in self.stage(node, p).items():
            self.put(table, (1, 1, mask), (i + 1, t + 4, s + 1, ('cut', 'invert', plan)))
        return table

    def stage(self, node, p_in):
        """A stage whose tree is `node` (cuts allowed below its root)."""
        key = (node, p_in)
        if key not in self.stage_memo:
            self.stage_memo[key] = {}   # guards against re-entry
            table = {}
            for (sa, so, mask), (i, t, s, plan) in self.body(node, p_in).items():
                self.put(table, mask, (i, t, s + 1, ('stage', p_in, plan)))
            self.stage_memo[key] = table
        return self.stage_memo[key]

    def root(self, tree):
        """Best ways to make f = tree at the cell output."""
        results = []
        # The root stage outputs !G; built as the dual of f's tree, !G = f.
        for mask, (i, t, s, plan) in self.stage(tree, 1).items():
            results.append((mask, i, t, s, plan, False))
        # Built as written it outputs !f; an output inverter restores f.
        for mask, (i, t, s, plan) in self.stage(tree, 0).items():
            results.append((mask, i + 1, t + 2, s + 1, plan, True))
        return results


def cost_key(entry, inverter_first):
    mask, inv_stages, transistors, stages = entry[:4]
    inverters = bin(mask).count('1') + inv_stages
    total = transistors + 2 * bin(mask).count('1')
    return (inverters, total, stages) if inverter_first else (total, inverters, stages)


# ---------------------------------------------------------------- building the network

class Builder:
    def __init__(self, names, output, frame, sizing):
        self.names, self.output, self.sizing = names, output, sizing
        devices = frame['frame']['devices']
        ref = frame['reference']
        self.model = {'nmos': devices['nmos'], 'pmos': devices['pmos']}
        self.length = devices['l']
        self.width = dict(ref['hd_x1_widths'])
        band = {'nmos': ref['nmos_diffusion_y'], 'pmos': ref['pmos_diffusion_y']}
        self.finger_max = {k: round(v[1] - v[0], 9) for k, v in band.items()}
        self.stages, self.devices = [], []
        self.count = Counter()

    def net(self, prefix):
        self.count[prefix] += 1
        return f'{prefix}{self.count[prefix]}'

    def input_net(self, v, polarity):
        return self.names[v] if polarity else self.names[v] + '_N'

    def build(self, stage_plan, out):
        """Create the stage for a ('stage', p_in, plan) and its children.

        Returns the stage's part of the decomposition tree: f's formula tree as it
        is implemented, every node tagged with the stage (output net) it belongs to.
        """
        _, p_in, plan = stage_plan
        tree, viz = self.gates(plan, p_in, out)
        self.add_stage(tree, out, 'written' if p_in == 0 else 'dual')
        return viz

    def gates(self, plan, p_in, stage):
        """Turn a plan into the stage's pull-down tree of gate nets, and its tree view."""
        kind = plan[0]
        if kind == 'lit':
            net = self.input_net(plan[1], plan[2])
            written = plan[2] if p_in == 0 else 1 - plan[2]    # the literal in f's formula
            return ('g', net), {'kind': 'lit', 'stage': stage, 'gate': net,
                                'text': ('' if written else '!') + self.names[plan[1]]}
        if kind == 'cut':
            _, mode, child = plan
            if mode == 'flip':
                net = self.net('n')
                return ('g', net), dict(self.build(child, net), cut='flip')
            inner, net = self.net('n'), self.net('n')
            child_viz = self.build(child, inner)
            self.add_stage(('g', inner), net, 'inverter')
            return ('g', net), {'kind': 'inv', 'stage': net, 'gate': inner, 'cut': 'invert',
                                'children': [child_viz]}
        op, children = plan
        # Built as written AND is series in the pull-down; built as the dual, OR is.
        series = (op == 'and') == (p_in == 0)
        built = [self.gates(c, p_in, stage) for c in children]
        return (('s' if series else 'p', tuple(t for t, _ in built)),
                {'kind': 'op', 'op': op, 'stage': stage,
                 'pull_down': 'series' if series else 'parallel',
                 'children': [v for _, v in built]})

    def add_stage(self, pdn, out, form):
        self.stages.append({'output': out, 'pdn': pdn, 'form': form})

    def finish(self, input_inverters):
        """Order stages inputs-first, then emit sized devices for each."""
        inverters = [{'output': self.input_net(v, 0), 'pdn': ('g', self.names[v]), 'form': 'input inverter'}
                     for v in input_inverters]
        # build() appends children before parents, so this order is topological.
        stages = inverters + self.stages
        serial = Counter()
        out = []
        for st in stages:
            pdn = st['pdn']
            pun = dual(pdn)
            final = st['output'] == self.output
            for kind, tree, top, bottom in (('nmos', pdn, st['output'], 'VGND'),
                                            ('pmos', pun, 'VPWR', st['output'])):
                # dual() keeps the leaf order, so leaf k is the same gate in both
                # networks: the NMOS/PMOS pair that shares one poly column.
                self.place(tree, top, bottom, kind, 0, final, serial, st['output'], [0])
            out.append({
                'output': st['output'],
                'kind': 'inverter' if pdn[0] == 'g' else 'complex',
                # written: the pull-down follows f's formula subtree; dual: its De Morgan dual.
                'form': st['form'],
                'function': f"{st['output']} = !({tree_text(pdn)})",
                'pull_down': tree_json(pdn), 'pull_up': tree_json(pun),
                'pull_down_series': series_count(pdn), 'pull_up_series': series_count(pun),
                'transistors': 2 * leaf_count(pdn),
            })
        return out

    def place(self, t, top, bottom, kind, extra, final, serial, stage, leaf):
        if t[0] == 'g':
            depth = extra + 1
            scale = depth if (self.sizing == 'stack' and final) else 1
            w = round(self.width[kind] * scale, 6)
            serial[kind] += 1
            # NMOS drain toward the output; PMOS source toward VPWR.
            drain, source = (top, bottom) if kind == 'nmos' else (bottom, top)
            self.devices.append({
                'id': f"{'N' if kind == 'nmos' else 'P'}{serial[kind]}", 'type': kind,
                'model': self.model[kind], 'gate': t[1], 'source': source, 'drain': drain,
                'bulk': 'VNB' if kind == 'nmos' else 'VPB', 'w_um': w, 'l_um': self.length,
                'stage': stage, 'leaf': leaf[0], 'stack_depth': depth,
                'fingers': math.ceil(w / self.finger_max[kind] - 1e-9)})
            leaf[0] += 1
            return
        if t[0] == 'p':
            for c in t[1]:
                self.place(c, top, bottom, kind, extra, final, serial, stage, leaf)
            return
        counts = [series_count(c) for c in t[1]]
        # Internal series nodes: <stage>_d<k> in the pull-down, <stage>_u<k> in the pull-up.
        prefix = f"{stage}_{'d' if kind == 'nmos' else 'u'}"
        nets = [top] + [self.net(prefix) for _ in t[1][1:]] + [bottom]
        for i, c in enumerate(t[1]):
            others = sum(counts) - counts[i]
            self.place(c, nets[i], nets[i + 1], kind, extra + others, final, serial, stage, leaf)


def dual(t):
    if t[0] == 'g':
        return t
    return ('p' if t[0] == 's' else 's', tuple(dual(c) for c in t[1]))


def series_count(t):
    if t[0] == 'g':
        return 1
    counts = [series_count(c) for c in t[1]]
    return sum(counts) if t[0] == 's' else max(counts)


def leaf_count(t):
    return 1 if t[0] == 'g' else sum(leaf_count(c) for c in t[1])


def tree_text(t, top=True):
    if t[0] == 'g':
        return t[1]
    body = (' & ' if t[0] == 's' else ' | ').join(tree_text(c, False) for c in t[1])
    return body if top else f'({body})'


def tree_json(t):
    if t[0] == 'g':
        return {'gate': t[1]}
    return {'op': 'series' if t[0] == 's' else 'parallel', 'children': [tree_json(c) for c in t[1]]}


# ---------------------------------------------------------------- checks and views

def simulate(stages, inputs, n):
    """Truth table of the output, evaluating stage functions in order (sanity check)."""
    def value(t, env):
        if t[0] == 'g':
            return env[t[1]]
        vals = [value(c, env) for c in t[1]]
        return all(vals) if t[0] == 's' else any(vals)
    tables = {}
    for row in range(1 << n):
        env = {name: bool((row >> i) & 1) for i, name in enumerate(inputs)}
        for st in stages:
            env[st['output']] = not value(st['_pdn'], env)
        for net, v in env.items():
            tables[net] = tables.get(net, 0) | (int(v) << row)
    return tables


def unateness(table, n, names):
    result = {}
    for i, name in enumerate(names):
        up = down = False
        for row in range(1 << n):
            if (row >> i) & 1:
                continue
            a, b = (table >> row) & 1, (table >> (row | 1 << i)) & 1
            up |= b > a
            down |= a > b
        result[name] = ('binate' if up and down else 'positive' if up
                        else 'negative' if down else 'independent')
    return result


def write_spice(network, path):
    ports = [*network['inputs'], *SUPPLIES, network['output']]
    lines = [f"* {network['name']}: {network['formula']}",
             '* Generated by synthesize.py; stages in signal order:']
    lines += [f"*   {s['function']}" for s in network['stages']]
    lines.append(f".subckt {network['name']} {' '.join(ports)}")
    for d in network['devices']:
        lines.append(f"X{d['id']} {d['drain']} {d['gate']} {d['source']} {d['bulk']} "
                     f"{d['model']} w={d['w_um']:.9g} l={d['l_um']:.9g}")
    lines.append(f".ends {network['name']}")
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def synthesize(request, frame, objective='inverters'):
    names, n = request['inputs'], len(request['inputs'])
    table = request['truth_table_value']
    full = (1 << (1 << n)) - 1
    if table in (0, full):
        raise ValueError('a constant function needs a tie cell, not a logic stage')
    f_min, nf_min = minimize(table, names), minimize(full & ~table, names)
    candidates = formula_candidates(f_min, nf_min)
    inverter_first = objective == 'inverters'
    best = None
    search = StageSearch(inverter_first)    # one memo: candidates share subtrees
    for tree, source in candidates.items():
        if truth_table(tree, n) != table:
            raise AssertionError(f'formula {text(tree, names)} does not compute the function')
        for entry in search.root(tree):
            key = cost_key(entry, inverter_first) + (leaves(tree), repr(tree))
            if best is None or key < best[0]:
                best = (key, entry, tree, source)
    if best is None:
        raise ValueError('no implementation within the series limit')
    _, (mask, inv_stages, _, _, plan, out_inv), tree, source = best

    builder = Builder(names, request['output'], frame, request['sizing'])
    if out_inv:
        inner = builder.net('n')
        root_viz = builder.build(plan, inner)
        builder.add_stage(('g', inner), request['output'], 'inverter')
        viz = {'kind': 'inv', 'stage': request['output'], 'gate': inner, 'cut': 'output',
               'children': [dict(root_viz, cut='invert')]}
    else:
        viz = builder.build(plan, request['output'])
    pdns = [st['pdn'] for st in builder.stages]
    input_inverters = [v for v in range(n) if (mask >> v) & 1]
    stages = builder.finish(input_inverters)
    for st, pdn in zip(stages, [('g', names[v]) for v in input_inverters] + pdns):
        st['_pdn'] = pdn
    tables = simulate(stages, names, n)
    if tables[request['output']] != table:
        raise AssertionError('the built network does not compute the function')
    for st in stages:
        del st['_pdn']

    # Inverters added to the logic: input inverters, inverters after cut stages and
    # the output inverter. (inv_1's single stage is the function itself, not one.)
    inverter_count = bin(mask).count('1') + inv_stages
    network = {
        'schema': 'dd2.network.v1', 'name': request['name'], 'request_id': request['request_id'],
        'inputs': names, 'output': request['output'], 'truth_table': request['truth_table'],
        'sizing': request['sizing'], 'objective': objective,
        'unateness': unateness(table, n, names),
        'minimization': {
            'f': {k: v for k, v in f_min.items() if not k.startswith('_')},
            'not_f': {k: v for k, v in nf_min.items() if not k.startswith('_')},
        },
        'formula': f"{request['output']} = {text(tree, names)}", 'formula_source': source,
        'formulas_considered': len(candidates),
        'cost': {
            'inverters': inverter_count, 'input_inverters': len(input_inverters),
            'output_inverter': out_inv,
            'stages': len(stages), 'complex_stages': sum(s['kind'] == 'complex' for s in stages),
            'transistors': len(builder.devices),
            'max_series_pull_down': max(s['pull_down_series'] for s in stages),
            'max_series_pull_up': max(s['pull_up_series'] for s in stages),
        },
        'stages': stages, 'devices': builder.devices,
        # f's formula tree as implemented: groups the search made are explicit
        # nodes, and every node names the stage (output net) it belongs to.
        'decomposition_tree': viz,
    }
    return network


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--request', required=True, type=Path,
                    help='A cell request, or the output of request_reader.py')
    ap.add_argument('--frame', required=True, type=Path, help='given/plan_hd.json')
    ap.add_argument('--outdir', required=True, type=Path, help='cells/<name>/')
    ap.add_argument('--objective', choices=('inverters', 'transistors'), default='inverters')
    ap.add_argument('--no-picture', action='store_true')
    args = ap.parse_args()
    try:
        raw = json.loads(args.request.read_text(encoding='utf-8'))
        if 'status' in raw:
            if raw['status'] != 'pass':
                raise RequestError('request_reader.py refused the request')
            raw = raw['request']
        request = validate_request(raw)
        frame = json.loads(args.frame.read_text(encoding='utf-8'))
        network = synthesize(request, frame, args.objective)
    except (OSError, ValueError, RequestError) as exc:
        print(f'FAIL: {exc}')
        return 1
    args.outdir.mkdir(parents=True, exist_ok=True)
    (args.outdir / 'network.json').write_text(json.dumps(network, indent=2) + '\n', encoding='utf-8')
    write_spice(network, args.outdir / 'network.spice')
    if not args.no_picture:
        from schematic_plot import draw_network
        from tree_plot import draw_tree
        draw_network(network, args.outdir / 'network_schematic.png')
        draw_tree(network, args.outdir / 'network_tree.png')
    c = network['cost']
    print(f"{network['name']}: {network['formula']} -> {c['stages']} stage{'s' if c['stages'] != 1 else ''} "
          f"({c['inverters']} inverter{'s' if c['inverters'] != 1 else ''} added), {c['transistors']} transistors, "
          f"max series {max(c['max_series_pull_down'], c['max_series_pull_up'])}")
    for st in network['stages']:
        print(f"    {st['function']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
