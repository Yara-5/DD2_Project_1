#!/usr/bin/env python3
"""Section 5.1, Table 5: validate the characterizer against the shipped HD Liberty.

Inputs: the shipped sky130_fd_sc_hd__tt_025C_1v80.lib, and one directory per simulator
build (or per broken case) holding the <out>.json reports of
`characterize.py --hd-cell <cell> --hd-gds ... --out <dir>/<cell>` for the six
reference cells (inv_1, nand2_1, nor2_1, nand3_1, a21oi_1, o21ai_1).

Checks (Table 5), per directory:
  setup        voltage, temperature, thresholds, units, process, derate and the table
               indices in the generated .lib equal the shipped ones, exactly; the run's
               table indices are a subset of the shipped ones;
  delay/trans  every entry of cell_rise, cell_fall, rise_transition, fall_transition of
               every arc, relative to the shipped value: median (must be <= 5 % for the
               --fixed build), maximum and where it occurs, pooled over the six cells;
  input cap    every input pin within 10 % of the shipped capacitance (--fixed build);
  syntax       with --syntax-check, the generated .lib loads in Yosys and OpenSTA;
  broken case  --broken DIR (tables indexed by the full ramp) must be flagged: its median
               must exceed the limit. A broken case that is not flagged is a FAIL.
The --stock build is reported, not judged. Standard library only.
"""
from __future__ import annotations
import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

from characterize import liberty_block, liberty_setup

KEYS = ('cell_rise', 'cell_fall', 'rise_transition', 'fall_transition')
MEDIAN_LIMIT, CAP_LIMIT = 0.05, 0.10
SETUP_KEYS = ('nom_voltage', 'nom_temperature', 'nom_process', 'slew_derate_from_library',
              'input_threshold_pct_rise', 'input_threshold_pct_fall', 'output_threshold_pct_rise',
              'output_threshold_pct_fall', 'slew_lower_threshold_pct_rise', 'slew_lower_threshold_pct_fall',
              'slew_upper_threshold_pct_rise', 'slew_upper_threshold_pct_fall', 'units', 'operating_conditions')


