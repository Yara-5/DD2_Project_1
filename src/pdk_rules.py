#!/usr/bin/env python3
"""Prove every design-rule value in rules.json against the installed PDK.

Part of `make check` (AGENTS.md, rule 7). For each rule, the value in rules.json
must equal the number the installed Magic technology file uses for that rule:
  drc   a DRC statement whose message names the rule, e.g. "(poly.2)"; lengths
        are in nm and areas in nm^2 in that section
  cif   an operation of a CIF output layer, e.g. NPC is grown 100 nm around
        poly contacts and bridged at 270 nm
  frame a key of the instructor's plan_hd.json
Writes a JSON report with the line of the PDK file behind every value.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def logical_lines(text):
    """Join backslash continuations; keep the first physical line number."""
    lines, start, parts = [], None, []
    for number, line in enumerate(text.splitlines(), 1):
        if start is None:
            start = number
        if line.rstrip().endswith('\\'):
            parts.append(line.rstrip()[:-1])
            continue
        parts.append(line)
        lines.append((start, ' '.join(p.strip() for p in parts)))
        start, parts = None, []
    return lines


def drc_numbers(statement):
    """The integer arguments of a DRC statement, before its quoted message."""
    head = statement.split('"', 1)[0]
    return [int(token) for token in head.split()[1:] if re.fullmatch(r'\d+', token)]


def check_drc(lines, check, value):
    scale = 1e6 if check.get('area') else 1e3
    expected = round(value * scale)
    candidates = [(number, text) for number, text in lines
                  if f"({check['rule']})" in text and check['message'].lower() in text.lower()]
    for number, text in candidates:
        if expected in drc_numbers(text):
            return {'passed': True, 'line': number, 'text': text, 'pdk_value': expected}
    return {'passed': False, 'expected_pdk_value': expected,
            'found': [{'line': n, 'numbers': drc_numbers(t)} for n, t in candidates]}


def check_cif(lines, check, value):
    expected = round(value * 1e3)
    starts = [i for i, (_, text) in enumerate(lines) if text.split() == ['layer', *check['layer'].split()]]
    if not starts:
        return {'passed': False, 'reason': f"CIF layer '{check['layer']}' not found"}
    seen = 0
    for number, text in lines[starts[0] + 1:]:
        words = text.split()
        if words and words[0] in ('layer', 'templayer', 'style'):
            break
        if words and words[0] == check['operation']:
            seen += 1
            if seen == check['occurrence']:
                found = int(words[1])
                return {'passed': found == expected, 'line': number, 'text': text,
                        'pdk_value': found, 'expected_pdk_value': expected}
    return {'passed': False, 'reason': f"no {check['operation']} in CIF layer '{check['layer']}'"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--rules', type=Path, default=Path('src/rules.json'))
    ap.add_argument('--tech', type=Path, required=True, help='<PDK>/libs.tech/magic/sky130A.tech')
    ap.add_argument('--frame', type=Path, required=True, help='given/plan_hd.json')
    ap.add_argument('--out', type=Path, default=Path('results/pdk_rules/report.json'))
    args = ap.parse_args()

    rules = json.loads(args.rules.read_text(encoding='utf-8'))
    frame = json.loads(args.frame.read_text(encoding='utf-8'))
    tech_bytes = args.tech.read_bytes()
    lines = logical_lines(tech_bytes.decode('utf-8', errors='replace'))

    results = {}
    for name, entry in rules['rules'].items():
        check = entry.get('check')
        if check is None:
            results[name] = {'passed': False, 'reason': 'no check: the value has no PDK source'}
        elif check['kind'] == 'drc':
            results[name] = check_drc(lines, check, entry['value'])
        elif check['kind'] == 'cif':
            results[name] = check_cif(lines, check, entry['value'])
        elif check['kind'] == 'frame':
            found = frame.get(check['key'])
            results[name] = {'passed': found == entry['value'], 'frame_value': found}
        else:
            results[name] = {'passed': False, 'reason': f"unknown check kind {check['kind']}"}
        results[name]['value'] = entry['value']

    report = {'check': 'pdk_rules', 'tech': str(args.tech),
              'tech_sha256': hashlib.sha256(tech_bytes).hexdigest(),
              'frame_sha256': hashlib.sha256(args.frame.read_bytes()).hexdigest(),
              'rules': results, 'passed': all(r['passed'] for r in results.values())}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for name, result in results.items():
        where = f"line {result['line']}" if 'line' in result else result.get('reason', '')
        print(f"{'PASS' if result['passed'] else 'FAIL'}  {name:24s} {result['value']:<8g} {where}")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {len(results)} rules against {args.tech.name}")
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
