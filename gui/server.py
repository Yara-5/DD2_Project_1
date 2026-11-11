"""Cellforge: a walkthrough of the design of one cell, in the browser.

Not a step of the handout's flow and not a check of its own: a viewer. It takes a Boolean
function (an expression over up to four inputs), runs the flow's own steps on it in a job
folder under build/gui/, and serves their outputs to the page in gui/static/:
  request_reader.py, synthesize.py (minimization, stages, sizing), euler.py,
  switch_level_check.py (on the network and on the reordered network), prepare_layout.py,
  generate_gds.py, then the real checks on the drawn cell:
  - DRC: verify_drc.py's own stage_context and layer_check build the 24 tapped-row
    abutment contexts, the broken DRC control and the layer check in a Tutorial 3 build
    folder of this job's own (verify_drc.py itself always uses build/cp1_drc, which a
    running make shares), and magic_drc_runner.py runs Magic on them, unchanged;
  - LVS and RC-LVS: verify_lvs.py, unchanged, on this job's cell folder;
  - transistor level: transistor_level_check.py, unchanged.
Each step's output streams to the page while it runs. Nothing under cells/ or results/
is written. When cells/ already holds a cell make built for the same function and sizing
family (same number of inputs, same truth table with the inputs matched by position,
same sizing), and results/ holds its switch-level, DRC, LVS and transistor-level
reports, the job loads those instead of running anything (stored_cell, load_stored).

Machine paths (the given/ folder, the PDK, Tutorial 3, the container image, the Linux
prefix) come from local.mk, as in the Makefile.
Run from the repository root:  python gui/server.py  then open http://127.0.0.1:8765
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / 'static'
JOBS = {}
LOCK = threading.Lock()
STEPS = ['request', 'network', 'euler', 'switch', 'layout', 'gds', 'drc', 'lvs', 'sim']
FAMILIES = ('hd', 'stack', 'opt')


def make_vars(path):
    """NAME = value lines of local.mk (the Makefile's machine settings)."""
    out = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Z_0-9]+)\s*\??=\s*(.*?)\s*$', line)
            if m and not line.lstrip().startswith('#'):
                out[m[1]] = m[2]
    return out


def settings():
    v = make_vars(ROOT / 'local.mk')
    given = Path(v.get('GIVEN', '../DD2_Project1_student_pack/given'))
    given = given if given.is_absolute() else ROOT / given
    pdk_host = v.get('PDK_HOST', '')
    # The same PDK as Linux sees it: the host path without the \\wsl.localhost\<distro> prefix.
    pdk_linux = re.sub(r'^//wsl\.localhost/[^/]+', '', pdk_host) if pdk_host else ''
    hd = 'libs.ref/sky130_fd_sc_hd'
    return {
        'python': sys.executable,
        'frame': str(given / 'plan_hd.json'),
        'hd_reduced': os.path.relpath(given / 'hd_reduced.lib', ROOT).replace('\\', '/'),
        'liberty': f'{pdk_host}/{hd}/lib/sky130_fd_sc_hd__tt_025C_1v80.lib' if pdk_host else '',
        'hd_gds': f'{pdk_host}/{hd}/gds/sky130_fd_sc_hd.gds' if pdk_host else '',
        'pdk_spice_linux': f'{pdk_linux}/{hd}/spice/sky130_fd_sc_hd.spice',
        'liberty_linux': f'{pdk_linux}/{hd}/lib/sky130_fd_sc_hd__tt_025C_1v80.lib',
        'tut3': v.get('TUT3', '../student'),
        'image': v.get('IMAGE', 'dd2-layout-tools'),
        'linux': shlex.split(v.get('LINUX', '')),
    }


