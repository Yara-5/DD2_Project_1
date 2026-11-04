#!/usr/bin/env python3
"""Where is the characterizer far from the shipped Liberty, and in which direction?
Usage: python src/explain_errors.py --liberty $LIB --dir results/char/fixed [--cell a21oi_1]
Reads the reports characterize.py wrote; runs nothing."""
import argparse
import json
import statistics
from pathlib import Path

from validate_characterizer import compare_cell


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--liberty', type=Path, required=True)
    ap.add_argument('--dir', type=Path, required=True)
    ap.add_argument('--cell', default='a21oi_1')
    ap.add_argument('--top', type=int, default=12)
    args = ap.parse_args()
    run = json.loads((args.dir / f'{args.cell}.json').read_text())
    if not run.get('passed') or 'timing' not in run:
        raise SystemExit(f"{args.dir / (args.cell + '.json')} is a failed run: {run.get('error', 'no timing data')}")
    entries, caps, notes = compare_cell(run, args.liberty.read_text(encoding='utf-8', errors='replace'), args.liberty)
    for n in notes:
        print('NOTE', n)
    for e in entries:
        e['signed'] = (e['measured'] - e['shipped']) / e['shipped']
    print(f"\n{run['cell']}  simulator {run['simulator']['version']}  slews {run['setup']['slews_ns']}  loads {run['setup']['loads_pf']}\n")
    print('median |error| and median signed error by pin and table (signed > 0: we are slower than HD)')
    for pin in sorted({e['pin'] for e in entries}):
        for key in ('cell_rise', 'cell_fall', 'rise_transition', 'fall_transition'):
            sel = [e for e in entries if e['pin'] == pin and e['table'] == key]
            print(f"  {pin:3} {key:16} |err| {100*statistics.median(e['error'] for e in sel):6.1f}%   "
                  f"signed {100*statistics.median(e['signed'] for e in sel):+7.1f}%   "
                  f"max {100*max(e['error'] for e in sel):6.1f}%")
    print(f'\nthe {args.top} worst entries (ps)')
    for e in sorted(entries, key=lambda e: -e['error'])[:args.top]:
        print(f"  {e['pin']:3} {e['table']:16} slew {e['slew_ns']:<6} load {e['load_pf']:<8} "
              f"shipped {1000*e['shipped']:8.2f}  measured {1000*e['measured']:8.2f}  err {100*e['error']:6.1f}%")
    worst = max(entries, key=lambda e: e['error'])
    print(f"\nsigned error grid for {worst['pin']} {worst['table']} (rows = slew, columns = load)")
    loads = run['setup']['loads_pf']
    print('        ' + ''.join(f'{c:>9}' for c in loads))
    for s in run['setup']['slews_ns']:
        row = {e['load_pf']: e['signed'] for e in entries if e['pin'] == worst['pin'] and e['table'] == worst['table'] and e['slew_ns'] == s}
        print(f'{s:<8}' + ''.join(f'{100*row[c]:+8.1f}%' for c in loads))
    print('\nshipped values of that table (ps)')
    for s in run['setup']['slews_ns']:
        row = {e['load_pf']: e['shipped'] for e in entries if e['pin'] == worst['pin'] and e['table'] == worst['table'] and e['slew_ns'] == s}
        print(f'{s:<8}' + ''.join(f'{1000*row[c]:9.1f}' for c in loads))
    t = next(t for t in run['timing'] if t['pin'] == worst['pin'])
    print('\nside-input setting chosen as slowest for', worst['pin'], ':', t['slowest_side_inputs'])
    print('input capacitance (pF):', [(c['pin'], round(c['shipped'], 5), round(c['measured'], 5)) for c in caps])


if __name__ == '__main__':
    main()