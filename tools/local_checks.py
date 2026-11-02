"""Run the existing local checks; these results do not certify analog simulation."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from request_reader import RequestError, validate_request
import switch_level_check as network
import model_level_check as model
from verify_drc import layer_check


def main():
    output = ROOT / 'results/tests/functional.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Request, switch, Verilog, Liberty and drawn-layer checks; no analog/physical-tool certification',
              'requests': [], 'cells': {}, 'sources': {}}
    def save():
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    save()
    scratch = ROOT / 'build/tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch.resolve())
    try:
        for filename in ('src/request_reader.py', 'src/switch_level_check.py', 'src/model_level_check.py', 'verify_drc.py'):
            report['sources'][filename] = hashlib.sha256((ROOT / filename).read_bytes()).hexdigest()
        for path in (ROOT / 'tests/inv_expression.json', ROOT / 'tests/nand2_table.json', ROOT / 'tests/broken/bad_request.json'):
            expected_bad = path.parent.name == 'broken'
            try:
                result = validate_request(json.loads(path.read_text(encoding='utf-8')))
                item = {'path': str(path.relative_to(ROOT)), 'passed': not expected_bad, 'normalized': result}
            except RequestError as exc:
                item = {'path': str(path.relative_to(ROOT)), 'passed': expected_bad, 'rejection': str(exc)}
            report['requests'].append(item)
        for path in sorted((ROOT / 'inputs').glob('*.json')):
            spec, kind = network.load_spec(path)
            switch = network.check(spec, kind)
            controls = {}
            for name in ('deleted_pullup', 'deleted_pulldown', 'supply_short'):
                damaged = copy.deepcopy(spec)
                if name == 'supply_short':
                    damaged['devices'].append({'id': 'FAULT', 'type': 'nmos', 'gate': spec['inputs'][0], 'source': 'VPWR', 'drain': 'VGND'})
                else:
                    polarity = 'pmos' if name == 'deleted_pullup' else 'nmos'
                    index = next(i for i, d in enumerate(damaged['devices']) if d['type'] == polarity)
                    del damaged['devices'][index]
                result = network.check(damaged, kind)
                controls[name] = {'rejected': not result['passed'], 'result': result}
            cell = ROOT / 'cells' / spec['name']
            model.generate_views(spec, kind, cell)
            verilog = model.check_verilog(spec, kind, cell / 'cell.v')
            liberty = model.check_liberty(spec, kind, cell / 'cell.lib')
            broken_path = ROOT / f'results/model_level/{kind}/cell_broken.v'
            broken = model.check_verilog(spec, kind, broken_path)
            # Missing tools/compile failures are not proof of a bad truth-table row.
            rejected = bool(broken.get('rows')) and any(not r['passed'] for r in broken['rows'])
            layers = layer_check(ROOT / 'generated' / spec['name'] / 'cell.gds')
            passed = switch['passed'] and all(c['rejected'] for c in controls.values()) and verilog['passed'] and liberty['passed'] and rejected and layers['passed']
            report['cells'][spec['name']] = {'passed': passed, 'switch': switch,
                'in_memory_network_controls': controls, 'verilog': verilog, 'liberty': liberty,
                'existing_broken_verilog': {'path': str(broken_path.relative_to(ROOT)), 'rejected_by_truth_table': rejected, 'result': broken},
                'drawn_layers': layers, 'liberty_status': 'functional_only_uncharacterized'}
            save()
        report['passed'] = bool(report['cells']) and all(c['passed'] for c in report['cells'].values()) and all(r['passed'] for r in report['requests'])
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        report['error'] = str(exc)
    save()
    print(json.dumps({'passed': report['passed'], 'report': str(output)}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
