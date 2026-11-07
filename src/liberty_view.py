#!/usr/bin/env python3
"""Step 8 (handout sections 3.3 and 5): the cell's final Liberty view.

`characterize.py` writes `<cell>/characterized.lib`. This script checks it against the
request and the flow's own files, and only then writes it as the cell's Liberty view,
`<cell>/cell.lib`:
  * exactly one library group holding exactly one cell group, named as the request;
  * HD's units (time in ns, capacitance in pF; area in um2, as the HD Liberty, which
    has no area unit attribute);
  * the pins are exactly the request's inputs and output (plus the four pg_pins);
  * the output's `function` gives the request's truth table (AGENTS.md rule 6);
  * every input has one timing arc with cell_rise, cell_fall, rise_transition and
    fall_transition, each a full table over the characterization's slews and loads (at
    least 5 x 5, handout section 5), with finite values and positive transitions, and a
    timing_sense that matches the truth table; a non-unate input has two arcs, one
    positive_unate and one negative_unate, as the HD Liberty writes xor2_1;
  * every input has a positive capacitance;
  * the area is the layout's width x height (`layout_plan.json`);
  * the header names the ngspice version and binary SHA-256 of `characterized.json`,
    and that SHA-256 is the one `make check`'s stress test passed on
    (`results/check/ngspice_stress.log`): results from two builds are never mixed.

With --merge, the views of several cells are put into one library (`libs/`): their
library headers must be identical except for the name, and their simulator the same.

Inputs: --cell-dir cells/<name>/ (request.json, characterized.json, characterized.lib or
--liberty, layout_plan.json), --stress-log. Outputs: <cell>/cell.lib and the JSON report
(--report). Exits non-zero when any check fails or a file is missing; nothing is written
to cell.lib then. Standard library only.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

from request_reader import function_truth_table

TABLES = ('cell_rise', 'cell_fall', 'rise_transition', 'fall_transition')
MIN_TABLE = 5            # handout section 5: "or at least a 5 x 5 subset"


# ---------------------------------------------------------------- a small Liberty parser

TOKEN = re.compile(r'\s*(?:(/\*.*?\*/)|("(?:[^"\\]|\\.)*")|([{}();:,])|(\\\n)|([^\s{}();:,"]+))', re.S)


def tokens(text):
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match or match.end() == position:
            if text[position:].strip() == '':
                return
            raise ValueError(f'Liberty syntax error near: {text[position:position + 40]!r}')
        position = match.end()
        comment, string, punct, _, word = match.groups()
        if comment is not None:
            yield ('comment', comment)
        elif string is not None:
            yield ('word', string[1:-1])
        elif punct is not None:
            yield ('punct', punct)
        elif word is not None:
            yield ('word', word)


class Group:
    def __init__(self, kind, args):
        self.kind, self.args = kind, args
        self.attributes = {}       # simple attributes: name -> value
        self.complex = {}          # complex attributes: name -> list of argument lists
        self.groups = []

    def find(self, kind):
        return [g for g in self.groups if g.kind == kind]


def parse(text):
    """Return (top-level groups, comments) of a Liberty file."""
    stream = list(tokens(text))
    comments = [v for k, v in stream if k == 'comment']
    stream = [(k, v) for k, v in stream if k != 'comment']
    position = 0

    def expect(value):
        nonlocal position
        if position >= len(stream) or stream[position][1] != value:
            got = stream[position][1] if position < len(stream) else 'end of file'
            raise ValueError(f'Liberty syntax error: expected {value!r}, got {got!r}')
        position += 1

    def arguments():
        nonlocal position
        expect('(')
        values, current = [], []
        while stream[position][1] != ')':
            kind, value = stream[position]
            position += 1
            if kind == 'punct' and value == ',':
                values.append(' '.join(current)); current = []
            else:
                current.append(value)
        if current:
            values.append(' '.join(current))
        expect(')')
        return values

    def body(group):
        nonlocal position
        while stream[position][1] != '}':
            name = stream[position][1]
            position += 1
            if stream[position][1] == ':':
                position += 1
                value = []
                while stream[position][1] not in (';', '}'):
                    value.append(stream[position][1]); position += 1
                if stream[position][1] == ';':
                    position += 1
                group.attributes[name] = ' '.join(value)
            elif stream[position][1] == '(':
                args = arguments()
                if position < len(stream) and stream[position][1] == '{':
                    position += 1
                    child = Group(name, args)
                    body(child)
                    expect('}')
                    group.groups.append(child)
                else:
                    if position < len(stream) and stream[position][1] == ';':
                        position += 1
                    group.complex.setdefault(name, []).append(args)
            else:
                raise ValueError(f'Liberty syntax error after {name!r}')

    top = Group('file', [])
    while position < len(stream):
        name = stream[position][1]; position += 1
        args = arguments()
        expect('{')
        group = Group(name, args)
        body(group)
        expect('}')
        top.groups.append(group)
    return top.groups, comments


def numbers(text):
    return [float(x) for x in text.replace('\\', ' ').replace('\n', ' ').split(',') if x.strip()]


# ---------------------------------------------------------------- checks

def unateness(table, inputs, pin):
    """'positive_unate', 'negative_unate' or 'non_unate' of the function in `pin`."""
    k = inputs.index(pin)
    rises = falls = False
    for row in range(1 << len(inputs)):
        if (row >> k) & 1:
            continue
        low, high = (table >> row) & 1, (table >> (row | 1 << k)) & 1
        rises |= high > low
        falls |= high < low
    return 'non_unate' if rises and falls else 'negative_unate' if falls else 'positive_unate'


def check_view(text, request, characterized, plan, stress_sha):
    """Every check of the module docstring; returns (checks, cell group or None)."""
    checks = {}

    def record(name, ok, detail=None):
        checks[name] = {'passed': bool(ok), **({'detail': detail} if detail is not None else {})}

    try:
        groups, comments = parse(text)
    except (ValueError, IndexError) as exc:
        record('parses', False, str(exc))
        return checks, None
    record('parses', True)
    libraries = [g for g in groups if g.kind == 'library']
    record('one_library_group', len(groups) == 1 and len(libraries) == 1, f'{len(groups)} top-level groups')
    if not libraries:
        return checks, None
    library = libraries[0]
    cells = library.find('cell')
    record('one_cell_group', len(cells) == 1, f'{len(cells)} cell groups')
    if len(cells) != 1:
        return checks, None
    cell = cells[0]
    name = cell.args[0] if cell.args else ''
    record('cell_name', name == request['name'], f'{name!r} against {request["name"]!r}')

    cap_unit = library.complex.get('capacitive_load_unit', [[None, None]])[0]
    units_ok = (library.attributes.get('time_unit') == '1ns' and len(cap_unit) == 2
                and float(cap_unit[0]) == 1.0 and cap_unit[1].lower() == 'pf')
    record('units_ns_pf', units_ok, {'time_unit': library.attributes.get('time_unit'),
                                     'capacitive_load_unit': cap_unit})

    pins = {p.args[0]: p for p in cell.find('pin')}
    expected = set(request['inputs']) | {request['output']}
    record('pins_are_the_request', set(pins) == expected,
           {'liberty': sorted(pins), 'request': sorted(expected)})
    pg = sorted(p.args[0] for p in cell.find('pg_pin'))
    record('pg_pins', pg == ['VGND', 'VNB', 'VPB', 'VPWR'], pg)
    directions_ok = all(pins[p].attributes.get('direction') == 'input' for p in request['inputs'] if p in pins) \
        and request['output'] in pins and pins[request['output']].attributes.get('direction') == 'output'
    record('pin_directions', directions_ok)
    if request['output'] not in pins:
        return checks, cell
    output = pins[request['output']]

    function = output.attributes.get('function', '')
    try:
        table = function_truth_table(function, request['inputs'])
        record('function_is_the_request', table == request['truth_table_value'],
               {'function': function, 'function_truth_table': f'0x{table:X}',
                'request_truth_table': request['truth_table']})
    except ValueError as exc:
        record('function_is_the_request', False, f'{function!r}: {exc}')

    slews = characterized['setup']['slews_ns']
    loads = characterized['setup']['loads_pf']
    arcs = {}
    for timing in output.find('timing'):
        arcs.setdefault(timing.attributes.get('related_pin'), []).append(timing)
    problems = []
    if len(slews) < MIN_TABLE or len(loads) < MIN_TABLE:
        problems.append(f'characterized over {len(slews)} x {len(loads)} points, less than {MIN_TABLE} x {MIN_TABLE}')
    for pin in request['inputs']:
        found = arcs.get(pin, [])
        sense = unateness(request['truth_table_value'], request['inputs'], pin)
        # A non-unate arc is written as HD writes it (xor2_1; S of mux2_1): one positive_unate
        # and one negative_unate timing group, so both output edges for both input edges.
        wanted = ['negative_unate', 'positive_unate'] if sense == 'non_unate' else [sense]
        got = sorted(t.attributes.get('timing_sense', '') for t in found)
        if got != wanted:
            problems.append(f'{pin}: timing groups {got}, the truth table ({sense}) needs {wanted}'); continue
        for timing in found:
            for key in TABLES:
                groups = timing.find(key)
                if len(groups) != 1:
                    problems.append(f'{pin} {key}: {len(groups)} tables'); continue
                t = groups[0]
                try:
                    index_1 = numbers(t.complex['index_1'][0][0])
                    index_2 = numbers(t.complex['index_2'][0][0])
                    values = [numbers(row) for row in t.complex['values'][0]]
                except (KeyError, IndexError, ValueError) as exc:
                    problems.append(f'{pin} {key}: unreadable table ({exc})'); continue
                if not (len(index_1) == len(slews) and all(math.isclose(a, b, rel_tol=1e-6) for a, b in zip(index_1, slews))
                        and len(index_2) == len(loads) and all(math.isclose(a, b, rel_tol=1e-6) for a, b in zip(index_2, loads))):
                    problems.append(f'{pin} {key}: indices differ from the characterization')
                if len(values) != len(slews) or any(len(row) != len(loads) for row in values):
                    problems.append(f'{pin} {key}: {len(values)} rows, not {len(slews)} x {len(loads)}')
                flat = [v for row in values for v in row]
                if not all(math.isfinite(v) for v in flat) or ('transition' in key and min(flat, default=0) <= 0):
                    problems.append(f'{pin} {key}: non-finite values or a transition <= 0')
    extra = sorted(set(arcs) - set(request['inputs']))
    if extra:
        problems.append(f'timing arcs on pins that are not inputs: {extra}')
    record('full_timing_tables', not problems, problems or f'{len(request["inputs"])} arcs, {len(slews)} x {len(loads)}')

    caps = {p: pins[p].attributes.get('capacitance') for p in request['inputs'] if p in pins}
    record('input_capacitance', all(c is not None and float(c) > 0 for c in caps.values()) and len(caps) == len(request['inputs']),
           caps)

    area = float(cell.attributes.get('area', 'nan'))
    layout_area = plan['dimensions']['cell_width_um'] * plan['dimensions']['cell_height_um']
    record('area_is_the_layout', math.isclose(area, layout_area, rel_tol=1e-9, abs_tol=1e-9),
           {'liberty_um2': area, 'layout_um2': layout_area})

    simulator = characterized.get('simulator', {})
    header = ' '.join(comments)
    named = simulator.get('version', '?') in header and simulator.get('sha256', '?') in header
    record('simulator_recorded', named, simulator)
    record('simulator_is_the_checked_build', simulator.get('sha256') == stress_sha,
           {'liberty': simulator.get('sha256'), 'make_check_stress_test': stress_sha})
    return checks, cell


def stress_sha256(path):
    """The ngspice SHA-256 that make check's stress test printed, if that test passed."""
    text = Path(path).read_text(encoding='utf-8')
    if 'PASS' not in text:
        raise ValueError(f'{path}: the ngspice stress test did not pass')
    match = re.search(r'sha256\s+([0-9a-f]{64})', text)
    if not match:
        raise ValueError(f'{path}: no ngspice SHA-256')
    return match[1]


