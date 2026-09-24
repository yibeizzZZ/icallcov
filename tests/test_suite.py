"""Real subprocess regression tests for suite orchestration.

The tiny drrun shim only removes client arguments and execs the test program.
This isolates process handling/resume from instrumentation. Native DynamoRIO
coverage is exercised separately by test_addresses and dynamorio/tests.
"""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_suite

HEADER = 'caller_module,caller_offset,target_module,target_offset\n'


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='icallcov-suite-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / 'work'
        self.cwd.mkdir()
        self.output = self.root / 'output'
        self.commands = self.root / 'commands.txt'
        self.client = self.root / 'client.so'
        self.client.write_bytes(b'test client')
        self.drrun = self.root / 'drrun'
        self.drrun.write_text(f'#!{sys.executable}\n' + '''import os, sys
args = sys.argv[1:]
os.environ['ICALLCOV_TEST_MODE'] = args[args.index('-mode') + 1]
command = args[args.index('--') + 1:]
os.execv(command[0], command)
''')
        self.drrun.chmod(0o755)
        self.program = self.root / 'program'
        self.program.write_text(f'#!{sys.executable}\n' + '''import os, signal, sys, time
from pathlib import Path
kind = sys.argv[1]
with Path('calls.txt').open('a') as f:
    f.write(kind + '\\n')
header = 'caller_module,caller_offset,target_module,target_offset\\n'
def trace(module):
    target = '<not-recorded>,0x0' if os.environ['ICALLCOV_TEST_MODE'] == 'fast' else 'target,0x20'
    pid = 999999 if kind == 'reuse' else os.getpid()
    Path(f'dynamic.{pid}.csv').write_text(header + module + ',0x10,' + target + '\\n')
if kind == 'timeout':
    pid = os.fork()
    if pid == 0:
        os.close(1)
        os.close(2)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path('child-ready').write_text(str(os.getpid()))
        time.sleep(0.7)
        trace('leaked-child')
        Path('contaminated').touch()
        os._exit(0)
    while not Path('child-ready').exists():
        time.sleep(0.005)
    time.sleep(10)
if kind == 'wait':
    time.sleep(1)
trace(kind)
sys.exit(1 if kind == 'fail' else 0)
''')
        self.program.chmod(0o755)

    def suite(self, commands, *extra):
        self.commands.write_text(''.join(f'{name}\t{self.program} {kind}\n'
                                        for name, kind in commands))
        proc = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'run_suite.py'), '--runner', 'commands',
             '--tests-file', str(self.commands), '--drrun', str(self.drrun),
             '--client', str(self.client), '--cwd', str(self.cwd),
             '--output-dir', str(self.output), *extra],
            capture_output=True, text=True, timeout=15,
        )
        path = self.output / 'summary.csv'
        self.assertTrue(path.exists(), proc.stdout + proc.stderr)
        with path.open() as stream:
            return proc, list(csv.DictReader(stream))

    def calls(self):
        return (self.cwd / 'calls.txt').read_text().splitlines()

    def test_names_that_sanitize_identically_execute_and_save_separately(self):
        proc, rows = self.suite([('foo/bar', 'first'), ('foo_bar', 'second')], '--resume')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ['first', 'second'])
        self.assertEqual(len(list((self.output / 'traces').glob('*.csv'))), 2)
        self.suite([('foo/bar', 'first'), ('foo_bar', 'second')], '--resume')
        self.assertEqual(self.calls(), ['first', 'second'])

    def test_compatible_success_is_reused_with_original_status(self):
        self.suite([('one', 'ok')])
        proc, rows = self.suite([('one', 'ok')], '--resume')
        self.assertEqual(self.calls(), ['ok'])
        self.assertEqual(rows[0]['status'], 'passed')
        self.assertEqual(rows[0]['returncode'], '0')
        self.assertEqual(rows[0]['resumed'], 'True')

    def test_resume_invalidates_mode_command_and_binary_changes(self):
        self.suite([('one', 'ok')])
        self.suite([('one', 'ok')], '--resume', '--mode', 'edge')
        self.assertEqual(self.calls(), ['ok', 'ok'])
        self.suite([('one', 'changed')], '--resume', '--mode', 'edge')
        self.assertEqual(self.calls(), ['ok', 'ok', 'changed'])
        self.program.write_text(self.program.read_text() + '\n# rebuilt executable\n')
        self.suite([('one', 'changed')], '--resume', '--mode', 'edge')
        self.assertEqual(self.calls(), ['ok', 'ok', 'changed', 'changed'])

    def test_failed_results_are_not_reused_or_hidden(self):
        proc, _ = self.suite([('one', 'fail')])
        proc, rows = self.suite([('one', 'fail')], '--resume')
        self.assertEqual(self.calls(), ['fail', 'fail'])
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertEqual(rows[0]['returncode'], '1')
        self.assertNotEqual(proc.returncode, 0)

    def test_timeout_results_are_retried(self):
        self.suite([('one', 'timeout')], '--timeout', '0.25')
        proc, rows = self.suite([('one', 'timeout')], '--timeout', '0.25', '--resume')
        self.assertEqual(self.calls(), ['timeout', 'timeout'])
        self.assertEqual(rows[0]['status'], 'timeout')
        self.assertEqual(rows[0]['resumed'], 'False')
        self.assertFalse((self.cwd / 'contaminated').exists())
        self.assertNotEqual(proc.returncode, 0)

    def test_resume_rejects_different_runner_or_test_identity(self):
        self.suite([('one', 'ok')])
        metadata_path = next((self.output / 'traces').glob('*.json'))
        for field, other in (('runner', 'gtest'), ('test_name', 'another-test')):
            with metadata_path.open() as stream:
                metadata = json.load(stream)
            metadata['identity'][field] = other
            metadata_path.write_text(json.dumps(metadata))
            self.suite([('one', 'ok')], '--resume')
        self.assertEqual(self.calls(), ['ok', 'ok', 'ok'])

    def test_corrupt_metadata_is_not_trusted(self):
        self.suite([('one', 'ok')])
        metadata_path = next((self.output / 'traces').glob('*.json'))
        metadata_path.write_text('{interrupted')
        self.suite([('one', 'ok')], '--resume')
        metadata_path.write_text('[]')
        self.suite([('one', 'ok')], '--resume')
        self.assertEqual(self.calls(), ['ok', 'ok', 'ok'])

    def test_partial_or_invalid_result_metadata_forces_rerun(self):
        self.suite([('one', 'ok')])
        path = next((self.output / 'traces').glob('*.json'))
        changes = [('duration_seconds', None), ('duration_seconds', 'bad'),
                   ('test_name', 'wrong'), ('helpers', 'wrong'),
                   ('unique_callsites', 999)]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                metadata = json.loads(path.read_text())
                if value is None:
                    del metadata['result'][field]
                else:
                    metadata['result'][field] = value
                path.write_text(json.dumps(metadata))
                proc, rows = self.suite([('one', 'ok')], '--resume')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(rows[0]['resumed'], 'False')
        self.assertEqual(len(self.calls()), 1 + len(changes))

    def test_missing_metadata_or_corrupt_csv_forces_rerun(self):
        self.suite([('one', 'ok')])
        metadata = list((self.output / 'traces').glob('*.json'))
        self.assertEqual(len(metadata), 1)
        metadata[0].unlink()
        self.suite([('one', 'ok')], '--resume')
        self.assertEqual(self.calls(), ['ok', 'ok'])
        trace = next((self.output / 'traces').glob('*.csv'))
        trace.write_text(HEADER)
        self.suite([('one', 'ok')], '--resume')
        self.assertEqual(self.calls(), ['ok', 'ok', 'ok'])

    def test_timeout_kills_children_before_collecting_next_test(self):
        kwargs = dict(drrun=self.drrun, client=self.client, mode='fast', cwd=self.cwd)
        first = run_suite.run_test(
            test={'name': 'first', 'helpers': []}, app_command=[str(self.program), 'timeout'],
            log_path=self.root / 'first.log', timeout=0.25, **kwargs,
        )
        second = run_suite.run_test(
            test={'name': 'second', 'helpers': []}, app_command=[str(self.program), 'wait'],
            log_path=self.root / 'second.log', timeout=3, **kwargs,
        )
        self.assertEqual(first['status'], 'timeout')
        self.assertEqual(second['status'], 'passed')
        self.assertFalse((self.cwd / 'contaminated').exists())
        self.assertFalse(any(row['caller_module'] == 'leaked-child' for row in second['rows']))
        self.assertFalse(list(self.cwd.glob('dynamic.*.csv')))

    def test_existing_traces_are_neither_collected_nor_deleted(self):
        existing = self.cwd / 'dynamic.999999.csv'
        existing.write_text(HEADER + 'unrelated,0x1,target,0x2\n')
        self.suite([('one', 'ok')])
        self.assertTrue(existing.exists())
        with (self.output / 'suite.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({r['caller_module'] for r in rows}, {'ok'})

    def test_reused_pid_name_preserves_old_trace_and_collects_new_trace(self):
        existing = self.cwd / 'dynamic.999999.csv'
        original = HEADER + 'unrelated,0x1,target,0x2\n'
        existing.write_text(original)
        proc, _ = self.suite([('one', 'reuse')])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(existing.read_text(), original)
        with (self.output / 'suite.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({r['caller_module'] for r in rows}, {'reuse'})

    def test_execution_alias_is_preserved(self):
        self.program.write_text(self.program.read_text().replace(
            'kind = sys.argv[1]', 'kind = Path(sys.argv[0]).name'))
        alias = self.root / 'alias'
        alias.symlink_to(self.program)
        self.program = alias
        proc, _ = self.suite([('one', 'ok')])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ['alias'])


if __name__ == '__main__':
    unittest.main()
