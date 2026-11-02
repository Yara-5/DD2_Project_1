"""Check units and net grouping for the new extraction summary."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from parasitics import spice_number, summarize


class ParasiticsTests(unittest.TestCase):
    def test_spice_suffixes(self):
        self.assertEqual(spice_number('1meg'), 1e6)
        self.assertEqual(spice_number('2m'), .002)
        self.assertAlmostEqual(spice_number('3f'), 3e-15)

    def test_internal_wire_nodes_group_into_the_port(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build/tmp') as temp:
            path = Path(temp) / 'rc.spice'
            path.write_text('.subckt test A Y VGND\nR0 Y Y.t0 100\nR1 Y.t0 Y.t1 50\nC0 Y.t1 VGND 2f\nC1 Y.t0 A 3f\n.ends test\n', encoding='utf-8')
            result = summarize(path)['nets']
        self.assertEqual(result['Y']['sum_resistance_ohm'], 150)
        self.assertAlmostEqual(result['Y']['incident_capacitance_ff'], 5)
        self.assertAlmostEqual(result['A']['incident_capacitance_ff'], 3)
        self.assertAlmostEqual(result['VGND']['incident_capacitance_ff'], 2)


if __name__ == '__main__':
    unittest.main()
