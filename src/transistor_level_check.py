#!/usr/bin/env python3
"""Step 7, section 4.2 level 2: transistor-level simulation of an extracted cell.

Runs ngspice on the RC-extracted netlist at the HD Liberty's nominal voltage and
temperature (TT corner):
  1. a DC operating point for every input combination: Y within 10% of its rail;
  2. a transient from every input state toggling each input once, with the input
     slew and output load taken from the HD Liberty tables: Y must be within 10%
     of the correct rail before the edge and after it settles, and must never
     cross half the supply when it should not change (charge sharing).
The expected function is the request's truth table. Supply, temperature, slew
thresholds and the table point all come from the Liberty file (AGENTS.md,
rule 7). The subcircuit port order is read from the netlist's .subckt line.
Exits 0 only if every case passes; a simulator error is a failure.

Every case is its own copy of the cell, with its own input sources and its own load,
all sharing only the ideal supply source, so the copies cannot affect one another.
The copies are simulated together in --jobs decks (one ngspice run each: one
operating point for the DC rows, one transient for the edges), instead of one
ngspice run per case: loading the SKY130 models took most of each run's time
(10 November: 80 runs of about 45 s for a four-input cell). The cases, the
transient's timing and every verdict are unchanged.
"""

import argparse
import concurrent.futures
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from request_reader import output_bit

SUPPLY_NODES = {'VPWR': 'VPWR', 'VPB': 'VPWR', 'VGND': '0', 'VNB': '0'}
EDGE_NS = 2.0        # the input edge, after the initial state has settled
SETTLE_NS = 1.0      # time allowed after the end of the input ramp


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--pex', required=True, type=Path, help='RC-extracted SPICE netlist')
    ap.add_argument('--request', required=True, type=Path, help='Output of request_reader.py')
    ap.add_argument('--models', required=True, type=Path,
                    help='SKY130 sky130.lib.spice, as the simulator sees it')
    ap.add_argument('--corner', default='tt')
    ap.add_argument('--liberty', required=True, type=Path, help='The HD Liberty, e.g. given/hd_reduced.lib')
    ap.add_argument('--liberty-cell', default='sky130_fd_sc_hd__inv_1',
                    help='HD cell whose delay-table indices give the slew and load')
    ap.add_argument('--slew-target-ns', type=float, default=0.1,
                    help='Use the table slew nearest to this (handout section 6: 100 ps)')
    ap.add_argument('--load-target-pf', type=float, default=0.01,
                    help='Use the table load nearest to this (handout section 6: 10 fF)')
    ap.add_argument('--ngspice', default='ngspice',
                    help='Command that runs the course ngspice, e.g. "<tut3>/tools.sh ngspice"')
    ap.add_argument('--workdir', required=True, type=Path,
                    help='Scratch folder; inside Tutorial 3 when ngspice runs in its container')
    ap.add_argument('--jobs', type=int, default=6,
                    help='Simulations at once; each one loads the SKY130 models (Tutorial 3 timing.py runs six)')
    ap.add_argument('--out', type=Path, required=True)
    return ap.parse_args()


def liberty_setup(path, cell, slew_target, load_target):
    """Read the operating point and one table point from the HD Liberty."""
    text = path.read_text(encoding='utf-8', errors='replace')

    def attribute(name):
        match = re.search(rf'\b{name}\s*:\s*([-+\d.eE]+)\s*;', text)
        if not match:
            raise ValueError(f'{name} not found in {path}')
        return float(match[1])

    start = text.find(f'cell ("{cell}")')
    if start < 0:
        raise ValueError(f'{cell} not found in {path}')
    body = text[start:]
    index_1 = re.search(r'index_1\("([^"]+)"\)', body)
    index_2 = re.search(r'index_2\("([^"]+)"\)', body)
    if not (index_1 and index_2):
        raise ValueError(f'no delay table indices in {cell}')
    slews = [float(x) for x in index_1[1].split(',')]
    loads = [float(x) for x in index_2[1].split(',')]
    lower = attribute('slew_lower_threshold_pct_rise')
    upper = attribute('slew_upper_threshold_pct_rise')
    slew = min(slews, key=lambda s: abs(s - slew_target))
    return {
        'liberty': str(path), 'liberty_cell': cell,
        'supply_v': attribute('nom_voltage'), 'temperature_c': attribute('nom_temperature'),
        'slew_thresholds_pct': [lower, upper],
        'slew_table_ns': slew,
        # The tables are indexed by the 20-80% time; a linear ramp spans 0-100%.
        'input_ramp_full_ns': slew / ((upper - lower) / 100),
        'load_table_pf': min(loads, key=lambda c: abs(c - load_target)),
    }


def subckt_ports(pex):
    text = pex.read_text(encoding='utf-8', errors='replace')
    match = re.search(r'(?im)^\.subckt\s+(\S+)\s+(.*)$', text)
    if not match:
        raise ValueError(f'no .subckt line in {pex}')
    return match[1], match[2].split()


