#!/usr/bin/env python3
"""Handout section 3.3: the round trip of the request and the answer, end to end.

For every cell whose response (write_response.py) says "pass":
  1. its Liberty cell group (the response's `files.liberty`) is appended inside the
     library group of the given hd_reduced.lib, before its final closing brace;
  2. a one-cell design that computes the requested function (the request's expression,
     or the sum of minterms of its truth table) is mapped onto that library with both
     ABC scripts of the Option 2 handout's section 2. The Yosys commands and the two
     scripts are read from the given scripts/map.sh at run time, not retyped. Yosys 0.52
     splits map.sh's inline `-script +strash;dch,-f;...` at its semicolons (map.sh then
     maps nothing and prints nothing), so each script is written to a file instead, with
     the commas made blanks, which is what the `+` form means (Yosys `help abc`);
  3. ABC's cec compares each mapped netlist with the request's truth table (AGENTS.md
     rule 6): the golden design is the sum of minterms of the truth table, and in the
     mapped netlist every HD cell is the function its Liberty gives and our cell is its
     Verilog model (the model the model-level check simulated), so a Liberty function
     that disagrees with the cell makes the mapped netlist wrong and cec fails.
  4. "At least one script must use your cell": required when no cell of hd_reduced.lib
     computes the same function under any order of its inputs (Table 6's "None" rows,
     found by this script); when HD already has the function, ABC is free to pick HD's
     cell, and the use of ours is reported.
A cell passes when both scripts mapped, cec proves both netlists equivalent, and the use
rule holds. Exits non-zero when a cell fails or a step could not run (Yosys or ABC
missing, a file missing). Linux; standard library only.

Inputs: --cell-dir (one or more cells/<name>/), --reduced given/hd_reduced.lib,
--map-script given/scripts/map.sh. Outputs: results/round_trip/<cell>.json and, for more
than one cell, results/round_trip/summary.json; work files in build/round_trip/<cell>/.
--liberty replaces the cell's Liberty file (the broken case of tests/broken/).
"""
import argparse
import itertools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from liberty_view import parse
from request_reader import function_truth_table

TOP = 'rt_top'


def map_sh(path):
    """The Yosys command template and the ABC scripts A and B of map.sh."""
    text = Path(path).read_text(encoding='utf-8')
    scripts = dict(re.findall(r"^\s*([AB])\)\s*SCRIPT='([^']+)'", text, re.M))
    command = re.search(r'yosys\s+-q\s+-p\s+"([^"]+)"', text)
    if set(scripts) != {'A', 'B'} or not command:
        raise ValueError(f'{path}: could not read the two ABC scripts and the Yosys command')
    return command[1], scripts


def cell_group(liberty_text, name):
    """The text of the one cell group of a cell view, checked by the Liberty parser."""
    groups, _ = parse(liberty_text)
    cells = [c for lib in groups for c in lib.find('cell')]
    if len(cells) != 1 or cells[0].args[0] != name:
        raise ValueError(f'the Liberty view must hold exactly the cell {name}')
    start = re.search(r'(?m)^[ \t]*cell\s*\(', liberty_text).start()
    end = liberty_text.rstrip().rindex('}')
    return liberty_text[start:end].rstrip() + '\n'


def hd_equivalents(reduced_text, truth, n):
    """hd_reduced.lib cells that compute the same function under some input order."""
    groups, _ = parse(reduced_text)
    found = []
    for cell in groups[0].find('cell'):
        if any(cell.find(k) for k in ('ff', 'latch', 'statetable')):
            continue
        pins = cell.find('pin')
        inputs = [p.args[0] for p in pins if p.attributes.get('direction') == 'input']
        outputs = [p for p in pins if p.attributes.get('direction') == 'output']
        if len(outputs) != 1 or len(inputs) != n or 'function' not in outputs[0].attributes:
            continue
        for order in itertools.permutations(inputs):
            try:
                if function_truth_table(outputs[0].attributes['function'], list(order)) == truth:
                    found.append(cell.args[0]); break
            except ValueError:
                break
    return found


