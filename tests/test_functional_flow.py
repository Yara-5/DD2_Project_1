"""Regression tests for the waveform-export repair and existing functional checks.

Synthetic waveforms exercise checker behavior, not analog circuit correctness.
Existing fixtures in tests/broken are read-only throughout this suite.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
import transistor_level_check as transistor
from hd_conditions import read_conditions
from request_reader import RequestError, validate_request
import switch_level_check as network
import project

scratch = ROOT / 'build/tmp'
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)


class TransistorRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='regression space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'models.spice'
        self.models.write_text('* test fixture only\n', encoding='utf-8')
        self.pex = self.root / 'cell.spice'
        self.pex.write_text('.subckt inv A Y VPWR VGND\nR0 Y Y0 100\nC0 Y0 VGND 1f\n.ends inv\n', encoding='utf-8')
        self.args = argparse.Namespace(pex=self.pex, models=self.models, corner='tt',
            subckt='inv', output='Y', pin_order=['A', 'Y', 'VPWR', 'VGND'],
            slew_ps=100, load_ff=10, timeout=10, ngspice='ngspice')

    def test_port_order_is_checked_against_extraction(self):
        transistor.validate_args(self.args, ['A'])
        self.args.pin_order = ['Y', 'A', 'VPWR', 'VGND']
        with self.assertRaisesRegex(ValueError, 'pin-order'):
            transistor.validate_args(self.args, ['A'])

    def test_schematic_cannot_masquerade_as_rc_extraction(self):
        self.pex.write_text('.subckt inv A Y VPWR VGND\n.ends inv\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'resistance and capacitance'):
            transistor.validate_args(self.args, ['A'])

    def test_nonfinite_or_too_slow_ramp_is_rejected(self):
        for value in (float('nan'), float('inf'), -1, 0, 2000):
            with self.subTest(value=value):
                self.args.slew_ps = value
                with self.assertRaises(ValueError):
                    transistor.validate_args(self.args, ['A'])

    def test_rail_check_rejects_nonfinite_and_out_of_range_values(self):
        for voltage, bit in ((float('nan'), 0), (float('inf'), 1), (-50, 0), (50, 1)):
            self.assertFalse(transistor.is_rail(voltage, bit))
        self.assertTrue(transistor.is_rail(0.01, 0))
        self.assertTrue(transistor.is_rail(1.79, 1))

    def test_waveform_rejects_nonfinite_samples(self):
        path = self.root / 'wave.dat'
        path.write_text('time v(y)\n0 1.8\n1e-9 nan\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'Nonfinite'):
            transistor.read_waveform(path)

    def run_wave(self, samples, inputs=('A',), start=(0,)):
        def simulator(args, deck, workdir):
            self.assertIn('wrdata waveform.dat v(Y)', deck)
            self.assertNotIn('wrdata "', deck)
            (workdir / 'waveform.dat').write_text('\n'.join(f'{t * 1e-9:.12g} {v}' for t, v in samples), encoding='utf-8')
            return 'No. of Data Rows'
        with patch.object(transistor, 'run_ngspice', side_effect=simulator):
            return transistor.transient_check(self.args, list(inputs), start, 0, self.root)

    def test_export_works_in_directory_with_spaces(self):
        result = self.run_wave([(0, 1.8), (1, 1.8), (1.5, 1.8), (2.1, 0), (3, 0), (5, 0)])
        self.assertTrue(result['passed'])

    def test_truncated_waveform_cannot_pass(self):
        result = self.run_wave([(0, 1.8), (1, 1.8), (1.5, 1.8), (3, 0)])
        self.assertFalse(result['passed'])
        self.assertFalse(result['waveform_complete'])

    def test_unchanged_nand_output_cannot_cross_half_supply(self):
        self.args.pin_order = ['A', 'B', 'Y', 'VPWR', 'VGND']
        result = self.run_wave([(0, 1.8), (1, 1.8), (1.5, 1.8), (2.1, .8), (3, 1.8), (5, 1.8)], ('A', 'B'), (0, 0))
        self.assertFalse(result['passed'])
        self.assertFalse(result['no_half_supply_crossing_when_output_unchanged'])

    def test_zero_exit_with_simulator_error_is_rejected(self):
        def run(*args, **kwargs):
            kwargs['stdout'].write('Error: transient simulation interrupted\n')
            return subprocess.CompletedProcess(args[0], 0)
        with patch.object(transistor.subprocess, 'run', side_effect=run):
            with self.assertRaises(RuntimeError):
                transistor.run_ngspice(self.args, '* synthetic deck', self.root)

    def test_missing_input_clears_stale_passing_report(self):
        output = self.root / 'report.json'
        output.write_text('{"passed": true}', encoding='utf-8')
        code = transistor.main(['--pex', str(self.root / 'missing.spice'), '--models', str(self.models),
            '--subckt', 'inv', '--inputs', 'A', '--pin-order', 'A,Y,VPWR,VGND',
            '--slew-ps', '100', '--load-ff', '10', '--out', str(output)])
        report = json.loads(output.read_text())
        self.assertEqual(code, 1)
        self.assertFalse(report['passed'])
        self.assertEqual(report['status'], 'error')


class InterfaceRegression(unittest.TestCase):
    def test_first_input_is_lsb(self):
        request = {'request_id': 'TEST', 'name': 'cell', 'inputs': ['A', 'B'],
                   'output': 'Y', 'sizing': 'hd', 'expression': 'A'}
        self.assertEqual(validate_request(request)['truth_table_value'], 0xA)

    def test_existing_bad_request_is_rejected(self):
        request = json.loads((ROOT / 'tests/broken/bad_request.json').read_text())
        with self.assertRaises(RequestError):
            validate_request(request)

    def test_supplied_networks_are_valid(self):
        for path in sorted((ROOT / 'inputs').glob('*.json')):
            spec, kind = network.load_spec(path)
            self.assertTrue(network.check(spec, kind)['passed'])

    def test_windows_path_with_spaces_keeps_a_single_mount_argument(self):
        self.assertEqual(project.linux_path(r'C:\course files\pdk'), '/mnt/c/course files/pdk')
        config = {'pdk': '/pdk path', 'given': r'C:\given files', 'tutorial_root': r'C:\tutorial files',
                  'image': 'course', 'distro': 'Ubuntu'}
        command = project.container(config, 'ngspice', ['-v'])
        self.assertIn('type=bind,src=/pdk path,dst=/pdk,readonly', command)


class LibertyRegression(unittest.TestCase):
    def test_full_ramp_is_converted_from_hd_threshold_slew(self):
        source = '''library (test) {
        time_unit : "1ns"; capacitive_load_unit(1, "pf");
        nom_voltage : 1.8; nom_temperature : 25;
        slew_lower_threshold_pct_rise : 20; slew_upper_threshold_pct_rise : 80;
        slew_lower_threshold_pct_fall : 20; slew_upper_threshold_pct_fall : 80;
        cell (test_inv) { pin (Y) { timing () { cell_rise (table) {
        index_1("0.0531329, 0.122474, 0.282311");
        index_2("0.00356533, 0.00952062, 0.0254232");
        } } } } }'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.lib'
            path.write_text(source, encoding='utf-8')
            conditions = read_conditions(path, 'test_inv')
        self.assertAlmostEqual(conditions['threshold_slew_ps'], 122.474)
        self.assertAlmostEqual(conditions['full_ramp_ps'], 122.474 / .6)
        self.assertAlmostEqual(conditions['load_ff'], 9.52062)


if __name__ == '__main__':
    unittest.main()
