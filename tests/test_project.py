"""Focused multi-binary regressions; no DynamoRIO required."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_project
import report


class ProjectTests(unittest.TestCase):
    def test_undefined_coverage_is_not_a_successful_zero_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            static = root / 'static.json'
            static.write_text(json.dumps({'address_coordinate': 'module-relative',
                'elf_image_base': 0, 'indirect_callsites': [
                    {'module': 'app', 'offset': 16, 'instruction': 'call *%rax'}]}))
            trace = root / 'suite.csv'
            trace.write_text('caller_module,caller_offset,target_module,target_offset\n'
                             'app,0x10,app,0x20\n')
            summary = root / 'coverage.json'
            edges = root / 'edges.json'
            proc = subprocess.run([sys.executable, str(Path(report.__file__)),
                str(static), str(trace), '--export-summary', str(summary),
                '--export-edges', str(edges), '--show-filtered'], capture_output=True,
                text=True, timeout=15)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('N/A', proc.stdout)
            self.assertIn('UNKNOWN CALLSITES', proc.stdout)
            data = json.loads(summary.read_text())
            self.assertIsNone(data['coverage'])
            self.assertEqual(data['coverage_status'], 'indeterminate')
            self.assertTrue(edges.exists())

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

    def test_project_separates_targets_and_propagates_undefined_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = []
            for name in ('one', 'two', 'unknown'):
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
                returncode = 0
                if script == 'run_suite.py':
                    suite = Path(command[command.index('--output-dir') + 1])
                    suite.mkdir(parents=True, exist_ok=True)
                    (suite / 'suite.csv').touch()
                if script == 'report.py':
                    binary = Path(command[command.index('--binary') + 1])
                    sites = {(binary.name, 0x1234)}
                    project_sites = sites if binary.name != 'unknown' else set()
                    summary = report.coverage_summary(sites, project_sites, {('one', 0x1234)})
                    returncode = 2 if summary['coverage'] is None else 0
                    path = Path(command[command.index('--export-summary') + 1])
                    path.write_text(json.dumps(summary))
                return subprocess.CompletedProcess(command, returncode)

            with patch.object(run_project.subprocess, 'run', side_effect=invoke):
                manifest.write_text(json.dumps({'targets': targets[:2]}))
                self.assertEqual(run_project.main([
                    str(manifest), '--output-dir', str(root / 'out')]), 0)
                manifest.write_text(json.dumps({'targets': targets}))
                status = run_project.main([str(manifest), '--output-dir', str(root / 'out')])
            self.assertNotEqual(status, 0)
            rows = json.loads((root / 'out' / 'summary.json').read_text())
            self.assertEqual([r['covered'] for r in rows], [1, 0, 0])
            self.assertEqual([r['uncovered'] for r in rows], [0, 1, 0])
            self.assertEqual([r['status'] for r in rows], ['passed', 'passed', 'indeterminate'])
            self.assertEqual([r['coverage'] for r in rows], [100.0, 0.0, None])
            self.assertEqual(len({r['binary'] for r in rows}), 3)
            suite_calls = [c for c in calls if Path(c[1]).name == 'run_suite.py']
            self.assertEqual(len({c[c.index('--output-dir') + 1] for c in suite_calls}), 3)


if __name__ == '__main__':
    unittest.main()
