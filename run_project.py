#!/usr/bin/env python3
"""Compose existing scan/suite/report CLIs for independent native binaries."""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from runners.commands import CommandsRunner
from run_suite import default_drrun, resolve_executable, resolve_path

SCRIPTS = Path(__file__).resolve().parent


def load_manifest(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('targets'), list) or not data['targets']:
        raise ValueError('manifest must contain a nonempty targets list')

    def manifest_path(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('manifest paths must be nonempty strings')
        result = Path(value).expanduser()
        return (path.parent / result).resolve()

    config = {'project_root': manifest_path(data.get('project_root', '.'))}
    for field, default in (('source_dirs', ['src']), ('test_dirs', ['test'])):
        values = data.get(field, default)
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f'{field} must be a nonempty list of paths')
        config[field] = values

    names = {'summary.json', 'summary.csv'}
    targets = []
    for entry in data['targets']:
        if not isinstance(entry, dict):
            raise ValueError('each target must be an object')
        name = entry.get('name')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                or name.casefold() in names):
            raise ValueError('target names must be unique safe directory names')
        names.add(name.casefold())
        target = {'name': name}
        for field in ('binary', 'tests_file'):
            target[field] = manifest_path(entry.get(field))
            if not target[field].is_file():
                raise ValueError(f'{name}: {field} not found: {target[field]}')
        target['cwd'] = manifest_path(entry.get('cwd', '.'))
        if not target['cwd'].is_dir():
            raise ValueError(f'{name}: cwd must be an existing directory')
        runner = CommandsRunner(target['tests_file'])
        for test in runner.discover_tests():
            executable = resolve_executable(runner.command_for_test(test), target['cwd']).resolve()
            if executable != target['binary']:
                raise ValueError(f'{name}: test {test["name"]!r} executes {executable}, '
                                 f'expected {target["binary"]}; use direct native commands')
        targets.append(target)
    config['targets'] = targets
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output-dir', default='coverage/project')
    parser.add_argument('--drrun', default=str(default_drrun()))
    parser.add_argument('--client', default=str(SCRIPTS / 'dynamorio/build/libicall_trace.so'))
    parser.add_argument('--mode', choices=('fast', 'edge'), default='fast')
    parser.add_argument('--timeout', type=float, default=60.0)
    args = parser.parse_args(argv)
    try:
        config = load_manifest(args.manifest)
        if not math.isfinite(args.timeout):
            raise ValueError('timeout must be finite')
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))

    output = resolve_path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for target in config['targets']:
        name = target['name']
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        row = {'target': name, 'binary': str(target['binary']), 'status': 'error',
               'raw_static': None, 'static': None, 'covered': None,
               'uncovered': None, 'coverage': None, 'coverage_status': None, 'error': ''}
        print(f'\nTarget: {name} ({target["binary"]})', flush=True)
        # Never consume a summary left behind by a previous failed invocation.
        summary = directory / 'coverage.json'
        summary.unlink(missing_ok=True)

        def invoke(script, arguments, log):
            with (directory / log).open('w', encoding='utf-8') as stream:
                return subprocess.run([sys.executable, str(SCRIPTS / script),
                                       *map(str, arguments)], stdout=stream,
                                      stderr=subprocess.STDOUT, check=False).returncode

        try:
            static = directory / 'static.json'
            if invoke('scan.py', [target['binary'], '-o', static], 'scan.log'):
                raise RuntimeError('scan failed; see scan.log')
            suite = directory / 'suite'
            # Avoid reporting a stale trace if the suite fails before writing output.
            (suite / 'suite.csv').unlink(missing_ok=True)
            suite_status = invoke('run_suite.py', [
                '--runner', 'commands', '--tests-file', target['tests_file'],
                '--cwd', target['cwd'], '--drrun', resolve_path(args.drrun),
                '--client', resolve_path(args.client), '--mode', args.mode,
                '--timeout', args.timeout, '--output-dir', suite], 'suite.log')
            if not (suite / 'suite.csv').is_file():
                raise RuntimeError('suite produced no trace; see suite.log')
            report_args = [static, suite / 'suite.csv', '--binary', target['binary'],
                           '--project-root', config['project_root'], '--show-filtered',
                           '--show-covered', '--export-summary', summary]
            for field, flag in (('source_dirs', '--source-dir'), ('test_dirs', '--test-dir')):
                for value in config[field]:
                    report_args.extend([flag, value])
            if args.mode == 'edge':
                report_args.extend(['--show-targets', '--export-edges', directory / 'observed_edges.json'])
            report_status = invoke('report.py', report_args, 'report.txt')
            if report_status not in (0, 2) or not summary.is_file():
                raise RuntimeError('report failed; see report.txt')
            row.update(json.loads(summary.read_text(encoding='utf-8')))
            if report_status == 2 and row['coverage_status'] != 'indeterminate':
                raise RuntimeError('report failed; see report.txt')
            row['status'] = 'failed' if suite_status else 'passed'
            if suite_status:
                row['error'] = 'tests failed; coverage may be partial; see suite/summary.csv'
            if row['coverage_status'] == 'indeterminate':
                if not suite_status:
                    row['status'] = 'indeterminate'
                row['error'] = (row['error'] + '; ' if row['error'] else '') + (
                    'no project callsites; check source paths/debug information in report.txt')
        except (OSError, ValueError, RuntimeError) as error:
            row['error'] = str(error)
            row['status'] = 'error'
        rows.append(row)
        # Persist progress even if a later target is interrupted.
        (output / 'summary.json').write_text(json.dumps(rows, indent=2) + '\n', encoding='utf-8')
        with (output / 'summary.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        coverage_text = 'N/A' if row['coverage'] is None else f'{row["coverage"]:.2f}%'
        print(f'{name}: {row["status"]}, static={row["static"]}, '
              f'covered={row["covered"]}, uncovered={row["uncovered"]}, '
              f'coverage={coverage_text}', flush=True)
        if row['error']:
            print(row['error'], file=sys.stderr)
    print(f'\nPer-binary summary: {output / "summary.csv"}')
    return int(any(row['status'] != 'passed' for row in rows))


if __name__ == '__main__':
    sys.exit(main())