def inputs_of(expression):
    return sorted(set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', expression)))


def rel(path):
    """A path both sides of the Linux prefix agree on: relative to the repository root."""
    return os.path.relpath(Path(path).resolve(), ROOT).replace('\\', '/')


def stream(job, step, command, total=None, count=None):
    """Run a command, keeping its last lines (and a progress count) in the job as it runs."""
    start = time.time()
    with LOCK:
        job['current'] = step
        job['live'][step] = {'lines': [], 'done': 0, 'total': total, 'running': True}
    proc = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding='utf-8', errors='replace')
    all_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        all_lines.append(line)
        with LOCK:
            live = job['live'][step]
            live['lines'] = (live['lines'] + [line])[-14:]
            if count and re.search(count, line):
                live['done'] += 1
    proc.wait()
    with LOCK:
        job['live'][step]['running'] = False
        job['live'][step]['exit'] = proc.returncode
        job['seconds'][step] = round(time.time() - start, 1)
    return proc.returncode, all_lines


def run(job, step, args, total=None, count=None):
    """A flow step in this Python; it must exit 0."""
    code, lines = stream(job, step, [job['python'], '-u', *args], total, count)
    if code != 0:
        raise RuntimeError(f'{step} failed: ' + (lines[-1] if lines else 'no output'))


def trim_plan(doc):
    """What the page draws and explains from layout_plan.json."""
    g, d = doc['geometry'], doc['dimensions']
    return {
        'name': doc['name'],
        'width_um': g['width_um'], 'height_um': g['height_um'], 'width_sites': g['width_sites'],
        'shapes': [{'layer': s['layer'], 'box': s['box'], 'net': s.get('net')} for s in g['shapes']],
        'labels': doc.get('labels', []),
        'vias': len(g.get('vias', [])),
        'router': g.get('router'),
        'negotiation_iterations': g.get('negotiation_iterations'),
        'contacts': [{'net': c['net'], 'type': c['type'], 'licons': c.get('licons', 1)} for c in g.get('contacts', [])],
        'gate_gaps': d.get('gate_gaps', []),
        'minimum_width_sites': d.get('minimum_width_sites'),
        'channels': d.get('channels', []),
        'retries': [{'placement': r.get('placement'), 'channels': len(r.get('channels', [])),
                     'extra_sites': r.get('extra_sites'), 'met1': r.get('met1'), 'router': r.get('router'),
                     'reason': r.get('reason', '')[:160]} for r in d.get('routing_retries', [])],
        'placement_used': doc['input'].get('placement_used'),
        'met1_in_single_stage_cell': g.get('met1_in_single_stage_cell'),
    }


