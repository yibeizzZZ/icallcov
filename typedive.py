#!/usr/bin/env python3
"""Run upstream TypeDive and compare candidates with observed dynamic positives."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from elf_addresses import elf_image_base
from report import load_dynamic, normalize_module
from scan_project_ir import ScanError, bitcode_command, load_compdb, run_tool, split_arguments

MODES = {'flta': 0, 'one_layer': 1, 'mlta': 2}
PREFIX = 'ICALLCOV_PAIR_V1\t'
FIELDS = ['schema_version', 'mode', 'callsite_id', 'caller_module', 'caller_function',
          'caller_instruction', 'caller_ir', 'caller_file', 'caller_line', 'caller_column',
          'callee_module', 'callee_function', 'callee_file', 'callee_line', 'callee_declaration']
STATS = {'indirect calls': 'indirect_calls',
         'indirect calls with targets': 'calls_with_targets',
         'indirect-call targets': 'indirect_call_targets',
         'address-taken functions': 'address_taken_functions',
         'multi-layer calls': 'multi_layer_calls', 'multi-layer targets': 'multi_layer_targets',
         'one-layer calls': 'one_layer_calls', 'one-layer targets': 'one_layer_targets'}


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else _digest(stream)


def _digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()


def new_output(path):
    Path(path).mkdir(parents=True, exist_ok=False)


def mappings(values):
    result = {}
    for value in values:
        old, sep, new = value.partition('=')
        if not sep or not old or not new:
            raise ValueError(f'expected OLD=NEW, got {value!r}')
        result[old] = new
    return result


def select_units(compdb, selected_path, includes, excludes, wrappers):
    """Filter raw entries, then delegate all native command parsing to the scanner."""
    compdb = Path(compdb).resolve()
    entries = json.loads(compdb.read_text())
    if not isinstance(entries, list):
        raise ValueError('compilation database must be an array')
    aliases = mappings(wrappers)
    if any(value not in ('clang', 'clang++') for value in aliases.values()):
        raise ValueError('wrapper aliases must name clang or clang++')
    selected, indices = [], []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f'invalid compilation database entry {index}')
        # Search arguments when available, as load_compdb does; never execute a shell.
        command = entry.get('arguments', entry.get('command', ''))
        text = (' '.join(command) if isinstance(command, list) else command)
        text += ' ' + entry.get('output', '')
        if (includes and not any(rule in text for rule in includes)) or any(rule in text for rule in excludes):
            continue
        entry = dict(entry)
        entry['directory'] = str((compdb.parent / entry['directory']).resolve())
        if aliases:
            args = entry.get('arguments')
            if args is None:
                args = split_arguments(entry['command'], f'entry {index}')
            args = list(args)
            if args and args[0] in aliases:
                args[0] = aliases[args[0]]
            entry['arguments'] = args
            entry.pop('command', None)
        selected.append(entry)
        indices.append(index)
    if not selected:
        raise ValueError('no compilation units selected')
    Path(selected_path).write_text(json.dumps(selected, indent=2) + '\n')
    return load_compdb(selected_path), indices


def compile_command(unit, output, clang, clangxx):
    command = bitcode_command(unit, output, clang, clangxx)
    # Keep the database's optimization/defines/ABI flags. Last -g wins over -g0.
    command[-5:-5] = ['-Xclang', '-no-opaque-pointers', '-g']
    return command


def source_path(filename, root, path_maps=None):
    if not filename or filename == '??':
        return ''
    for old, new in sorted((path_maps or {}).items(), key=lambda item: -len(item[0])):
        if filename == old or filename.startswith(old.rstrip('/') + '/'):
            filename = new.rstrip('/') + filename[len(old.rstrip('/')):]
            break
    path = Path(os.path.normpath(filename))
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def parse_log(log, mode, root):
    stats, pairs = {}, {}
    if 'error loading file' in log:
        raise ValueError('TypeDive skipped an unreadable module; see raw log')
    for line in log.splitlines():
        match = re.match(r'# Number of (.+?):\s*(\d+)\s*$', line)
        if match and match[1] in STATS:
            stats[STATS[match[1]]] = int(match[2])
        if not line.startswith(PREFIX):
            continue
        record = json.loads(line[len(PREFIX):])
        if record.get('schema_version') != 1 or record.get('mode') != mode:
            raise ValueError('unsupported pair schema or incorrect TypeDive mode')
        for key in FIELDS:
            if key != 'callsite_id' and key not in record:
                raise ValueError(f'pair missing {key}')
        for key in ('caller_instruction', 'caller_line', 'caller_column', 'callee_line'):
            if type(record[key]) is not int or record[key] < 0:
                raise ValueError(f'invalid pair field {key}')
        if type(record['callee_declaration']) is not bool:
            raise ValueError('invalid declaration flag')
        for key in ('caller_module', 'caller_function', 'caller_ir', 'caller_file',
                    'callee_module', 'callee_function', 'callee_file'):
            if not isinstance(record[key], str):
                raise ValueError(f'invalid pair field {key}')
        identity = [record['caller_module'], record['caller_function'], record['caller_instruction']]
        record['callsite_id'] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        for key in ('caller_file', 'callee_file'):
            record[key] = source_path(record[key], root)
        key = (record['callsite_id'], record['callee_module'], record['callee_function'])
        if key in pairs and pairs[key] != record:
            raise ValueError('conflicting TypeDive candidate records')
        pairs[key] = record
    required = {'indirect_calls', 'calls_with_targets', 'indirect_call_targets', 'address_taken_functions'}
    if not required.issubset(stats):
        raise ValueError('missing TypeDive statistics; see raw log')
    if len(pairs) != stats['indirect_call_targets']:
        raise ValueError('pair count differs from TypeDive target count; check patch/raw log')
    if len({key[0] for key in pairs}) != stats['calls_with_targets']:
        raise ValueError('callsite count differs from TypeDive statistics')
    return [pairs[key] for key in sorted(pairs)], stats


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def analyze(args):
    output = args.output.resolve()
    new_output(output)
    (output / 'raw').mkdir()
    (output / 'ir').mkdir()
    root = args.project_root.resolve()
    units, indices = select_units(args.compdb, output / 'compile_commands.json',
                                  args.include_command, args.exclude_command, args.compiler_wrapper)
    versions = {}
    for tool in (args.clang, args.clangxx):
        version = subprocess.check_output([tool, '--version'], text=True)
        if not re.search(r'clang version 15\.', version):
            raise ValueError(f'{tool}: TypeDive requires the pinned LLVM 15 toolchain')
        versions[tool] = version.strip()
    manifest = {'schema_version': 1, 'project_root': str(root),
                'compdb': str(args.compdb.resolve()), 'compdb_sha256': sha256(args.compdb),
                'include_command': args.include_command, 'exclude_command': args.exclude_command,
                'compiler_wrapper': args.compiler_wrapper, 'tool_versions': versions,
                'kanalyzer': args.kanalyzer, 'kanalyzer_sha256': sha256(shutil.which(args.kanalyzer) or args.kanalyzer),
                'patch_sha256': sha256(Path(__file__).parent / 'integrations/typedive/typedive.patch'),
                'units': [], 'modes': {}, 'interpretation': 'Static candidates are evidence, not a sound ground-truth upper bound.'}
    version_file = Path('/opt/typedive/versions.txt')
    if version_file.exists():
        manifest['environment_revisions'] = version_file.read_text().splitlines()
    bitcode = []
    for ordinal, (unit, original_index) in enumerate(zip(units, indices), 1):
        path = output / 'ir' / f'unit-{original_index:06d}.bc'
        command = compile_command(unit, path, args.clang, args.clangxx)
        print(f'[{ordinal}/{len(units)}] {unit.source}', file=sys.stderr, flush=True)
        run_tool(command, unit.directory, 'typed-pointer bitcode compilation failed')
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'compiler produced no bitcode: {path}')
        bitcode.append(path)
        manifest['units'].append({'compdb_index': original_index, 'source': str(unit.source),
                                  'source_sha256': sha256(unit.source), 'output': unit.output,
                                  'cwd': str(unit.directory), 'command': command,
                                  'bitcode': str(path), 'bitcode_sha256': sha256(path)})
    manifest['translation_units'] = len(units)
    # LLVM's response-file parser accepts shell-style quoting, including paths with spaces.
    response = output / 'bitcode.list'
    response.write_text('\n'.join(shlex.quote(str(p)) for p in bitcode) + '\n')
    write_json(output / 'manifest.json', manifest)
    for name, mode in MODES.items():
        command = [args.kanalyzer, f'--mlta={mode}', '@' + str(response)]
        raw = output / 'raw' / f'{name}.log'
        print(f'Running TypeDive {name}', file=sys.stderr, flush=True)
        with raw.open('w') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError(f'TypeDive {name} exited {result.returncode}; see {raw}')
        pairs, stats = parse_log(raw.read_text(errors='replace'), mode, root)
        with (output / f'{name}.tsv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, FIELDS, delimiter='\t')
            writer.writeheader()
            writer.writerows(pairs)
        manifest['modes'][name] = dict(stats, candidate_pairs=len(pairs), command=command)
        print(f'{name}: {stats}', flush=True)
    write_json(output / 'summary.json', manifest)
    print(f'Results: {output}')


def read_pairs(path):
    with Path(path).open(newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if reader.fieldnames != FIELDS:
            raise ValueError(f'unsupported static TSV schema: {path}')
        rows = list(reader)
    for row in rows:
        for key in ('schema_version', 'mode', 'caller_instruction', 'caller_line', 'caller_column', 'callee_line'):
            row[key] = int(row[key])
        if row['schema_version'] != 1 or row['callee_declaration'] not in ('True', 'False'):
            raise ValueError(f'invalid static TSV record: {path}')
        row['callee_declaration'] = row['callee_declaration'] == 'True'
    return rows


def symbolize_offsets(binary, offsets, addr2line, root, path_maps):
    base = elf_image_base(binary)
    offsets = sorted(offsets)
    # Do not demangle: TypeDive's IR names use C++ linkage names too.
    result = subprocess.run([addr2line, '-a', '-f', '-i', '-e', str(binary)],
                            input=''.join(hex(base + offset) + '\n' for offset in offsets),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    blocks, current = {}, None
    for line in result.stdout.splitlines():
        if re.fullmatch(r'0x[0-9a-fA-F]+', line):
            current = int(line, 16) - base
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
        else:
            raise ValueError('unexpected addr2line response')
    if set(blocks) != set(offsets):
        raise ValueError('addr2line did not resolve every requested address')
    symbols = {}
    for offset, lines in blocks.items():
        if not lines or len(lines) % 2:
            raise ValueError('unexpected addr2line inline frames')
        frames = []
        for function, location in zip(lines[::2], lines[1::2]):
            match = re.match(r'^(.*):(\d+)(?:\s.*)?$', location)
            frames.append({'function': function,
                           'file': source_path(match[1], root, path_maps) if match else '',
                           'line': int(match[2]) if match else 0})
        # A caller is attributed to its inlined source expression. A callee
        # entry may begin inside an inline helper: its outer frame owns it.
        symbols[offset] = {'caller': frames[0], 'callee': frames[-1]}
    return symbols


def compare_pairs(static, dynamic):
    candidates = {}
    for pair in static:
        key = (pair['caller_file'], pair['caller_line'], pair['callee_function'])
        candidates.setdefault(key, []).append(pair)
    covered, missing, unresolved = [], [], []
    for edge in dynamic:
        if not edge['caller_file'] or not edge['caller_line'] or edge['callee_function'] in ('', '??'):
            unresolved.append(edge)
            continue
        key = (edge['caller_file'], edge['caller_line'], edge['callee_function'])
        matches = candidates.get(key, [])
        # Distinguish internal functions with the same name in different source files.
        matches = [pair for pair in matches if not pair['callee_file'] or not edge['callee_file']
                   or pair['callee_file'] == edge['callee_file']]
        if matches:
            covered.append(dict(edge, static_callsite_ids=sorted({pair['callsite_id'] for pair in matches})))
        else:
            missing.append(edge)
    return {'static_candidates': len(static), 'dynamic_covered': len(covered),
            'dynamic_missing': len(missing), 'dynamic_unresolved': len(unresolved),
            'covered': covered, 'missing': missing, 'unresolved': unresolved}


def compare(args):
    summary = json.loads((args.static / 'summary.json').read_text())
    root = Path(summary['project_root'])
    path_maps = mappings(args.path_map)
    executed, edges, skipped = load_dynamic(args.dynamic)
    module = normalize_module(args.module)
    internal = [(caller[1], target[1], info['count']) for caller, targets in edges.items()
                if caller[0] == module for target, info in targets.items() if target[0] == module]
    if not internal:
        raise ValueError('no internal dynamic edges in requested module scope (edge tracing required)')
    symbols = symbolize_offsets(args.binary, {v for c, t, _ in internal for v in (c, t)},
                                args.addr2line, root, path_maps)
    observed = {}
    for caller, target, count in internal:
        c, t = symbols[caller]['caller'], symbols[target]['callee']
        edge = {f'caller_{k}': v for k, v in c.items()}
        edge.update({f'callee_{k}': v for k, v in t.items()})
        # Missing source information must not merge unrelated runtime edges.
        key = tuple(edge.values()) if c['file'] and c['line'] and t['function'] != '??' else (caller, target)
        # Caller function is diagnostic only: inlining can change its ownership.
        if c['file'] and c['line'] and t['function'] != '??':
            key = (c['file'], c['line'], t['function'], t['file'])
        if key not in observed:
            observed[key] = dict(edge, runtime_pairs=[], trace_events=0)
        observed[key]['runtime_pairs'].append({'caller_offset': hex(caller), 'target_offset': hex(target)})
        observed[key]['trace_events'] += count
    dynamic = list(observed.values())
    results = {name: compare_pairs(read_pairs(args.static / f'{name}.tsv'), dynamic) for name in MODES}
    def edge_key(edge):
        return edge['caller_file'], edge['caller_line'], edge['callee_function'], edge['callee_file']
    baseline = {edge_key(edge) for name in ('flta', 'one_layer') for edge in results[name]['covered']}
    removed = [edge for edge in results['mlta']['missing'] if edge_key(edge) in baseline]
    document = {'schema_version': 1, 'module': module, 'binary': str(args.binary.resolve()),
                'binary_sha256': sha256(args.binary), 'dynamic_csv': str(args.dynamic.resolve()),
                'dynamic_sha256': sha256(args.dynamic), 'static_summary_sha256': sha256(args.static / 'summary.json'),
                'same_build_verified': False, 'path_map': path_maps,
                'dynamic_total_runtime_callsites': len(executed), 'skipped_dynamic_rows': skipped,
                'dynamic_internal_runtime_callsites': len({c for c, _, _ in internal}),
                'dynamic_internal_runtime_pairs': len(internal), 'dynamic_observed_internal_edges': len(dynamic),
                'dynamic_internal_trace_events': sum(n for _, _, n in internal),
                'modes': results, 'dynamically_observed_edges_removed_by_mlta': removed,
                'interpretation': 'Dynamic observations are a confirmed-positive lower bound. Static-only candidates are not false positives. Source matching can merge multiple IR callsites; covered records list all matching static IDs. Supply the exact unstripped traced binary; CSV alone cannot verify same-build provenance.'}
    output = args.output or args.static / 'comparison.json'
    # Exclusive creation, including when a custom destination is supplied.
    with output.open('x') as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(f'Dynamic observed internal edges: {len(dynamic)} ({len(internal)} runtime pairs)')
    for name, result in results.items():
        print(f"{name}: static candidates={result['static_candidates']}, dynamic covered={result['dynamic_covered']}, "
              f"missing={result['dynamic_missing']}, unresolved={result['dynamic_unresolved']}")
        for edge in result['missing']:
            print(f"  missing: {edge['caller_file']}:{edge['caller_line']} -> {edge['callee_function']}")
    print('Dynamically observed edges removed by MLTA (retained by FLTA or one-layer):')
    for edge in removed:
        print(f"  {edge['caller_file']}:{edge['caller_line']} -> {edge['callee_function']}")
    print(f'Comparison: {output}')


def docker_analyze(args):
    """Keep absolute database paths valid; users supply additional mounts for relocated builds."""
    repo = Path(__file__).resolve().parent
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    mounts = {str(repo): (str(repo), 'ro'),
              str(args.compdb.resolve().parent): (str(args.compdb.resolve().parent), 'ro'),
              str(args.output.resolve().parent): (str(args.output.resolve().parent), 'rw')}
    if args.project_root.is_dir() and str(args.project_root.resolve()) not in mounts:
        mounts[str(args.project_root.resolve())] = (str(args.project_root.resolve()), 'ro')
    for mount in args.mount:
        host, sep, container = mount.partition(':')
        if not sep or not Path(container).is_absolute():
            raise ValueError('--mount requires HOST:ABSOLUTE_CONTAINER_PATH')
        mounts[container] = (str(Path(host).resolve()), 'ro')
    command = ['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}']
    for target, (source, access) in mounts.items():
        command.extend(['--mount', f'type=bind,source={source},target={target}' + (',readonly' if access == 'ro' else '')])
    # Resolve user-relative CLI paths before switching into the container.
    forwarded = ['analyze', '--native', '--compdb', str(args.compdb.resolve()),
                 '--project-root', str(args.project_root.resolve()), '--output', str(args.output.resolve()),
                 '--clang', args.clang, '--clangxx', args.clangxx, '--kanalyzer', args.kanalyzer]
    for flag, values in (('--include-command', args.include_command), ('--exclude-command', args.exclude_command),
                         ('--compiler-wrapper', args.compiler_wrapper)):
        for value in values:
            forwarded.extend([flag, value])
    command.extend([args.image, 'python3', str(repo / 'typedive.py'), *forwarded])
    return subprocess.run(command).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    analyze_parser = sub.add_parser('analyze', help='compile typed-pointer IR and run all three TypeDive modes')
    analyze_parser.add_argument('--compdb', type=Path, required=True)
    analyze_parser.add_argument('--project-root', type=Path, required=True)
    analyze_parser.add_argument('--include-command', action='append', default=[], help='literal command/output substring; repeated rules use OR')
    analyze_parser.add_argument('--exclude-command', action='append', default=[], help='exclude any matching literal command/output substring')
    analyze_parser.add_argument('--compiler-wrapper', action='append', default=[], metavar='PATH=clang', help='explicit transparent wrapper alias; clang or clang++')
    analyze_parser.add_argument('--output', type=Path, required=True, help='new directory; existing paths are refused')
    analyze_parser.add_argument('--image', default='icallcov-typedive:llvm15')
    analyze_parser.add_argument('--mount', action='append', default=[], metavar='HOST:CONTAINER', help='extra read-only Docker mount (e.g. historical compdb paths)')
    analyze_parser.add_argument('--native', action='store_true', help='run installed tools directly, without Docker')
    analyze_parser.add_argument('--clang', default='/opt/llvm15-typedive/bin/clang')
    analyze_parser.add_argument('--clangxx', default='/opt/llvm15-typedive/bin/clang++')
    analyze_parser.add_argument('--kanalyzer', default='/opt/typedive/build/lib/kanalyzer')
    compare_parser = sub.add_parser('compare', help='compare static candidates with internal dynamic positives')
    compare_parser.add_argument('--static', type=Path, required=True)
    compare_parser.add_argument('--dynamic', type=Path, required=True)
    compare_parser.add_argument('--binary', type=Path, required=True)
    compare_parser.add_argument('--module', required=True)
    compare_parser.add_argument('--addr2line', default='addr2line')
    compare_parser.add_argument('--path-map', action='append', default=[], metavar='OLD=NEW', help='remap dynamic DWARF source roots to the static project root')
    compare_parser.add_argument('--output', type=Path, help='new JSON file; default STATIC/comparison.json')
    args = parser.parse_args(argv)
    try:
        if args.action == 'analyze':
            if not args.native:
                return docker_analyze(args)
            analyze(args)
        else:
            compare(args)
    except (OSError, ValueError, ScanError, subprocess.SubprocessError, KeyError, TypeError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
