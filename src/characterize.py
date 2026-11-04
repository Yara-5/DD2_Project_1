#!/usr/bin/env python3
"""Step 8, section 5: characterize one cell on its RC-extracted netlist.

Extends Tutorial 3's timing.py (one ngspice run per arc, which loads the models once
and `alter`s its way through every table point) to what the HD Liberty needs:
  1. Magic extracts the cell with Tutorial 3's magic_script(), as verify_lvs.py does,
     from the generated GDS or, for the validation of section 5.1, from the installed
     HD cell in the PDK GDS;
  2. every transistor gets its BSIM4 stress distances SA and SB: Magic's .ext file
     gives each device's position, and the GDS gives the diffusion strip it sits in;
     SA and SB run from the gate edges to the strip's ends, through any other gates;
  3. for every input pin, every side-input setting in which the pin controls Y is
     simulated over the table indices (both input edges, so both output edges); the
     slowest setting of each arc and output edge is kept, as the HD TT library does;
  4. the input capacitance of every pin is the charge its source delivers over each
     edge, divided by the supply.
Supply, temperature, thresholds, slew derating and table indices are read from the HD
Liberty (AGENTS.md rule 7); the tables are indexed by the 20-80% slew, so each input
ramp lasts slew / (upper - lower threshold). The ngspice version and the SHA-256 of
its binary in the course image are recorded in every output.

Outputs: <out>.json (setup, devices with SA/SB, every setting's tables, the chosen
ones) and <out>.lib (one cell group in HD units). Exits non-zero when a measurement
is missing, an output does not settle, or a tool fails: a cell that was not
characterized is never reported as characterized. Linux only; standard library only.
"""
import argparse
import concurrent.futures
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from request_reader import function_truth_table, output_bit
from verify_lvs import instructor_functions, subckt

DIFF = (65, 20)
POLY = (66, 20)
SUPPLY_NODES = {'VPWR': 'VPWR', 'VPB': 'VPWR', 'VGND': '0', 'VNB': '0'}
EDGE_NS = 1.0            # the first input edge, once the initial state has settled
HOLD_NS = 4.0            # time the output gets after each ramp, plus the ramp itself
# The course ngspice evaluates BSIM4 with OpenMP, as many threads as its num_threads
# variable says, whatever OMP_NUM_THREADS is; several runs at once then oversubscribe
# the cores and each run slows down a hundredfold. One thread per run, as tools.sh intends.
THREADS = 'set num_threads=1'


# ---------------------------------------------------------------- the HD Liberty