def verilog(module, inputs, output, expression):
    return (f"module {module} ({', '.join(inputs + [output])});\n"
            + ''.join(f'  input {p};\n' for p in inputs)
            + f'  output {output};\n  assign {output} = {expression};\nendmodule\n')


def minterms(inputs, truth):
    terms = ['(' + ' & '.join(p if (row >> i) & 1 else f'~{p}' for i, p in enumerate(inputs)) + ')'
             for row in range(1 << len(inputs)) if (truth >> row) & 1]
    return ' | '.join(terms) if terms else "1'b0"


def run(command, cwd):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def round_trip(folder, reduced_path, template, scripts, workroot, override=None):
    name = folder.name
    report = {'check': 'round_trip', 'cell': name, 'passed': False}
    response = json.loads((folder / 'response.json').read_text(encoding='utf-8'))
    if response.get('status') != 'pass':
        raise ValueError(f'the response of {name} is not "pass": {response.get("reason")}')
    request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))['request']
    inputs, output, truth = request['inputs'], request['output'], request['truth_table_value']
    liberty_path = Path(override) if override else Path(response['files']['liberty'])
    model_path = Path(response['files']['verilog'])
    report.update(liberty=liberty_path.as_posix(), verilog_model=model_path.as_posix(),
                  truth_table=request['truth_table'])

    work = workroot / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    reduced = Path(reduced_path).read_text(encoding='utf-8')
    if re.search(rf'cell\s*\(\s*"?{re.escape(name)}"?\s*\)', reduced):
        raise ValueError(f'{name} is already in {reduced_path}')
    block = cell_group(liberty_path.read_text(encoding='utf-8'), name)
    end = reduced.rstrip().rindex('}')
    (work / 'library.lib').write_text(reduced[:end] + block + '}\n', encoding='utf-8')
    shutil.copyfile(model_path, work / 'cell_model.v')
    shutil.copyfile(reduced_path, work / 'hd_reduced.lib')
    expression = request.get('expression')
    design = expression.replace('!', '~') if expression else minterms(inputs, truth)
    (work / 'design.v').write_text(verilog(TOP, inputs, output, design), encoding='utf-8')
    (work / 'gold.v').write_text(verilog(TOP, inputs, output, minterms(inputs, truth)), encoding='utf-8')

    code, log = run(['yosys', '-q', '-p', f'read_verilog gold.v; synth -flatten -top {TOP}; write_blif gold.blif'], work)
    if code:
        raise RuntimeError(f'Yosys could not build the golden design: {log[-400:]}')

    equivalents = hd_equivalents(reduced, truth, len(inputs))
    report['hd_reduced_cells_with_this_function'] = equivalents
    report['our_cell_required'] = not equivalents
    report['scripts'] = {}
    for key in ('A', 'B'):
        script = scripts[key]
        (work / f'abc_{key}.script').write_text(script.lstrip('+').replace(',', ' ').replace(';', '\n') + '\n')
        command = (template.replace('$SV ', '').replace('$V', 'design.v').replace('$TOP', TOP)
                   .replace('$LIB', 'library.lib').replace('$SCRIPT', f'abc_{key}.script')
                   .replace('tee -o /dev/stdout stat', f'tee -o stat_{key}.txt stat'))
        command += f'; write_verilog -noattr mapped_{key}.v'
        code, log = run(['yosys', '-q', '-p', command], work)
        entry = {'map_sh_script': script, 'yosys_command': command, 'yosys_exit': code}
        if code or not (work / f'stat_{key}.txt').is_file():
            entry.update(passed=False, error=log[-600:]); report['scripts'][key] = entry; continue
        stat = (work / f'stat_{key}.txt').read_text()
        cells = {m[0]: int(m[1]) for m in re.findall(r'^\s+(\S+)\s+(\d+)\s*$', stat, re.M)
                 if not m[0].startswith('$') and m[0] != 'cells'}
        area = re.search(r'Chip area for module .*?:\s*([0-9.]+)', stat)
        entry.update(cells=cells, area_um2=float(area[1]) if area else None, uses_our_cell=name in cells)
        code, log = run(['yosys', '-q', '-p',
                         f'read_liberty -ignore_miss_func hd_reduced.lib; read_verilog cell_model.v; '
                         f'read_verilog mapped_{key}.v; hierarchy -top {TOP}; flatten; synth -flatten -top {TOP}; '
                         f'write_blif gate_{key}.blif'], work)
        if code:
            entry.update(passed=False, error=f'Yosys could not read the mapped netlist: {log[-400:]}')
            report['scripts'][key] = entry; continue
        code, log = run(['yosys-abc', '-c', f'cec gold.blif gate_{key}.blif'], work)
        verdict = ('equivalent' if re.search(r'Networks are equivalent', log)
                   else 'not equivalent' if re.search(r'NOT EQUIVALENT', log, re.I) else 'not run')
        entry.update(cec=verdict, cec_output=log.strip().splitlines()[-1] if log.strip() else '',
                     passed=verdict == 'equivalent')
        report['scripts'][key] = entry
    used = any(e.get('uses_our_cell') for e in report['scripts'].values())
    report['our_cell_used'] = used
    report['passed'] = (all(e.get('passed') for e in report['scripts'].values())
                        and (used or not report['our_cell_required']))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cell-dir', type=Path, nargs='+', required=True)
    ap.add_argument('--reduced', type=Path, required=True, help='given/hd_reduced.lib')
    ap.add_argument('--map-script', type=Path, required=True, help='given/scripts/map.sh')
    ap.add_argument('--liberty', type=Path, help='Use this Liberty file instead of the response\'s (one cell only)')
    ap.add_argument('--outdir', type=Path, default=Path('results/round_trip'))
    ap.add_argument('--workdir', type=Path, default=Path('build/round_trip'))
    args = ap.parse_args()
    if args.liberty and len(args.cell_dir) != 1:
        ap.error('--liberty needs exactly one --cell-dir')
    for tool in ('yosys', 'yosys-abc'):
        if not shutil.which(tool):
            print(f'FAIL: round trip not run: {tool} is not on PATH')
            return 1
    template, scripts = map_sh(args.map_script)
    args.outdir.mkdir(parents=True, exist_ok=True)
    reports = []
    for folder in args.cell_dir:
        try:
            report = round_trip(folder, args.reduced, template, scripts, args.workdir, args.liberty)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            report = {'check': 'round_trip', 'cell': folder.name, 'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
        reports.append(report)
        short = folder.name.removeprefix('dd2_g5__')
        (args.outdir / f'{short}.json').write_text(json.dumps(report, indent=2) + '\n')
        scripts_text = ', '.join(f"{k}: {e.get('cec', 'not run')}, {'ours' if e.get('uses_our_cell') else 'HD only'}"
                                 for k, e in report.get('scripts', {}).items())
        print(f"{'PASS' if report['passed'] else 'FAIL'}: round trip, {folder.name}"
              + (f" ({report['error']})" if 'error' in report else
                 f"; {scripts_text}; our cell {'required' if report['our_cell_required'] else 'optional'}"))
    passed = all(r['passed'] for r in reports)
    if len(reports) > 1:
        summary = {'check': 'round_trip', 'cells': len(reports), 'passed': passed,
                   'failing': [r['cell'] for r in reports if not r['passed']],
                   'our_cell_required': [r['cell'] for r in reports if r.get('our_cell_required')],
                   'our_cell_used': [r['cell'] for r in reports if r.get('our_cell_used')]}
        (args.outdir / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
