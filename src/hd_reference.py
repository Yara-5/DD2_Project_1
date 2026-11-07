#!/usr/bin/env python3
"""Handout Table 4: which installed HD cell a generated cell is compared with, and how.

A library module, used by euler.py (step 4: names the reference, maps its pins, prefers
HD's series order among equal-cost Euler solutions) and by verify_lvs.py (step 6: decides
whether LVS against the HD cell must match). Standard library only.

  * REFERENCE_CELLS: the Table 6 cells compared with HD (docs/TECHNICAL.md, section 21);
    MUST_MATCH: the six of section 4.1, whose transistors must match HD's.
  * pin_maps: every map of the request's inputs onto the HD cell's inputs under which the
    HD Liberty 'function' equals the request's truth table (input 0 is the least
    significant bit, AGENTS.md rule 6). The map comes from the function, never from the
    order or the names of the pins.
  * spice_devices, same_circuit: the transistors of a subcircuit, and whether two such
    lists are the same circuit (ports fixed by name, internal nets free, W/L equal).
"""
import itertools
import re

from request_reader import function_truth_table, output_bit

REFERENCE_CELLS = ('inv_1', 'nand2_1', 'nor2_1', 'nand3_1', 'nor3_1',
                   'a21oi_1', 'o21ai_1', 'a21o_1', 'mux2_1')
# Handout section 4.1: these six must match the installed cell ("must match" in Table 4,
# transistors included); a21o_1 and mux2_1 (and nor3_1) are compared and reported.
MUST_MATCH = ('inv_1', 'nand2_1', 'nor2_1', 'nand3_1', 'a21oi_1', 'o21ai_1')
SUPPLIES = ('VGND', 'VNB', 'VPB', 'VPWR')
SUFFIX = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9,
          'p': 1e-12, 'f': 1e-15}


def subckt_text(text, name):
    match = re.search(rf'(?ims)^\.subckt\s+{re.escape(name)}\s.*?^\.ends\b', text)
    if not match:
        raise ValueError(f'subcircuit {name} not found')
    return match[0]


def spice_value(token):
    """A SPICE number with an optional scale suffix; sky130 HD writes um as e.g. 650000u
    under its scale of 1e-6, so both netlists come out in um."""
    match = re.fullmatch(r'([-+]?[\d.]+(?:e[-+]?\d+)?)(meg|[tgkmunpf])?', token.lower())
    if not match:
        raise ValueError(f'not a SPICE number: {token}')
    return float(match[1]) * SUFFIX.get(match[2], 1.0)


def spice_devices(text, name):
    """Ports and transistors (model, gate, drain, source, bulk, w, l) of one subcircuit."""
    body = re.sub(r'\n\+', ' ', subckt_text(text, name))
    lines = body.splitlines()
    ports = lines[0].split()[2:]
    devices = []
    for line in lines[1:]:
        tokens = line.split()
        if not tokens or tokens[0][0].lower() not in 'xm':
            continue
        params = dict(t.split('=', 1) for t in tokens[6:] if '=' in t)
        devices.append({'model': tokens[5], 'drain': tokens[1], 'gate': tokens[2],
                        'source': tokens[3], 'bulk': tokens[4],
                        'w': spice_value(params['w']), 'l': spice_value(params['l'])})
    return ports, devices


def renamed(devices, mapping):
    return [{k: mapping.get(v, v) if k in ('drain', 'gate', 'source', 'bulk') else v
             for k, v in d.items()} for d in devices]


def same_circuit(ours, theirs, ports):
    """True when the two transistor lists are one circuit: every port net is the same net
    in both, internal nets correspond one to one, each transistor has the same model, W
    and L, and source and drain may swap. Backtracking over the transistors."""
    def size(d):
        return d['model'], round(d['w'], 4), round(d['l'], 4)
    if sorted(map(size, ours)) != sorted(map(size, theirs)):
        return False
    ports = set(ports)

    def bind(m, a, b):
        if a in ports or b in ports:
            return m if a == b else None
        if a in m:
            return m if m[a] == b else None
        if b in m.values():
            return None
        return dict(m, **{a: b})

    def go(i, m, used):
        if i == len(ours):
            return True
        a = ours[i]
        for j, b in enumerate(theirs):
            if j in used or size(a) != size(b):
                continue
            for b_drain, b_source in ((b['drain'], b['source']), (b['source'], b['drain'])):
                n = m
                for x, y in ((a['gate'], b['gate']), (a['bulk'], b['bulk']),
                             (a['drain'], b_drain), (a['source'], b_source)):
                    n = bind(n, x, y) if n is not None else None
                if n is not None and go(i + 1, n, used | {j}):
                    return True
        return False
    return go(0, {}, frozenset())


def hd_cell(liberty_text, spice_text, short):
    """The installed cell sky130_fd_sc_hd__<short>: ports, transistors, inputs, output and
    function (from the Liberty file), or None when the library has no such cell."""
    from characterize import shipped_cell      # imported here: characterize imports verify_lvs
    name = f'sky130_fd_sc_hd__{short}'
    try:
        ports, devices = spice_devices(spice_text, name)
    except ValueError:
        return None
    pins = shipped_cell(liberty_text, name)['pins']
    inputs = [p for p in ports if pins.get(p, {}).get('direction') == 'input']
    outputs = [p for p in ports if pins.get(p, {}).get('direction') == 'output']
    if len(outputs) != 1 or sorted(inputs + outputs + list(SUPPLIES)) != sorted(ports):
        raise ValueError(f'{name}: unexpected ports {ports}')
    return {'name': name, 'ports': ports, 'devices': devices, 'inputs': inputs,
            'output': outputs[0], 'function': pins[outputs[0]]['function']}


def pin_maps(inputs, output, truth_table, hd):
    """Every {our pin: HD pin} under which the HD function equals our truth table."""
    if len(inputs) != len(hd['inputs']):
        return []
    hd_table = function_truth_table(hd['function'], hd['inputs'])
    rows = [dict(zip(inputs, bits)) for bits in itertools.product((0, 1), repeat=len(inputs))]
    found = []
    for perm in itertools.permutations(hd['inputs']):
        m = dict(zip(inputs, perm))
        if all(output_bit(truth_table, v, inputs) ==
               output_bit(hd_table, {m[k]: b for k, b in v.items()}, hd['inputs']) for v in rows):
            found.append(dict(m, **{output: hd['output']}))
    return found
