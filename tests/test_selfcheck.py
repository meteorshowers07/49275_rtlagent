import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import selfcheck


class SelfcheckTests(unittest.TestCase):
    def test_interrupted_check_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'check'
            with patch('sys.argv', ['selfcheck.py', '--output', str(output)]), \
                 patch.object(selfcheck.subprocess, 'run', side_effect=KeyboardInterrupt), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(selfcheck.main(), 1)
            report = json.loads((output/'selfcheck.json').read_text(encoding='utf-8'))
            self.assertFalse(report['software_checks_passed'])
            self.assertIn('KeyboardInterrupt', report['error'])
