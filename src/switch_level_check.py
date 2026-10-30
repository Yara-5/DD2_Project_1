#!/usr/bin/env python3
"""Step 2 check and section 4.2 level 1: switch-level check of a transistor network.

For every input combination, each transistor is an ideal switch (NMOS on when its
gate is 1, PMOS when it is 0). A multi-stage network is evaluated in signal order:
a stage output settles once every gate in its stage is known. The output must
connect to the rail the request's truth table asks for, never to both; every inner
stage output must reach exactly one rail; VPWR must never connect to VGND.
The expected function comes from the request (--request, the output of
request_reader.py), or from the spec's own truth_table when no request is given.
Exits 0 only if every row passes.
"""
import argparse
import itertools
import json
import sys
from collections import deque
from pathlib import Path

from request_reader import output_bit

SUPPLIES = ('VPWR', 'VGND')


def load_spec(path, request_path=None):
    spec = json.loads(Path(path).read_text(encoding='utf-8'))
    inputs = spec['inputs']
    if not spec.get('devices') or not spec.get('output'):
        raise ValueError('The specification needs devices and output.')
    truth_table = int(spec['truth_table'], 16)
    if request_path:
        reader = json.loads(Path(request_path).read_text(encoding='utf-8'))
        if reader.get('status') != 'pass':
            raise ValueError('request_reader.py refused the request')
        request = reader['request']
        if request['inputs'] != inputs or request['output'] != spec['output']:
            raise ValueError('The request and the spec have different pins')
        if request['truth_table_value'] != truth_table:
            raise ValueError(f"The spec implements {spec['truth_table']}, the request asks for {request['truth_table']}")
        truth_table = request['truth_table_value']

    channel_nets = {n for d in spec['devices'] for n in (d['source'], d['drain'])}
    for d in spec['devices']:
        if d['type'].lower() not in ('nmos', 'pmos'):
            raise ValueError(f"Invalid device type: {d['type']}")
        # A gate is a primary input or the output of a stage of this network.
        if d['gate'] not in inputs and d['gate'] not in channel_nets:
            raise ValueError(f"{d['id']}: gate {d['gate']} is neither an input nor driven by a stage")
    if spec['output'] not in channel_nets:
        raise ValueError(f"the output {spec['output']} is not driven by any transistor")
    return spec, truth_table


def connected(graph, start, target):
    """True if conducting switches connect two circuit nodes."""
    queue = deque([start])
    visited = {start}
    while queue:
        node = queue.popleft()
        if node == target:
            return True
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return False


def conducting_graph(devices, values):
    # MOS source and drain are treated as bidirectional.
    # NMOS conducts when its gate is 1; PMOS when its gate is 0.
    graph = {}
    for d in devices:
        if values.get(d['gate']) == (1 if d['type'].lower() == 'nmos' else 0):
            graph.setdefault(d['source'], set()).add(d['drain'])
            graph.setdefault(d['drain'], set()).add(d['source'])
    return graph


def channel_groups(devices):
    """Nets joined by transistor channels, not counting paths through the rails:
    one group per stage (its output and the inner nodes of its series chains)."""
    parent = {}

    def find(n):
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for d in devices:
        a, b = d['source'], d['drain']
        if a not in SUPPLIES and b not in SUPPLIES:
            parent[find(a)] = find(b)
    return find


def resolve(spec, values):
    """Values of every stage output for one input vector, settled in signal order.

    A net settles once every transistor in its channel group has a known gate: 1 if
    conducting switches reach only VPWR, 0 if only VGND. Reaching both rails, or
    neither, is an error; so is a net that never settles (a loop).
    """
    find = channel_groups(spec['devices'])
    members = {}
    for d in spec['devices']:
        for n in {d['source'], d['drain']} - set(SUPPLIES):
            members.setdefault(find(n), []).append(d)
    pending = {d['gate'] for d in spec['devices'] if d['gate'] not in values} | {spec['output']}
    errors = {}
    while pending:
        ready = [n for n in sorted(pending)
                 if all(d['gate'] in values for d in members[find(n)])]
        if not ready:
            errors.update({n: 'never settles' for n in pending})
            break
        for net in ready:
            graph = conducting_graph(members[find(net)], values)
            high, low = connected(graph, net, 'VPWR'), connected(graph, net, 'VGND')
            if high == low:
                errors[net] = 'driven to both rails' if high else 'floating'
            values[net] = None if high == low else int(high)
            pending.discard(net)
    return values, errors


def check(spec, truth_table):
    inputs = spec['inputs']
    output = spec['output']
    rows = []

    for vector in itertools.product((0, 1), repeat=len(inputs)):
        values, errors = resolve(spec, dict(zip(inputs, vector)))
        graph = conducting_graph(spec['devices'], values)

        expected = output_bit(truth_table, dict(zip(inputs, vector)), inputs)
        rail_short = connected(graph, 'VPWR', 'VGND')
        output_on_high = connected(graph, output, 'VPWR')
        output_on_low = connected(graph, output, 'VGND')
        correct_rail = output_on_high if expected == 1 else output_on_low
        # A conducting path to both rails is always a failure, and so is any stage
        # output that floats or fights inside a multi-stage network.
        passed = (not rail_short and correct_rail and not (output_on_high and output_on_low)
                  and not errors)

        rows.append({
            'inputs': dict(zip(inputs, vector)),
            'expected_output': expected,
            'expected_rail': 'VPWR' if expected else 'VGND',
            'output_connected_to_VPWR': output_on_high,
            'output_connected_to_VGND': output_on_low,
            'VPWR_to_VGND_path': rail_short,
            'internal_nets': {n: v for n, v in values.items() if n not in inputs and n != output},
            'errors': errors,
            'passed': passed,
        })

    return {
        'check': 'switch_level',
        'cell': spec['name'],
        'truth_table': f'0x{truth_table:X}',
        'passed': all(row['passed'] for row in rows),
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('cell_spec', help='Stage spec: devices and Euler paths')
    parser.add_argument('--request', type=Path, help='Output of request_reader.py for this cell')
    parser.add_argument('--out', type=Path, help='JSON report path')
    args = parser.parse_args()

    try:
        spec, truth_table = load_spec(args.cell_spec, args.request)
        report = check(spec, truth_table)
    except (OSError, KeyError, ValueError) as exc:
        report = {'check': 'switch_level', 'cell_spec': str(args.cell_spec),
                  'passed': False, 'error': str(exc)}

    text = json.dumps(report, indent=2)
    print(text if not report['passed'] else
          f"PASS: {report['cell']} switch level, {len(report['rows'])} rows")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
