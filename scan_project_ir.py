#!/usr/bin/env python3
"""Replay native C/C++ compile commands as bitcode, then scan the complete set."""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


class ScanError(Exception):
    """An input or tool failure that must abort the batch."""


@dataclass
class CompilationUnit:
    index: int
    directory: Path
    source: Path
    arguments: list
    output: str


# GNU-style driver options with separate values. Unknown joined flags pass
# through to Clang; unknown bare operands fail rather than silently losing flags.
VALUE_OPTIONS = {
    '-I', '-D', '-U', '-isystem', '-iquote', '-idirafter', '-include', '-imacros',
    '-isysroot', '--sysroot', '-target', '--target', '-arch', '-B', '-iprefix',
    '-iwithprefix', '-iwithprefixbefore', '-resource-dir', '-gcc-toolchain',
    '--gcc-toolchain', '-Xclang', '-Xpreprocessor', '-Xassembler', '-Xlinker',
    '-mllvm', '-stdlib', '-std', '-isystem-after', '-iframework', '-F',
}
DROP_VALUES = {'-o', '--output', '-MF', '-MT', '-MQ', '-MJ', '-serialize-diagnostics'}
DROP_FLAGS = {'-c', '-S', '-E', '-emit-llvm', '-fsyntax-only', '-M', '-MM',
              '-MD', '-MMD', '-MP', '-MG', '-flto', '-fno-lto', '-save-temps',
              '--save-temps', '-ftime-trace'}
UNSUPPORTED = {'-cc1', '--analyze', '-emit-ast', '-emit-pch', '-include-pch',
               '-include-pth', '--coverage', '-fprofile-arcs', '-ftest-coverage',
               '-working-directory'}
CPP_SUFFIXES = {'.C', '.cc', '.cp', '.cpp', '.cxx', '.c++', '.ii', '.CPP', '.CC'}


def split_arguments(text, description):
    try:
        return shlex.split(text, posix=True)
    except ValueError as error:
        raise ScanError(f'{description}: {error}') from error


def expand_response_files(arguments, directory, stack=()):
    result = []
    for argument in arguments:
        if not argument.startswith('@'):
            result.append(argument)
            continue
        path = (directory / argument[1:]).resolve()
        if path in stack or len(stack) >= 32:
            raise ScanError(f'recursive response file: {path}')
        try:
            nested = split_arguments(path.read_text(), f'response file {path}')
        except (OSError, UnicodeError) as error:
            raise ScanError(f'cannot read response file {path}: {error}') from error
        result.extend(expand_response_files(nested, directory, stack + (path,)))
    return result


def joined_output(argument):
    if argument.startswith('--output='):
        return argument.partition('=')[2]
    # Clang also has -objc* and -object* options; they are not joined -o paths.
    if argument.startswith('-o') and len(argument) > 2 and not argument.startswith(('-objc', '-object')):
        return argument[2:]
    return None


def original_output(arguments):
    """Find -o without requiring an unselected target to be Clang-compatible."""
    output = ''
    args = iter(arguments[1:])
    for arg in args:
        if arg == '--':
            break
        if arg in DROP_VALUES | VALUE_OPTIONS | {'-x', '-include-pch', '-include-pth', '-working-directory'}:
            value = next(args, None)
            if value is None:
                raise ScanError(f'missing value after {arg}')
            if arg in ('-o', '--output'):
                output = value
        elif joined_output(arg) is not None:
            output = joined_output(arg)
    return output


