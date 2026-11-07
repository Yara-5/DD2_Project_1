#!/usr/bin/env python3
"""Step 8 (handout section 3.3, overview Appendix A): the cell response and the cell report.

Reads what the flow wrote for one cell and writes
  * <cell>/response.json, in the format of docs/interface/response_example.json: request
    id, status ("pass" only when every check below passed), reason (the failing checks,
    or the request reader's message for a refused request), cell, stages, transistors
    (devices drawn, fingers counted), width in sites, area, the files (GDS, Liberty,
    Verilog model, report), the check results, the ngspice SHA-256 and the tool version
    (git revision; "-dirty" when the working tree has changes);
  * <cell>/report.json, the cell's report: request, network, layout, every verification
    and simulation result, the characterization at the HD table point nearest 100 ps and
    10 fF (handout section 6), and the HD comparison when the cell has one.

Check results in the response: drc = DRC errors of the cell in a tapped row (the 8
contexts next to a tap cell), abutment = DRC errors over all 24 contexts (tap, inv_1,
fill; side by side, mirrored, flipped), layers = the implant/npc layer check, lvs and
rc_lvs = Netgen's result, truth_table = switch level (network and reordered network),
transistor level and model level (Verilog and the characterized Liberty) all passed.
Every number is read from a file the flow wrote (AGENTS.md rule 2); a missing file
makes the check "not run" and the status "fail".

A refused request (request_reader.py status "fail") gets a response with status "fail",
the reader's reason and no cell data. Exits 0 only for status "pass". Standard library only.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

PREFIX = 'dd2_g5__'


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def tool_version():
    try:
        head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'],
                               capture_output=True, text=True, check=True).stdout.strip()
        return f'git:{head}' + ('-dirty' if dirty else '')
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'


def refused(raw_path, reader):
    raw = load(raw_path) or {}
    return {'request_id': raw.get('request_id'), 'status': 'fail',
            'reason': f'request refused: {reader.get("reason") if reader else "request_reader.py output missing"}',
            'cell': raw.get('name'), 'stages': None, 'transistors': None, 'width_sites': None,
            'area_um2': None, 'files': {}, 'checks': {}, 'ngspice_sha256': None, 'tool_version': tool_version()}


def at_point(table, slews, loads, point):
    return table[slews.index(point['slew_ns'])][loads.index(point['load_pf'])]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cell-dir', type=Path, required=True, help='cells/<name>/')
    ap.add_argument('--raw-request', type=Path, help='tests/requests/<cell>.json (for a refused request)')
    ap.add_argument('--results', type=Path, default=Path('results'))
    args = ap.parse_args()

    folder, results = args.cell_dir, args.results
    name = folder.name
    short = name[len(PREFIX):] if name.startswith(PREFIX) else name
    # request_original.json is request_reader.py's own verdict; request.json (written after
    # it by reference_cell.py) may be left from an earlier run of the same name.
    original = load(folder / 'request_original.json')
    reader = original if original and original.get('status') != 'pass' else load(folder / 'request.json')
    if not reader or reader.get('status') != 'pass':
        response = refused(args.raw_request, reader)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'response.json').write_text(json.dumps(response, indent=2) + '\n', encoding='utf-8')
        print(f'REFUSED: {name}: {response["reason"]}')
        return 1
    request = reader['request']

    network = load(folder / 'network.json') or {}
    plan = load(folder / 'layout_plan.json') or {}
    spec = load(folder / 'layout_spec.json') or {}
    characterized = load(folder / 'characterized.json') or {}
    drc = load(results / 'drc' / 'verification.json') or {}
    lvs = (load(results / 'lvs' / 'verification.json') or {}).get('cells', {}).get(name)
    switch = [load(results / 'switch_level' / f'{short}_network.json'), load(results / 'switch_level' / f'{short}.json')]
    transistor = load(results / 'transistor_level' / f'{short}.json')
    model = load(results / 'model_level' / f'{short}.json')
    view = load(results / 'views' / f'{short}.json')
    comparison = (load(results / 'hd_comparison' / 'comparison.json') or {}).get('cells', {}).get(short)

    failing = []
    contexts = [c for c in drc.get('cases', []) if c.get('generated') == name and c.get('kind') == 'abutment']
    clean_run = bool(contexts) and all(c.get('completed_cleanly') for c in contexts)
    tap = [c for c in contexts if 'tap' in c.get('neighbor', '')]
    drc_errors = sum(c['drc_count'] for c in tap) if clean_run and tap else 'not run'
    abutment_errors = sum(c['drc_count'] for c in contexts) if clean_run else 'not run'
    layers_report = drc.get('layers', {}).get(name)
    layers = ('pass' if layers_report['passed'] else 'fail') if layers_report else 'not run'
    lvs_result = ('match' if lvs['lvs_passed'] else 'mismatch') if lvs else 'not run'
    rc_result = ('match' if lvs['rc']['collapsed_lvs_passed'] else 'mismatch') if lvs and 'rc' in lvs else 'not run'
    levels = {'switch_level_network': switch[0], 'switch_level_reordered': switch[1],
              'transistor_level': transistor, 'model_level': model}
    characterized_model = bool(model) and model.get('liberty_status') == 'characterized'
    truth = 'pass' if all(r and r.get('passed') for r in levels.values()) and characterized_model else \
        ('not run' if not all(levels.values()) or not characterized_model else 'fail')
    checks = {'drc': drc_errors, 'lvs': lvs_result, 'rc_lvs': rc_result, 'truth_table': truth,
              'abutment': abutment_errors, 'layers': layers}
    for key, good in (('drc', 0), ('abutment', 0), ('lvs', 'match'), ('rc_lvs', 'match'),
                      ('truth_table', 'pass'), ('layers', 'pass')):
        if checks[key] != good:
            failing.append(f'{key}: {checks[key]}')
    if not characterized.get('passed'):
        failing.append('characterization: not run or failed')
    if not (view and view.get('passed')):
        failing.append('Liberty view: ' + ('not run' if not view else 'failed'))
    if not characterized_model:
        failing.append('model level on the characterized Liberty: not run')

    geometry = plan.get('geometry', {})
    dimensions = plan.get('dimensions', {})
    area = dimensions['cell_width_um'] * dimensions['cell_height_um'] if dimensions else None
    files = {'gds': (folder / 'cell.gds').as_posix(), 'liberty': (folder / 'cell.lib').as_posix(),
             'verilog': (folder / 'cell.v').as_posix(), 'report': (folder / 'report.json').as_posix()}
    for key, path in files.items():
        if key != 'report' and not Path(path).is_file():
            failing.append(f'{key} file missing')
    response = {'request_id': request.get('request_id'), 'status': 'pass' if not failing else 'fail',
                'reason': None if not failing else '; '.join(failing), 'cell': name,
                'stages': len(network.get('stages', [])) or None,
                'transistors': len(geometry.get('devices', [])) or None,
                'width_sites': geometry.get('width_sites'), 'area_um2': round(area, 6) if area else None,
                'files': files, 'checks': checks,
                'ngspice_sha256': characterized.get('simulator', {}).get('sha256'),
                'tool_version': tool_version()}

    # The cell report.
    nominal = characterized.get('nominal_point')
    timing = []
    if nominal:
        slews, loads = characterized['setup']['slews_ns'], characterized['setup']['loads_pf']
        for arc in characterized['timing']:
            timing.append({'pin': arc['pin'], 'sense': arc['sense'],
                           **{k: at_point(v, slews, loads, nominal) for k, v in arc['tables'].items()}})
    report = {
        'cell': name, 'response': response, 'request': request,
        'network': {k: network.get(k) for k in ('formula', 'formula_source', 'objective', 'sizing', 'cost', 'sizes_from')}
        | {'stages': [{k: s.get(k) for k in ('output', 'kind', 'function', 'pull_down_series', 'pull_up_series')}
                      for s in network.get('stages', [])]},
        'layout': {'width_sites': geometry.get('width_sites'), 'width_um': dimensions.get('cell_width_um'),
                   'height_um': dimensions.get('cell_height_um'), 'area_um2': response['area_um2'],
                   'devices': response['transistors'], 'euler': spec.get('placement'),
                   'met1_vias': len(geometry.get('vias', [])),
                   'met1_in_single_stage_cell': geometry.get('met1_in_single_stage_cell'),
                   'routing_attempts_failed': len(dimensions.get('routing_retries', []))},
        'verification': {'drc_contexts': len(contexts), 'drc_errors_tapped_row': drc_errors,
                         'drc_errors_all_contexts': abutment_errors, 'layer_check': layers,
                         'lvs': lvs_result, 'rc_lvs': rc_result,
                         'rc_elements': lvs.get('rc') if lvs else None,
                         'hd_reference_lvs': {k: lvs.get(k) for k in ('hd_reference', 'hd_reference_lvs_required',
                                                                      'hd_reference_lvs_passed', 'hd_transistors')}
                         if lvs and lvs.get('hd_reference') else None},
        'simulation': {k: (v.get('passed') if v else 'not run') for k, v in levels.items()}
        | {'transistor_level_dc_rows': len(transistor.get('dc', [])) if transistor else None,
           'transistor_level_edges': len(transistor.get('transient', [])) if transistor else None},
        'characterization': {'simulator': characterized.get('simulator'), 'nominal_point': nominal,
                             'arcs_at_nominal_point_ns': timing,
                             'input_capacitance_pf': {p: c['capacitance'] for p, c in characterized.get('capacitance_pf', {}).items()},
                             'extended_windows_ns': characterized.get('extended_windows_ns')} if characterized else None,
        'liberty_view': view.get('passed') if view else 'not run',
        'hd_comparison': {k: comparison.get(k) for k in ('extra_sites', 'area_increase_percent')}
        | {'installed_width_sites': comparison['installed']['width_sites'],
           'median_timing_difference_percent': (comparison.get('timing') or {}).get('median_difference_percent')}
        if comparison else None,
    }
    (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (folder / 'response.json').write_text(json.dumps(response, indent=2) + '\n', encoding='utf-8')
    print(f"{'PASS' if response['status'] == 'pass' else 'FAIL'}: response, {name}"
          + (f" ({response['reason']})" if response['reason'] else ''))
    return 0 if response['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
