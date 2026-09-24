"""Compdb rewriting and native batch scanning regression tests."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scan_project_ir.py'


class ProjectIRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():
            return
        spec = importlib.util.spec_from_file_location('scan_project_ir', SCRIPT)
        cls.mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.mod
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'project IR workflow is not implemented')
        self.temp = tempfile.TemporaryDirectory(prefix='icallcov-project-ir-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / 'build space'
        self.build.mkdir()
        self.db = self.build / 'compile_commands.json'
        self.source = self.build / 'unit.c'
        self.source.write_text('int dispatch(int (*fn)(int), int x) { return fn(x); }\n')

    def entry(self, flags=None, output='CMakeFiles/one.dir/unit.o'):
        return {'directory': str(self.build), 'file': 'unit.c',
                'arguments': ['cc', '-g', '-O0', *(flags or []), '-c', 'unit.c', '-o', output]}

    def load(self, entries, match=None):
        self.db.write_text(json.dumps(entries))
        return self.mod.load_compdb(self.db, match)

    def test_arguments_preferred_and_command_quoting(self):
        first = self.entry(['-DNAME=two words', '-I', 'include space'])
        first['command'] = 'invalid shell && false'
        second = {'directory': '.', 'file': 'unit.c',
                  'command': 'cc -DNAME="two words" -I"include space" -c unit.c -ojoined.o'}
        units = self.load([first, second])
        self.assertEqual(len(units), 2)
        for unit in units:
            self.assertEqual(unit.directory, self.build)
            self.assertEqual(unit.source, self.source)
            self.assertIn('-DNAME=two words', unit.arguments)
        self.assertEqual(units[1].output, 'joined.o')

    def test_filter_uses_output_field_or_original_o_not_source(self):
        first = self.entry()
        first['output'] = 'CMakeFiles/chosen.dir/unit.o'
        second = self.entry(output='CMakeFiles/other.dir/unit.o')
        units = self.load([first, second], 'CMakeFiles/chosen.dir/')
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].output, first['output'])
        self.assertEqual(len(self.load([first, second], 'CMakeFiles/other.dir/')), 1)
        with self.assertRaisesRegex(self.mod.ScanError, 'matched|selected'):
            self.load([first, second], 'absent')

    def test_duplicate_sources_are_not_collapsed_by_filename(self):
        units = self.load([self.entry(['-DONE=1']), self.entry(['-DONE=2'], 'other.o')])
        self.assertEqual(len(units), 2)
        commands = [self.mod.bitcode_command(u, self.root / f'{i}.bc', 'clang', 'clang++')
                    for i, u in enumerate(units)]
        self.assertIn('-DONE=1', commands[0])
        self.assertIn('-DONE=2', commands[1])
        self.assertNotEqual(commands[0][-1], commands[1][-1])

    def test_filter_excludes_unselected_commands_before_rewriting(self):
        unsupported = dict(self.entry(), output='other.o', arguments=['custom-wrapper', '@missing.rsp'])
        selected = dict(self.entry(), output='selected.o')
        units = self.load([unsupported, selected], 'selected.o')
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].index, 1)

    def test_filter_without_output_field_excludes_unsupported_target_flags(self):
        units = self.load([self.entry(['-fmodules'], 'other.o'),
                           self.entry(output='chosen.o')], 'chosen.o')
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].output, 'chosen.o')

    def test_rewrite_preserves_semantics_and_removes_output_side_effects(self):
        flags = ['-I', 'include space', '-isystem', 'sys', '-include', 'generated.h',
                 '-DVALUE=3', '-std=c11', '-fPIC', '-pthread', '--sysroot=/sdk',
                 '-MMD', '-MP', '-MFdeps.d', '-MT', 'unit.o', '-MJ', 'entry.json',
                 '-S', '-save-temps=obj', '-flto=thin', '-ftime-trace']
        unit = self.load([self.entry(flags)])[0]
        got = self.mod.bitcode_command(unit, self.root / 'unit.bc', 'my-clang', 'my-clang++')
        self.assertEqual(got, ['my-clang', '-g', '-O0', '-I', 'include space',
                              '-isystem', 'sys', '-include', 'generated.h', '-DVALUE=3',
                              '-std=c11', '-fPIC', '-pthread', '--sysroot=/sdk',
                              '-emit-llvm', '-c', 'unit.c', '-o', str(self.root / 'unit.bc')])

    def test_cpp_driver_and_explicit_language(self):
        entry = self.entry(['-x', 'c++', '-std=c++17'])
        unit = self.load([entry])[0]
        cmd = self.mod.bitcode_command(unit, self.root / 'x.bc', 'clang', 'clang++')
        self.assertEqual(cmd[0], 'clang++')
        self.assertIn('c++', cmd)
        entry = self.entry()
        entry['arguments'][0] = '/usr/bin/g++'
        unit = self.load([entry])[0]
        self.assertEqual(self.mod.bitcode_command(unit, self.root / 'x.bc', 'clang', 'clang++')[0], 'clang++')

    def test_response_file_expansion_and_cycle_failure(self):
        rsp = self.build / 'args.rsp'
        rsp.write_text('-I"include space" -DVALUE=3 -MMD -MF deps.d -c unit.c -o target.o')
        entry = {'directory': str(self.build), 'file': 'unit.c', 'arguments': ['cc', '@args.rsp']}
        unit = self.load([entry], 'target.o')[0]
        cmd = self.mod.bitcode_command(unit, self.root / 'x.bc', 'clang', 'clang++')
        self.assertIn('-Iinclude space', cmd)
        self.assertNotIn('@args.rsp', cmd)
        rsp.write_text('@args.rsp')
        with self.assertRaises(self.mod.ScanError):
            self.load([entry])

    def test_bad_database_and_ambiguous_commands_fail(self):
        for value in ({}, [42], [{}], [dict(self.entry(), arguments=[])],
                      [dict(self.entry(), arguments=['cc', '-c', 'other.c'])],
                      [dict(self.entry(), arguments=['cc', '-c', 'unit.c', 'other.c'])],
                      [dict(self.entry(), arguments=['cc', '-c', 'unit.c', '-o'])]):
            with self.subTest(value=value), self.assertRaises(self.mod.ScanError):
                units = self.load(value)
                for u in units:
                    self.mod.bitcode_command(u, self.root / 'x.bc', 'clang', 'clang++')

    def test_readable_text_keeps_one_line_and_handles_missing_debug(self):
        doc = {'indirect_callsites': [
            {'function': {'name': 'dispatch'}, 'source_location': {
                'file': 'unit.c', 'directory': '/src', 'line': 7}, 'called_operand': '%fn'},
            {'function': {'name': None}, 'source_location': None, 'called_operand': '%a\nb'},
        ]}
        text = self.mod.readable_callsites(doc)
        self.assertEqual(text.splitlines(), ['dispatch | /src/unit.c:7 | %fn',
                                            '<unnamed> | <unknown>:0 | %a\\nb'])

    def run_workflow(self, entries, *extra):
        self.db.write_text(json.dumps(entries))
        return subprocess.run([sys.executable, str(SCRIPT), '--compdb', str(self.db),
                               '--output-dir', str(self.root / 'result'), *extra],
                              capture_output=True, text=True, timeout=60)

    def require_native(self):
        if not shutil.which('clang-21') or not (ROOT / 'llvm/build/icall_ir_scan').exists():
            self.skipTest('build optional LLVM scanner and install Clang 21 for native tests')

    def test_native_batch_preserves_generated_headers_flags_and_duplicate_sources(self):
        self.require_native()
        (self.build / 'include space').mkdir()
        (self.build / 'include space/generated.h').write_text('#define INPUT 42\n')
        self.source.write_text('#include "generated.h"\n'
                               'int FUNCTION(int (*fn)(int)) { return fn(INPUT); }\n')
        flags = ['-I', 'include space', '-std=c11']
        entries = [self.entry(flags + ['-DFUNCTION=first']),
                   self.entry(flags + ['-DFUNCTION=second'], 'CMakeFiles/two.dir/unit.o')]
        result = self.run_workflow(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / 'result'
        doc = json.loads((output / 'callsites.json').read_text())
        self.assertEqual(doc['kind'], 'llvm-ir-callsites')
        self.assertEqual(doc['count'], 2)
        self.assertEqual({s['function']['name'] for s in doc['indirect_callsites']}, {'first', 'second'})
        self.assertEqual(len(list((output / 'ir').rglob('*.bc'))), 2)
        self.assertEqual(len((output / 'callsites.txt').read_text().splitlines()), 2)
        result = self.run_workflow(entries, '--output-match', 'CMakeFiles/two.dir/')
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads((output / 'callsites.json').read_text())
        self.assertEqual(doc['count'], 1)
        self.assertEqual(doc['indirect_callsites'][0]['function']['name'], 'second')

    def test_identical_emitted_modules_report_deduplication(self):
        self.require_native()
        result = self.run_workflow([self.entry(), self.entry(output='other.o')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('identical bitcode', result.stderr)
        doc = json.loads((self.root / 'result/callsites.json').read_text())
        self.assertEqual(doc['count'], 1)

    def test_native_cpp_suffixes_and_language_standard(self):
        self.require_native()
        if not shutil.which('clang++-21'):
            self.skipTest('Clang++ 21 is required')
        for suffix in ('.cp', '.CPP', '.CC'):
            with self.subTest(suffix=suffix):
                source = self.build / ('unit' + suffix)
                source.write_text('template <typename T> int call(T fn) { return fn(); }\n'
                                  'int dispatch(int (*fn)()) { if constexpr (true) return call(fn); }\n')
                entry = {'directory': str(self.build), 'file': source.name,
                         'arguments': ['cc', '-g', '-std=c++17', '-c', source.name, '-o', 'unit.o']}
                result = self.run_workflow([entry])
                self.assertEqual(result.returncode, 0, result.stderr)
                doc = json.loads((self.root / 'result/callsites.json').read_text())
                self.assertEqual(doc['count'], 1)

    def test_source_symlink_suffix_does_not_change_language(self):
        self.require_native()
        actual = self.build / 'generated.cpp'
        actual.write_text('#ifdef __cplusplus\n#error wrong language\n#endif\n'
                          'int dispatch(int (*fn)(int)) { return fn(1); }\n')
        self.source.unlink()
        self.source.symlink_to(actual)
        result = self.run_workflow([self.entry()])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_relative_compiler_path_is_resolved_before_entering_build_directory(self):
        self.require_native()
        (self.root / 'my-clang').symlink_to(shutil.which('clang-21'))
        self.db.write_text(json.dumps([self.entry()]))
        result = subprocess.run([sys.executable, str(SCRIPT), '--compdb', str(self.db),
                                 '--output-dir', 'result', '--clang', './my-clang'],
                                cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scanner_is_invoked_once_with_all_generated_modules(self):
        self.require_native()
        self.source.write_text('int dispatch(int (*fn)(int)) { return fn(FLAG); }\n')
        log = self.root / 'scanner-invocations.jsonl'
        wrapper = self.root / 'scanner-wrapper'
        wrapper.write_text('#!' + sys.executable + '\n'
                           'import json, os, sys\n'
                           f'with open({str(log)!r}, "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                           f'os.execv({str(ROOT / "llvm/build/icall_ir_scan")!r}, '
                           f'[{str(ROOT / "llvm/build/icall_ir_scan")!r}] + sys.argv[1:])\n')
        wrapper.chmod(0o755)
        result = self.run_workflow([self.entry(['-DFLAG=1']), self.entry(['-DFLAG=2'], 'other.o')],
                                   '--scanner', str(wrapper))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][:-2]), 2)
        for path in calls[0][:-2]:
            self.assertTrue(Path(path).is_file())

    def test_compile_and_scanner_failures_preserve_previous_results(self):
        self.require_native()
        output = self.root / 'result'
        output.mkdir()
        for name in ('callsites.json', 'callsites.txt'):
            (output / name).write_text('previous result')
        for entries, extra in (([self.entry(), self.entry(['-include', 'absent.h'])], []),
                               ([self.entry()], ['--scanner', '/bin/false']),
                               ([self.entry()], ['--scanner', '/bin/true']),
                               ([self.entry()], ['--clang', '/bin/true']),
                               ([self.entry()], ['--clang', '/does/not/exist'])):
            result = self.run_workflow(entries, *extra)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('error:', result.stderr)
            for name in ('callsites.json', 'callsites.txt'):
                self.assertEqual((output / name).read_text(), 'previous result')
            self.assertFalse(list((output / 'ir').rglob('*.bc')))

    def test_optimization_record_outputs_do_not_overwrite_build_files(self):
        flags = ['-fsave-optimization-record', '-fsave-optimization-record=yaml',
                 '-foptimization-record-file=remarks.yaml',
                 '-foptimization-record-passes=.*']
        unit = self.load([self.entry(flags)])[0]
        command = self.mod.bitcode_command(unit, self.root / 'out.bc', 'clang', 'clang++')
        self.assertFalse(any('optimization-record' in arg for arg in command))
        for forwarded in (['-Xclang', '-opt-record-file', '-Xclang', 'remarks.yaml'],
                          ['-mllvm', '-pass-remarks-output=remarks.yaml'],
                          ['-Xclang=-opt-record-file', '-Xclang=remarks.yaml'],
                          ['-mllvm=-pass-remarks-output=remarks.yaml'],
                          ['-Xclang', '-mllvm', '-Xclang', '-pass-remarks-output=remarks.yaml']):
            with self.subTest(forwarded=forwarded):
                with self.assertRaises(self.mod.ScanError):
                    self.load([self.entry(forwarded)])
        self.require_native()
        report = self.build / 'remarks.yaml'
        report.write_text('original optimization report')
        result = self.run_workflow([self.entry(flags)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report.read_text(), 'original optimization report')

    def test_publication_failure_restores_previous_reports(self):
        self.require_native()
        result = self.run_workflow([self.entry()])
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.root / 'result'
        previous = {name: (output / name).read_bytes()
                    for name in ('callsites.json', 'callsites.txt')}
        original_replace = Path.replace
        for filename in previous:
            injected = []

            def fail_once(path, destination):
                if path.name == filename and Path(destination) == output / filename and not injected:
                    injected.append(True)
                    raise OSError('injected publication failure')
                return original_replace(path, destination)

            with self.subTest(filename=filename), patch.object(Path, 'replace', fail_once):
                with self.assertRaises((OSError, self.mod.ScanError)):
                    self.mod.scan_project(self.db, output)
            self.assertTrue(injected)
            for name, content in previous.items():
                self.assertEqual((output / name).read_bytes(), content)
            for module in json.loads((output / 'callsites.json').read_text())['modules']:
                self.assertTrue(Path(module['input_file']).is_file())
        (output / 'callsites.txt').unlink()
        (output / 'callsites.txt').mkdir()
        result = self.run_workflow([self.entry()])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / 'callsites.json').read_bytes(), previous['callsites.json'])
        self.assertTrue((output / 'callsites.txt').is_dir())
        (output / 'callsites.txt').rmdir()
        (output / 'callsites.txt').write_bytes(previous['callsites.txt'])

        def fail_publication_and_rollback(path, destination):
            if path.name in ('callsites.txt', 'previous-callsites.json'):
                raise OSError('injected rollback failure')
            return original_replace(path, destination)

        with patch.object(Path, 'replace', fail_publication_and_rollback):
            with self.assertRaisesRegex(self.mod.ScanError, 'rollback errors'):
                self.mod.scan_project(self.db, output)
        document = json.loads((output / 'callsites.json').read_text())
        for module in document['modules']:
            self.assertTrue(Path(module['input_file']).is_file())
        self.assertTrue(list((output / 'ir').rglob('previous-callsites.json')))


if __name__ == '__main__':
    unittest.main()