def command_parts(unit):
    """Return preserved flags, one source spelling, effective language, output.

    Relative arguments stay relative: subprocess cwd is the database directory.
    Values of options must never be mistaken for an input or an output option.
    """
    args = unit.arguments[1:]
    kept = []
    source = None
    source_language = None
    language = None
    output = ''
    i = 0
    positional = False
    while i < len(args):
        arg = args[i]
        i += 1
        if not positional and arg == '--':
            positional = True
            continue
        if not positional and (arg in DROP_VALUES or arg in VALUE_OPTIONS or arg == '-x'):
            if i == len(args):
                raise ScanError(f'entry {unit.index}: missing value after {arg}')
            value = args[i]
            i += 1
            if arg in ('-o', '--output'):
                output = value
            elif arg == '-x':
                language = None if value == 'none' else value
            elif arg in VALUE_OPTIONS:
                if arg.startswith('-X') and (value in DROP_VALUES | DROP_FLAGS | UNSUPPORTED
                                               or value.startswith(('-dependency-file', '-emit-', '-o='))):
                    raise ScanError(f'entry {unit.index}: unsupported forwarded option {arg} {value}')
                kept.extend((arg, value))
            continue
        if not positional and arg.startswith('-x') and len(arg) > 2:
            language = None if arg[2:] == 'none' else arg[2:]
            continue
        if not positional:
            original_output = joined_output(arg)
            if original_output is not None:
                output = original_output
                continue
            if arg in DROP_FLAGS or arg.startswith(('-MF', '-MT', '-MQ', '-MJ',
                                                     '-flto=', '-save-temps=', '--save-temps=',
                                                     '-ftime-trace=', '-serialize-diagnostics=')):
                continue
            if arg in UNSUPPORTED or arg.startswith(('--driver-mode=', '--config',
                                                     '-fmodule', '-fplugin', '-working-directory=')):
                raise ScanError(f'entry {unit.index}: unsupported compilation option {arg}')
            if arg.startswith('-Wp,'):
                # Common GCC dependency-output spelling. Do not discard mixed
                # preprocessor flags whose meaning cannot safely be rewritten.
                forwarded = arg[4:].split(',')
                if len(forwarded) == 2 and forwarded[0] in ('-MD', '-MMD'):
                    continue
                if any(f.startswith('-M') for f in forwarded):
                    raise ScanError(f'entry {unit.index}: unsupported dependency forwarding {arg}')
            if arg.startswith('-'):
                kept.append(arg)
                continue
        if (unit.directory / arg).resolve() != unit.source or source is not None:
            raise ScanError(f'entry {unit.index}: unexpected input/argument {arg!r}; '
                            'expected exactly the compilation database source')
        source = arg
        source_language = language
    if source is None:
        raise ScanError(f'entry {unit.index}: source argument missing for {unit.source}')
    return kept, source, source_language, output


def load_compdb(path, output_match=None):
    path = Path(path).resolve()
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError) as error:
        raise ScanError(f'cannot read compilation database {path}: {error}') from error
    if not isinstance(entries, list):
        raise ScanError('compilation database must be a JSON array')
    units = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k]
                                               for k in ('directory', 'file')):
            raise ScanError(f'entry {index}: directory and file must be nonempty strings')
        explicit_output = entry.get('output')
        if explicit_output is not None and (not isinstance(explicit_output, str) or not explicit_output):
            raise ScanError(f'entry {index}: output must be a nonempty string')
        # A target excluded by its explicit output need not have a compiler or
        # response files usable on this host.
        if output_match is not None and explicit_output is not None and output_match not in explicit_output:
            continue
        directory = (path.parent / entry['directory']).resolve()
        if 'arguments' in entry:
            args = entry['arguments']
            if not isinstance(args, list) or not args or any(not isinstance(a, str) for a in args):
                raise ScanError(f'entry {index}: arguments must be a nonempty string array')
        elif isinstance(entry.get('command'), str):
            args = split_arguments(entry['command'], f'entry {index}')
        else:
            raise ScanError(f'entry {index}: missing arguments or command')
        if not args or not args[0]:
            raise ScanError(f'entry {index}: empty compiler command')
        # Support transparent, common compiler launchers; arbitrary wrappers and
        # shell setup require an exported database of the underlying compiler.
        while args and Path(args[0]).name in ('ccache', 'sccache'):
            args = args[1:]
        if not args:
            raise ScanError(f'entry {index}: missing compiler after launcher')
        args = [args[0], *expand_response_files(args[1:], directory)]
        output = explicit_output if explicit_output is not None else original_output(args)
        if output_match is not None and output_match not in output:
            continue
        if not args or not re.search(r'(?:^|-)(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(?:-[\d.]+)?$', Path(args[0]).name):
            raise ScanError(f'entry {index}: unsupported compiler/wrapper command; use a native GCC/Clang database')
        unit = CompilationUnit(index, directory, (directory / entry['file']).resolve(), args, output)
        command_parts(unit)
        units.append(unit)
    if not units:
        raise ScanError('no compilation units selected (no outputs matched the filter)')
    return units


def bitcode_command(unit, output, clang, clangxx):
    flags, source, language, _ = command_parts(unit)
    if language not in (None, 'c', 'c++', 'cpp-output', 'c++-cpp-output'):
        raise ScanError(f'entry {unit.index}: unsupported input language {language}')
    # Drivers infer language from the command spelling, not a symlink target.
    suffix = Path(source).suffix
    if language is None and suffix not in CPP_SUFFIXES | {'.c', '.i'}:
        raise ScanError(f'entry {unit.index}: unsupported native C/C++ source {unit.source}')
    cpp = ('c++' in language if language else
           ('++' in Path(unit.arguments[0]).name or suffix in CPP_SUFFIXES))
    if language:
        flags.extend(('-x', language))
    # Preserve the source spelling, so __FILE__ and debug paths retain the
    # original command's meaning. Protect a dash-leading source with ./.
    if source.startswith('-'):
        source = './' + source
    return [clangxx if cpp else clang, *flags, '-emit-llvm', '-c', source, '-o', str(output)]


