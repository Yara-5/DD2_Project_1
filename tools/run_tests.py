"""Run regression tests and save an explicit, fresh pass/fail report."""
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / 'results/tests/unit.json'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text('{"passed": false, "status": "running"}\n', encoding='utf-8')
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {'passed': result.wasSuccessful() and result.testsRun > 0,
          'checked_at': datetime.now(timezone.utc).isoformat(),
          'tests_run': result.testsRun, 'failures': [(str(t), s) for t, s in result.failures],
          'errors': [(str(t), s) for t, s in result.errors], 'skipped': result.skipped}
output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
raise SystemExit(0 if report['passed'] else 1)
