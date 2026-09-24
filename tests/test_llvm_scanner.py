"""Build llvm/ first, then run with unittest discover -s tests.

ICALL_IR_SCANNER overrides the executable; LLVM_AS overrides llvm-as.
An explicit missing executable is an error, while an unbuilt optional LLVM
scanner is skipped during binary-only pipeline testing.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LLVMScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = Path(os.environ.get('ICALL_IR_SCANNER',
                                         ROOT / 'llvm/build/icall_ir_scan'))
        if not cls.scanner.is_file():
            if 'ICALL_IR_SCANNER' in os.environ:
                raise AssertionError(f'LLVM scanner has not been built: {cls.scanner}')
            raise unittest.SkipTest('optional LLVM scanner not built; see llvm/README.md')
        cls.scanner = cls.scanner.resolve()
        cls.fixture = ROOT / 'llvm/tests/callsites.ll'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='icallcov-ir-tests-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def scan(self, *args):
        result = subprocess.run([str(self.scanner), *map(str, args)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_indirect_calls_only_including_invoke_and_tail_call(self):
        data = self.scan(self.fixture)
        self.assertEqual(data['kind'], 'llvm-ir-callsites')
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['count'], 4)
        sites = data['indirect_callsites']
        self.assertEqual([s['function']['name'] for s in sites],
                         ['dispatch', 'dispatch', 'through_invoke', 'tail_call'])
        self.assertEqual([s['opcode'] for s in sites], ['call', 'call', 'invoke', 'call'])
        self.assertEqual(len({s['callsite_id'] for s in sites}), 4)
        for site in sites:
            self.assertEqual(site['target_analysis'], {
                'status': 'not_analyzed', 'backend': None, 'possible_callees': None,
            })
            self.assertEqual(site['called_operand'], '%fp')
            self.assertNotIn('offset', site)  # IR instruction ordinals are not binary addresses.

    def test_source_location_does_not_define_callsite_identity(self):
        sites = self.scan(self.fixture)['indirect_callsites']
        for site in sites[:2]:
            loc = site['source_location']
            self.assertEqual(loc['file'], 'same_line.c')
            self.assertEqual(loc['directory'], '/fixture/src')
            self.assertEqual(loc['line'], 7)
            self.assertEqual(loc['column'], 3)
        self.assertNotEqual(sites[0]['callsite_id'], sites[1]['callsite_id'])
        self.assertEqual(sites[0]['function']['id'], sites[1]['function']['id'])
        self.assertIsNone(sites[2]['source_location'])

    def test_ids_are_repeatable_and_do_not_depend_on_input_path(self):
        copied = self.directory / 'renamed.ll'
        copied.write_bytes(self.fixture.read_bytes())
        first = self.scan(self.fixture)
        second = self.scan(copied)
        self.assertEqual(first['build_id'], second['build_id'])
        self.assertEqual(first['indirect_callsites'], second['indirect_callsites'])
        changed = self.directory / 'changed.ll'
        changed.write_text(copied.read_text().replace('@tail_call', '@different'))
        other = self.scan(changed)
        self.assertNotEqual(first['build_id'], other['build_id'])
        self.assertTrue(set(s['callsite_id'] for s in first['indirect_callsites']).isdisjoint(
            s['callsite_id'] for s in other['indirect_callsites']))

    def test_multiple_modules_have_distinct_ids_and_order_independent_build_id(self):
        other = self.directory / 'other.ll'
        other.write_text(self.fixture.read_text().replace('same_line.c', 'other.c'))
        first = self.scan(self.fixture, other)
        second = self.scan(other, self.fixture)
        self.assertEqual(first['count'], 8)
        self.assertEqual(len(first['modules']), 2)
        self.assertEqual(len({s['callsite_id'] for s in first['indirect_callsites']}), 8)
        self.assertEqual(first['build_id'], second['build_id'])
        self.assertEqual(first['indirect_callsites'], second['indirect_callsites'])

    def test_bitcode_and_text_have_same_logical_callsites(self):
        assembler = os.environ.get('LLVM_AS') or shutil.which('llvm-as-21') or shutil.which('llvm-as')
        if not assembler:
            self.skipTest('matching llvm-as is needed for bitcode test')
        bitcode = self.directory / 'fixture.bc'
        subprocess.run([assembler, str(self.fixture), '-o', str(bitcode)], check=True)
        textual = self.scan(self.fixture)['indirect_callsites']
        binary = self.scan(bitcode)['indirect_callsites']
        self.assertEqual([(s['function']['name'], s['opcode'], s['source_location']) for s in textual],
                         [(s['function']['name'], s['opcode'], s['source_location']) for s in binary])

    def test_empty_result_is_valid(self):
        path = self.directory / 'direct.ll'
        path.write_text('declare void @direct()\ndefine void @f() { call void @direct()\n ret void }\n')
        data = self.scan(path)
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['indirect_callsites'], [])

    def test_bad_inputs_fail_without_overwriting_existing_output(self):
        bad = self.directory / 'bad.ll'
        bad.write_text('this is not LLVM IR')
        invalid = self.directory / 'invalid.ll'
        # Parses successfully, but %value does not dominate the return.
        invalid.write_text('''define i32 @bad(i1 %condition) {
entry:
  br i1 %condition, label %left, label %merge
left:
  %value = add i32 1, 2
  br label %merge
merge:
  ret i32 %value
}
''')
        duplicate = self.directory / 'copy.ll'
        duplicate.write_bytes(self.fixture.read_bytes())
        output = self.directory / 'result.json'
        output.write_text('keep this')
        for inputs in ([bad], [invalid], [self.directory / 'missing.bc'],
                       [self.fixture, duplicate], [self.fixture, bad]):
            with self.subTest(inputs=inputs):
                result = subprocess.run([str(self.scanner), *map(str, inputs), '-o', str(output)],
                                        capture_output=True, text=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                if inputs == [invalid]:
                    self.assertIn('invalid LLVM IR', result.stderr)
                self.assertEqual(output.read_text(), 'keep this')

    def test_output_file_and_input_overwrite_guard(self):
        output = self.directory / 'result.json'
        result = subprocess.run([str(self.scanner), str(self.fixture), '-o', str(output)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(json.loads(output.read_text())['count'], 4)
        copied = self.directory / 'input.ll'
        original = self.fixture.read_bytes()
        copied.write_bytes(original)
        result = subprocess.run([str(self.scanner), str(copied), '-o', str(copied)],
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(copied.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
