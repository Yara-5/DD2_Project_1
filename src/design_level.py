#!/usr/bin/env python3
"""Section 5.1, Table 5 'Design level': our six reference cells in place of the shipped ones,
Tutorial 4's SPM mapping run again, and the worst delay from OpenSTA.

"Replace the six cells in a copy of the HD Liberty with your versions, rerun Tutorial 4's
SPM mapping, and time it with OpenSTA. Report the change in worst delay."

Inputs: the shipped sky130_fd_sc_hd__tt_025C_1v80.lib; --cells, the folder with
characterize.py's <cell>.lib for inv_1, nand2_1, nor2_1, nand3_1, a21oi_1 and o21ai_1 (made
from the installed layouts, `make char_hd`); Tutorial 4's folder (SPM.v, CSADD.v, TCMP.v,
synth.ys, abc_constr.txt), used unchanged.
  1. A copy of the HD Liberty with those six cell groups replaced by ours.
  2. For each library: Tutorial 4's synth.ys, unchanged, run by Yosys in
     build/design_level/<library>/, where pdk/lib/sky130_fd_sc_hd__tt_025C_1v80.lib (the
     path synth.ys reads) is that library. Result: SPM.mapped.gl.v.
  3. OpenSTA times each mapped netlist with its own library, and the shipped mapping also
     with our library (the same netlist, so only the six cells' timing differs). The clock
     is SPM's clk at the period synth.ys gives ABC (abc -D, in ps). The worst delay is the
     data arrival time of the worst max-delay path.
Output: --out (results/characterization/design_level.json). The change in worst delay is
reported, not judged (Table 5). Exit 1 if a tool is missing, fails or gives no number
(AGENTS.md rule 5). Linux only: Yosys and OpenSTA on the PATH. Standard library only.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from validate_characterizer import balanced

SIX = ('inv_1', 'nand2_1', 'nor2_1', 'nand3_1', 'a21oi_1', 'o21ai_1')
TUTORIAL4_FILES = ('SPM.v', 'CSADD.v', 'TCMP.v', 'synth.ys', 'abc_constr.txt')
LIB_PATH_IN_SYNTH = Path('pdk/lib/sky130_fd_sc_hd__tt_025C_1v80.lib')


def cell_span(text, name):
    """(start, end) of the group cell ("<name>") { ... } in a Liberty text."""
    m = re.search(r'(?m)^[ \t]*cell\s*\(\s*"?' + re.escape(name) + r'"?\s*\)\s*\{', text)
    if not m:
        raise ValueError(f'cell {name} not found')
    return m.start(), m.start() + len(balanced(text, m.start()))


def replaced_library(shipped, ours):
    """The shipped library text with each of our cell groups in place of its own."""
    text = shipped
    for name, lib in ours.items():
        a, b = cell_span(lib, name)
        start, end = cell_span(text, name)
        text = text[:start] + lib[a:b].strip() + text[end:]
    return text


def run(cmd, cwd, log, stdin=None):
    proc = subprocess.run(cmd, cwd=cwd, text=True, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)
    Path(log).write_text(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited with {proc.returncode}; see {log}")
    return proc.stdout


def synthesize(work, tutorial4, library):
    """Tutorial 4's synth.ys, unchanged, with `library` at the path it reads."""
    if work.exists():
        shutil.rmtree(work)
    (work / LIB_PATH_IN_SYNTH.parent).mkdir(parents=True)
    (work / 'rpt').mkdir()
    for f in TUTORIAL4_FILES:
        shutil.copyfile(tutorial4 / f, work / f)
    shutil.copyfile(library, work / LIB_PATH_IN_SYNTH)
    run(['yosys', '-q', '-l', 'synth.log', 'synth.ys'], work, work / 'yosys.out')
    netlist = work / 'SPM.mapped.gl.v'
    if not netlist.is_file():
        raise RuntimeError(f'Yosys wrote no {netlist}')
    cells = {}
    for m in re.finditer(r'^\s*(sky130_fd_sc_hd__\w+)\s+\S+\s*\(', netlist.read_text(), re.M):
        cells[m[1]] = cells.get(m[1], 0) + 1
    return netlist, cells


