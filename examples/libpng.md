# Multi-binary libpng coverage

`run_project.py` composes the existing scanner, commands runner, and reporter.
It scans each target binary, runs that target's native commands under DynamoRIO,
and reports that binary independently. No cross-binary offsets are merged.
The existing single-binary CLIs and libuv workflow are unchanged.

## Run a local example

This example assumes adjacent `icallcov/` and `libpng/` checkouts, with the
existing static libpng build in `libpng/build-static`. The `pngvalid` and
`pngtest` executables must contain libpng code and debug information. Build the
icallcov DynamoRIO client as described in the main README first.

The `examples/libpng/` directory is ignored by Git: manifests and command lists
there are local experiment configuration. If it already exists in your workspace,
run the following from the icallcov directory:

```sh
python3 run_project.py examples/libpng/project.json \
  --mode edge --timeout 300 \
  --output-dir coverage/libpng-project
```

The local manifest and two command files run **two smoke cases, not the complete
libpng test suite**:
`pngvalid --strict --standard` and `pngtest --strict ../pngtest.png`.

This workspace's local `project_root` is `/mnt/d/SRE_Bench_research/libpng`, matching the
source paths in this workspace's existing debug binaries. On another machine,
set it to the source root recorded in your binaries' debug information. Path
classification is case-sensitive: `/mnt/d/sre_bench_research` does not match
`/mnt/d/SRE_Bench_research`, even on a mount that accepts both spellings. If the
report shows zero project sites, inspect the unknown source locations before
interpreting coverage. Binary/tests/cwd paths remain relative to the manifest.

To extend coverage, add native invocations to those files and additional target
entries for `pngstest`, `pngunknown`, `pngimage`, and `pnggetset`. Each target needs
its own commands file. Expand image globs into explicit arguments: the commands
runner uses `shlex`, not a shell. Libpng's shell tests also contain loops,
expected failures and output checks; these cannot be reproduced by simply
passing their script paths to this runner.

## Manifest contract

For a fresh checkout, create `examples/libpng/project.json` using the following
manifest and replace `/path/to/libpng` with your source/build location. Also
create these two command files (each line below is a native command):

`examples/libpng/pngvalid-tests.txt`:

```text
./pngvalid --strict --standard
```

`examples/libpng/pngtest-tests.txt`:

```text
./pngtest --strict ../pngtest.png
```

The parent directory can be created with `mkdir -p examples/libpng`.

```json
{
  "project_root": "/path/to/libpng",
  "source_dirs": ["."],
  "test_dirs": ["contrib", "tests", "pngtest.c"],
  "targets": [
    {
      "name": "pngvalid",
      "binary": "/path/to/libpng/build-static/pngvalid",
      "tests_file": "pngvalid-tests.txt",
      "cwd": "/path/to/libpng/build-static"
    },
    {
      "name": "pngtest",
      "binary": "/path/to/libpng/build-static/pngtest",
      "tests_file": "pngtest-tests.txt",
      "cwd": "/path/to/libpng/build-static"
    }
  ]
}
```

- `targets` is nonempty. Names must be unique (case-insensitively) and consist of
  letters, digits, `_`, `-`, or `.`, starting with a letter or digit.
  `summary.json` and `summary.csv` are reserved names.
- `binary` and `tests_file` are required. All manifest file paths, including
  `project_root` and `cwd`, are relative to the manifest directory, not the shell.
  `project_root` and each target's `cwd` default to that directory.
- `source_dirs` and `test_dirs` are relative to `project_root`; defaults are
  `["src"]` and `["test"]`. Explicit test paths take precedence. `pngtest.c`
  is excluded explicitly because it lives beside the library sources.
- Commands keep the existing commands-runner format (`NAME<TAB>COMMAND` or
  just `COMMAND`). Relative executable and argument paths are relative to `cwd`.
  Each command's executable must resolve to that target's binary. Wrappers are
  rejected before any targets run. Working directories must already exist.
- `--mode fast` (default) records coverage only; `--mode edge` also records
  observed targets. `--timeout` is per test, default 60 seconds, <= 0 disables it.
  `--drrun` and `--client` override tool paths; CLI paths use the shell directory.

## Outputs

```text
coverage/libpng-project/
  summary.csv                 # one row per target, with binary path and status
  summary.json                # same rows, machine-readable
  pngvalid/
    static.json
    scan.log
    suite.log
    suite/
      suite.csv
      summary.csv
      logs/...
      traces/...              # per-test CSV and execution metadata
    report.txt
    coverage.json
    observed_edges.json       # edge mode only
  pngtest/
    ...                       # same layout
```

Summary columns `static`, `covered`, `uncovered`, `coverage` use **project-classified
callsites**; `raw_static` also counts excluded test/runtime/unknown callsites.
No project-wide percentage is calculated: static linking duplicates library
code, and averaging or summing binary counts is not unique library coverage.
Even binaries with identical basenames remain in separate target directories
and summary rows. Within each report identity remains `(module, offset)`.

Scan/report errors mark the target `error`; test failures mark it `failed` and
may still yield partial coverage. Remaining targets are attempted, and the
command exits nonzero if any target fails. Inspect suite summaries and logs,
not just coverage percentages. Reruns execute all tests; project-level resume
and target selection are not implemented. Use a new output directory for a new
manifest/mode to avoid confusing old artifacts from targets no longer selected.
Do not rebuild binaries or edit manifests/command files during a run.

The immediate implementation supports native test executables with statically
linked project code. Shared-library analysis is not implemented: basename/SONAME
identity and unload handling remain existing tracer limitations. The manifest
keeps the analyzed `binary` separate from `tests_file` so a future shared-library
mode can relax command ownership validation and analyze a single library module
without changing the current per-binary address model.
