#!/usr/bin/env python3
"""Step 6 checks: extraction, LVS, RC-LVS, and LVS against the installed HD cell.

For every cells/<name>/ with cell.gds, schematic.spice and input.json:
  1. Magic extracts three netlists (devices; + C; + distributed RC) with Tutorial 3's
     magic_script(), ports in the HD order: inputs, VGND, VNB, VPB, VPWR, outputs;
  2. Netgen compares the device netlist with schematic.spice (Tutorial 3's lvs());
  3. the RC netlist is collapsed and compared again (Tutorial 3's collapse_rc());
  4. if the spec names an 'hd_reference', the device netlist is compared with that
     cell's subcircuit in the PDK SPICE library, W and L included (Table 4).
The instructor functions are read from Tutorial 3's verify.py and used unchanged;
tools run in the course container through its flow.py and tools.sh, so the work
folder is <tutorial-root>/build/dd2_<name>/. The DRC count of the isolated cell is
recorded, not judged: an HD logic cell alone always lacks taps (nwell.4), and
verify_drc.py judges DRC in tapped rows. Extracted netlists are copied to
cells/<name>/ and the logs to results/lvs/<name>/. Linux only; standard library only.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

INSTRUCTOR_NAMES = ('MAGIC_ERRORS', 'magic_script', 'lvs', 'collapse_rc')


def instructor_functions(root):
    """Tutorial 3's flow module and its verify.py functions, unchanged."""
    spec = importlib.util.spec_from_file_location('course_flow', root / 'flow.py')
    flow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flow)
    source = (root / 'verify.py').read_text()
    # verify.py imports cells.py, which needs GDSfactory; take only what is used.
    nodes = [n for n in ast.parse(source).body
             if (isinstance(n, ast.FunctionDef) and n.name in INSTRUCTOR_NAMES)
             or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in INSTRUCTOR_NAMES
                                                   for t in n.targets))]
    ns = {'TECH': flow.TECH, 'LVS_SETUP': flow.LVS_SETUP, 'run': flow.run, 're': re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root / 'verify.py'), 'exec'), ns)
    missing = [n for n in INSTRUCTOR_NAMES if n not in ns]
    if missing:
        raise RuntimeError(f'Tutorial 3 verify.py has no {missing}')
    ns['verify_sha256'] = hashlib.sha256(source.encode()).hexdigest()
    return flow, ns


def subckt(text, name):
    match = re.search(rf'(?ims)^\.subckt\s+{re.escape(name)}\s.*?^\.ends\b[^\n]*\n?', text)
    if not match:
        raise RuntimeError(f'subcircuit {name} not found')
    return match[0]


