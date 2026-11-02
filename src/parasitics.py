"""Summarize extracted R/C by resistor-connected net, without claiming effective R."""
import hashlib
import re
from pathlib import Path


def spice_number(text):
    match = re.fullmatch(r'([-+0-9.eE]+)(meg|[fpnumkgt])?', text, re.I)
    if not match:
        raise ValueError(f'Unsupported SPICE value: {text}')
    scale = {'': 1, 'f': 1e-15, 'p': 1e-12, 'n': 1e-9, 'u': 1e-6,
             'm': 1e-3, 'k': 1e3, 'meg': 1e6, 'g': 1e9, 't': 1e12}
    return float(match[1]) * scale[(match[2] or '').lower()]


def summarize(path):
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    header = re.search(r'(?im)^\.subckt\s+\S+\s+(.+)', text)
    if not header:
        raise ValueError('Extracted netlist has no subcircuit declaration')
    ports = header[1].split()
    parent = {}
    def find(node):
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]
    resistors, capacitors = [], []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and re.fullmatch(r'[RC]\d+', fields[0], re.I):
            kind, a, b, raw = fields
            value = spice_number(raw)
            if value < 0:
                raise ValueError('Negative parasitic component value')
            if kind.upper().startswith('R'):
                parent[find(b)] = find(a)
                resistors.append((a, b, value))
            else:
                capacitors.append((a, b, value))
    names = {find(port): port for port in ports}
    result = {}
    def entry(node):
        name = names.get(find(node), 'internal:' + find(node))
        return result.setdefault(name, {'resistor_count': 0, 'sum_resistance_ohm': 0,
                                        'incident_capacitor_count': 0, 'incident_capacitance_ff': 0})
    for port in ports:
        entry(port)
    for a, b, value in resistors:
        item = entry(a)
        item['resistor_count'] += 1
        item['sum_resistance_ohm'] += value
    for a, b, value in capacitors:
        for node in (a, b):
            item = entry(node)
            item['incident_capacitor_count'] += 1
            item['incident_capacitance_ff'] += value * 1e15
    return {'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'nets': result,
            'definition': 'R is the sum of extracted resistor values, not effective resistance. Coupling C is counted at both incident nets.'}
