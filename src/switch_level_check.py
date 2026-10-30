#!/usr/bin/env python3
"""Step 2 check and section 4.2 level 1: switch-level check of a transistor network.

For every input combination, each transistor is an ideal switch (NMOS on when its
gate is 1, PMOS when it is 0). The output must connect to the rail the request's
truth table asks for, never to both, and VPWR must never connect to VGND.
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

    for d in spec['devices']:
        if d['type'].lower() not in ('nmos', 'pmos'):
            raise ValueError(f"Invalid device type: {d['type']}")
        if d['gate'] not in inputs:
            # A multi-stage network needs the internal stage outputs evaluated first.
            raise ValueError(f"{d['id']}: gate {d['gate']} is not a primary input; "
                             'multi-stage networks are not supported yet.')
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


def check(spec, truth_table):
    inputs = spec['inputs']
    output = spec['output']
    rows = []

    for vector in itertools.product((0, 1), repeat=len(inputs)):
        values = dict(zip(inputs, vector))
        graph = {}

        # MOS source and drain are treated as bidirectional.
        # NMOS conducts when its gate is 1; PMOS when its gate is 0.
        for device in spec['devices']:
            gate_value = values[device['gate']]
            is_on = gate_value == 1 if device['type'].lower() == 'nmos' else gate_value == 0
            if is_on:
                graph.setdefault(device['source'], set()).add(device['drain'])
                graph.setdefault(device['drain'], set()).add(device['source'])

        expected = output_bit(truth_table, values, inputs)
        rail_short = connected(graph, 'VPWR', 'VGND')
        output_on_high = connected(graph, output, 'VPWR')
        output_on_low = connected(graph, output, 'VGND')
        correct_rail = output_on_high if expected == 1 else output_on_low
        # A conducting path to both rails is always a failure.
        passed = not rail_short and correct_rail and not (output_on_high and output_on_low)

        rows.append({
            'inputs': values,
            'expected_output': expected,
            'expected_rail': 'VPWR' if expected else 'VGND',
            'output_connected_to_VPWR': output_on_high,
            'output_connected_to_VGND': output_on_low,
            'VPWR_to_VGND_path': rail_short,
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
