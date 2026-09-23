# icallcov

## Overview

icallcov is a prototype tool for measuring indirect-call coverage in native programs by combining static binary discovery with dynamic test execution tracing.

The current early prototype targets Linux x86-64 ELF binaries. It reports which statically discovered project indirect callsites were executed by a particular test run.

**A covered callsite only means that the indirect-call instruction was executed at least once. It does NOT mean that all possible call targets were exercised.**

## Motivation

Native programs use indirect calls for callbacks, function pointers, and other forms of dynamic dispatch. Identifying which of these instructions tests execute can help highlight unexercised project callsites. This prototype provides a starting point for that investigation; it does not establish test completeness or security guarantees.

## How It Works

1. `scan.py` invokes `objdump` to statically scan an ELF binary and discover x86-64 indirect callsites.
2. A DynamoRIO client records indirect calls executed while running tests, including their observed targets.
3. `report.py` compares static callsites against dynamically executed callsites using module names and offsets.
4. The report classifies callsites as project, test, runtime, or unknown.
5. Project callsites form the denominator for indirect-callsite test coverage:

   ```text
   coverage = executed project callsites / all discovered project callsites × 100%
   ```

6. When debug information is available, `addr2line` maps binary offsets back to source functions, files, and lines to support classification and inspection.

Classification uses runtime-name patterns, source/test directories, and fallback heuristics (including libuv function names). Review filtered callsites when interpreting results. If no project callsites are identified, the current report prints `0.0%`.

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

Run the following from the icallcov repository root, replacing paths and test arguments for your project. Use the **same binary build** for scanning, execution, and symbolization.

```sh
python3 scan.py /path/to/project/build/test_binary -o static.json

"$HOME/tools/dynamorio/build/bin64/drrun" \
  -c ./dynamorio/build/libicall_trace.so \
  -- /path/to/project/build/test_binary test_arguments

python3 report.py static.json dynamic.csv \
  --binary /path/to/project/build/test_binary \
  --project-root /path/to/project \
  --source-dir src \
  --test-dir test \
  --show-filtered
```

Adjust the `drrun` path for your DynamoRIO installation. The tracer writes `dynamic.csv` in the process working directory and **overwrites it on each run**. Save it elsewhere before another run if you need to retain it. The prototype does not automatically aggregate multiple test runs.

`--source-dir` and `--test-dir` can each be repeated; relative directories are resolved against `--project-root`. Their defaults are `src` and `test`. To inspect covered callsites and observed targets, add both `--show-covered --show-targets` to the report command. Run `python3 scan.py --help` or `python3 report.py --help` for the available options.

## Example: libuv

See [examples/libuv.md](examples/libuv.md) for a debug build and a single `timer timer` test run. The supplied prototype observation was 11 covered project callsites out of 62 (17.7%). This is environment/test-specific, **not whole-libuv test-suite coverage**.

## Output

- `static.json` (or the filename supplied with `-o`): binary path, module name, callsite count, and indirect-callsite records containing module, numeric offset, hexadecimal offset, and instruction text. Without `-o`, the scanner prints a summary but does not save JSON.
- `dynamic.csv`: one row per recorded indirect-call event, with this header:

  ```csv
  caller_module,caller_offset,target_module,target_offset
  ```

- Report on standard output: raw callsite count, category counts, project coverage, and uncovered project callsites. Optional flags show covered sites, observed target hit counts, and filtered categories. For a saved report, redirect standard output to `report.txt`.

Callsite-to-target information is already collected dynamically. Observed target hit counts describe this execution only; full legal-target / indirect-edge coverage analysis is future work.

## Current Limitations

- The prototype targets Linux x86-64 ELF binaries and depends on the instruction text emitted by `objdump`; it is not a complete binary-analysis framework.
- Callsite coverage measures execution of the instruction at least once, not coverage of all legal targets or indirect edges.
- Classification is heuristic and depends on debug information and directory settings. Missing or incomplete symbols can leave sites unknown or misclassified; unknown sites are excluded from project coverage.
- Static addresses come directly from `objdump`, while dynamic offsets are relative to loaded module bases. The prototype does not normalize all ELF layouts (for example, non-PIE executables with a nonzero image base); verify address correspondence before interpreting coverage.
- Module matching uses basenames, which can be ambiguous. A scan covers one binary, not automatically all its dependent libraries.
- Tracing adds overhead and can produce large logs. The fixed output filename is unsuitable for concurrent runs in the same directory; multi-process tracing and suite aggregation need further work.
- Results depend on the binary build and the exact tests executed. They do not prove correctness, security, or complete test coverage.

## Roadmap

Future work, not current capabilities:

- Model legal targets and compare them with observed callsite-to-target edges.
- Improve address normalization and module identity across binary layouts.
- Broaden and validate classification beyond the libuv-oriented fallbacks.
- Add configurable trace output and reliable aggregation across tests/processes.
- Evaluate additional projects and document reproducible experiments.

## Repository Structure

```text
icallcov/
├── scan.py
├── report.py
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