class Simulator:
    def __init__(self, args, setup, inputs, output):
        self.command = shlex.split(args.ngspice)
        if '/' in self.command[0]:          # a path: it must survive the change of directory
            self.command[0] = os.path.abspath(self.command[0])
        # abspath removes '..' without resolving the pdk link, which the container needs.
        self.models = Path(os.path.abspath(args.models)).as_posix()
        self.pex = Path(os.path.abspath(args.pex)).as_posix()
        self.corner = args.corner
        self.setup = setup
        self.inputs = inputs
        self.output = output
        self.name, self.ports = subckt_ports(args.pex)
        known = set(inputs) | {output} | set(SUPPLY_NODES)
        if set(self.ports) != known:
            raise ValueError(f'netlist ports {self.ports} differ from {sorted(known)}')

    def header(self):
        vdd = self.setup['supply_v']
        return (f'* functional check of {self.name}\n'
                f'.lib "{self.models}" {self.corner}\n'
                f'.include "{self.pex}"\n'
                f'.temp {self.setup["temperature_c"]:g}\n'
                f'V_SUPPLY VPWR 0 {vdd}\n')

    def copy(self, tag, sources):
        """One copy of the cell for one case: its inputs IN_<tag>_<pin> driven by
        sources {pin: source value}, its output Y_<tag> loaded as in the tables."""
        nodes = [f'IN_{tag}_{p}' if p in self.inputs else f'Y_{tag}' if p == self.output else SUPPLY_NODES[p]
                 for p in self.ports]
        lines = [f'V_{tag}_{p} IN_{tag}_{p} 0 {sources[p]}' for p in self.inputs]
        lines.append(f'X_{tag} {" ".join(nodes)} {self.name}')
        lines.append(f'CL_{tag} Y_{tag} 0 {self.setup["load_table_pf"]}p')
        return '\n'.join(lines) + '\n'

    def run(self, deck_text, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / 'check.sp').write_text(deck_text, encoding='utf-8')
        result = subprocess.run(self.command + ['-b', 'check.sp'], cwd=workdir, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        (workdir / 'ngspice.log').write_text(result.stdout, encoding='utf-8')
        if result.returncode != 0:
            raise RuntimeError(f'ngspice failed (exit {result.returncode}); see {workdir / "ngspice.log"}')
        if re.search(r'fatal error|no such file|unknown subckt|could not find', result.stdout, re.I):
            raise RuntimeError(f'ngspice reported an error; see {workdir / "ngspice.log"}')
        return result.stdout


def level(v, vdd, bit):
    return v <= 0.1 * vdd if bit == 0 else v >= 0.9 * vdd


NUMBER = r'([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)'


def simulate(sim, dc_cases, edge_cases, workdir):
    """One ngspice run for a group of cases. dc_cases: [(tag, input values)];
    edge_cases: [(tag, start values, toggled input)]. Returns ({tag: DC voltage of Y},
    {tag: [(t ns, v)]} for the transient)."""
    vdd = sim.setup['supply_v']
    ramp = sim.setup['input_ramp_full_ns']
    stop = EDGE_NS + ramp + SETTLE_NS + 1.0
    deck = sim.header()
    for tag, values in dc_cases:
        deck += sim.copy(tag, {p: vdd if values[p] else 0 for p in sim.inputs})
    for tag, start, toggled in edge_cases:
        sources = {}
        for p in sim.inputs:
            v0 = vdd if start[p] else 0
            if p == toggled:
                v1 = 0 if start[p] else vdd
                sources[p] = f'PWL(0 {v0} {EDGE_NS}n {v0} {EDGE_NS + ramp:.6g}n {v1})'
            else:
                sources[p] = v0
        deck += sim.copy(tag, sources)
    control = '.control\n'
    if dc_cases:
        control += 'op\n' + ''.join(f'print v(Y_{tag})\n' for tag, _ in dc_cases)
    if edge_cases:
        control += ('set wr_singlescale\n'
                    f'tran 1p {stop:.6g}n\n'
                    'wrdata waveform.dat ' + ' '.join(f'v(Y_{tag})' for tag, _, _ in edge_cases) + '\n')
    deck += control + 'quit\n.endc\n.end\n'
    log = sim.run(deck, workdir)
    dc = {}
    for tag, _ in dc_cases:
        match = re.search(rf'v\(y_{re.escape(tag.lower())}\)\s*=\s*{NUMBER}', log, re.I)
        dc[tag] = float(match[1]) if match else None
    waves = {tag: [] for tag, _, _ in edge_cases}
    if edge_cases:
        for line in (workdir / 'waveform.dat').read_text(encoding='utf-8').splitlines():
            fields = line.split()
            try:
                row = [float(x) for x in fields]
            except ValueError:
                continue
            if len(row) != 1 + len(edge_cases):
                continue
            for (tag, _, _), v in zip(edge_cases, row[1:]):
                waves[tag].append((row[0] * 1e9, v))
        for tag, samples in waves.items():
            if not samples:
                raise RuntimeError(f'no waveform samples for {tag} in {workdir / "waveform.dat"}')
    return dc, waves


def dc_verdict(sim, truth_table, values, voltage):
    vdd = sim.setup['supply_v']
    expected = output_bit(truth_table, values, sim.inputs)
    if voltage is None:
        return {'inputs': values, 'expected': expected, 'passed': False,
                'error': 'could not read v(Y) from the ngspice output'}
    return {'inputs': values, 'expected': expected, 'voltage_v': voltage,
            'passed': level(voltage, vdd, expected)}


def transient_verdict(sim, truth_table, start, toggled, samples):
    """Toggle exactly one input, starting from a settled input state."""
    vdd = sim.setup['supply_v']
    ramp = sim.setup['input_ramp_full_ns']
    end = dict(start, **{toggled: 1 - start[toggled]})
    settle_from = EDGE_NS + ramp + SETTLE_NS

    before = output_bit(truth_table, start, sim.inputs)
    after = output_bit(truth_table, end, sim.inputs)
    pre = [v for t, v in samples if EDGE_NS - 1.0 <= t < EDGE_NS]
    post = [v for t, v in samples if t >= settle_from]
    pre_ok = bool(pre) and all(level(v, vdd, before) for v in pre)
    post_ok = bool(post) and all(level(v, vdd, after) for v in post)
    # If the output should not change, it must stay on its side of VDD/2
    # throughout, including any charge-sharing glitch.
    no_crossing = True
    worst = None
    if before == after:
        watched = [v for t, v in samples if t >= EDGE_NS]
        worst = min(watched) if before else max(watched)
        no_crossing = (worst > vdd / 2) if before else (worst < vdd / 2)
    return {'start_inputs': start, 'toggled_input': toggled, 'end_inputs': end,
            'expected_before': before, 'expected_after': after,
            'pre_edge_valid': pre_ok, 'settled': post_ok,
            'worst_glitch_v': worst,
            'no_half_supply_crossing_when_output_unchanged': no_crossing,
            'passed': pre_ok and post_ok and no_crossing}


def main():
    args = parse_args()
    report = {'check': 'transistor_level', 'pex': str(args.pex), 'passed': False}
    try:
        reader = json.loads(args.request.read_text(encoding='utf-8'))
        if reader.get('status') != 'pass':
            raise ValueError('request_reader.py refused the request')
        request = reader['request']
        inputs, truth_table = request['inputs'], request['truth_table_value']
        setup = liberty_setup(args.liberty, args.liberty_cell, args.slew_target_ns, args.load_target_pf)
        # A containerized ngspice sees only the work folder's tree, so simulate a copy.
        if args.workdir.exists():
            shutil.rmtree(args.workdir)
        args.workdir.mkdir(parents=True)
        args.pex = Path(shutil.copyfile(args.pex, args.workdir / args.pex.name))
        sim = Simulator(args, setup, inputs, request['output'])
        report.update(cell=sim.name, corner=args.corner, truth_table=request['truth_table'],
                      ngspice=args.ngspice, **setup, dc=[], transient=[])

        dc_cases, edge_cases = [], []
        for bits in itertools.product((0, 1), repeat=len(inputs)):
            values = dict(zip(inputs, bits))
            name = ''.join(map(str, bits))
            dc_cases.append((f'd{name}', values))
            edge_cases += [(f't{name}_{p}', values, p) for p in inputs]
        # --jobs decks, each one ngspice run, the cases dealt out in turn.
        groups = max(1, min(args.jobs, len(edge_cases)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=groups) as pool:
            futures = [pool.submit(simulate, sim, dc_cases[g::groups], edge_cases[g::groups],
                                   args.workdir / f'group_{g}') for g in range(groups)]
            dc_v, waves = {}, {}
            for f in futures:                  # result() re-raises a simulator error
                d, w = f.result()
                dc_v.update(d)
                waves.update(w)
        report['simulation'] = {'ngspice_runs': groups, 'cases_per_run': 'every case its own copy of the cell'}
        report['dc'] = [dc_verdict(sim, truth_table, values, dc_v[tag]) for tag, values in dc_cases]
        report['transient'] = [transient_verdict(sim, truth_table, start, p, waves[tag])
                               for tag, start, p in edge_cases]
        report['dc_passed'] = all(x['passed'] for x in report['dc'])
        report['transient_passed'] = all(x['passed'] for x in report['transient'])
        report['passed'] = report['dc_passed'] and report['transient_passed']
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        report['error'] = str(exc)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    summary = (f"{report.get('cell', args.pex.name)}: DC {len(report.get('dc', []))} rows, "
               f"transient {len(report.get('transient', []))} edges")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {summary}" +
          (f" ({report['error']})" if 'error' in report else ''))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