def verify_cell(folder, root, flow, ns, pdk_spice, outdir):
    spec = json.loads((folder / 'input.json').read_text())
    name = spec['name']
    ports = list(spec['inputs']) + ['VGND', 'VNB', 'VPB', 'VPWR', spec['output']]
    build = root / 'build' / f'dd2_{name}'
    if build.exists():
        shutil.rmtree(build)
    rpt = build / 'rpt'
    rpt.mkdir(parents=True)
    shutil.copyfile(folder / 'cell.gds', build / f'{name}.gds')
    shutil.copyfile(folder / 'schematic.spice', build / f'{name}.spice')
    result = {'cell': name, 'ports': ports,
              'gds_sha256': hashlib.sha256((folder / 'cell.gds').read_bytes()).hexdigest()}
    try:
        script = ns['magic_script'](name, ports)
        # Pin labels read from GDS pin layers are ports already, so Magic refuses
        # Tutorial 3's 'port X make N'; set the HD port order with 'port X index N'.
        order = ''.join(f'port {pin} index {i}\n' for i, pin in enumerate(ports, 1))
        script = script.replace('extract all\n', order + 'extract all\n', 1)
        (build / f'{name}_verify.tcl').write_text(script)
        out = flow.run('magic', ['-dnull', '-noconsole', '-rcfile', '/dev/null'],
                       build, rpt / f'{name}_magic.log', stdin=script)
        count = re.search(r'DRC_COUNT (\d+)', out)
        if not count or 'MAGIC_DONE' not in out or re.search(ns['MAGIC_ERRORS'], out):
            raise RuntimeError(f'Magic did not finish cleanly on {name}; see {name}_magic.log')
        result['isolated_drc_count'] = int(count[1])
        result['isolated_drc_why'] = re.findall(r'\(([a-zA-Z0-9./]+)\)', out.split('DRC_WHY', 1)[1].split('\n', 1)[0])
        result['lvs_passed'] = ns['lvs'](build, rpt, name, f'{name}_lvs')
        r, c = ns['collapse_rc'](build, name)
        result['rc'] = {'resistors': r, 'capacitors': c,
                        'collapsed_lvs_passed': ns['lvs'](build, rpt, name, f'{name}_rc_collapsed')}
        reference = spec.get('hd_reference')
        if reference:
            # lvs() compares '<layout>.spice <cell>' with '<cell>.spice <cell>', so give the
            # extracted netlist the reference cell's name and the reference its own file.
            (build / f'{reference}.spice').write_text(subckt(pdk_spice.read_text(), reference))
            layout = (build / f'{name}_lvs.spice').read_text()
            layout = re.sub(rf'(?im)^(\.subckt\s+){re.escape(name)}\b', rf'\g<1>{reference}', layout)
            layout = re.sub(rf'(?im)^(\.ends\s+){re.escape(name)}\b', rf'\g<1>{reference}', layout)
            (build / f'{name}_as_hd.spice').write_text(layout)
            result['hd_reference'] = reference
            result['hd_reference_lvs_passed'] = ns['lvs'](build, rpt, reference, f'{name}_as_hd')
        result['passed'] = (result['lvs_passed'] and result['rc']['collapsed_lvs_passed']
                            and result.get('hd_reference_lvs_passed', True))
    except (RuntimeError, OSError) as exc:
        result['error'] = str(exc)
        result['passed'] = False
    finally:
        for suffix in ('_lvs.spice', '_cap.spice', '_pex.spice', '_rc_collapsed.spice'):
            if (build / f'{name}{suffix}').is_file():
                shutil.copyfile(build / f'{name}{suffix}', folder / f'{name}{suffix}')
        logs = outdir / name
        if logs.exists():
            shutil.rmtree(logs)
        shutil.copytree(rpt, logs)
    return result


def main():
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tutorial-root', type=Path, required=True)
    ap.add_argument('--pdk-spice', type=Path, required=True,
                    help='<PDK>/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice')
    ap.add_argument('--cells', type=Path, default=base.parent / 'cells')
    ap.add_argument('--outdir', type=Path, default=base.parent / 'results' / 'lvs')
    ap.add_argument('--image', default='dd2-layout-tools')
    ap.add_argument('--netgen', default=str(base / 'netgen.sh'),
                    help="Command flow.py runs for Netgen; '' for Tutorial 3's tools.sh (see netgen.sh)")
    args = ap.parse_args()
    root, outdir = args.tutorial_root.resolve(), args.outdir.resolve()
    os.environ['LAYOUT_IMAGE'] = args.image
    os.environ['TUT3_ROOT'] = str(root)
    if args.netgen:
        os.environ['NETGEN'] = args.netgen
    outdir.mkdir(parents=True, exist_ok=True)
    # An interrupted run must never leave an old PASS behind.
    (outdir / 'verification.json').write_text(json.dumps({'passed': False, 'status': 'running'}) + '\n')
    flow, ns = instructor_functions(root)
    cells = [f for f in sorted(args.cells.iterdir()) if (f / 'cell.gds').is_file()]
    if not cells:
        print(f'FAIL: no cells/<name>/cell.gds under {args.cells}')
        return 1
    results = {}
    for folder in cells:
        r = results[folder.name] = verify_cell(folder.resolve(), root, flow, ns, args.pdk_spice, outdir)
        print(f"{folder.name}: LVS={'match' if r.get('lvs_passed') else 'FAIL'} "
              f"RC-LVS={'match' if r.get('rc', {}).get('collapsed_lvs_passed') else 'FAIL'} "
              f"HD-LVS={r.get('hd_reference_lvs_passed', 'n/a')} isolated DRC={r.get('isolated_drc_count')} "
              + (f"ERROR {r['error']}" if 'error' in r else ''), flush=True)
    report = {'check': 'lvs', 'image': args.image, 'source_verify_sha256': ns['verify_sha256'],
              'instructor_checks_modified': False, 'cells': results,
              'passed': all(r['passed'] for r in results.values())}
    (outdir / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS' if report['passed'] else 'FAIL')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
