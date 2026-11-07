#!/usr/bin/env python3
"""Step 1b (handout section 4.1): the reference cells take their pin names and transistor
sizes from the installed HD cell.

"Generate inv_1, nand2_1, nor2_1, nand3_1, a21oi_1 and o21ai_1 from their functions, with
the transistor sizes and pin names (A1, A2, B1 and so on) read from the installed cell's
SPICE netlist", and a21o_1 and mux2_1 "in the same way" (also nor3_1, Table 6).

Input: the output of request_reader.py, the HD Liberty (pin directions and the output's
function) and the PDK SPICE library (ports and transistors). For a request whose name is
dd2_g5__<cell> with <cell> in hd_reference.REFERENCE_CELLS:
  * the pin map is one under which HD's Liberty function equals the request's truth
    table (hd_reference.pin_maps; the map that keeps the most names is taken), never one
    read from the pin order or the cell name alone (AGENTS.md rule 6);
  * the inputs are renamed and put in HD's port order, the truth table is permuted to
    that order, the expression is renamed, and the result is validated again by
    request_reader.validate_request (expression and table must still agree);
  * hd_reference.json records the map and the installed cell's transistors, which
    synthesize.py uses as the 'hd' sizes (step 3).
Any other request passes through unchanged, with "reference": null.
Outputs: --out (the request, renamed when a reference applies) and --reference-out.
Exit 1 on any error.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import hd_reference
from request_reader import RequestError, format_truth_table, validate_request


def renamed_request(request, hd, mapping):
    """The request with mapping applied: inputs in HD's order, the table permuted to it."""
    old_inputs = request['inputs']
    inverse = {v: k for k, v in mapping.items()}
    new_inputs = [p for p in hd['inputs'] if p in inverse]
    table = request['truth_table_value']
    permuted = 0
    for row in range(1 << len(new_inputs)):
        value = {inverse[name]: (row >> k) & 1 for k, name in enumerate(new_inputs)}
        old_row = sum(value[name] << k for k, name in enumerate(old_inputs))
        permuted |= ((table >> old_row) & 1) << row
    out = {k: v for k, v in request.items()
           if k not in ('truth_table_value', 'recognized', 'supported_by_prototype')}
    out.update(inputs=new_inputs, output=mapping[request['output']],
               truth_table=format_truth_table(permuted, len(new_inputs)))
    if 'expression' in request:
        out['expression'] = re.sub(r'[A-Za-z_][A-Za-z0-9_]*',
                                   lambda m: mapping.get(m[0], m[0]), request['expression'])
    return validate_request(out)


def reference_for(request, liberty_text, spice_text):
    short = request['name'].split('__', 1)[-1]
    if short not in hd_reference.REFERENCE_CELLS:
        return request, {'reference': None, 'reason': f'{short} is not a reference cell'}
    hd = hd_reference.hd_cell(liberty_text, spice_text, short)
    if hd is None:
        raise ValueError(f'sky130_fd_sc_hd__{short} is not in the PDK SPICE library')
    maps = hd_reference.pin_maps(request['inputs'], request['output'], request['truth_table_value'], hd)
    if not maps:
        raise ValueError(f"{request['name']}: no pin map gives {hd['name']}'s function {hd['function']}")
    maps.sort(key=lambda m: -sum(k == v for k, v in m.items()))
    mapping = maps[0]
    new = renamed_request(request, hd, mapping)
    # The renamed function must be HD's, now with HD's own pin names and order.
    hd_table = hd_reference.function_truth_table(hd['function'], hd['inputs'])
    if new['inputs'] != hd['inputs'] or new['truth_table_value'] != hd_table:
        raise ValueError(f"{request['name']}: renamed function {new['truth_table']} is not {hd['name']}'s")
    return new, {'reference': hd['name'], 'pin_map': mapping, 'valid_pin_maps': maps,
                 'ports': hd['ports'], 'function': hd['function'], 'devices': hd['devices'],
                 'source': 'PDK SPICE library (ports, W, L) and HD Liberty (directions, function)'}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('request', type=Path, help='output of request_reader.py')
    ap.add_argument('--hd-liberty', type=Path, required=True)
    ap.add_argument('--hd-spice', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True, help='request to use from here on')
    ap.add_argument('--reference-out', type=Path, required=True, help='hd_reference.json')
    args = ap.parse_args()
    try:
        reader = json.loads(args.request.read_text(encoding='utf-8'))
        if reader.get('status') != 'pass':
            raise RequestError('request_reader.py refused the request')
        request = reader['request']
        new, info = reference_for(request, args.hd_liberty.read_text(encoding='utf-8', errors='replace'),
                                  args.hd_spice.read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError, KeyError, RequestError) as exc:
        print(f'FAIL: {exc}')
        return 1
    args.out.write_text(json.dumps({'status': 'pass', 'reason': None, 'request': new}, indent=2) + '\n',
                        encoding='utf-8')
    args.reference_out.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    if info['reference']:
        print(f"{request['name']}: pins from {info['reference']}: "
              + ', '.join(f'{k}->{v}' for k, v in info['pin_map'].items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