def drc_manifest(job, cell_dir):
    """verify_drc.py's contexts, control and layer check for one cell, in this job's folder."""
    s = job['settings']
    sys.path.insert(0, str(ROOT / 'src'))
    import klayout.db as db
    import verify_drc as vd
    rules = {k: v['value'] for k, v in json.loads((ROOT / 'src' / 'rules.json').read_text())['rules'].items()}
    height = json.loads(Path(s['frame']).read_text())['frame']['site']['height']
    tut = (ROOT / s['tut3']).resolve()
    build = tut / 'build' / f"dd2_gui_{job['id']}_drc"
    build.mkdir(parents=True, exist_ok=True)
    library = db.Layout()
    library.read(s['hd_gds'])
    gds = cell_dir / 'cell.gds'
    layers = {cell_dir.name: vd.layer_check(gds, rules)}
    cases = []
    for neighbor in ('sky130_fd_sc_hd__tapvpwrvgnd_1', 'sky130_fd_sc_hd__inv_1', 'sky130_fd_sc_hd__fill_1'):
        for reverse in (False, True):
            for how in ('R0', 'MY', 'MX_above', 'MX_below'):
                cases.append(vd.stage_context(build, library, gds, neighbor, how, reverse, len(cases), height))
    broken_layers = []
    for pair, label in [((93, 44), 'nsdm'), ((94, 20), 'psdm'), ((78, 44), 'hvtp'), ((95, 20), 'npc')]:
        broken = db.Layout(); broken.read(str(gds)); broken.clear_layer(broken.layer(*pair))
        path = build / f'{cell_dir.name}_missing_{label}.gds'; broken.write(str(path))
        broken_layers.append({'case': path.name, 'removed': label, 'rejected': not vd.layer_check(path, rules)['passed']})
    first = build / (cases[0]['name'] + '.gds')
    broken = db.Layout(); broken.read(str(first)); cell = broken.top_cell()
    cell.name = 'cp1_broken_met1_width'
    cell.shapes(broken.layer(68, 20)).insert(db.DBox(.2, 1.10, .25, 1.60))
    path = build / (cell.name + '.gds'); vd.save_top(broken, cell, path)
    cases.append({'name': cell.name, 'kind': 'broken_drc', 'expected': 'nonzero', 'gds_sha256': vd.sha256(path)})
    manifest = {'image': s['image'], 'cases': cases, 'layers': layers, 'broken_layer_cases': broken_layers,
                'reference_gds_sha256': vd.sha256(s['hd_gds']),
                'source_verify_sha256': vd.sha256(tut / 'verify.py'), 'source_abutment_sha256': vd.sha256(tut / 'abutment.py')}
    (build / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return tut, build / 'manifest.json', len(cases)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def stored_cell(request):
    """The cell under cells/ that make built for the same function and sizing family, if
    every view and check report of it is there: same number of inputs, same truth table
    (inputs matched by position), same sizing. Returns (folder, short name) or None."""
    want = (len(request['inputs']), request['truth_table_value'], request['sizing'])
    for folder in sorted((ROOT / 'cells').glob('dd2_g5__*')):
        try:
            r = read(folder / 'request.json')['request']
        except (OSError, ValueError, KeyError):
            continue
        if (len(r['inputs']), r['truth_table_value'], r['sizing']) != want:
            continue
        short = folder.name.removeprefix('dd2_g5__')
        needed = [folder / 'network.json', folder / 'layout_spec.json', folder / 'layout_plan.json',
                  ROOT / 'results' / 'switch_level' / f'{short}.json', ROOT / 'results' / 'switch_level' / f'{short}_network.json',
                  ROOT / 'results' / 'drc' / 'verification.json', ROOT / 'results' / 'lvs' / 'verification.json',
                  ROOT / 'results' / 'transistor_level' / f'{short}.json']
        if all(p.exists() for p in needed):
            return folder, short
    return None


def load_stored(job, folder, short):
    """Fill the job from cells/<name>/ and results/ (the reports make wrote), with the
    report lines the page replays on its checks cards."""
    name = folder.name
    res = ROOT / 'results'
    drc = read(res / 'drc' / 'verification.json')
    cases = [c for c in drc['cases'] if c.get('generated') == name]
    control = [c for c in drc['cases'] if c.get('kind') == 'broken_drc']
    broken = [b for b in drc['broken_layer_cases'] if b['case'].startswith(name + '_missing_')]
    layers = {name: drc['layers'][name]} if name in drc.get('layers', {}) else {}
    drc_part = {'cases': cases + control, 'layers': layers, 'broken_layer_cases': broken,
                'passed': bool(cases) and all(c['passed'] for c in cases + control)
                and all(x['passed'] for x in layers.values()) and all(b['rejected'] for b in broken)}
    lvs_all = read(res / 'lvs' / 'verification.json')
    lvs = dict(lvs_all.get('cells', {}).get(name, {'passed': False, 'error': f'{name} not in results/lvs'}))
    sim = read(res / 'transistor_level' / f'{short}.json')
    data = {
        'request': read(folder / 'request.json'),
        'network': read(folder / 'network.json'),
        'euler': read(folder / 'layout_spec.json'),
        'switch': {'network': read(res / 'switch_level' / f'{short}_network.json'),
                   'reordered': read(res / 'switch_level' / f'{short}.json')},
        'layout': trim_plan(read(folder / 'layout_plan.json')),
        'drc': drc_part, 'lvs': lvs, 'sim': sim,
    }
    replay = {
        'switch': [f"PASS: {name} switch level, {len(data['switch']['network'].get('rows', []))} rows" if data['switch']['network'].get('passed') else f'FAIL: {name} switch level'],
        'drc': [f"{c['name']}: DRC={c['drc_count']} {'PASS' if c['passed'] else 'FAIL'}" for c in cases + control] + ['PASS' if drc_part['passed'] else 'FAIL'],
        'lvs': [f"{name}: LVS={'match' if lvs.get('lvs_passed') else 'mismatch'} RC-LVS={'match' if (lvs.get('rc') or {}).get('collapsed_lvs_passed') else 'mismatch'} isolated DRC={lvs.get('isolated_drc_count')}",
                'PASS' if lvs.get('passed') else 'FAIL'],
        'sim': [f"{'PASS' if sim.get('passed') else 'FAIL'}: {name}: DC {len(sim.get('dc', []))} rows, transient {len(sim.get('transient', []))} edges"],
    }
    stamp = time.strftime('%d %b %H:%M', time.localtime((folder / 'layout_plan.json').stat().st_mtime))
    with LOCK:
        job['data'] = data
        job['stored'] = {'cell': name, 'built': stamp, 'replay': replay,
                         'typed_inputs': job['request']['inputs'], 'cell_inputs': data['request']['request']['inputs']}
        job['state'] = 'done'


def pipeline(job):
    d, s = job['dir'], job['settings']
    req = job['request']
    (d / 'request_raw.json').write_text(json.dumps(req, indent=2), encoding='utf-8')
    try:
        run(job, 'request', ['src/request_reader.py', str(d / 'request_raw.json'), '--out', str(d / 'request.json')])
        job['data']['request'] = json.loads((d / 'request.json').read_text(encoding='utf-8'))
        # A cell make already built and checked: show its views and reports.
        hit = stored_cell(job['data']['request']['request'])
        if hit:
            load_stored(job, *hit)
            return
        args = ['src/synthesize.py', '--request', str(d / 'request.json'), '--frame', s['frame'],
                '--outdir', str(d), '--no-picture']
        if s['liberty']:
            args += ['--hd-liberty', s['liberty']]
        run(job, 'network', args)
        job['data']['network'] = json.loads((d / 'network.json').read_text(encoding='utf-8'))
        run(job, 'euler', ['src/euler.py', str(d / 'network.json'), '--out', str(d / 'layout_spec.json')])
        job['data']['euler'] = json.loads((d / 'layout_spec.json').read_text(encoding='utf-8'))
        results = {}
        for name, source in (('network', d / 'network.json'), ('reordered', d / 'layout_spec.json')):
            out = d / f'switch_{name}.json'
            stream(job, 'switch', [job['python'], 'src/switch_level_check.py', str(source), '--request',
                                   str(d / 'request.json'), '--out', str(out)])
            results[name] = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'passed': False}
        job['data']['switch'] = results
        run(job, 'layout', ['src/prepare_layout.py', str(d / 'layout_spec.json'), '--request', str(d / 'request.json'),
                            '--frame', s['frame'], '--outdir', str(d / 'out')])
        plan = next((d / 'out').glob('*/layout_plan.json'))
        cell_dir = plan.parent
        job['data']['layout'] = trim_plan(json.loads(plan.read_text(encoding='utf-8')))
        name = cell_dir.name

        # The real checks on the drawn cell.
        run(job, 'gds', ['src/generate_gds.py', str(plan), '--tutorial-root', s['tut3'], '--outdir', str(d / 'out')])
        with LOCK:
            job['current'] = 'drc'
            job['live']['drc'] = {'lines': ['staging 24 abutment contexts and the layer check (KLayout)'],
                                  'done': 0, 'total': 25, 'running': True}
        tut, manifest, total = drc_manifest(job, cell_dir)
        (d / 'drc').mkdir(exist_ok=True)
        stream(job, 'drc', s['linux'] + ['python3' if s['linux'] else job['python'], rel(ROOT / 'src' / 'magic_drc_runner.py'),
                                         '--tutorial-root', rel(tut), '--manifest', rel(manifest),
                                         '--outdir', rel(d / 'drc'), '--jobs', '4'],
               total=total, count=r'^cp1_\w+: DRC=')
        report = d / 'drc' / 'verification.json'
        job['data']['drc'] = json.loads(report.read_text()) if report.exists() else {'passed': False, 'error': 'no report'}

        env = ['env', f"LAYOUT_IMAGE={s['image']}", 'NETGEN_ENTRYPOINT=netgen']
        stream(job, 'lvs', s['linux'] + env + ['python3', 'src/verify_lvs.py', '--tutorial-root', s['tut3'],
                                               '--pdk-spice', s['pdk_spice_linux'], '--hd-liberty', s['liberty_linux'],
                                               '--image', s['image'], '--cells', rel(d / 'out'),
                                               '--outdir', rel(d / 'lvs'), '--jobs', '1'])
        report = d / 'lvs' / 'verification.json'
        lvs = json.loads(report.read_text()) if report.exists() else {}
        job['data']['lvs'] = dict((lvs.get('cells') or {}).get(name, {'passed': False, 'error': 'no report'}),
                                  run_passed=lvs.get('passed', False))

        pex = cell_dir / f'{name}_pex.spice'
        stream(job, 'sim', s['linux'] + ['env', f"LAYOUT_IMAGE={s['image']}", 'python3', 'src/transistor_level_check.py',
                                         '--pex', rel(pex), '--request', rel(d / 'request.json'),
                                         '--models', f"{s['tut3']}/pdk/libs.tech/ngspice/sky130.lib.spice",
                                         '--liberty', s['hd_reduced'], '--ngspice', f"{s['tut3']}/tools.sh ngspice",
                                         '--jobs', '2', '--workdir', f"{s['tut3']}/build/dd2_gui_{job['id']}_sim",
                                         '--out', rel(d / 'sim.json')])
        report = d / 'sim.json'
        job['data']['sim'] = json.loads(report.read_text()) if report.exists() else {'passed': False, 'error': 'no report'}
        with LOCK:
            job['state'] = 'done'
    except Exception as exc:          # the page shows the step's message
        with LOCK:
            job['state'] = 'failed'
            job['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        with LOCK:
            job['current'] = None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(STATIC), **k)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def reply(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/api/design':
            return self.reply(404, {'error': 'not found'})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            expression = str(body.get('expression', '')).strip()
            sizing = body.get('sizing', 'hd')
            if not expression:
                return self.reply(400, {'error': 'Enter a Boolean function first.'})
            if sizing not in FAMILIES:
                return self.reply(400, {'error': f'sizing must be one of {", ".join(FAMILIES)}'})
            inputs = inputs_of(expression)
            if not 1 <= len(inputs) <= 4:
                return self.reply(400, {'error': f'Use one to four inputs; found {len(inputs)}.'})
            output = 'Y' if 'Y' not in inputs else 'Z'
            job_id = uuid.uuid4().hex[:10]
            folder = ROOT / 'build' / 'gui' / job_id
            folder.mkdir(parents=True, exist_ok=True)
            job = {'id': job_id, 'dir': folder, 'python': sys.executable, 'settings': settings(),
                   'request': {'request_id': f'GUI-{job_id}', 'name': f'dd2_g5__gui_{job_id}_1', 'inputs': inputs,
                               'output': output, 'expression': expression, 'sizing': sizing},
                   'state': 'running', 'current': None, 'data': {}, 'live': {}, 'seconds': {}, 'started': time.time()}
            with LOCK:
                JOBS[job_id] = job
            threading.Thread(target=pipeline, args=(job,), daemon=True).start()
            self.reply(200, {'id': job_id})
        except (ValueError, OSError) as exc:
            self.reply(400, {'error': str(exc)})

    def do_GET(self):
        m = re.fullmatch(r'/api/job/([0-9a-f]{10})', self.path)
        if not m:
            return super().do_GET()
        with LOCK:
            job = JOBS.get(m[1])
            if not job:
                return self.reply(404, {'error': 'unknown job'})
            payload = {k: job[k] for k in ('id', 'state', 'current', 'data', 'seconds', 'request', 'live')}
            payload['stored'] = job.get('stored')
            payload['error'] = job.get('error')
            payload['elapsed'] = round(time.time() - job['started'], 1)
            payload['steps'] = STEPS
            payload = json.loads(json.dumps(payload))
        self.reply(200, payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    s = settings()
    if not Path(s['frame']).exists():
        print(f"plan_hd.json not found at {s['frame']}: set GIVEN in local.mk")
        return 1
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Cellforge on http://{args.host}:{args.port}  (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
