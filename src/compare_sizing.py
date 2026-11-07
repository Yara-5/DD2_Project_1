#!/usr/bin/env python3
"""Handout section 8.2: "The two sizings of !(A | B | C): width, and delay per input pin."

Inputs: two generated cells of the same function, one sized 'hd' and one 'stack'
(cells/<name>/request.json, layout_plan.json, and characterized.json from `make char`).
The two requests' truth tables must be equal (AGENTS.md rule 6) and their sizings must be
'hd' and 'stack'. Reports, for each: width in sites and um, area, transistor count and
total W per type, and per input pin cell_rise, cell_fall, rise_transition and
fall_transition at characterize.py's nominal point (the HD table point nearest 100 ps and
10 fF), with stack / hd ratios.
Output: --out (results/sizing/<function>.json). Exit 1 if a file is missing or the two
cells do not compute the same function. Standard library only.
"""
import argparse
import json
import sys
from pathlib import Path

KEYS = ('cell_rise', 'cell_fall', 'rise_transition', 'fall_transition')


def cell_view(folder):
    request = json.loads((folder / 'request.json').read_text())['request']
    plan = json.loads((folder / 'layout_plan.json').read_text())
    char = json.loads((folder / 'characterized.json').read_text())
    if not char.get('passed'):
        raise ValueError(f'{folder.name}: characterization did not pass')
    devices = plan['input']['devices']
    si = char['setup']['slews_ns'].index(char['nominal_point']['slew_ns'])
    li = char['setup']['loads_pf'].index(char['nominal_point']['load_pf'])
    pins = {}
    for t in char['timing']:
        entry = pins.setdefault(t['pin'], {})
        for key in KEYS:
            entry[key] = t['tables'][key][si][li]
    return request, {
        'cell': folder.name, 'sizing': request['sizing'],
        'width_sites': plan['dimensions']['width_sites'], 'width_um': plan['dimensions']['cell_width_um'],
        'area_um2': round(plan['dimensions']['cell_width_um'] * plan['dimensions']['cell_height_um'], 6),
        'transistors': len(devices),
        'total_w_um': {k: round(sum(d['w_um'] for d in devices if d['type'] == k), 6) for k in ('nmos', 'pmos')},
        'nominal_point': char['nominal_point'],
        'delay_per_pin_ns': pins,
        'input_capacitance_pf': {p: v['capacitance'] for p, v in char['capacitance_pf'].items()},
        'characterization': (folder / 'characterized.json').as_posix()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--hd', type=Path, required=True, help="cells/<name>/ sized 'hd'")
    ap.add_argument('--stack', type=Path, required=True, help="cells/<name>/ sized 'stack'")
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    try:
        (ra, a), (rb, b) = cell_view(args.hd), cell_view(args.stack)
        if (ra['sizing'], rb['sizing']) != ('hd', 'stack'):
            raise ValueError(f"sizings are {ra['sizing']} and {rb['sizing']}, not hd and stack")
        if ra['inputs'] != rb['inputs'] or ra['truth_table_value'] != rb['truth_table_value']:
            raise ValueError('the two cells do not compute the same function')
        ratio = {p: {k: round(b['delay_per_pin_ns'][p][k] / a['delay_per_pin_ns'][p][k], 4) for k in KEYS}
                 for p in a['delay_per_pin_ns']}
        report = {'check': 'sizing_comparison', 'function': ra.get('expression', ra['truth_table']),
                  'truth_table': ra['truth_table'], 'hd': a, 'stack': b,
                  'stack_over_hd': {'width': round(b['width_um'] / a['width_um'], 4),
                                    'area': round(b['area_um2'] / a['area_um2'], 4), 'delay_per_pin': ratio},
                  'passed': True}
    except (OSError, ValueError, KeyError) as exc:
        report = {'check': 'sizing_comparison', 'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    if not report['passed']:
        print(f"FAIL: {report['error']}")
        return 1
    print(f"{report['function']}: hd {a['width_sites']} sites, stack {b['width_sites']} sites")
    for p, r in ratio.items():
        print(f"  {p}: " + ', '.join(f"{k} {a['delay_per_pin_ns'][p][k]:.4f} -> {b['delay_per_pin_ns'][p][k]:.4f} ns"
                                    for k in KEYS))
    return 0


if __name__ == '__main__':
    sys.exit(main())
