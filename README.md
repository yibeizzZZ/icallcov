# icallcov

## Overview

icallcov is a prototype tool for measuring indirect-call coverage in native programs by combining static binary discovery with dynamic test execution tracing.

**icallcov core is project-agnostic.** The supported workflow is a single main Linux x86-64 ELF executable, built as PIE or non-PIE, with tests that invoke native executables directly. Debug information is strongly recommended for source classification and symbolization. Within that scope, it:

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

1. `scan.py` invokes `objdump` to statically scan an ELF binary and discover x86-64 indirect callsites, including prefixed forms such as `notrack call` and `bnd call`. Offsets refer to the start of the full instruction, including its prefixes.
2. `run_suite.py` discovers tests via a runner adapter, runs each one under the DynamoRIO client (`-mode fast` or `-mode edge`), and aggregates the resulting per-process traces.
3. `report.py` compares static callsites against dynamically executed callsites using module names and offsets.
4. The report classifies callsites as project, test, runtime, or unknown.
5. Project callsites form the denominator for indirect-callsite test coverage:

   ```text
   coverage = executed project callsites / all discovered project callsites × 100%
   ```

6. When debug information is available, `addr2line` maps ELF virtual addresses back to source functions, files, and lines to support classification and inspection.
7. In `edge` mode, `report.py --show-targets`/`--export-edges` reports the runtime targets *observed* at each covered callsite, symbolized the same way.

**Classification is generic by default**: explicit `--project-root`/`--source-dir`/`--test-dir` matches take precedence over runtime-name heuristics. Outside those directories, it checks runtime symbols and a small path-based fallback (`test-*` filenames, `/test/`/`/tests/` directories). `_start` is an exact symbol match; a project function named `worker_start` is not excluded just because its name contains `_start`. It does **not** classify arbitrary `uv_*`/`uv__*`-style names as project code unless you explicitly opt in with `--libuv-compat`. Review filtered callsites when interpreting results. If no project callsites are identified, the report prints `N/A`, exports `coverage: null` and `coverage_status: "indeterminate"`, and exits with status 2 after writing diagnostic outputs. Check debug information and source/test classification paths; a binary with no project indirect calls also has an undefined denominator. A valid nonzero denominator with no executed sites still reports `0.0%`.

### Address coordinates

Static callsites and dynamic callers/targets use the same **module-relative offset**. For Linux x86-64 ELF, the image base is the lowest `PT_LOAD` virtual address rounded down to a 4096-byte page. `scan.py` subtracts that base from the virtual addresses printed by `objdump`; the tracer records `PC - module start`. These coordinates match for both PIE and non-PIE binaries. Before calling `addr2line`, the report adds the ELF image base back to main-binary caller and target offsets.

Static JSON records `"address_coordinate": "module-relative"` and an integer `"elf_image_base"`. Older static JSON lacks this metadata and is rejected with an instruction to regenerate it using `scan.py`. Regenerate scans after upgrading, and always scan, trace, and symbolize the same binary build. A report also rejects a supplied binary whose ELF image base differs from the scan metadata; this check is not a complete binary identity check.

## Tracing Modes

The DynamoRIO client (`dynamorio/icall_trace.c`) supports two modes, selected with `-mode fast` or `-mode edge` (passed through by `run_suite.py --mode`):

- **`fast`** — callsite-only tracing. Each row records that a callsite executed, with `target_module`/`target_offset` recorded as the placeholder `<not-recorded>`. Lowest overhead; sufficient for callsite coverage but not target-set analysis.
- **`edge`** — callsite + observed target tracing. Each row also records the actual runtime target module/offset, enabling `report.py --show-targets`/`--export-edges`.

Both modes produce the same trace CSV shape (`caller_module,caller_offset,target_module,target_offset`), so `report.py` handles either transparently. Module names containing CSV delimiters or quotes are escaped, including executable names containing commas.

## Multi-Process Tracing and Suite Aggregation

