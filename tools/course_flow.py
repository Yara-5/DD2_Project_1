"""Container adapter for unchanged instructor extraction/LVS and project simulation.

Only build/, results/ and cells/ are writable mounts. Sources and PDK are read-only.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, '/repo/src')
from parasitics import summarize

OUT = Path('/results/checkpoint1')
OUT.mkdir(parents=True, exist_ok=True)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def run(command, cwd, log, stdin=None, timeout=180):
    with log.open('w', encoding='utf-8') as output:
        process = subprocess.run(command, cwd=cwd, input=stdin, text=True,
                                 stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    text = log.read_text(encoding='utf-8', errors='replace')
    if process.returncode:
        raise RuntimeError(f'{command[0]} exited {process.returncode}; see {log}')
    return text


def instructor_functions():
    source = Path('/tutorial/verify.py').read_text(encoding='utf-8')
    # Reuse complete function bodies without importing GDSfactory into the tool image.
    nodes = [n for n in ast.parse(source).body if
             isinstance(n, ast.FunctionDef) and n.name in ('magic_script', 'collapse_rc') or
             isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MAGIC_ERRORS' for t in n.targets)]
    namespace = {'TECH': '/pdk/libs.tech/magic/sky130A.tech', 're': re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '/tutorial/verify.py', 'exec'), namespace)
    return namespace


def extract(namespace, name, gds, schematic, ports):
    work = Path('/build/extracted') / name
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gds, work / (name + '.gds'))
    (work / (name + '.spice')).write_text(schematic, encoding='utf-8')
    script = namespace['magic_script'](name, ports)
    (work / 'extract.tcl').write_text(script, encoding='utf-8')
    text = run(['magic', '-dnull', '-noconsole', '-rcfile', '/dev/null'], work,
               OUT / (name + '_magic.log'), script)
    count = re.search(r'DRC_COUNT (\d+)', text)
    if not count or 'MAGIC_DONE' not in text or re.search(namespace['MAGIC_ERRORS'], text):
        raise RuntimeError(f'Magic extraction failed: {name}')
    r, c = namespace['collapse_rc'](work, name)
    return work, {'isolated_core_drc_count': int(count[1]), 'resistors': r, 'capacitors': c}


def lvs(work, source, source_cell, target, target_cell, label):
    comparison = OUT / (label + '_compare.log')
    # Invalidate stale comparisons before launching Netgen.
    comparison.write_text('RUNNING\n', encoding='utf-8')
    run(['netgen-lvs', '-batch', 'lvs', f'{source} {source_cell}', f'{target} {target_cell}',
         '/pdk/libs.tech/netgen/sky130A_setup.tcl', str(comparison)], work, OUT / (label + '_netgen.log'))
    text = comparison.read_text().lower()
    return 'circuits match uniquely.' in text and 'do not match' not in text and 'property errors' not in text


def main():
    if sys.argv[1] == 'reference':
        target = Path('/build/reference')
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile('/pdk/libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds', target / 'hd.gds')
        return 0
    path = OUT / 'verification.json'
    report = {'passed': False, 'status': 'running', 'cells': {}}
    save(path, report)
    try:
        namespace = instructor_functions()
        report['instructor_verify_sha256'] = hashlib.sha256(Path('/tutorial/verify.py').read_bytes()).hexdigest()
        library = Path('/pdk/libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice').read_text()
        selected = json.loads(Path('/build/selected_cells.json').read_text())
        for name in selected:
            folder = Path('/repo/generated') / name
            spec = json.loads((folder / 'input.json').read_text())
            work, data = extract(namespace, name, folder / 'cell.gds', (folder / 'schematic.spice').read_text(),
                                 ['VPWR', 'VGND'] + spec['inputs'] + [spec['output'], 'VPB', 'VNB'])
            data['schematic_lvs'] = lvs(work, work / (name + '_lvs.spice'), name,
                                        work / (name + '.spice'), name, name + '_schematic')
            data['rc_lvs'] = lvs(work, work / (name + '_rc_collapsed.spice'), name,
                                 work / (name + '.spice'), name, name + '_rc')
            suffix = 'inv_1' if len(spec['inputs']) == 1 else 'nand2_1'
            refname = 'sky130_fd_sc_hd__' + suffix
            match = re.search(r'(?ims)^\.subckt\s+' + re.escape(refname) + r'\s+.*?^\.ends\b[^\n]*', library)
            if not match:
                raise RuntimeError(f'Installed HD subcircuit not found: {refname}')
            reftext = match[0] + '\n'
            refports = reftext.splitlines()[0].split()[2:]
            refwork, refdata = extract(namespace, refname, Path('/build/reference/cells') / (refname + '.gds'), reftext, refports)
            data['installed_reference_lvs'] = lvs(work, work / (name + '_lvs.spice'), name,
                                                  refwork / (refname + '.spice'), refname, name + '_reference')
            save(OUT / (name + '_parasitics.json'), {'generated': summarize(work / (name + '_pex.spice')),
                'installed': summarize(refwork / (refname + '_pex.spice')), 'reference_extraction': refdata})
            pex = work / (name + '_pex.spice')
            ports = re.search(r'(?im)^\.subckt\s+\S+\s+(.+)', pex.read_text())[1].split()
            destination = Path('/cells') / name
            destination.mkdir(exist_ok=True)
            shutil.copyfile(pex, destination / 'extracted.spice')
            transistor = OUT / 'transistor' / (name + '.json')
            transistor.parent.mkdir(exist_ok=True)
            command = [sys.executable, '/repo/src/transistor_level_check.py', '--pex', str(pex),
                '--models', '/pdk/libs.tech/ngspice/sky130.lib.spice', '--subckt', name,
                '--inputs', ','.join(spec['inputs']), '--output', spec['output'], '--pin-order', ','.join(ports),
                '--hd-liberty', '/given/hd_reduced.lib', '--hd-cell', refname, '--out', str(transistor)]
            run(command, work, OUT / (name + '_transistor.log'), timeout=3600)
            data['transistor'] = json.loads(transistor.read_text())['passed']
            data['passed'] = all(data[k] for k in ('schematic_lvs', 'rc_lvs', 'installed_reference_lvs', 'transistor'))
            report['cells'][name] = data
            save(path, report)
        drc = json.loads((OUT / 'drc/verification.json').read_text())
        comparison = json.loads((OUT / 'hd_comparison/comparison.json').read_text())
        frames = [c['passed'] for cell in comparison['cells'].values() for group in cell['frame_checks'].values() for c in group.values()]
        report['passed'] = bool(selected) and all(c['passed'] for c in report['cells'].values()) and drc['passed'] and bool(frames) and all(frames)
        report['status'] = 'completed'
    except (OSError, ValueError, RuntimeError, KeyError, AssertionError, subprocess.TimeoutExpired) as exc:
        report.update(passed=False, status='error', error=str(exc))
    save(path, report)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
