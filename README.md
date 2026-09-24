# icallcov

## Overview

icallcov is a prototype tool for measuring indirect-call coverage in native programs by combining static binary discovery with dynamic test execution tracing.

**icallcov core is project-agnostic.** It works on any Linux x86-64 ELF C/C++ binary:

- statically discovers indirect callsites (`scan.py`),
- traces indirect calls executed under DynamoRIO in `fast` (callsite-only) or `edge` (callsite + observed target) mode,
- runs a test suite and aggregates per-process traces (`run_suite.py`),
- classifies callsites as project, test, runtime, or unknown, and reports coverage (`report.py`),
- can export the observed callsite → runtime-target edges it collected as machine-readable JSON.

It reports which statically discovered project indirect callsites were executed by a particular test run, and — in `edge` mode — which runtime targets were *observed* at each callsite.

**A covered callsite only means that the indirect-call instruction was executed at least once. Observed targets are runtime observations from the traced run(s), NOT a complete or legal target set** (i.e. not every target permitted by the program's semantics, calling convention, or CFI policy — only the ones actually seen).

Project-specific test discovery/execution is isolated behind a small **runner adapter** interface, so the tracing core never has to know how a particular project's tests are organized. See [Runner support](#runner-support) below.

## Motivation

Native programs use indirect calls for callbacks, function pointers, and other forms of dynamic dispatch. Identifying which of these instructions tests execute can help highlight unexercised project callsites. This prototype provides a starting point for that investigation; it does not establish test completeness or security guarantees.

## How It Works

1. `scan.py` invokes `objdump` to statically scan an ELF binary and discover x86-64 indirect callsites.
2. `run_suite.py` discovers tests via a runner adapter, runs each one under the DynamoRIO client (`-mode fast` or `-mode edge`), and aggregates the resulting per-process traces.
3. `report.py` compares static callsites against dynamically executed callsites using module names and offsets.
4. The report classifies callsites as project, test, runtime, or unknown.
5. Project callsites form the denominator for indirect-callsite test coverage:

   ```text
   coverage = executed project callsites / all discovered project callsites × 100%
   ```

6. When debug information is available, `addr2line` maps binary offsets back to source functions, files, and lines to support classification and inspection.
7. In `edge` mode, `report.py --show-targets`/`--export-edges` reports the runtime targets *observed* at each covered callsite, symbolized the same way.

**Classification is generic by default**: it uses runtime-name patterns (glibc/loader internals), `--project-root`/`--source-dir`/`--test-dir`, and a small path-based fallback (`test-*` filenames, `/test/`/`/tests/` directories). It does **not** classify arbitrary `uv_*`/`uv__*`-style names as project code unless you explicitly opt in with `--libuv-compat`. Review filtered callsites when interpreting results. If no project callsites are identified, the current report prints `0.0%`.

## Tracing Modes

The DynamoRIO client (`dynamorio/icall_trace.c`) supports two modes, selected with `-mode fast` or `-mode edge` (passed through by `run_suite.py --mode`):

- **`fast`** — callsite-only tracing. Each row records that a callsite executed, with `target_module`/`target_offset` recorded as the placeholder `<not-recorded>`. Lowest overhead; sufficient for callsite coverage but not target-set analysis.
- **`edge`** — callsite + observed target tracing. Each row also records the actual runtime target module/offset, enabling `report.py --show-targets`/`--export-edges`.

Both modes produce the same trace CSV shape (`caller_module,caller_offset,target_module,target_offset`), so `report.py` handles either transparently.

## Multi-Process Tracing and Suite Aggregation

Each traced process writes its own `dynamic.<pid>.csv` in its working directory (DynamoRIO instruments the process it's attached to, and per-PID files avoid clobbering when a test forks/execs helper or child processes). `run_suite.py`:

- runs one test at a time, collects all `dynamic.<pid>.csv` files written during that test, and merges them into a single per-test trace under `<output-dir>/traces/<test>.csv`,
- deletes the raw per-PID files after collecting them so the next test starts clean,
- appends every per-test row (tagged with `test_name`) into a suite-wide `<output-dir>/suite.csv`,
- writes one summary row per test (status, duration, trace/callsite counts, process-trace count) to `<output-dir>/summary.csv`,
- supports `--resume` to skip tests whose per-test CSV already exists (for re-running a suite after a partial failure).

`suite.csv` is what you normally pass to `report.py`; it is a superset of a single-test `dynamic.csv` with an added `test_name` column, and `report.py` uses that column (when present) to record which tests observed each edge in `--export-edges` output.

## Runner Support

`run_suite.py --runner {libuv,ctest,gtest,commands}` selects how tests are discovered and how the correct command to execute one test is produced. This is the only project-specific part of the tool; the DynamoRIO wrapping, tracing, per-PID aggregation, timeout handling, resume behavior, logging, and summary/suite generation are all generic and live in `run_suite.py` itself (see `runners/base.py` for the two-method `TestRunner` interface).

- **`libuv`** (default, for backward compatibility) — discovers tests via `<binary> --list` and runs each one as `<binary> TEST_NAME`. Preserved from the original prototype.
- **`gtest`** — for GoogleTest-compatible binaries. Discovers tests via `<binary> --gtest_list_tests` and runs each one as `<binary> --gtest_filter=Suite.Case`.
- **`commands`** — universal fallback for any project. Reads one test command per line from `--tests-file` (blank lines and `#` comments ignored; optional `NAME<TAB>COMMAND` form for explicit names; otherwise a deterministic name is derived from the command). Commands are parsed with `shlex.split`, not a shell.
- **`ctest`** — for CMake/CTest projects, given `--build-dir`. Test names come from `ctest --show-only=json-v1` (CMake/CTest ≥ 3.14 required), and **each test is executed by running its resolved `command` directly under DynamoRIO**, not by wrapping `ctest` itself — wrapping `ctest` does not reliably propagate instrumentation into the child test binary it spawns. **Limitation:** this only works when a test's resolved `command` is the real test executable (the common `add_test(... COMMAND <exe> ...)` case); if a project wraps tests in a launcher/emulator, or its CTest predates `--show-only=json-v1`, resolution fails with an explicit error instead of silently tracing the wrong process — use the `commands` runner with an explicit invocation of the real binary in that case.

icallcov does not claim to support every C/C++ test framework. For anything not listed above, use the `commands` runner, or add a new adapter implementing `runners/base.py:TestRunner` (discover_tests, command_for_test, and the optional working_dir_for_test).

## Requirements

- Linux on x86-64, with ELF binaries to analyze.
- Python 3 (the scripts use the standard library).
- GNU binutils: `objdump` and `addr2line`.
- A C compiler and CMake 3.14 or newer.
- DynamoRIO, installed separately, including its CMake package and `drmgr` extension.

See [REQUIREMENTS.md](REQUIREMENTS.md) for setup and path conventions. DynamoRIO is an external dependency and must not be vendored into this repository. The local `dynamorio/` directory contains only the icallcov client and its build configuration.

## Build

From the repository root, build the client:

```sh
cd dynamorio
mkdir -p build
cd build
cmake \
  -DDynamoRIO_DIR="$HOME/tools/dynamorio/build/cmake" \
  ..
cmake --build . -j
cd ../..
```

These paths illustrate an external DynamoRIO installation under `$HOME/tools/dynamorio`. Adjust `DynamoRIO_DIR` to the directory containing your installation's `DynamoRIOConfig.cmake`; installation layouts vary. The commands below assume the client was built as `dynamorio/build/libicall_trace.so`.

## Usage

Run the following from the icallcov repository root, replacing paths for your project. Use the **same binary build** for scanning, execution, and symbolization.

```sh
python3 scan.py /path/to/project/build/test_binary -o static.json

python3 run_suite.py \
  --runner commands \
  --tests-file my_tests.txt \
  --mode edge \
  --output-dir coverage/edge

python3 report.py static.json coverage/edge/suite.csv \
  --binary /path/to/project/build/test_binary \
  --project-root /path/to/project \
  --source-dir src \
  --test-dir test \
  --show-filtered \
  --show-covered --show-targets \
  --export-edges coverage/edge/observed_edges.json
```

Swap `--runner commands --tests-file my_tests.txt` for `--runner libuv --binary ...`, `--runner gtest --binary ...`, or `--runner ctest --build-dir ...` depending on your project's test system (see [Runner Support](#runner-support)). `run_suite.py --mode {fast,edge}` selects the tracing mode (default `fast`); pass the same mode's `suite.csv` to `report.py`.

`--source-dir` and `--test-dir` can each be repeated; relative directories are resolved against `--project-root`. Their defaults are `src` and `test`. `--export-edges PATH` writes one JSON record per project callsite with its status (`observed`/`unobserved`) and, in `edge` mode, its **observed** (not complete/legal) runtime targets, hit counts, and the tests that observed them. Run `python3 scan.py --help`, `python3 run_suite.py --help`, or `python3 report.py --help` for the full option list.

## Example: libuv

See [examples/libuv.md](examples/libuv.md) for a debug build and a `run_suite.py --runner libuv` run of the `timer` test. The supplied prototype observation was 11 covered project callsites out of 62 (17.7%). This is environment/test-specific, **not whole-libuv test-suite coverage**.

## Output

- `static.json` (or the filename supplied with `-o`): binary path, module name, callsite count, and indirect-callsite records containing module, numeric offset, hexadecimal offset, and instruction text. Without `-o`, the scanner prints a summary but does not save JSON.
- `<output-dir>/traces/<test>.csv`, `<output-dir>/suite.csv`: one row per recorded indirect-call event, with this header (`suite.csv` adds a leading `test_name` column):

  ```csv
  caller_module,caller_offset,target_module,target_offset
  ```

  In `fast` mode, `target_module`/`target_offset` are the placeholder `<not-recorded>`/`0x0`; in `edge` mode they are the actual observed runtime target.

- `<output-dir>/summary.csv`: one row per test with status, return code, duration, trace-event/unique-callsite counts, and per-process trace count.
- Report on standard output: raw callsite count, category counts, project coverage, and uncovered project callsites. Optional flags show covered sites, observed target hit counts, and filtered categories. For a saved report, redirect standard output to `report.txt`.
- `--export-edges`: a JSON file with one record per project callsite (`caller_module`, `caller_offset`, `caller_function`, `caller_location`, `instruction`, `status`, `observed_target_count`, `targets`); each target has `target_module`, `target_offset`, `target_function`, `target_location`, `total_hits`, and the `tests` that observed it.

**Observed target hit counts and the `--export-edges` output describe only what the traced run(s) executed. They are not a complete or legal target set** — full legal-target / indirect-edge coverage analysis (e.g. comparing against a CFI policy) is future work.

## Current Limitations

- The prototype targets Linux x86-64 ELF binaries and depends on the instruction text emitted by `objdump`; it is not a complete binary-analysis framework.
- Callsite coverage measures execution of the instruction at least once, not coverage of all legal targets or indirect edges. Observed dynamic targets are not a legal/complete target set.
- Classification is heuristic and depends on debug information and directory settings. Missing or incomplete symbols can leave sites unknown or misclassified; unknown sites are excluded from project coverage. The `--libuv-compat` fallback is libuv-specific and off by default.
- Static addresses come directly from `objdump`, while dynamic offsets are relative to loaded module bases. The prototype does not normalize all ELF layouts (for example, non-PIE executables with a nonzero image base); verify address correspondence before interpreting coverage.
- Module matching uses basenames, which can be ambiguous. A scan/report invocation corresponds to one main analyzed executable; observed dynamic edges into shared libraries may still be recorded, but whole-program shared-library static scanning is out of scope for now.
- The `ctest` runner depends on `ctest --show-only=json-v1` resolving each test's real executable command; it does not work for CTest configurations that wrap tests in a launcher/emulator or predate that CMake version (see [Runner Support](#runner-support)).
- Results depend on the binary build and the exact tests executed. They do not prove correctness, security, or complete test coverage.

## Roadmap

Future work, not current capabilities:

- Model legal targets and compare them with observed callsite-to-target edges.
- Improve address normalization and module identity across binary layouts.
- Add more runner adapters, and a more robust CTest executable-resolution strategy.
- Evaluate additional projects and document reproducible experiments.

## Repository Structure

```text
icallcov/
├── scan.py
├── run_suite.py
├── report.py
├── runners/
│   ├── base.py
│   ├── libuv.py
│   ├── gtest.py
│   ├── ctest.py
│   └── commands.py
├── dynamorio/
│   ├── icall_trace.c
│   └── CMakeLists.txt
├── examples/
│   └── libuv.md
├── README.md
├── REQUIREMENTS.md
├── LICENSE
└── .gitignore
```

icallcov is distributed under the [MIT License](LICENSE). External dependencies retain their own licenses.