def numbers(text):
    return [float(x) for x in re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', text)]


def balanced(text, start):
    """text[start:] up to and including the brace that closes the first '{'."""
    depth, i = 0, text.index('{', start)
    for j in range(i, len(text)):
        depth += {'{': 1, '}': -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise ValueError('unbalanced braces in the Liberty file')


def shipped_timing(lib_text, cell):
    """{(related_pin, sense): [group]} of the cell's output pin; a group is {'when': str|None,
    key: {'index_1': [...], 'index_2': [...], 'values': [[...]]}}."""
    block = liberty_block(lib_text, 'cell', cell)
    out = {}
    for m in re.finditer(r'(?<![\w])timing\s*\(\s*\)\s*\{', block):
        group = balanced(block, m.start())
        pin = re.search(r'related_pin\s*:\s*"(\w+)"', group)[1]
        sense = re.search(r'timing_sense\s*:\s*"(\w+)"', group)
        when = re.search(r'\bwhen\s*:\s*"([^"]+)"', group)
        entry = {'when': when[1] if when else None}
        for key in KEYS:
            t = re.search(rf'(?<![\w]){key}\s*\(', group)
            if not t:
                continue
            body = balanced(group, t.start())
            i1, i2 = (numbers(re.search(rf'index_{n}\s*\(\s*"([^"]+)"', body)[1]) for n in (1, 2))
            raw = re.search(r'values\s*\((.*?)\)\s*;', body, re.S)[1]
            rows = [numbers(r) for r in re.findall(r'"([^"]+)"', raw)]
            entry[key] = {'index_1': i1, 'index_2': i2, 'values': rows}
        out.setdefault((pin, sense[1] if sense else None), []).append(entry)
    return out


def pick(groups, key):
    """The shipped table to compare with. One group per pin and sense is the usual HD TT
    layout; if there are several (state-dependent timing), the elementwise worst is used
    and the comparison says so."""
    tables = [g[key] for g in groups if key in g]
    if len(tables) == 1:
        return tables[0], False
    worst = [[max(t['values'][i][j] for t in tables) for j in range(len(tables[0]['values'][0]))]
             for i in range(len(tables[0]['values']))]
    return dict(tables[0], values=worst), True


def index_of(values, x):
    for i, v in enumerate(values):
        if abs(v - x) <= 1e-6 * max(1.0, abs(v)):
            return i
    raise ValueError(f'index value {x} is not an index of the shipped table {values}')


def compare_cell(run, lib_text, shipped_lib_path):
    cell = run['cell']
    timing, notes, entries = shipped_timing(lib_text, cell), [], []
    slews, loads = run['setup']['slews_ns'], run['setup']['loads_pf']
    for t in run['timing']:
        groups = timing.get((t['pin'], t['sense']))
        if not groups:
            raise ValueError(f"{cell}: no shipped arc {t['pin']} {t['sense']}")
        for key in KEYS:
            ship, merged = pick(groups, key)
            if merged:
                notes.append(f"{cell} {t['pin']} {key}: {len(groups)} shipped state-dependent groups, worst used")
            for si, s in enumerate(slews):
                for li, c in enumerate(loads):
                    a, b = index_of(ship['index_1'], s), index_of(ship['index_2'], c)
                    ref, got = ship['values'][a][b], t['tables'][key][si][li]
                    entries.append({'cell': cell, 'pin': t['pin'], 'table': key, 'slew_ns': s, 'load_pf': c,
                                    'shipped': ref, 'measured': got, 'error': abs(got - ref) / abs(ref)})
    shipped_pins = {m[1]: m[0] for m in re.finditer(r'pin \("(\w+)"\)', liberty_block(lib_text, 'cell', cell))}
    caps = []
    block = liberty_block(lib_text, 'cell', cell)
    for pin, c in run['capacitance_pf'].items():
        body = liberty_block(block, 'pin', pin)
        ref = float(re.search(r'(?<![\w])capacitance\s*:\s*([\d.eE+-]+)', body)[1])
        caps.append({'cell': cell, 'pin': pin, 'shipped': ref, 'measured': c['capacitance'],
                     'error': abs(c['capacitance'] - ref) / ref})
    return entries, caps, notes


def setup_check(run, shipped_lib_path):
    """The generated .lib's setup equals the shipped one, re-read here, not trusted from the run."""
    cell = run['cell']
    want, _ = liberty_setup(shipped_lib_path, cell)
    got, _ = liberty_setup(Path(run['liberty']), cell)
    bad = [k for k in SETUP_KEYS if want[k] != got[k]]
    subset = all(any(abs(s - v) < 1e-9 for v in want['slews_ns']) for s in got['slews_ns']) and \
        all(any(abs(c - v) < 1e-9 for v in want['loads_pf']) for c in got['loads_pf'])
    return {'cell': cell, 'mismatched': bad, 'indices_subset_of_shipped': subset,
            'passed': not bad and subset}


def syntax_check(lib, tools):
    results = {}
    for name, cmd in tools.items():
        proc = subprocess.run(cmd.format(lib=lib), shell=True, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False)
        results[name] = {'passed': proc.returncode == 0 and not re.search(r'\berror\b', proc.stdout, re.I),
                         'tail': proc.stdout[-300:]}
    return results


def evaluate(directory, shipped_path, lib_text, syntax):
    runs = [json.loads(p.read_text()) for p in sorted(Path(directory).glob('*.json'))]
    runs = [r for r in runs if r.get('check') == 'characterization']
    if not runs:
        raise ValueError(f'no characterize.py reports in {directory}')
    entries, caps, notes, setups, syn = [], [], [], [], {}
    for r in runs:
        if not r['passed']:
            raise ValueError(f"{r.get('cell')}: characterization failed: {r.get('error')}")
        e, c, n = compare_cell(r, lib_text, shipped_path)
        entries += e; caps += c; notes += n
        setups.append(setup_check(r, shipped_path))
        if syntax:
            syn[r['cell']] = syntax_check(r['liberty'], syntax)
    errors = [x['error'] for x in entries]
    worst = max(entries, key=lambda x: x['error'])
    per_cell = {c: statistics.median(x['error'] for x in entries if x['cell'] == c)
                for c in sorted({x['cell'] for x in entries})}
    per_table = {k: statistics.median(x['error'] for x in entries if x['table'] == k) for k in KEYS}
    sim = runs[0].get('simulator', {})
    return {'directory': str(directory), 'cells': [r['cell'] for r in runs],
            'simulator': {'version': sim.get('version'), 'sha256': sim.get('sha256')},
            'index_by_ramp': any(r.get('index_by_ramp') for r in runs),
            'entries': len(entries), 'median_error': statistics.median(errors),
            'median_error_per_cell': per_cell, 'median_error_per_table': per_table,
            'max_error': worst['error'], 'max_error_at': {k: worst[k] for k in ('cell', 'pin', 'table', 'slew_ns', 'load_pf', 'shipped', 'measured')},
            'input_capacitance': caps, 'max_capacitance_error': max(c['error'] for c in caps),
            'setup': setups, 'syntax': syn, 'notes': notes}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--liberty', type=Path, required=True, help='shipped sky130_fd_sc_hd__tt_025C_1v80.lib')
    ap.add_argument('--fixed', type=Path, required=True, help='reports made with the fixed ngspice (judged)')
    ap.add_argument('--stock', type=Path, help='reports made with the stock ngspice (reported)')
    ap.add_argument('--broken', type=Path, help='reports indexed by the full ramp (must be flagged)')
    ap.add_argument('--syntax-check', action='store_true', help='load every generated .lib in Yosys and OpenSTA')
    ap.add_argument('--yosys', default='yosys -q -p "read_liberty -lib {lib}"')
    ap.add_argument('--sta', default="printf 'read_liberty {lib}\\nexit\\n' | sta -no_init")
    ap.add_argument('--out', type=Path, default=Path(__file__).resolve().parent.parent / 'results' / 'characterizer_validation.json')
    args = ap.parse_args()
    text = args.liberty.read_text(encoding='utf-8', errors='replace')
    syntax = {'yosys': args.yosys, 'opensta': args.sta} if args.syntax_check else None
    report = {'check': 'characterizer_validation', 'shipped_liberty': str(args.liberty),
              'limits': {'median_error': MEDIAN_LIMIT, 'input_capacitance': CAP_LIMIT}}
    try:
        fixed = report['fixed'] = evaluate(args.fixed, args.liberty, text, syntax)
        if args.stock:
            report['stock'] = evaluate(args.stock, args.liberty, text, None)
            if report['stock']['simulator'] == fixed['simulator']:
                raise ValueError('--stock and --fixed were made with the same ngspice build')
        checks = {'setup_exact': all(s['passed'] for s in fixed['setup']),
                  'median_error_within_5_percent': fixed['median_error'] <= MEDIAN_LIMIT,
                  'input_capacitance_within_10_percent': fixed['max_capacitance_error'] <= CAP_LIMIT,
                  'cells_are_the_six': sorted(c.split('__')[-1] for c in fixed['cells']) ==
                  sorted(['inv_1', 'nand2_1', 'nor2_1', 'nand3_1', 'a21oi_1', 'o21ai_1'])}
        if syntax:
            checks['liberty_loads_in_yosys_and_opensta'] = all(v['passed'] for c in fixed['syntax'].values() for v in c.values())
        if args.broken:
            broken = report['broken'] = evaluate(args.broken, args.liberty, text, None)
            checks['broken_case_flagged'] = broken['index_by_ramp'] and broken['median_error'] > MEDIAN_LIMIT
        report['checks'] = checks
        report['passed'] = all(checks.values())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        report.update(passed=False, error=f'{type(exc).__name__}: {exc}')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    for label in ('fixed', 'stock', 'broken'):
        r = report.get(label)
        if r:
            w = r['max_error_at']
            print(f"{label:6} {r['simulator']['version']}: median {100 * r['median_error']:.2f}% over {r['entries']} entries, "
                  f"max {100 * r['max_error']:.1f}% ({w['cell'].split('__')[-1]} {w['pin']} {w['table']} "
                  f"slew {w['slew_ns']} load {w['load_pf']}), worst input cap {100 * r['max_capacitance_error']:.1f}%")
    for k, v in report.get('checks', {}).items():
        print(f"  {'ok  ' if v else 'FAIL'} {k}")
    print('PASS' if report['passed'] else 'FAIL' + (f": {report['error']}" if 'error' in report else ''))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())