def merge(lib_paths, out):
    """One library holding the cell groups of several validated views."""
    headers, blocks, shas = [], [], set()
    for path in lib_paths:
        text = Path(path).read_text(encoding='utf-8')
        start = text.index('library (')
        cell = re.search(r'(?m)^    cell \(', text)
        end = text.rstrip().rindex('}')
        header = re.sub(r'library \("[^"]*"\)', 'library ("NAME")', text[start:cell.start()])
        headers.append(header)
        blocks.append(text[cell.start():end].rstrip() + '\n')
        shas.update(re.findall(r'SHA-256 ([0-9a-f]{64})', text[:start]))
    if len(set(headers)) != 1:
        raise ValueError('the library headers differ (units, thresholds or templates)')
    if len(shas) != 1:
        raise ValueError(f'the views come from {len(shas)} ngspice builds')
    first = Path(lib_paths[0]).read_text(encoding='utf-8')
    comment = first[:first.index('library (')].strip().splitlines()
    text = '\n'.join([f'/* dd2_g5 generated cells, merged by src/liberty_view.py from {len(lib_paths)} cell views.',
                      *[line for line in comment[1:]]]) + '\n'
    text += headers[0].replace('library ("NAME")', f'library ("{Path(out).stem}")') + ''.join(blocks) + '}\n'
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding='utf-8')
    return {'cells': len(blocks), 'sha256': shas.pop()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cell-dir', type=Path, help='cells/<name>/')
    ap.add_argument('--liberty', type=Path, help='The Liberty file to check (default: <cell-dir>/characterized.lib)')
    ap.add_argument('--stress-log', type=Path, default=Path('results/check/ngspice_stress.log'))
    ap.add_argument('--report', type=Path, help='JSON report (default: results/views/<cell without the dd2_g5__ prefix>.json)')
    ap.add_argument('--no-write', action='store_true', help='Check only; do not write cell.lib (broken cases)')
    ap.add_argument('--merge', type=Path, nargs='+', help='cell.lib files to merge into --out')
    ap.add_argument('--out', type=Path, help='The merged library (with --merge)')
    args = ap.parse_args()

    if args.merge:
        try:
            result = merge(args.merge, args.out)
            print(f'PASS: {args.out}: {result["cells"]} cells, ngspice SHA-256 {result["sha256"][:12]}...')
            return 0
        except (OSError, ValueError) as exc:
            print(f'FAIL: merging into {args.out}: {exc}')
            return 1

    if not args.cell_dir:
        ap.error('--cell-dir is required without --merge')
    report = {'check': 'liberty_view', 'cell': args.cell_dir.name, 'passed': False}
    try:
        reader = json.loads((args.cell_dir / 'request.json').read_text(encoding='utf-8'))
        if reader.get('status') != 'pass':
            raise ValueError('request_reader.py refused the request')
        request = reader['request']
        characterized = json.loads((args.cell_dir / 'characterized.json').read_text(encoding='utf-8'))
        if not characterized.get('passed'):
            raise ValueError('characterize.py did not characterize this cell')
        plan = json.loads((args.cell_dir / 'layout_plan.json').read_text(encoding='utf-8'))
        source = args.liberty or args.cell_dir / 'characterized.lib'
        text = source.read_text(encoding='utf-8')
        checks, _ = check_view(text, request, characterized, plan, stress_sha256(args.stress_log))
        report.update(liberty=source.as_posix(), checks=checks,
                      passed=all(c['passed'] for c in checks.values()))
        if report['passed'] and not args.no_write:
            (args.cell_dir / 'cell.lib').write_text(text, encoding='utf-8')
            report['written'] = (args.cell_dir / 'cell.lib').as_posix()
    except (OSError, KeyError, ValueError) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'

    path = args.report or Path('results/views') / (args.cell_dir.name.removeprefix('dd2_g5__') + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    failing = [k for k, c in report.get('checks', {}).items() if not c['passed']]
    print(f"{'PASS' if report['passed'] else 'FAIL'}: Liberty view, {report['cell']}"
          + (f" ({report['error']})" if 'error' in report else f"; failing: {failing}" if failing else ''))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