Each traced process writes `dynamic.<pid>.csv` in its working directory. Separate PIDs have separate files; an `exec` that retains its PID can overwrite an earlier trace (see [Current Limitations](#current-limitations)). `run_suite.py`:

- runs one test at a time under an advisory lock for its working directory, and stops its process group before collecting traces, including after timeouts,
- temporarily sets aside preexisting `dynamic.<pid>.csv` files, collects the new traces into `<output-dir>/traces/<stem>.csv`, and restores the original files after cleanup (including when a PID filename is reused),
- deletes only the newly collected raw per-PID files,
- appends every per-test row (tagged with `test_name`) into a suite-wide `<output-dir>/suite.csv`,
- writes one summary row per test (status, duration, trace/callsite counts, process-trace count, and a `resumed` flag) to `<output-dir>/summary.csv`,
- writes per-test JSON metadata alongside each trace, and supports `--resume` only for matching, previously passed results with an intact trace,
- exits with a nonzero status if any test fails or times out,
- exits nonzero when filtering selects no tests, removing previous `suite.csv` and `summary.csv` aggregates while preserving per-test traces, metadata, and logs for later reuse.

The per-test filename stem combines a readable, sanitized test-name prefix with the first 20 hexadecimal characters of the test name's SHA-256 digest. Names such as `A/B` and `A:B` therefore have distinct artifacts even if their readable prefixes match.

Resume validates the trace hash and saved execution identity: runner, tracing mode, test, command arguments, executable path/content, working directory, client and `drrun` paths/content, environment digest, and timeout. A resumed result keeps its original `passed` status and sets `resumed` to true. Failed or timed-out tests, changed inputs, missing metadata, and damaged traces rerun. Older filename-only caches are not sufficient for resume. This does not fingerprint arbitrary input files, shared libraries, or other external state used by a test; rerun without `--resume` when those change.

`suite.csv` is what you normally pass to `report.py`; it is a superset of a single-test `dynamic.csv` with an added `test_name` column, and `report.py` uses that column (when present) to record which tests observed each edge in `--export-edges` output.

If a process trace is malformed or truncated, the suite excludes that entire
process trace and retains valid traces from other processes. The test log keeps
stdout/stderr, elapsed time, and the execution result, followed by `TRACE ERROR`
diagnostics. A timeout or failed execution keeps its original status; a process
that exits successfully but produces a damaged trace is marked `runner-error`.
Coverage from such a run is partial, and the suite exits nonzero.

## Runner Support

`run_suite.py --runner {libuv,ctest,gtest,commands}` selects how tests are discovered and how the correct command to execute one test is produced. This is the only project-specific part of the tool; the DynamoRIO wrapping, tracing, per-PID aggregation, timeout handling, resume behavior, logging, and summary/suite generation are all generic and live in `run_suite.py` itself (see `runners/base.py` for the two-method `TestRunner` interface).

- **`libuv`** (default, for backward compatibility) — discovers tests via `<binary> --list` and runs each one as `<binary> TEST_NAME`. Preserved from the original prototype.
- **`gtest`** — for GoogleTest-compatible binaries. Discovers tests via `<binary> --gtest_list_tests` and runs each one as `<binary> --gtest_filter=Suite.Case`.
- **`commands`** — explicit native test invocations. Reads one test command per line from `--tests-file` (blank lines and `#` comments ignored; optional `NAME<TAB>COMMAND` form for explicit names; otherwise a deterministic name is derived from the command). Commands are parsed with `shlex.split`, not a shell.
- **`ctest`** — for CMake/CTest projects, given `--build-dir`. Test names and commands come from `ctest --show-only=json-v1` (CMake/CTest ≥ 3.14 required). The adapter executes each command directly under DynamoRIO and honors its working directory. This supports simple `add_test(... COMMAND <exe> ...)` cases. It does not reproduce CTest's full execution semantics, including fixtures, dependencies, environment properties, or expected-failure rules. It does not reliably detect launchers/emulators; inspect the resolved command and use `commands` with an explicit native executable when needed.

icallcov does not claim to support every C/C++ test framework. For anything not listed above, use the `commands` runner, or add a new adapter implementing `runners/base.py:TestRunner` (discover_tests, command_for_test, and the optional working_dir_for_test).

## Requirements

- Linux on x86-64, with one main ELF executable to analyze (PIE or non-PIE).
- Debug information strongly recommended; tests should invoke native executables directly.
- Python 3.8 or newer (the scripts use the standard library).
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

For projects with multiple statically linked test executables, use
`python3 run_project.py project.json --mode edge --output-dir coverage/project`.
Each manifest target gets its own scan, suite trace, and report. The root
`summary.csv`/`summary.json` lists per-binary coverage without merging offsets
or claiming unique library-wide coverage. See the [libpng multi-binary
example](examples/libpng.md) for the manifest contract, local smoke configuration
commands, output layout, and limitations. Existing single-binary commands
remain unchanged. `report.py --export-summary FILE` optionally exports the
coverage counts for one binary as JSON.

See [examples/libuv.md](examples/libuv.md) for a debug build and a `run_suite.py --runner libuv` run of the `timer` test. The supplied prototype observation was 11 covered project callsites out of 62 (17.7%). This is environment/test-specific, **not whole-libuv test-suite coverage**.

## Output

- `static.json` (or the filename supplied with `-o`): binary path, module name, `address_coordinate`, `elf_image_base`, callsite count, and indirect-callsite records containing module, numeric module-relative offset, hexadecimal offset, and instruction text. Without `-o`, the scanner prints a summary but does not save JSON.
- `<output-dir>/traces/<stem>.csv`, `<output-dir>/suite.csv`: covered callsite rows in fast mode (duplicates are possible), or executed callsite-to-target events in edge mode, with this header (`suite.csv` adds a leading `test_name` column):

  ```csv
  caller_module,caller_offset,target_module,target_offset
  ```

  In `fast` mode, `target_module`/`target_offset` are the placeholder `<not-recorded>`/`0x0`; in `edge` mode they are the actual observed runtime target.

- `<output-dir>/traces/<stem>.json`: execution identity, trace hash, and result details used to validate resume; it shares its filename stem with the corresponding CSV.
- `<output-dir>/summary.csv`: one row per test with status, return code, duration, trace-event/unique-callsite counts, per-process trace count, and `resumed`. Resuming a passed test preserves its `passed` status.
- Report on standard output: raw callsite count, category counts, project coverage, and uncovered project callsites. Optional flags show covered sites, observed target hit counts, and filtered categories. For a saved report, redirect standard output to `report.txt`.
- `--export-edges`: a JSON file with one record per project callsite (`caller_module`, `caller_offset`, `caller_function`, `caller_location`, `instruction`, `status`, `observed_target_count`, `targets`); each target has `target_module`, `target_offset`, `target_function`, `target_location`, `total_hits`, and the `tests` that observed it.

**Observed target hit counts and the `--export-edges` output describe only what the traced run(s) executed. They are not a complete or legal target set** — full legal-target / indirect-edge coverage analysis (e.g. comparing against a CFI policy) is future work.

## Optional LLVM IR Scanner

An independent [LLVM IR callsite scanner](llvm/README.md) reads `.ll`/`.bc` using `CallBase::isIndirectCall()` and emits artifact-local unique IDs, owning functions, debug source locations, and an explicit `not_analyzed` target-analysis placeholder. It requires LLVM 21.1.x and builds separately. See the linked guide for commands and the sample input.

The existing binary scanner and coverage workflow remain unchanged. IR output is not accepted by `report.py`: static callee estimation and IR-to-runtime address mapping are not implemented yet.

## Current Limitations

- The prototype supports one main Linux x86-64 ELF executable (PIE or non-PIE), simple native test execution, and the instruction text emitted by `objdump`; it is not a complete binary-analysis framework.
- Callsite coverage measures execution of the instruction at least once, not coverage of all legal targets or indirect edges. Observed dynamic targets are not a legal/complete target set.
- Classification is heuristic and depends on debug information and directory settings. Missing or incomplete symbols can leave sites unknown or misclassified; unknown sites are excluded from project coverage. The `--libuv-compat` fallback is libuv-specific and off by default.
- **Shared libraries and module identity:** module matching uses basenames, while runtime preferred names can differ from on-disk names, including SONAME differences. Unloading a library with `dlclose` can also prevent reliable attribution of buffered addresses. Shared-library targets may be recorded, but complete shared-library coverage, unload-safe attribution, and robust module identity are outside the supported scope.
- **Same-PID `exec`:** a replacement process image can reopen and overwrite `dynamic.<pid>.csv`, losing events from before `exec`. Per-PID aggregation does not solve this.
- **Complex CTest execution:** the adapter directly executes discovered commands; it does not implement CTest fixtures, dependencies, environment properties, expected-failure semantics, or reliable launcher/emulator detection. Use only simple native commands whose behavior does not depend on those features (see [Runner Support](#runner-support)).
- **Process and trace ownership:** cleanup covers the test's process group. Descendants that detach or escape with `setsid` are unsupported. The working-directory lock coordinates cooperating suite runs; concurrent external tracers that ignore it cannot safely share that directory. Preexisting trace files are excluded from collection and cleanup.
- **Resume scope:** execution identity and trace hashes protect against stale or corrupted cached results, but do not cover changes to arbitrary test data, loaded dependencies, or external services.
- Results depend on the binary build and the exact tests executed. They do not prove correctness, security, or complete test coverage.

## Roadmap

Future work, not current capabilities:

- Model legal targets and compare them with observed callsite-to-target edges.
- Improve shared-library module identity, unload handling, and same-PID `exec` trace preservation.
- Add more runner adapters, and a more robust CTest executable-resolution strategy.
- Evaluate additional projects and document reproducible experiments.

## Repository Structure

```text
icallcov/
├── scan.py
├── elf_addresses.py
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
│   ├── trace_common.c / trace_common.h
│   ├── fast_trace.c / fast_trace.h
│   ├── edge_trace.c / edge_trace.h
│   ├── tests/
│   └── CMakeLists.txt
├── tests/
│   ├── test_addresses.py
│   ├── test_suite.py
│   └── test_llvm_scanner.py
├── llvm/
│   ├── icall_ir_scan.cpp
│   ├── CMakeLists.txt
│   ├── README.md
│   └── tests/callsites.ll
├── examples/
│   ├── libuv.md
│   └── ir_callbacks.c
├── README.md
├── REQUIREMENTS.md
├── LICENSE
└── .gitignore
```

icallcov is distributed under the [MIT License](LICENSE). External dependencies retain their own licenses.
