"""Focused multi-binary regressions; no DynamoRIO required."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_project
import report


class ProjectTests(unittest.TestCase):
    def test_manifest_paths_and_command_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / 'app'
            binary.touch()
            (root / 'tests.txt').write_text('case\t./app --check\n')
            manifest = root / 'project.json'
            data = {'targets': [{'name': 'app', 'binary': 'app',
                                 'tests_file': 'tests.txt'}]}
            manifest.write_text(json.dumps(data))
            config = run_project.load_manifest(manifest)
            self.assertEqual(config['targets'][0]['binary'], binary)
            self.assertEqual(config['targets'][0]['cwd'], root)
            data['targets'].append(dict(data['targets'][0]))
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                run_project.load_manifest(manifest)
            data['targets'].pop()
            data['targets'][0]['name'] = 'summary.json'
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                run_project.load_manifest(manifest)
            data['targets'][0]['name'] = 'app'
            manifest.write_text(json.dumps(data))
            (root / 'tests.txt').write_text('wrong\t/bin/true\n')
            with self.assertRaises(ValueError):
                run_project.load_manifest(manifest)

    def test_equal_offsets_stay_separate_in_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = []
            for name in ('one', 'two'):
                binary = root / name
                binary.touch()
                tests = root / (name + '.txt')
                tests.write_text(str(binary) + '\n')
                targets.append({'name': name, 'binary': name,
                                'tests_file': tests.name})
            manifest = root / 'project.json'
            manifest.write_text(json.dumps({'targets': targets}))
            calls = []

            def invoke(command, **kwargs):
                calls.append(command)
                script = Path(command[1]).name
                if script == 'run_suite.py':
                    suite = Path(command[command.index('--output-dir') + 1])
                    suite.mkdir(parents=True)
                    (suite / 'suite.csv').touch()
                if script == 'report.py':
                    binary = Path(command[command.index('--binary') + 1])
                    sites = {(binary.name, 0x1234)}
                    summary = report.coverage_summary(sites, sites,
                                                      {('one', 0x1234)})
                    path = Path(command[command.index('--export-summary') + 1])
                    path.write_text(json.dumps(summary))
                return type('Result', (), {'returncode': 0})()

            with patch.object(run_project.subprocess, 'run', side_effect=invoke):
                status = run_project.main([str(manifest), '--output-dir', str(root / 'out')])
            self.assertEqual(status, 0)
            rows = json.loads((root / 'out' / 'summary.json').read_text())
            self.assertEqual([r['covered'] for r in rows], [1, 0])
            self.assertEqual([r['uncovered'] for r in rows], [0, 1])
            self.assertNotEqual(rows[0]['binary'], rows[1]['binary'])
            suite_calls = [c for c in calls if Path(c[1]).name == 'run_suite.py']
            self.assertNotEqual(suite_calls[0][suite_calls[0].index('--output-dir') + 1],
                                suite_calls[1][suite_calls[1].index('--output-dir') + 1])


if __name__ == '__main__':
    unittest.main()
