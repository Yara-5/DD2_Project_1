"""Portable commands for the Option 1 checkpoint-one flow."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.dd2.local.json'


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def load_config():
    data = json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.is_file() else {}
    for key in ('given', 'tutorial_root', 'pdk', 'layout_python', 'image', 'distro'):
        data[key] = os.getenv('DD2_' + key.upper(), data.get(key, ''))
    data['layout_python'] = data['layout_python'] or sys.executable
    data['image'] = data['image'] or 'dd2-project1-tools'
    data['distro'] = data['distro'] or 'Ubuntu'
    return data


def execute(command, log, stdin=None, timeout=600, required=True):
    started = time.monotonic()
    temporary = ROOT / 'build/tmp'
    cache = ROOT / 'build/cache/matplotlib'
    temporary.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run([str(x) for x in command], cwd=ROOT, input=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace', timeout=timeout,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                     'MPLCONFIGDIR': str(cache), 'TEMP': str(temporary), 'TMP': str(temporary)})
        code, output = result.returncode, result.stdout.replace('\x00', '')
    except (OSError, subprocess.TimeoutExpired) as exc:
        code, output = None, str(exc)
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output, encoding='utf-8')
    record = {'command': [str(x) for x in command], 'exit_code': code, 'passed': code == 0,
              'seconds': round(time.monotonic() - started, 3), 'log': str(log.relative_to(ROOT))}
    if required and code != 0:
        raise RuntimeError(f"Command failed; see {record['log']}.\n{output[-1200:]}")
    return record


def linux_path(path):
    value = str(path).replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', value):
        return '/mnt/' + value[0].lower() + value[2:]
    return value


def container(config, tool, args=()):
    command = ['wsl', '-d', config['distro'], '--'] if os.name == 'nt' else []
    command += ['docker', 'run', '--rm', '-i', '--network', 'none', '-e', 'OMP_NUM_THREADS=1',
                '-e', 'PYTHONDONTWRITEBYTECODE=1']
    mounts = [(ROOT, '/repo', True), (config['pdk'], '/pdk', True),
              (config['given'], '/given', True), (config['tutorial_root'], '/tutorial', True)]
    for name in ('build', 'results', 'cells'):
        (ROOT / name).mkdir(exist_ok=True)
        mounts.append((ROOT / name, '/' + name, False))
    for source, target, readonly in mounts:
        if not source:
            raise ValueError('Run setup: pdk, given and tutorial-root are required')
        command += ['--mount', f'type=bind,src={linux_path(source)},dst={target}' + (',readonly' if readonly else '')]
    return command + ['--entrypoint', tool, config['image'], *args]


def setup(args):
    config = load_config()
    for key in ('given', 'tutorial_root', 'pdk', 'layout_python', 'image', 'distro'):
        if getattr(args, key):
            config[key] = getattr(args, key)
    for key, files in (('given', ('plan_hd.json', 'hd_reduced.lib')),
                       ('tutorial_root', ('cells.py', 'plan.json', 'verify.py', 'requirements.txt'))):
        for file in files:
            if not config[key] or not (Path(config[key]) / file).is_file():
                raise ValueError(f'Supply --{key.replace("_", "-")} containing {file}')
        config[key] = str(Path(config[key]).resolve())
    if not config['pdk']:
        raise ValueError('Supply --pdk: the real SKY130A directory, using a Linux path with WSL')
    write_json(CONFIG, config)
    print('Saved local paths in .dd2.local.json. Run make check next.')


def check(config):
    report = {'passed': False, 'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': []}
    path = ROOT / 'results/environment/check.json'
    write_json(path, report)
    try:
        pins = "import importlib.metadata as m,json; print(json.dumps({p:m.version(p) for p in ['gdsfactory','klayout','matplotlib']}))"
        record = execute([config['layout_python'], '-B', '-c', pins], ROOT / 'results/environment/python_packages.log', required=False)
        if record['passed']:
            versions = json.loads((ROOT / record['log']).read_text())
            expected = dict(line.strip().split('==') for line in (ROOT / 'requirements.txt').read_text().splitlines() if line.strip())
            record.update(versions=versions, passed=versions == expected)
        report['checks'].append(record)
        for tool in ('iverilog', 'vvp'):
            record = execute([tool, '-V'], ROOT / f'results/environment/{tool}.log', required=False)
            if tool == 'iverilog' and record['passed']:
                match = re.search(r'Icarus Verilog version (\d+)', (ROOT / record['log']).read_text())
                record['passed'] = bool(match) and int(match[1]) >= 13
                record['requirement'] = 'Icarus Verilog 13 or newer (overview Table 1)'
            report['checks'].append(record)
        script = 'set -eu\nmagic --version\ncommand -v netgen-lvs\nngspice -v\ntest -f /pdk/libs.tech/magic/sky130A.tech\ntest -f /pdk/libs.tech/ngspice/sky130.lib.spice\n/opt/dd2/stress_test/check_stress.sh\n'
        report['checks'].append(execute(container(config, 'sh', ['-s']), ROOT / 'results/environment/course_tools.log', stdin=script, required=False))
        yosys = ['wsl', '-d', config['distro'], '--', 'yosys', '-V'] if os.name == 'nt' else ['yosys', '-V']
        record = execute(yosys, ROOT / 'results/environment/yosys.log', required=False)
        if record['passed']:
            match = re.search(r'Yosys (\d+)\.(\d+)', (ROOT / record['log']).read_text())
            record['passed'] = bool(match) and tuple(map(int, match.groups())) >= (0, 34)
        report['checks'].append(record)
        report['passed'] = all(item['passed'] for item in report['checks'])
        if not report['passed']:
            report['error'] = 'One or more required tools/versions failed; inspect the failing checks and their logs.'
    except (ValueError, OSError, RuntimeError) as exc:
        report['error'] = str(exc)
    write_json(path, report)
    if not report['passed']:
        raise RuntimeError('Environment check failed: see results/environment/check.json and its logs.')


def local_tests(config):
    execute([config['layout_python'], '-B', str(ROOT / 'tools/local_checks.py')], ROOT / 'results/tests/functional.log')
    execute([config['layout_python'], '-B', str(ROOT / 'tools/run_tests.py')], ROOT / 'results/tests/unittest.log')


def generate(config, request=None):
    specifications = sorted((ROOT / 'inputs').glob('*.json'))
    normalized = None
    if request:
        sys.path.insert(0, str(ROOT / 'src'))
        from request_reader import validate_request
        normalized = {'status': 'pass', 'request': validate_request(json.loads(Path(request).read_text(encoding='utf-8')))}
        req = normalized['request']
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', req['name']):
            raise ValueError('Cell name must be a simple identifier')
        if not req['supported_by_prototype']:
            raise ValueError('Checkpoint-one demo supports only inv and nand2')
        specifications = [ROOT / 'inputs' / ('inverter.json' if req['recognized'] == 'inv' else 'nand2.json')]
    selected = []
    for specification in specifications:
        spec = json.loads(specification.read_text(encoding='utf-8'))
        name = normalized['request']['name'] if normalized else spec['name']
        request_args = []
        if normalized:
            path = ROOT / 'build/normalized_request.json'
            write_json(path, normalized)
            request_args = ['--request', str(path)]
        execute([config['layout_python'], '-B', str(ROOT / 'src/prepare_layout.py'), str(specification),
                 '--frame', str(Path(config['given']) / 'plan_hd.json'), '--rules', str(ROOT / 'config/layout_rules.json'),
                 '--outdir', str(ROOT / 'prepared'), *request_args], ROOT / f'results/generation/{name}_prepare.log')
        execute([config['layout_python'], '-B', str(ROOT / 'src/generate_gds.py'), str(ROOT / f'prepared/{name}/layout_plan.json'),
                 '--tutorial-root', config['tutorial_root'], '--outdir', str(ROOT / 'generated')], ROOT / f'results/generation/{name}_draw.log')
        selected.append(name)
    write_json(ROOT / 'build/selected_cells.json', selected)


def physical(config):
    execute(container(config, 'python3', ['/repo/tools/course_flow.py', 'reference']), ROOT / 'results/checkpoint1/reference.log')
    execute([config['layout_python'], '-B', str(ROOT / 'tools/layout_contexts.py'), '--frame', str(Path(config['given']) / 'plan_hd.json')], ROOT / 'results/checkpoint1/contexts.log')
    execute(container(config, 'python3', ['/repo/tools/course_flow.py', 'verify']), ROOT / 'results/checkpoint1/physical.log', timeout=7200)


def status():
    report = {'passed': False, 'status': 'incomplete', 'requirements': {}}
    for label, relative in (('environment', 'results/environment/check.json'),
                            ('local_functional', 'results/tests/functional.json'),
                            ('regression_tests', 'results/tests/unit.json'),
                            ('physical_and_transistor', 'results/checkpoint1/verification.json'),
                            ('calibrated_timing_comparison', 'results/checkpoint1/timing_comparison.json'),
                            ('retained_negative_fixtures', 'results/checkpoint1/negative_fixtures.json')):
        path = ROOT / relative
        data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        report['requirements'][label] = {'passed': data.get('passed') is True, 'report': relative, 'error': data.get('error')}
    report['remaining'] = [label for label, item in report['requirements'].items() if not item['passed']]
    report['limitations'] = [
        'Physical adapters require validation on the configured course tools; local waveform unit tests do not simulate transistors.',
        'Calibrated timing comparison is not implemented. Section 5.1 requires validating all six HD reference cells first.',
        'Fixed broken fixtures remain incomplete under tests/broken; existing protected fixtures are unchanged.',
    ]
    report['passed'] = all(item['passed'] for item in report['requirements'].values())
    report['status'] = 'passed' if report['passed'] else 'incomplete'
    report['submission_note'] = 'No commit, push or tag change was performed. These results describe the working tree.'
    write_json(ROOT / 'results/checkpoint1/status.json', report)
    print(json.dumps(report, indent=2))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('setup', 'check', 'test', 'test-local', 'generate', 'physical', 'all', 'demo', 'status', 'clean'))
    for key in ('given', 'tutorial-root', 'pdk', 'layout-python', 'image', 'distro'):
        parser.add_argument('--' + key)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args(argv)
    config = load_config()
    try:
        if args.command == 'setup':
            setup(args)
        elif args.command == 'check':
            check(config)
        elif args.command == 'test-local':
            local_tests(config)
        elif args.command == 'generate':
            generate(config, args.input)
        elif args.command == 'physical':
            check(config)
            physical(config)
        elif args.command in ('all', 'test', 'demo'):
            if args.command == 'demo' and (not args.input or args.input == Path('.')):
                raise ValueError('Usage: make demo IN=tests/inv_expression.json')
            write_json(ROOT / 'results/checkpoint1/verification.json', {'passed': False, 'status': 'not_started'})
            local_tests(config)
            check(config)
            generate(config, args.input if args.command == 'demo' else None)
            physical(config)
            if not status()['passed']:
                raise RuntimeError('Checkpoint one is incomplete; see results/checkpoint1/status.json')
        elif args.command == 'status':
            return 0 if status()['passed'] else 1
        elif args.command == 'clean':
            target = (ROOT / 'build').resolve()
            if target.parent != ROOT or target.name != 'build' or target.is_symlink():
                raise ValueError('Refusing to clean a path outside build/')
            if target.exists():
                shutil.rmtree(target)
            print('Removed build/; retained configuration and results.')
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        if args.command in ('all', 'test', 'demo', 'physical', 'check'):
            status()
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