def sta(work, library, netlist, period_ns, tag):
    script = work / f'sta_{tag}.tcl'
    script.write_text('\n'.join([
        f'read_liberty {library}', f'read_verilog {netlist}', 'link_design SPM',
        f'create_clock -name clk -period {period_ns} [get_ports clk]',
        'report_checks -path_delay max -digits 4', 'exit', '']))
    # This OpenSTA build (2.0.17) runs a script given on standard input.
    out = run(['sta', '-no_init'], work, work / f'sta_{tag}.log', stdin=script.read_text())
    if re.search(r'^Error', out, re.M):
        raise RuntimeError(f'OpenSTA reported an error; see sta_{tag}.log')
    arrival = re.search(r'^\s*([-\d.]+)\s+data arrival time', out, re.M)
    slack = re.search(r'^\s*([-\d.]+)\s+slack', out, re.M)
    if not arrival or not slack:
        raise RuntimeError(f'no path in OpenSTA report; see sta_{tag}.log')
    path = re.search(r'Startpoint:\s*(\S+).*?Endpoint:\s*(\S+)', out, re.S)
    return {'worst_delay_ns': float(arrival[1]), 'slack_ns': float(slack[1]),
            'startpoint': path[1] if path else None, 'endpoint': path[2] if path else None,
            'log': str(work / f'sta_{tag}.log')}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--liberty', type=Path, required=True, help='shipped sky130_fd_sc_hd__tt_025C_1v80.lib')
    ap.add_argument('--cells', type=Path, required=True, help="characterize.py's six <cell>.lib (make char_hd)")
    ap.add_argument('--tutorial4', type=Path, required=True, help="Tutorial 4's folder (SPM.v, synth.ys, ...)")
    ap.add_argument('--work', type=Path, default=Path('build/design_level'))
    ap.add_argument('--out', type=Path, default=Path('results/characterization/design_level.json'))
    args = ap.parse_args()
    report = {'check': 'design_level', 'shipped_liberty': str(args.liberty), 'cells': list(SIX)}
    try:
        for tool in ('yosys', 'sta'):
            if not shutil.which(tool):
                raise RuntimeError(f'{tool} is not installed (not run)')
        shipped = args.liberty.read_text(encoding='utf-8', errors='replace')
        ours = {f'sky130_fd_sc_hd__{c}': (args.cells / f'{c}.lib').read_text(encoding='utf-8') for c in SIX}
        args.work.mkdir(parents=True, exist_ok=True)
        ours_lib = (args.work / 'sky130_fd_sc_hd__tt_025C_1v80_ours.lib').resolve()
        ours_lib.write_text(replaced_library(shipped, ours), encoding='utf-8')
        synth = (args.tutorial4 / 'synth.ys').read_text()
        period = re.search(r'^\s*abc\b.*?-D\s+(\d+(?:\.\d+)?)', synth, re.M)
        if not period:
            raise RuntimeError("no 'abc -D <ps>' in Tutorial 4's synth.ys")
        period_ns = float(period[1]) / 1000
        libs = {'shipped': args.liberty.resolve(), 'ours': ours_lib}
        mapped, timing = {}, {}
        for tag, lib in libs.items():
            netlist, cells = synthesize(args.work / tag, args.tutorial4, lib)
            mapped[tag] = {'netlist': str(netlist), 'cell_counts': cells,
                           'reference_cells_used': {c: cells.get(c, 0) for c in ours}}
            timing[tag] = sta(args.work / tag, lib, netlist.resolve(), period_ns, tag)
        # The shipped mapping timed with our cells: the same netlist, only the timing differs.
        timing['shipped_netlist_ours_timing'] = sta(args.work / 'shipped', ours_lib,
                                                    Path(mapped['shipped']['netlist']).resolve(), period_ns, 'ours_timing')
        report.update({
            'tutorial4_synth_sha256': hashlib.sha256((args.tutorial4 / 'synth.ys').read_bytes()).hexdigest(),
            'clock_period_ns': period_ns, 'ours_liberty': str(ours_lib), 'mapping': mapped, 'timing': timing,
            'worst_delay_change_ns': round(timing['ours']['worst_delay_ns'] - timing['shipped']['worst_delay_ns'], 6),
            'worst_delay_change_percent': round(100 * (timing['ours']['worst_delay_ns'] /
                                                       timing['shipped']['worst_delay_ns'] - 1), 3),
            'same_netlist_change_percent': round(100 * (timing['shipped_netlist_ours_timing']['worst_delay_ns'] /
                                                        timing['shipped']['worst_delay_ns'] - 1), 3),
            'passed': True})
    except (OSError, ValueError, RuntimeError) as exc:
        report.update(passed=False, error=f'{type(exc).__name__}: {exc}')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    if report['passed']:
        t = report['timing']
        print(f"SPM worst delay: shipped {t['shipped']['worst_delay_ns']} ns, ours {t['ours']['worst_delay_ns']} ns "
              f"({report['worst_delay_change_percent']:+.2f} %); shipped netlist with our timing "
              f"{t['shipped_netlist_ours_timing']['worst_delay_ns']} ns ({report['same_netlist_change_percent']:+.2f} %)")
        print('PASS')
        return 0
    print(f"FAIL: {report['error']}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
