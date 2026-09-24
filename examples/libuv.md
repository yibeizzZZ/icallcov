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

## 3. Run the test suite under DynamoRIO

From the icallcov repository root, run the `timer` test through `run_suite.py`'s libuv runner:

```sh
python3 run_suite.py \
  --runner libuv \
  --binary /path/to/libuv/build-debug/uv_run_tests_a \
  --mode edge \
  --match '^timer$' \
  --output-dir coverage/edge
```

This invokes the test the same way libuv's own runner does — `uv_run_tests_a timer` (never the two-argument `uv_run_tests_a timer timer` form, which bypasses libuv's normal process/test-runner semantics and can hang some tests). Each traced process writes its own `dynamic.<pid>.csv`; `run_suite.py` collects and merges those per-process files into `coverage/edge/traces/timer.csv` and the suite-wide `coverage/edge/suite.csv` (with a `test_name` column), and writes `coverage/edge/summary.csv`. Use `--mode fast` instead of `--mode edge` for lower-overhead callsite-only coverage (no observed-target data). Drop `--match '^timer$'` to run the whole discovered test list.

## 4. Generate the report

Still from the icallcov repository root:

```sh
python3 report.py \
  static-debug.json \
  coverage/edge/suite.csv \
  --binary /path/to/libuv/build-debug/uv_run_tests_a \
  --project-root /path/to/libuv \
  --source-dir src \
  --test-dir test \
  --show-filtered \
  --show-covered --show-targets \
  --export-edges coverage/edge/observed_edges.json
```

`addr2line` uses the binary's debug information to resolve source locations. The source/test directory settings guide classification (add `--libuv-compat` only if you also want the libuv-specific `uv_*`/`uv__*` function-name fallback). `--show-filtered` lets you inspect the test, runtime, and unknown categories excluded from the project coverage denominator.

`--show-covered --show-targets` prints, for each covered project callsite, the runtime targets *observed* during this run (with hit counts and symbolization when resolvable). `--export-edges` writes the same information as machine-readable JSON, one record per project callsite, labeled `status: observed|unobserved` — this is strictly an **observed** edge set, not a legal/complete target set. Save a text report by appending `> report.txt` to the command if needed.

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

The percentage is `11 / 62 × 100`, rounded to one decimal place. This is **NOT whole-libuv test-suite coverage**. It only shows how many project callsites were exercised by that specific `timer` test run. Compiler, libuv version, build settings, debug information, and test execution can change the counts.

A covered callsite means the indirect-call instruction executed at least once. It does **not** mean all possible targets were exercised. The tracer already records callsite-to-target information, but full legal-target / indirect-edge coverage analysis remains future work. See the [current limitations](../README.md#current-limitations) before interpreting results.