def liberty_block(text, kind, name):
    """The text of `kind ("name") { ... }`, braces balanced."""
    found = re.search(rf'(?<![\w]){kind} \("{re.escape(name)}"\)', text)
    if not found:
        raise ValueError(f'{kind} {name} not found in the Liberty file')
    depth, start = 0, found.start()
    i = text.index('{', start)
    for j in range(i, len(text)):
        depth += {'{': 1, '}': -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise ValueError(f'unbalanced braces in {kind} {name}')


def liberty_setup(path, template_cell):
    """Operating point, thresholds and table indices, read from the HD Liberty."""
    text = path.read_text(encoding='utf-8', errors='replace')
    header = text[:text.find('cell (')]

    def attribute(name):
        match = re.search(rf'\b{name}\s*:\s*([-+\d.eE]+)\s*;', header)
        if not match:
            raise ValueError(f'{name} not found in {path}')
        return float(match[1])

    names = ['nom_voltage', 'nom_temperature', 'nom_process', 'slew_derate_from_library']
    for edge in ('rise', 'fall'):
        names += [f'input_threshold_pct_{edge}', f'output_threshold_pct_{edge}',
                  f'slew_lower_threshold_pct_{edge}', f'slew_upper_threshold_pct_{edge}']
    setup = {name: attribute(name) for name in names}
    units = {key: re.search(rf'\b{key}\s*:\s*"([^"]+)"', header)[1]
             for key in ('time_unit', 'voltage_unit', 'current_unit', 'pulling_resistance_unit',
                         'leakage_power_unit')}
    cap = re.search(r'capacitive_load_unit\s*\(\s*([\d.]+)\s*,\s*"(\w+)"\s*\)', header)
    if units['time_unit'] != '1ns' or cap[2] != 'pf' or float(cap[1]) != 1.0:
        raise ValueError('expected the HD units: 1ns and 1pf')
    setup['units'] = dict(units, capacitive_load_unit=[float(cap[1]), cap[2]])
    setup['library'] = re.search(r'library\s*\(\s*"([^"]+)"', text)[1]
    setup['operating_conditions'] = re.search(r'default_operating_conditions\s*:\s*"([^"]+)"', header)[1]
    setup['template_cell'] = template_cell
    setup.update(cell_indices(liberty_block(text, 'cell', template_cell)))
    return setup, text


def cell_indices(block):
    """The delay table indices of one HD cell (they differ from cell to cell)."""
    table = re.search(r'cell_rise\s*\("([^"]+)"\)\s*\{\s*index_1\("([^"]+)"\);\s*index_2\("([^"]+)"\)', block)
    if not table:
        raise ValueError('no cell_rise table in the template cell')
    return {'table_template': table[1],
            'slews_ns': [float(x) for x in table[2].split(',')],
            'loads_pf': [float(x) for x in table[3].split(',')]}


def shipped_cell(text, cell):
    """The shipped values of one HD cell: area, pin capacitances, Y's function."""
    block = liberty_block(text, 'cell', cell)
    pins = {}
    for match in re.finditer(r'(?<![\w])pin \("(\w+)"\) \{', block):
        body = liberty_block(block, 'pin', match[1])
        direction = re.search(r'direction\s*:\s*"(\w+)"', body)[1]
        entry = {'direction': direction}
        for key in ('capacitance', 'rise_capacitance', 'fall_capacitance'):
            found = re.search(rf'\b{key}\s*:\s*([\d.eE+-]+)', body)
            if found and direction == 'input':
                entry[key] = float(found[1])
        function = re.search(r'function\s*:\s*"([^"]+)"', body)
        if function:
            entry['function'] = function[1]
        pins[match[1]] = entry
    return {'area': float(re.search(r'\barea\s*:\s*([\d.]+)', block)[1]), 'pins': pins}


# ---------------------------------------------------------------- GDS geometry

def gds_real(data):
    """An 8-byte GDSII real (excess-64, base-16 exponent)."""
    sign = -1 if data[0] & 0x80 else 1
    exponent = (data[0] & 0x7F) - 64
    mantissa = int.from_bytes(data[1:], 'big') / 2 ** 56
    return sign * mantissa * 16 ** exponent


def gds_records(path):
    data = Path(path).read_bytes()
    i = 0
    while i + 4 <= len(data):
        length, rtype = struct.unpack('>HB', data[i:i + 3])
        if length < 4:
            break
        yield rtype, data[i + 4:i + length], data[i:i + length]
        i += length


def gds_cell(path, name):
    """Layer -> polygons (lists of (x, y) in um) of one flat GDS structure, and the
    raw records of a GDS file holding only that structure."""
    unit, inside, element, shapes, raw, head = None, False, None, {}, [], []
    for rtype, body, record in gds_records(path):
        if rtype == 0x03:                                   # UNITS
            unit = gds_real(body[8:16]) * 1e6               # database unit in um
        if rtype in (0x00, 0x01, 0x02, 0x03):               # HEADER, BGNLIB, LIBNAME, UNITS
            head.append(record)
        elif rtype == 0x05:                                 # BGNSTR
            start = record
        elif rtype == 0x06:                                 # STRNAME
            inside = body.rstrip(b'\0').decode() == name
            if inside:
                raw += [start, record]
        elif inside:
            raw.append(record)
            if rtype == 0x07:                               # ENDSTR
                inside = False
            elif rtype in (0x0A, 0x0B):                     # SREF, AREF
                raise ValueError(f'{name} references other cells; flatten it first')
            elif rtype in (0x08, 0x2D):                     # BOUNDARY, BOX
                element = {}
            elif rtype == 0x0D and element is not None:
                element['layer'] = struct.unpack('>h', body)[0]
            elif rtype in (0x0E, 0x2E) and element is not None:
                element['type'] = struct.unpack('>h', body)[0]
            elif rtype == 0x10 and element is not None:
                values = struct.unpack(f'>{len(body) // 4}i', body)
                element['xy'] = list(zip(values[::2], values[1::2]))
            elif rtype == 0x11 and element is not None:     # ENDEL
                key = (element.get('layer'), element.get('type'))
                shapes.setdefault(key, []).append([(x * unit, y * unit) for x, y in element['xy']])
                element = None
    if not raw:
        raise ValueError(f'{name} not found in {path}')
    single = b''.join(head) + b''.join(raw) + struct.pack('>HBB', 4, 0x04, 0)   # ENDLIB
    return shapes, single


def intervals_at(polygons, y):
    """The x intervals a horizontal line at y crosses inside Manhattan polygons, merged."""
    spans = []
    for poly in polygons:
        xs = sorted(a[0] for a, b in zip(poly, poly[1:] + poly[:1])
                    if a[0] == b[0] and min(a[1], b[1]) < y < max(a[1], b[1]))
        spans += list(zip(xs[::2], xs[1::2]))
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def ext_devices(path):
    """Magic .ext devices: model, position (um), L, W, substrate, gate, terminals."""
    text = path.read_text()
    scale = re.search(r'^scale\s+\d+\s+\d+\s+(\d+)', text, re.M)
    unit = int(scale[1]) * 1e-8                             # lscale is in 1e-8 um
    devices = []
    for line in text.splitlines():
        if not line.startswith('device '):
            continue
        words = line.split()
        nets = [n.rstrip('#') for n in re.findall(r'"([^"]+)"', line)]
        size = dict(re.findall(r'\b([lw])=(\d+)', line))
        devices.append({'model': words[2], 'x': int(words[3]) * unit, 'y': int(words[4]) * unit,
                        'l': int(size['l']) * unit, 'w': int(size['w']) * unit,
                        'gate': nets[1], 'terminals': sorted(nets[2:4])})
    return devices


def stress_distances(devices, shapes):
    """SA and SB of every device, from the diffusion strip at the middle of its channel."""
    for d in devices:
        y = d['y'] + d['w'] / 2
        x = d['x'] + min(d['l'], 0.001) / 2                 # inside the gate, at its lower left
        gate = next(((lo, hi) for lo, hi in intervals_at(shapes.get(POLY, []), y) if lo <= x <= hi), None)
        strip = next(((lo, hi) for lo, hi in intervals_at(shapes.get(DIFF, []), y) if lo <= x <= hi), None)
        if gate is None or strip is None:
            raise ValueError(f'no gate or diffusion at the {d["model"]} at ({d["x"]:.3f}, {d["y"]:.3f})')
        d['sa_um'] = round(gate[0] - strip[0], 4)
        d['sb_um'] = round(strip[1] - gate[1], 4)
    return devices


def annotate(pex_text, devices):
    """Add sa= sb= to every transistor of the RC netlist, matched to the .ext devices
    by model, W, L, gate net and the two diffusion nets (RC sub-node suffixes removed).
    Devices with identical connections are interchangeable, so any order is right."""
    base = lambda node: re.sub(r'\.[tn]\d+$', '', node)
    pool = list(devices)
    lines = []
    for line in pex_text.splitlines():
        if re.match(r'^X\S*\s', line) and 'sky130_fd_pr__' in line:
            words = line.split()
            drain, gate, source = (base(n) for n in words[1:4])
            size = dict(re.findall(r'\b([wl])=([\d.eE+-]+)', line))
            key = (words[5], gate, sorted([drain, source]))
            match = next((d for d in pool if (d['model'], d['gate'], d['terminals']) == key
                          and abs(d['w'] - float(size['w'])) < 1e-6 and abs(d['l'] - float(size['l'])) < 1e-6),
                         None)
            if match is None:
                raise ValueError(f'no extracted device position for: {line}')
            pool.remove(match)
            line += f" sa={match['sa_um']:g} sb={match['sb_um']:g}"
        lines.append(line)
    if pool:
        raise ValueError(f'{len(pool)} extracted devices did not appear in the RC netlist')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------- simulation

def arcs(inputs, table):
    """Every (pin, side-input setting) in which the pin controls Y, with its sense."""
    out = []
    for pin in inputs:
        others = [p for p in inputs if p != pin]
        for bits in itertools.product((0, 1), repeat=len(others)):
            side = dict(zip(others, bits))
            low = output_bit(table, dict(side, **{pin: 0}), inputs)
            high = output_bit(table, dict(side, **{pin: 1}), inputs)
            if low != high:
                out.append({'pin': pin, 'side': side,
                            'sense': 'positive_unate' if high else 'negative_unate'})
    return out


class Bench:
    def __init__(self, args, setup, flow, name, ports, inputs, output, netlist):
        self.flow, self.setup, self.name, self.ports = flow, setup, name, ports
        self.inputs, self.output, self.netlist, self.build = inputs, output, netlist, args.build
        self.vdd = setup['nom_voltage']
        self.index_by_ramp = args.index_by_ramp

    def ramp(self, slew):
        """Full 0-100% ramp of a table slew: the tables are indexed by the 20-80% time."""
        if self.index_by_ramp:      # BROKEN CASE (Table 5): the index is taken as the full ramp
            return slew
        rise = (self.setup['slew_upper_threshold_pct_rise'] - self.setup['slew_lower_threshold_pct_rise']) / 100
        return slew * self.setup['slew_derate_from_library'] / rise

    def header(self, title, held, pin):
        nodes = [f'IN_{p}' if p in self.inputs else 'Y' if p == self.output else SUPPLY_NODES[p]
                 for p in self.ports]
        lines = [f'* {title}', self.flow.MODELS, f'.include "{self.netlist}"',
                 f'.temp {self.setup["nom_temperature"]:g}', f'V_SUPPLY VPWR 0 {self.vdd:g}',
                 f'XU {" ".join(nodes)} {self.name}', 'CL Y 0 1f']
        for p in self.inputs:
            if p == pin:
                lines.append(f'V_{p} IN_{p} 0 PULSE(0 {self.vdd:g} {EDGE_NS}n 100p 100p 5n 20n)')
            else:
                lines.append(f'V_{p} IN_{p} 0 {self.vdd if held[p] else 0:g}')
        return lines

    def run(self, deck, tag):
        (self.build / f'{tag}.sp').write_text('\n'.join(deck) + '\n')
        out = self.flow.run('ngspice', ['-b', f'{tag}.sp'], self.build, self.build / f'{tag}.log', timeout=3600)
        if re.search(r'fatal error|no such file|unknown subckt|could not find|singular matrix', out, re.I):
            raise RuntimeError(f'ngspice reported an error; see {tag}.log')
        return out

    def measure(self, arc, points, tag):
        """Both input edges at every (slew, load) point; delays and output transitions."""
        s, v = self.setup, self.vdd
        pin, positive = arc['pin'], arc['sense'] == 'positive_unate'
        y_after_rise, y_after_fall = ('RISE', 'FALL') if positive else ('FALL', 'RISE')
        deck = self.header(f'{self.name}: {pin} with {arc["side"]}', arc['side'], pin) + ['.control', THREADS]
        for i, (slew, load) in enumerate(points):
            r = self.ramp(slew)
            hold = HOLD_NS + r
            stop = EDGE_NS + 2 * (r + hold)
            deck += [f'alter CL = {load}e-12',
                     f'alter @V_{pin}[pulse] = [ 0 {v:g} {EDGE_NS}n {r:.6g}n {r:.6g}n {hold:.6g}n {4 * hold:.6g}n ]',
                     f'tran 1p {stop:.6g}n']
            vin = {e: v * s[f'input_threshold_pct_{e}'] / 100 for e in ('rise', 'fall')}
            vout = {e: v * s[f'output_threshold_pct_{e}'] / 100 for e in ('rise', 'fall')}
            lower = {e: v * s[f'slew_lower_threshold_pct_{e}'] / 100 for e in ('rise', 'fall')}
            upper = {e: v * s[f'slew_upper_threshold_pct_{e}'] / 100 for e in ('rise', 'fall')}
            for first, (in_edge, y_edge) in enumerate([('RISE', y_after_rise), ('FALL', y_after_fall)]):
                o = y_edge.lower()
                deck.append(f'meas tran d{first}_{i} TRIG v(IN_{pin}) VAL={vin[in_edge.lower()]:.6g} {in_edge}=1 '
                            f'TARG v(Y) VAL={vout[o]:.6g} {y_edge}=1')
                a, b = (lower[o], upper[o]) if o == 'rise' else (upper[o], lower[o])
                deck.append(f'meas tran t{first}_{i} TRIG v(Y) VAL={a:.6g} {y_edge}=1 TARG v(Y) VAL={b:.6g} {y_edge}=1')
            # The output must have settled before each next edge.
            deck += [f'meas tran s0_{i} FIND v(Y) AT={EDGE_NS + r + hold - 0.05:.6g}n',
                     f'meas tran s1_{i} FIND v(Y) AT={stop - 0.05:.6g}n']
        deck += ['quit', '.endc', '.end']
        out = self.run(deck, tag)
        value = lambda key: re.search(rf'^{key}\s*=\s*([-+\deE.]+)', out, re.M)
        rows = []
        for i, (slew, load) in enumerate(points):
            got = {k: value(f'{k}_{i}') for k in ('d0', 't0', 'd1', 't1', 's0', 's1')}
            missing = [k for k, m in got.items() if not m]
            if missing:
                raise RuntimeError(f'{tag}: no {missing} at slew {slew} ns, load {load} pF; see {tag}.log')
            got = {k: float(m[1]) for k, m in got.items()}
            settled = (abs(got['s0'] - (v if positive else 0)) < 0.1 * v
                       and abs(got['s1'] - (0 if positive else v)) < 0.1 * v)
            if not settled or min(got['d0'], got['t0'], got['d1'], got['t1']) <= 0:
                raise RuntimeError(f'{tag}: did not switch cleanly at slew {slew} ns, load {load} pF: {got}')
            rise_first = positive
            rows.append({'slew_ns': slew, 'load_pf': load,
                         'cell_rise': got['d0'] * 1e9 if rise_first else got['d1'] * 1e9,
                         'rise_transition': (got['t0'] if rise_first else got['t1']) * 1e9 / s['slew_derate_from_library'],
                         'cell_fall': got['d1'] * 1e9 if rise_first else got['d0'] * 1e9,
                         'fall_transition': (got['t1'] if rise_first else got['t0']) * 1e9 / s['slew_derate_from_library']})
        return rows

    def capacitance(self, arc, slew, load, tag):
        """Charge the pin's source delivers while its input ramps, divided by the swing.

        The window is the input transition itself, the usual definition of a pin's
        capacitance; the charge until Y has settled (which adds the Miller charge
        after the ramp) is recorded too.
        """
        v, pin, r = self.vdd, arc['pin'], self.ramp(slew)
        hold = HOLD_NS + r
        deck = self.header(f'{self.name}: input capacitance of {pin}', arc['side'], pin)
        deck[deck.index('CL Y 0 1f')] = f'CL Y 0 {load}p'
        deck = [l if not l.startswith(f'V_{pin} ') else
                f'V_{pin} IN_{pin} 0 PULSE(0 {v:g} {EDGE_NS}n {r:.6g}n {r:.6g}n {hold:.6g}n {4 * hold:.6g}n)'
                for l in deck]
        fall = EDGE_NS + r + hold
        windows = {'rise_capacitance': (EDGE_NS, EDGE_NS + r), 'fall_capacitance': (fall, fall + r),
                   'rise_until_settled': (EDGE_NS, fall), 'fall_until_settled': (fall, fall + r + hold)}
        deck += ['.control', THREADS, f'tran 1p {fall + r + hold:.6g}n']
        deck += [f'meas tran q_{k} INTEG i(V_{pin}) FROM={a:.6g}n TO={b:.6g}n' for k, (a, b) in windows.items()]
        deck += ['quit', '.endc', '.end']
        out = self.run(deck, tag)
        got = {k: re.search(rf'^q_{k}\s*=\s*([-+\deE.]+)', out, re.M) for k in windows}
        if not all(got.values()):
            raise RuntimeError(f'{tag}: no charge measurement; see {tag}.log')
        return {k: abs(float(m[1])) / v * 1e12 for k, m in got.items()}


# ---------------------------------------------------------------- outputs

def table(rows, key, slews, loads):
    by = {(r['slew_ns'], r['load_pf']): r[key] for r in rows}
    return [[by[(s, c)] for c in loads] for s in slews]


def liberty_text(name, setup, simulator, area, inputs, output, function, caps, timing):
    s = setup
    fmt = lambda values: ', '.join(f'{x:.10f}' for x in values)
    lines = [f'/* {name}: characterized by src/characterize.py on its RC-extracted netlist.',
             f'   Simulator: {simulator["version"]}; binary SHA-256 {simulator["sha256"]}',
             f'   in image {simulator["image"]}. Thresholds, units and indices copied from {s["library"]}. */',
             f'library ("{name}_lib") {{',
             '    delay_model : "table_lookup";']
    for key in ('time_unit', 'voltage_unit', 'current_unit', 'pulling_resistance_unit', 'leakage_power_unit'):
        lines.append(f'    {key} : "{s["units"][key]}";')
    cap = s['units']['capacitive_load_unit']
    lines.append(f'    capacitive_load_unit({cap[0]:.10f}, "{cap[1]}");')
    for key in ('nom_voltage', 'nom_temperature', 'nom_process', 'slew_derate_from_library'):
        lines.append(f'    {key} : {s[key]:.10f};')
    for edge in ('rise', 'fall'):
        for key in ('input_threshold_pct', 'output_threshold_pct', 'slew_lower_threshold_pct',
                    'slew_upper_threshold_pct'):
            lines.append(f'    {key}_{edge} : {s[f"{key}_{edge}"]:.10f};')
    lines += [f'    default_operating_conditions : "{s["operating_conditions"]}";',
              f'    operating_conditions ("{s["operating_conditions"]}") {{',
              f'        voltage : {s["nom_voltage"]:.10f};', f'        process : {s["nom_process"]:.10f};',
              f'        temperature : {s["nom_temperature"]:.10f};', '    }',
              f'    lu_table_template ("{s["table_template"]}") {{',
              '        variable_1 : "input_net_transition";',
              '        variable_2 : "total_output_net_capacitance";',
              f'        index_1("{", ".join(str(i + 1) for i in range(len(s["slews_ns"])))}");',
              f'        index_2("{", ".join(str(i + 1) for i in range(len(s["loads_pf"])))}");', '    }',
              f'    cell ("{name}") {{', f'        area : {area:.10f};']
    for net, kind in (('VGND', 'primary_ground'), ('VNB', 'pwell'), ('VPB', 'nwell'), ('VPWR', 'primary_power')):
        lines +=[f'        pg_pin ("{net}") {{', f'            pg_type : "{kind}";',
                  f'            voltage_name : "{net}";', '        }']
    for pin in inputs:
        c = caps[pin]
        lines += [f'        pin ("{pin}") {{', '            direction : "input";',
                  f'            capacitance : {c["capacitance"]:.10f};',
                  f'            rise_capacitance : {c["rise_capacitance"]:.10f};',
                  f'            fall_capacitance : {c["fall_capacitance"]:.10f};',
                  '            related_ground_pin : "VGND";', '            related_power_pin : "VPWR";', '        }']
    lines += [f'        pin ("{output}") {{', '            direction : "output";',
              f'            function : "{function}";',
              '            related_ground_pin : "VGND";', '            related_power_pin : "VPWR";']
    for t in timing:
        lines += ['            timing () {', f'                related_pin : "{t["pin"]}";',
                  f'                timing_sense : "{t["sense"]}";', '                timing_type : "combinational";']
        for key in ('cell_rise', 'rise_transition', 'cell_fall', 'fall_transition'):
            rows = t['tables'][key]
            lines += [f'                {key} ("{s["table_template"]}") {{',
                      f'                    index_1("{fmt(s["slews_ns"])}");',
                      f'                    index_2("{fmt(s["loads_pf"])}");',
                      '                    values(' + ', \\\n                           '.join(f'"{fmt(r)}"' for r in rows) + ');',
                      '                }']
        lines.append('            }')
    lines += ['        }', '    }', '}']
    return '\n'.join(lines) + '\n'


def simulator_identity(image):
    """The ngspice version and the SHA-256 of its binary inside the course image."""
    docker = os.getenv('DOCKER', 'docker')
    script = 'p=$(command -v ngspice); sha256sum "$p"; ngspice -v'
    out = subprocess.run([docker, 'run', '--rm', '--entrypoint', 'sh', image, '-c', script],
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    sha = re.search(r'\b([0-9a-f]{64})\b', out.stdout)
    version = re.search(r'ngspice-(\S+)', out.stdout)
    if out.returncode or not sha or not version:
        raise RuntimeError(f'could not identify the ngspice in {image}: {out.stdout[-300:]}')
    return {'image': image, 'version': f'ngspice-{version[1]}', 'sha256': sha[1]}


# ---------------------------------------------------------------- main

def subset(values, size):
    """`size` of the indices, spread over the table, both ends kept."""
    if size >= len(values):
        return list(values)
    picks = sorted({round(i * (len(values) - 1) / (size - 1)) for i in range(size)})
    return [values[i] for i in picks]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tutorial-root', type=Path, required=True)
    ap.add_argument('--liberty', type=Path, required=True,
                    help='The shipped HD Liberty, sky130_fd_sc_hd__tt_025C_1v80.lib')
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument('--cell-dir', type=Path, help='cells/<name>/ of a generated cell')
    which.add_argument('--hd-cell', help='An installed HD cell, for the validation of section 5.1')
    ap.add_argument('--hd-gds', type=Path, help='The PDK HD GDS (with --hd-cell)')
    ap.add_argument('--pdk-spice', type=Path, help='The PDK HD SPICE library (with --hd-cell): port order')
    ap.add_argument('--template-cell', default='sky130_fd_sc_hd__nand2_1',
                    help='HD cell whose table indices a generated cell uses')
    ap.add_argument('--table-size', type=int, default=7, help='Indices per axis (handout: all, or at least 5)')
    ap.add_argument('--image', default='dd2-layout-tools')
    ap.add_argument('--jobs', type=int, default=6)
    ap.add_argument('--index-by-ramp', action='store_true',
                    help='BROKEN CASE of Table 5: use the table slew as the full ramp time (validation must flag it)')
    ap.add_argument('--out', type=Path, required=True, help='Output stem: writes <out>.json and <out>.lib')
    args = ap.parse_args()

    report = {'check': 'characterization', 'passed': False}
    try:
        root = args.tutorial_root.resolve()
        os.environ['LAYOUT_IMAGE'] = args.image
        flow, ns = instructor_functions(root)
        if args.hd_cell:
            name = args.hd_cell
            setup, lib_text = liberty_setup(args.liberty, name)
            shipped = shipped_cell(lib_text, name)
            ports = re.search(rf'(?im)^\.subckt\s+{re.escape(name)}\s+(.*)$', args.pdk_spice.read_text())[1].split()
            output = next(p for p, e in shipped['pins'].items() if e['direction'] == 'output')
            inputs = [p for p in ports if p in shipped['pins'] and p != output]
            function = shipped['pins'][output]['function']
            truth = function_truth_table(function, inputs)
            gds_path, area = args.hd_gds, shipped['area']
        else:
            request = json.loads((args.cell_dir / 'request.json').read_text())['request']
            name, inputs, output = request['name'], request['inputs'], request['output']
            truth = request['truth_table_value']
            setup, lib_text = liberty_setup(args.liberty, args.template_cell)
            ports = inputs + ['VGND', 'VNB', 'VPB', 'VPWR', output]
            function = request.get('expression') or ' | '.join(
                '(' + ' & '.join(p if (row >> i) & 1 else f'!{p}' for i, p in enumerate(inputs)) + ')'
                for row in range(1 << len(inputs)) if (truth >> row) & 1) or '0'
            plan = json.loads((args.cell_dir / 'layout_plan.json').read_text())
            area = plan['dimensions']['cell_width_um'] * plan['dimensions']['cell_height_um']
            gds_path = args.cell_dir / 'cell.gds'
        setup['slews_ns'] = subset(setup['slews_ns'], args.table_size)
        setup['loads_pf'] = subset(setup['loads_pf'], args.table_size)
        report.update(cell=name, inputs=inputs, output=output, function=function, setup=setup,
                      index_by_ramp=args.index_by_ramp)

        # 1. Extraction with Tutorial 3's Magic script, in a folder two levels below its
        #    root, where its relative TECH and MODELS paths point at the PDK.
        build = args.build = root / 'build' / f'dd2_char_{name}'
        if build.exists():
            shutil.rmtree(build)
        build.mkdir(parents=True)
        shapes, single = gds_cell(gds_path, name)
        (build / f'{name}.gds').write_bytes(single)
        script = ns['magic_script'](name, ports)
        order = ''.join(f'port {pin} index {i}\n' for i, pin in enumerate(ports, 1))
        script = script.replace('extract all\n', order + 'extract all\n', 1)
        out = flow.run('magic', ['-dnull', '-noconsole', '-rcfile', '/dev/null'], build,
                       build / 'magic.log', stdin=script)
        if 'MAGIC_DONE' not in out or re.search(ns['MAGIC_ERRORS'], out):
            raise RuntimeError(f'Magic did not finish cleanly on {name}; see {build / "magic.log"}')
        pex = build / f'{name}_pex.spice'
        found = re.search(r'(?im)^\.subckt\s+\S+\s+(.*)$', pex.read_text())[1].split()
        if found != ports:
            raise RuntimeError(f'extracted ports {found} differ from {ports}')

        # 2. SA and SB on every transistor.
        devices = stress_distances(ext_devices(build / f'{name}.ext'), shapes)
        netlist = build / f'{name}_pex_sasb.spice'
        netlist.write_text(annotate(pex.read_text(), devices))
        report['devices'] = devices
        report['simulator'] = simulator_identity(args.image)

        # 3. Every arc and side-input setting, over the table.
        bench = Bench(args, setup, flow, name, ports, inputs, output, netlist.name)
        points = [(s, c) for s in setup['slews_ns'] for c in setup['loads_pf']]
        settings = arcs(inputs, truth)
        if not settings:
            raise ValueError('Y depends on no input: nothing to characterize')
        nominal = (min(setup['slews_ns'], key=lambda x: abs(x - 0.1)),
                   min(setup['loads_pf'], key=lambda x: abs(x - 0.01)))
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            futures = [pool.submit(bench.measure, a, points, f'arc{i}_{a["pin"]}') for i, a in enumerate(settings)]
            caps = [pool.submit(bench.capacitance, a, *nominal, f'cap{i}_{a["pin"]}') for i, a in enumerate(settings)]
            for a, f, c in zip(settings, futures, caps):
                a['rows'], a['capacitance_pf'] = f.result(), c.result()

        # The slowest setting of each arc and output edge (HD TT library practice).
        timing = []
        for (pin, sense), group in itertools.groupby(sorted(settings, key=lambda a: (a['pin'], a['sense'])),
                                                     key=lambda a: (a['pin'], a['sense'])):
            group = list(group)
            tables, chosen = {}, {}
            for edge in ('rise', 'fall'):
                worst = max(group, key=lambda a: sum(r[f'cell_{edge}'] for r in a['rows']))
                chosen[edge] = worst['side']
                for key in (f'cell_{edge}', f'{edge}_transition'):
                    tables[key] = table(worst['rows'], key, setup['slews_ns'], setup['loads_pf'])
            timing.append({'pin': pin, 'sense': sense, 'slowest_side_inputs': chosen, 'tables': tables})
        caps = {}
        for pin in inputs:
            values = [a['capacitance_pf'] for a in settings if a['pin'] == pin]
            if not values:              # Y does not depend on this pin: no switching setting
                continue
            rise = max(v['rise_capacitance'] for v in values)
            fall = max(v['fall_capacitance'] for v in values)
            caps[pin] = {'rise_capacitance': rise, 'fall_capacitance': fall, 'capacitance': (rise + fall) / 2}
        report.update(settings=settings, timing=timing, capacitance_pf=caps, area_um2=area,
                      nominal_point={'slew_ns': nominal[0], 'load_pf': nominal[1]})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix('.lib').write_text(
            liberty_text(name, setup, report['simulator'], area, [p for p in inputs if p in caps],
                         output, function, caps, timing))
        report['liberty'] = str(args.out.with_suffix('.lib'))
        report['passed'] = True
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    arcs_done = len(report.get('timing', []))
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {report.get('cell', '?')}: {arcs_done} arcs"
          + (f" ({report['error']})" if 'error' in report else ''))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())