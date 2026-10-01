"""Small regression checks for the TypeDive adapter, not its analysis."""
import json
from pathlib import Path
import tempfile
import unittest
import shutil
import subprocess

import typedive


class TypeDiveTests(unittest.TestCase):
    def test_filter_before_wrapper_validation_and_keep_build_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [
                {'directory': '.', 'file': 'a.c', 'arguments': [
                    '/old/wrapper', '-O2', '-g', '-DNDEBUG', '-c', 'a.c',
                    '-o', 'CMakeFiles/shared.dir/a.o']},
                {'directory': '.', 'file': 'a.c', 'arguments': [
                    'unsupported', '@absent', '-o', 'CMakeFiles/static.dir/a.o']},
            ]
            db = root / 'compile_commands.json'
            db.write_text(json.dumps(entries))
            units, selected = typedive.select_units(
                db, root / 'selected.json', ['shared.dir/'], [], ['/old/wrapper=clang'])
            self.assertEqual(len(units), 1)
            self.assertEqual(selected, [0])
            command = typedive.compile_command(units[0], root / 'a.bc', 'clang', 'clang++')
            for flag in ('-O2', '-g', '-DNDEBUG', '-no-opaque-pointers'):
                self.assertIn(flag, command)
            self.assertNotIn('CMakeFiles/static.dir/a.o', command)

    def test_records_keep_same_line_sites_and_external_candidates(self):
        records = []
        for ordinal in (4, 8):
            records.append({'schema_version': 1, 'mode': 0,
                            'caller_module': 'unit.bc', 'caller_function': 'outer',
                            'caller_instruction': ordinal, 'caller_ir': 'call void %f()',
                            'caller_file': '/src/p/a.c', 'caller_line': 12,
                            'caller_column': 3, 'callee_module': 'unit.bc',
                            'callee_function': 'external', 'callee_file': '',
                            'callee_line': 0, 'callee_declaration': True})
        log = '\n'.join('ICALLCOV_PAIR_V1\t' + json.dumps(r) for r in records)
        log += '\n# Number of indirect calls: 2\n# Number of indirect calls with targets: 2'
        log += '\n# Number of indirect-call targets: 2\n# Number of address-taken functions: 1\n'
        pairs, stats = typedive.parse_log(log, 0, Path('/src/p'))
        self.assertEqual(len({r['callsite_id'] for r in pairs}), 2)
        self.assertEqual(pairs[0]['caller_file'], 'a.c')
        self.assertTrue(pairs[0]['callee_declaration'])
        self.assertEqual(stats['indirect_call_targets'], 2)
        with self.assertRaises(ValueError):
            typedive.parse_log(log + '\nICALLCOV_PAIR_V1\t{broken', 0, Path('/src/p'))
        with self.assertRaises(ValueError):
            typedive.parse_log(log.replace('targets: 2\n', 'targets: 3\n'), 0, Path('/src/p'))

    def test_source_matching_ignores_inlined_caller_but_not_directory(self):
        static = [{'callsite_id': 'one', 'caller_function': 'outer',
                   'caller_file': 'src/core.c', 'caller_line': 12,
                   'callee_function': '_Z8callbackv', 'callee_file': 'src/core.c',
                   'callee_line': 2, 'callee_declaration': False}]
        dynamic = [dict(caller_function='inlined', caller_file='src/core.c', caller_line=12,
                        callee_function='_Z8callbackv', callee_file='src/core.c', callee_line=2),
                   dict(caller_function='other', caller_file='other/core.c', caller_line=12,
                        callee_function='_Z8callbackv', callee_file='src/core.c', callee_line=2),
                   dict(caller_function='??', caller_file='', caller_line=0,
                        callee_function='??', callee_file='', callee_line=0)]
        result = typedive.compare_pairs(static, dynamic)
        self.assertEqual(result['dynamic_covered'], 1)
        self.assertEqual(result['dynamic_missing'], 1)
        self.assertEqual(result['dynamic_unresolved'], 1)
        self.assertEqual(result['covered'][0]['static_callsite_ids'], ['one'])

    def test_output_directory_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'output'
            typedive.new_output(path)
            (path / 'sentinel').write_text('keep')
            with self.assertRaises(FileExistsError):
                typedive.new_output(path)
            self.assertEqual((path / 'sentinel').read_text(), 'keep')

    @unittest.skipUnless(shutil.which('clang++') and shutil.which('addr2line') and shutil.which('nm'),
                         'requires clang++, addr2line and nm')
    def test_optimized_cpp_target_uses_outer_callback_not_inlined_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'inner.h').write_text(
                '__attribute__((always_inline)) inline int inner(int x) { return x * 3 + 1; }\n')
            (root / 'main.cc').write_text(
                '#include "inner.h"\n'
                '__attribute__((noinline)) int callback(int x) { return inner(x); }\n'
                'int main(int argc, char**) { return callback(argc); }\n')
            binary = root / 'test'
            # The pinned pre-release LLVM15 + Ubuntu binutils combination has
            # incomplete DWARF5 decoding for this fixture; isolate frame selection.
            subprocess.run(['clang++', '-O2', '-g', '-gdwarf-4', str(root / 'main.cc'),
                            '-o', str(binary)], check=True)
            symbols = subprocess.check_output(['nm', '-n', str(binary)], text=True)
            address = next(int(line.split()[0], 16) for line in symbols.splitlines()
                           if line.split()[-1] == '_Z8callbacki')
            offset = address - typedive.elf_image_base(binary)
            locations = typedive.symbolize_offsets(binary, {offset}, 'addr2line', root, {})
            self.assertEqual(locations[offset]['caller']['function'], '_Z5inneri')
            self.assertEqual(locations[offset]['callee']['function'], '_Z8callbacki')
            self.assertEqual(locations[offset]['callee']['file'], 'main.cc')


if __name__ == '__main__':
    unittest.main()
