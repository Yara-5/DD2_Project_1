#!/usr/bin/env python3
"""Step 8 views and section 4.2 level 3: model-level check.

Writes the cell's Verilog model (cell.v) and a functional Liberty cell (cell.lib)
from the request, then checks both against the request's truth table:
  * Verilog: Icarus simulates every input combination;
  * Liberty: the output pin's 'function' is parsed and its truth table compared.
A copy of the model with one wrong row is written and must be rejected, and so
must every --broken Verilog file (tests/broken/). The Liberty cell is functional
only (no timing, area 0) until the characterizer of section 5 exists.
Exits 0 only if the good views pass and every broken model is rejected; a
missing Icarus is a failure.
"""

import argparse
import itertools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from request_reader import function_truth_table, output_bit


def load_request(path):
    reader = json.loads(Path(path).read_text(encoding='utf-8'))
    if reader.get('status') != 'pass':
        raise ValueError('request_reader.py refused the request')
    return reader['request']


def sum_of_products(request, negate='!', conj=' & ', disj=' | '):
    """A function expression for a request given only as a truth table."""
    inputs, table = request['inputs'], request['truth_table_value']
    terms = []
    for row in range(1 << len(inputs)):
        if (table >> row) & 1:
            terms.append('(' + conj.join(p if (row >> i) & 1 else negate + p
                                         for i, p in enumerate(inputs)) + ')')
    return disj.join(terms) if terms else '0'


def liberty_function(request):
    return request.get('expression') or sum_of_products(request)


def verilog_function(request):
    return liberty_function(request).replace('!', '~')