def readable_callsites(document):
    def one_line(value):
        return str(value).replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n')

    lines = []
    for site in document['indirect_callsites']:
        location = site.get('source_location') or {}
        filename = location.get('file') or '<unknown>'
        if filename != '<unknown>' and location.get('directory'):
            filename = str(Path(location['directory']) / filename)
        lines.append('{} | {}:{} | {}'.format(
            one_line(site['function'].get('name') or '<unnamed>'), one_line(filename),
            location.get('line', 0), one_line(site['called_operand'])))
    return ''.join(line + '\n' for line in lines)


def run_tool(command, cwd, description):
    try:
        result = subprocess.run(command, cwd=cwd, text=True, errors='replace',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise ScanError(f'{description}: {error}\ncwd: {cwd}\ncommand: {shlex.join(command)}') from error
    if result.returncode:
        raise ScanError(f'{description}: exit {result.returncode}\ncwd: {cwd}\n'
                        f'command: {shlex.join(command)}\n{result.stderr}{result.stdout}')
    if result.stderr:
        print(result.stderr, file=sys.stderr, end='')


def scan_project(compdb, output_dir, output_match=None, clang='clang-21',
                 clangxx='clang++-21', scanner=None):
    units = load_compdb(compdb, output_match)
    scanner = str(Path(scanner or Path(__file__).resolve().parent / 'llvm/build/icall_ir_scan').resolve())
    # Resolve tool paths before changing cwd, including user-provided ./clang.
    # Do not resolve symlinks: argv[0] ending in clang++ selects C++ defaults.
    clang = str(Path(shutil.which(clang) or clang).absolute())
    clangxx = str(Path(shutil.which(clangxx) or clangxx).absolute())
    output_dir = Path(output_dir).resolve()
    ir_root = output_dir / 'ir'
    ir_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=ir_root))
    succeeded = False
    try:
        bitcode = []
        digests = set()
        for ordinal, unit in enumerate(units, 1):
            # Entry ordinal distinguishes repeated source paths and filenames.
            output = run_dir / f'unit-{unit.index:06d}.bc'
            command = bitcode_command(unit, output, clang, clangxx)
            print(f'[{ordinal}/{len(units)}] {unit.source} ({unit.output})', file=sys.stderr)
            run_tool(command, unit.directory, f'entry {unit.index}: bitcode compilation failed for {unit.source}')
            if not output.is_file() or output.stat().st_size == 0:
                raise ScanError(f'entry {unit.index}: compiler did not produce bitcode: {output}')
            digest = hashlib.sha256(output.read_bytes()).digest()
            if digest in digests:
                print(f'entry {unit.index}: identical bitcode; scanning its content once', file=sys.stderr)
                output.unlink()
            else:
                digests.add(digest)
                bitcode.append(output)
        json_path = run_dir / 'callsites.json'
        run_tool([scanner, *map(str, bitcode), '-o', str(json_path)],
                 output_dir, 'LLVM scanner failed')
        try:
            document = json.loads(json_path.read_text())
            if document.get('kind') != 'llvm-ir-callsites' or document.get('schema_version') != 1:
                raise ValueError('expected LLVM callsite scanner schema version 1')
            if document.get('count') != len(document['indirect_callsites']):
                raise ValueError('callsite count does not match the scanner records')
            readable = readable_callsites(document)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ScanError(f'invalid scanner output: {error}') from error
        text_path = run_dir / 'callsites.txt'
        text_path.write_text(readable)
        # Compilation and scanning failures leave both previous reports intact.
        # Keep IR at its scanned path so JSON module input_file remains valid.
        json_path.replace(output_dir / 'callsites.json')
        text_path.replace(output_dir / 'callsites.txt')
        succeeded = True
        print(f'{len(units)} compilation units, {len(bitcode)} distinct IR modules, '
              f'{document["count"]} indirect callsites -> {output_dir}', file=sys.stderr)
    finally:
        if not succeeded:
            shutil.rmtree(run_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compdb', required=True, type=Path, help='compile_commands.json path')
    parser.add_argument('--output-match', help='literal substring of output field or original -o path')
    parser.add_argument('--output-dir', required=True, type=Path, help='report and retained IR directory')
    parser.add_argument('--clang', default='clang-21', help='Clang C driver (default: clang-21)')
    parser.add_argument('--clangxx', default='clang++-21', help='Clang C++ driver (default: clang++-21)')
    parser.add_argument('--scanner', type=Path, help='scanner executable (default: llvm/build/icall_ir_scan)')
    args = parser.parse_args()
    try:
        scan_project(**vars(args))
    except (ScanError, OSError, UnicodeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
