# Example: libuv

This example documents the current prototype workflow for one libuv test. It assumes the [requirements](../REQUIREMENTS.md) are installed and the icallcov DynamoRIO client has been built. libuv and DynamoRIO remain external to this repository.

Replace `/path/to/libuv` and `/path/to/icallcov` with your local checkout paths. Adjust the DynamoRIO paths for your installation. The example assumes the libuv build produces `build-debug/uv_run_tests_a`; test targets and their locations can vary with the libuv version and build configuration.

## 1. Build libuv with debug information

```sh
cd /path/to/libuv

cmake -B build-debug \
  -DCMAKE_BUILD_TYPE=Debug

cmake --build build-debug -j
```

Ensure that the test executable `build-debug/uv_run_tests_a` exists before continuing. Use this same build throughout the remaining steps.

## 2. Discover static callsites

Return to the icallcov repository root so that script and client paths resolve correctly:

```sh
cd /path/to/icallcov

python3 scan.py \
  /path/to/libuv/build-debug/uv_run_tests_a \
  -o static-debug.json
```

## 3. Run one test under DynamoRIO

From the icallcov repository root:

```sh
"$HOME/tools/dynamorio/build/bin64/drrun" \
  -c ./dynamorio/build/libicall_trace.so \
  -- /path/to/libuv/build-debug/uv_run_tests_a timer timer
```

This records indirect-call events and their observed targets in `dynamic.csv` in the process working directory. Each run overwrites that file; preserve any previous trace you want to keep before running again. This command executes the specific `timer timer` test invocation, not the full test suite.

## 4. Generate the report

Still from the icallcov repository root:

```sh
python3 report.py \
  static-debug.json \
  dynamic.csv \
  --binary /path/to/libuv/build-debug/uv_run_tests_a \
  --project-root /path/to/libuv \
  --source-dir src \
  --test-dir test \
  --show-filtered
```

`addr2line` uses the binary's debug information to resolve source locations. The source/test directory settings guide classification, with additional runtime and libuv-oriented heuristics. `--show-filtered` lets you inspect the test, runtime, and unknown categories excluded from the project coverage denominator.

Add `--show-covered --show-targets` to inspect covered project callsites and their observed targets. Save a text report by appending `> report.txt` to the command if needed.

## Example observation (environment/test-specific)

The following is a supplied observation from a prototype run, presented as a summary rather than a guaranteed output or reproducible benchmark. Exact dependency versions and build details for that observation are not recorded here.

```text
Raw indirect callsites: 77
Project: 62
Test: 13
Runtime: 1
Unknown: 1

Project callsites: 62
Covered by the timer test: 11
Uncovered: 51
Coverage: 17.7%
```

The percentage is `11 / 62 × 100`, rounded to one decimal place. This is **NOT whole-libuv test-suite coverage**. It only shows how many project callsites were exercised by that specific `timer timer` test run. Compiler, libuv version, build settings, debug information, and test execution can change the counts.

A covered callsite means the indirect-call instruction executed at least once. It does **not** mean all possible targets were exercised. The tracer already records callsite-to-target information, but full legal-target / indirect-edge coverage analysis remains future work. See the [current limitations](../README.md#current-limitations) before interpreting results.