def generate_views(request, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    inputs, output, name = request['inputs'], request['output'], request['name']

    lines = [f"module {name} ({', '.join(inputs + [output])});"]
    lines += [f'  input {pin};' for pin in inputs]
    lines += [f'  output {output};', f'  assign {output} = {verilog_function(request)};',
              'endmodule', '']
    (outdir / 'cell.v').write_text('\n'.join(lines), encoding='utf-8')

    liberty = [
        'library (dd2_functional) {',
        '  time_unit : "1ns";',
        '  voltage_unit : "1V";',
        '  current_unit : "1mA";',
        '  capacitive_load_unit (1, pf);',
        f'  cell ({name}) {{',
        '    area : 0.0;',
    ]
    for pin in inputs:
        liberty += [f'    pin ({pin}) {{', '      direction : input;', '    }']
    liberty += [f'    pin ({output}) {{', '      direction : output;',
                f'      function : "{liberty_function(request)}";', '    }', '  }', '}', '']
    (outdir / 'cell.lib').write_text('\n'.join(liberty), encoding='utf-8')


def check_liberty(request, lib_path):
    text = Path(lib_path).read_text(encoding='utf-8')
    output, inputs = request['output'], request['inputs']
    pin = re.search(r'\bpin\s*\(\s*' + re.escape(output) + r'\s*\)\s*\{([^}]*)\}', text, re.S)
    if not pin:
        return {'passed': False, 'error': f'Output pin {output} not found in Liberty.'}
    function = re.search(r'\bfunction\s*:\s*"([^"]+)"\s*;', pin[1])
    if not function:
        return {'passed': False, 'error': 'Output pin has no Liberty function.'}
    try:
        table = function_truth_table(function[1], inputs)
    except ValueError as exc:
        return {'passed': False, 'function': function[1], 'error': str(exc)}
    return {'passed': table == request['truth_table_value'], 'function': function[1],
            'function_truth_table': f'0x{table:X}', 'expected_truth_table': request['truth_table']}


def check_verilog(request, verilog_path):
    iverilog, vvp = shutil.which('iverilog'), shutil.which('vvp')
    if not iverilog or not vvp:
        return {'passed': False, 'error': 'Icarus Verilog (iverilog, vvp) is not on PATH.', 'rows': []}

    inputs, output, name = request['inputs'], request['output'], request['name']
    vectors = list(itertools.product((0, 1), repeat=len(inputs)))
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        tb = ['module tb;', *[f'  reg {p};' for p in inputs], f'  wire {output};',
              f"  {name} dut ({', '.join(inputs + [output])});", '  initial begin']
        for vector in vectors:
            key = ''.join(map(str, vector))
            tb.append('    ' + ' '.join(f"{p} = 1'b{b};" for p, b in zip(inputs, vector)))
            tb += ['    #1;', f'    $display("ROW_{key}=%b", {output});']
        tb += ['    $finish;', '  end', 'endmodule', '']
        (temp_dir / 'tb.v').write_text('\n'.join(tb), encoding='utf-8')

        compiled = subprocess.run([iverilog, '-g2012', '-o', str(temp_dir / 'sim.out'),
                                   str(Path(verilog_path).resolve()), str(temp_dir / 'tb.v')],
                                  capture_output=True, text=True)
        if compiled.returncode != 0:
            return {'passed': False, 'compile_error': compiled.stderr, 'rows': []}
        simulation = subprocess.run([vvp, str(temp_dir / 'sim.out')], capture_output=True, text=True)
        if simulation.returncode != 0:
            return {'passed': False, 'simulation_error': simulation.stderr, 'rows': []}

    observed = {}
    for line in simulation.stdout.splitlines():
        match = re.fullmatch(r'ROW_([01]+)=([01xXzZ])', line.strip())
        if match:
            observed[tuple(map(int, match[1]))] = match[2].lower()
    rows = []
    for vector in vectors:
        values = dict(zip(inputs, vector))
        expected = output_bit(request['truth_table_value'], values, inputs)
        actual = observed.get(vector, 'missing')
        rows.append({'inputs': values, 'expected': expected, 'actual': actual,
                     'passed': actual == str(expected)})
    return {'passed': all(row['passed'] for row in rows), 'rows': rows}


def make_broken_verilog(request, path):
    """The model with the output of the all-zero row flipped."""
    inputs, output, name = request['inputs'], request['output'], request['name']
    lines = [f"module {name} ({', '.join(inputs + [output])});",
             *[f'  input {pin};' for pin in inputs], f'  output reg {output};',
             '  always @* begin', f"    case ({{{', '.join(inputs)}}})"]
    for vector in itertools.product((0, 1), repeat=len(inputs)):
        result = output_bit(request['truth_table_value'], dict(zip(inputs, vector)), inputs)
        if not any(vector):
            result = 1 - result
        lines.append(f"      {len(inputs)}'b{''.join(map(str, vector))}: {output} = 1'b{result};")
    lines += [f"      default: {output} = 1'bx;", '    endcase', '  end', 'endmodule', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--request', required=True, type=Path, help='Output of request_reader.py')
    parser.add_argument('--outdir', required=True, type=Path, help='cells/<name>/')
    parser.add_argument('--report', type=Path, help='JSON report path (default: <outdir>/model_level_report.json)')
    parser.add_argument('--broken', type=Path, action='append', default=[],
                        help='A Verilog model with a wrong row; it must be rejected')
    args = parser.parse_args()

    try:
        request = load_request(args.request)
        outdir = args.outdir.resolve()
        generate_views(request, outdir)
        verilog = check_verilog(request, outdir / 'cell.v')
        liberty = check_liberty(request, outdir / 'cell.lib')
        broken_cases = []
        with tempfile.TemporaryDirectory() as temp_dir:
            generated_broken = Path(temp_dir) / 'cell_broken.v'
            make_broken_verilog(request, generated_broken)
            for label, path in [('generated: row 0 flipped', generated_broken),
                                *[(str(p), p) for p in args.broken]]:
                result = check_verilog(request, path)
                # A broken model counts as caught only if it ran and failed on a row;
                # a model that does not even compile proves nothing about the check.
                caught = bool(result['rows']) and not result['passed']
                broken_cases.append({'case': label, 'rejected': caught,
                                     'failing_rows': [r['inputs'] for r in result['rows'] if not r['passed']]})
        report = {'check': 'model_level', 'cell': request['name'], 'truth_table': request['truth_table'],
                  'generated': ['cell.v', 'cell.lib'], 'verilog': verilog, 'liberty': liberty,
                  'broken_cases': broken_cases, 'liberty_status': 'functional_only_uncharacterized'}
        report['passed'] = verilog['passed'] and liberty['passed'] and all(b['rejected'] for b in broken_cases)
    except (OSError, KeyError, ValueError) as exc:
        report = {'check': 'model_level', 'passed': False, 'error': str(exc)}

    report_path = args.report or args.outdir / 'model_level_report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f"{'PASS' if report['passed'] else 'FAIL'}: model level, {report.get('cell', '')}"
          + (f" ({report['error']})" if 'error' in report else
             f"; verilog={report['verilog']['passed']} liberty={report['liberty']['passed']} "
             f"broken rejected={sum(b['rejected'] for b in report['broken_cases'])}/{len(report['broken_cases'])}"))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
