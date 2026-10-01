# TypeDive candidate analysis

`typedive.py` packages [upstream TypeDive](https://github.com/umnsec/mlta)
without reimplementing FLTA, one-layer matching, or MLTA. It replays selected
compile commands using `scan_project_ir.py`, runs all three upstream modes,
and compares candidates with icallcov edge traces. The binary scanner and
DynamoRIO tracing semantics are unchanged.

Dynamic observations are confirmed-positive **lower bounds**. Static candidates
are evidence, **not a sound ground-truth upper bound**. Unobserved static
candidates are never classified as false positives. In the supplied libuv
experiment, MLTA removed two observed legal edges retained by both baselines.

## Build the environment

From the icallcov repository, on a Linux x86-64 Docker host:

```bash
docker build -t icallcov-typedive:llvm15 --build-arg JOBS=8 integrations/typedive
```

Allow substantial time and disk space for LLVM. Reduce `JOBS` on hosts with
less memory. This builds from Ubuntu 22.04 with gcc-10/g++-10 and pins:

- LLVM: `e758b77161a70d7e7260d8f52bf161a89d73af8a` (LLVM 15).
- TypeDive: `1f2b4b7babb3308710940573efacfe78a53c9f6b` (the manual experiment's revision).

The one checked-in `integrations/typedive/typedive.patch` guards null `getlogin()`,
prints targets for modes 0 and 1, and adds versioned JSON pair records for all
modes, including declaration-only candidates. It changes no analysis algorithm
or configuration: upstream `SOUND_MODE`, loop unrolling, and other options remain
as pinned. The Docker build applies the patch with `git apply --check` first.
Ubuntu packages are obtained from current Ubuntu 22.04 repositories; the recipe
pins the analysis source revisions, not a byte-identical OS/package snapshot.

The image includes typed-pointer compiler wrappers at `/opt/typedive/bin/clang`
and `/opt/typedive/bin/clang++`. They add `-Xclang -no-opaque-pointers` and `-g`.
The adapter also adds these flags when replaying the database. Optimization,
defines, include paths, and ABI options come from the selected compile commands;
the adapter does not silently replace them with `-O2`. For the reference build,
use `-O2 -g -DNDEBUG` as below.

## New same-configuration libuv build

Use a **new build directory**; do not overwrite a binary that existing traces
refer to. Substitute absolute host paths as needed:

```bash
export ICALLCOV=/mnt/d/SRE_Bench_research/icallcov
export PROJECT=/mnt/d/SRE_Bench_research/libuv
export BUILD="$PROJECT/build-typedive-repro"
export RESULT=/mnt/d/SRE_Bench_research/typedive-libuv-repro

# Record source revision and local changes for the experiment.
git -C "$PROJECT" rev-parse HEAD
git -C "$PROJECT" diff --stat

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PROJECT:$PROJECT" icallcov-typedive:llvm15 \
  cmake -S "$PROJECT" -B "$BUILD" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_C_COMPILER=/opt/typedive/bin/clang \
    -DCMAKE_CXX_COMPILER=/opt/typedive/bin/clang++ \
    '-DCMAKE_C_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG' \
    '-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG' \
    -DLIBUV_BUILD_SHARED=ON -DLIBUV_BUILD_TESTS=ON \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PROJECT:$PROJECT" icallcov-typedive:llvm15 \
  cmake --build "$BUILD" --parallel 8

python3 "$ICALLCOV/typedive.py" analyze \
  --compdb "$BUILD/compile_commands.json" --project-root "$PROJECT" \
  --include-command 'CMakeFiles/uv.dir/' --output "$RESULT"
```

The shared target filter is essential: libuv's database includes shared, static,
and test targets. Selecting both `uv.dir` and `uv_a.dir` analyzes the library
sources twice. The filter belongs to this example, not the analysis engine.

Collect new dynamic observations from this build using the existing runner:

```bash
# Set these to your installed DynamoRIO runtime and built icallcov client.
export DRRUN=/absolute/path/to/drrun
export CLIENT="$ICALLCOV/dynamorio/build/libicall_trace.so"
export DYNAMIC=/absolute/path/to/new-libuv-dynamic-output

LD_LIBRARY_PATH="$BUILD${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
python3 "$ICALLCOV/run_suite.py" --runner libuv \
  --binary "$BUILD/uv_run_tests" --cwd "$PROJECT" \
  --drrun "$DRRUN" --client "$CLIENT" --mode edge --output-dir "$DYNAMIC"

python3 "$ICALLCOV/typedive.py" compare --static "$RESULT" \
  --dynamic "$DYNAMIC/suite.csv" --binary "$BUILD/libuv.so.1.0.0" \
  --module libuv.so.1
```

Use the shared test executable `uv_run_tests`, not `uv_run_tests_a`, and verify
that it loads the intended shared library. Preserve the exact unstripped binary
used for tracing. Different source versions, test outcomes, compiler options,
and dependencies can change the observations. Project prerequisites must also
be installed in the analysis image (derive a small project-specific image when
needed).

## Reproduce the supplied saved experiment

The supplied libuv checkout is at
`b05973ed36b2de7ca51a3dca9cfa9160b42fe34d`. Its existing database records paths
under `/work`, and names historical transparent compiler wrappers under `/tmp`.
The following uses those commands without executing or depending on the old
wrapper scripts, and leaves all old outputs untouched:

```bash
cd /mnt/d/SRE_Bench_research/icallcov
python3 typedive.py analyze \
  --compdb /mnt/d/SRE_Bench_research/libuv/build-clang15-o2/compile_commands.json \
  --project-root /work/libuv \
  --mount /mnt/d/SRE_Bench_research:/work \
  --include-command 'CMakeFiles/uv.dir/' \
  --compiler-wrapper /tmp/clang-typedive-g=clang \
  --compiler-wrapper /tmp/clangxx-typedive-g=clang++ \
  --output /mnt/d/SRE_Bench_research/typedive-libuv-packaged

python3 typedive.py compare \
  --static /mnt/d/SRE_Bench_research/typedive-libuv-packaged \
  --dynamic /mnt/d/SRE_Bench_research/libuv-clang15-dynamic/suite.csv \
  --binary /mnt/d/SRE_Bench_research/libuv/build-clang15-o2/libuv.so.1.0.0 \
  --module libuv.so.1
```

`--compiler-wrapper PATH=clang` (or `clang++`) is an explicit assertion that the
named wrapper is transparent except for the typed-pointer/debug flags supplied
by the adapter. Do not use it for wrappers that inject other essential build
settings. Export a native compiler database in that case. Paths in the database,
response files, and includes must exist inside the container. Use repeatable
`--mount HOST:CONTAINER` for historical paths or external dependencies. Mounts
are read-only except for the output parent. `--project-root` can be an existing
container path supplied by such a mount.

The historical CSV cannot cryptographically prove which binary generated it.
Comparison records hashes of the supplied CSV, binary, and static summary, with
`same_build_verified: false`. This records artifact identity, not an unsupported
same-build claim. The saved test summary is the authority for suite pass/fail
counts; the adapter does not rerun the suite as a side effect of comparison.

## Another project

For the local libpng checkout, create the shared build with the same container
compiler, then change the database, root, and target filter:

```bash
export PNG=/mnt/d/SRE_Bench_research/libpng
docker run --rm --user "$(id -u):$(id -g)" -v "$PNG:$PNG" \
  icallcov-typedive:llvm15 cmake -S "$PNG" -B "$PNG/build-typedive-repro" \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_COMPILER=/opt/typedive/bin/clang \
  '-DCMAKE_C_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG' \
  -DPNG_SHARED=ON -DPNG_TESTS=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
docker run --rm --user "$(id -u):$(id -g)" -v "$PNG:$PNG" \
  icallcov-typedive:llvm15 cmake --build "$PNG/build-typedive-repro" \
  --target png_shared --parallel 8

python3 /mnt/d/SRE_Bench_research/icallcov/typedive.py analyze \
  --compdb /mnt/d/SRE_Bench_research/libpng/build-typedive-repro/compile_commands.json \
  --project-root /mnt/d/SRE_Bench_research/libpng \
  --include-command 'CMakeFiles/png_shared.dir/' \
  --output /mnt/d/SRE_Bench_research/typedive-libpng-packaged

python3 /mnt/d/SRE_Bench_research/icallcov/typedive.py compare \
  --static /mnt/d/SRE_Bench_research/typedive-libpng-packaged \
  --dynamic /absolute/path/to/libpng-dynamic/suite.csv \
  --binary /mnt/d/SRE_Bench_research/libpng/build-typedive-repro/libpng18.so \
  --module libpng18.so.18
```

Inspect that project's database for its actual target output and its trace for
the runtime module name. Repeated `--include-command` rules use OR; any
`--exclude-command` match excludes the entry. Rules are literal substrings of
`arguments` (preferred), otherwise `command`, plus the optional `output` field.
They apply before native compiler validation, so excluded test/tool targets need
not be replayable. Included response files still use the scanner's expansion
and validation. With no include rules, all nonexcluded entries are selected.
No source-path deduplication is performed: two differently compiled versions of
the same source are distinct translation units.

The same commands work for other C/C++ databases supported by
`scan_project_ir.py`. Cross compilation and non-x86-64 dynamic comparison are
outside the current ELF helper's scope. A project may require additional headers
or libraries in a derived image. `--image` selects it; `--native` runs the tools
directly when already inside the environment. `--clang`, `--clangxx`, and
`--kanalyzer` can select explicit installed paths.

## Outputs and interpretation

Analysis refuses an existing output directory, even an empty one. Failed runs
retain their artifacts; select a new directory for a retry. Comparison creates
`STATIC/comparison.json` exclusively, or a new path supplied with `--output`.

```text
output/
  compile_commands.json       selected/normalized database
  manifest.json               compilation provenance (available before analysis)
  bitcode.list                quoted LLVM response file
  ir/unit-NNNNNN.bc           one retained module per selected entry
  raw/{flta,one_layer,mlta}.log
  {flta,one_layer,mlta}.tsv
  summary.json                provenance and parsed upstream statistics
  comparison.json            optional dynamic comparison
```

Raw logs preserve all upstream diagnostics. Lines beginning
`ICALLCOV_PAIR_V1<TAB>` contain one JSON object (schema version 1) per candidate,
using JSON string escaping. The parser validates required fields and reconciles
unique candidate/callsite counts with upstream statistics; missing output
instrumentation or unreadable modules abort the run. Empty candidate sets appear
in upstream statistics/logs and have no candidate row.

TSV files use Python's CSV quoting with a tab delimiter; read them with a CSV/TSV
parser rather than splitting arbitrary lines. Each row retains mode, caller and
callee modules, raw function/linkage names, source paths and lines, declaration
flag, caller column, instruction ordinal, and full printed IR instruction.
`callsite_id` hashes `(caller module, caller function, instruction ordinal)`.
IDs are unique within the retained IR artifact set and shared across the three
modes; they are not promised stable across rebuilds or relocated output paths.
The ordinal includes all instructions and reflects TypeDive's upstream loop
unrolling. Two calls on the same debug line remain separate static candidates.
Project source paths are normalized relative to `--project-root`; external paths
remain absolute. Original module identities and IR text remain in the TSV.

Comparison filters both caller and target to the requested module basename. It
uses the existing ELF image-base conversion and `addr2line -a -f -i` without demangling,
so C++ linkage names remain compatible with IR names. It uses the innermost
frame for caller source attribution and the outermost frame for callee identity;
a callback entry can begin inside an inlined helper. It matches
`caller source path + line + callee function`, also checking callee source file
when both sides have it. Caller function is diagnostic, since optimized inlining
can change ownership. Directory components are retained: matching only basenames
would conflate unrelated files. Use `--path-map OLD=NEW` when dynamic DWARF paths
need remapping to the static project's root.

The JSON retains runtime offsets, trace-event counts, and all matching strong
static IDs. It reports runtime pairs separately from source-normalized edges,
which can merge multiple optimized calls. Unresolved source locations or symbols
are reported separately, not counted as definite static misses. Resolved source
mismatches are misses under this mapping, not a proof of algorithmic unsoundness
without checking debug attribution. Module basename collisions, optimized debug
information, aliases, and missing symbols can limit correspondence. The pinned
LLVM15 snapshot and Ubuntu binutils can fail to resolve some DWARF5 inputs;
`--addr2line /path/to/llvm-addr2line` selects another available symbolizer. The
small C++ regression uses DWARF4 to isolate inline-frame selection. Static-only
candidates receive no false-positive label. `dynamically_observed_edges_removed_by_mlta`
contains observed edges missing from MLTA but retained by FLTA or one-layer.

The manifest includes compiler versions, commands, selected original database
indices, source and bitcode hashes, analyzer and patch hashes, and the image's
pinned revision file when available. It is not a complete dependency snapshot;
preserve source state, generated headers, the build environment, and the binary
with the experimental artifacts.

## Focused check

```bash
python3 -m unittest discover -s tests -p test_typedive.py
```

Full repository checks use `python3 -m unittest discover -s tests`. Optional
LLVM21/DynamoRIO checks skip when their tools are unavailable. The LLVM15 image
is independent of the existing LLVM21 IR scanner.

## Recorded reproduction

The checked-in Dockerfile was built successfully, including a fresh LLVM source
build. Both projects were then replayed with `icallcov-typedive:llvm15`; the
image's patch hash matched the checked-in patch. A preliminary validation using
the previously built LLVM toolchain produced the same counts.

The packaged replay of the saved Clang15/O2 libuv database selected 35 shared
translation units and matched the manual observations:

| Mode | Indirect calls | Calls with targets | Candidate pairs | Address-taken functions | Dynamic covered / missing |
|---|---:|---:|---:|---:|---:|
| FLTA | 74 | 31 | 62 | 42 | 14 / 0 |
| One-layer | 74 | 22 | 33 | 42 | 14 / 0 |
| MLTA | 74 | 19 | 30 | 42 | 12 / 2 |

MLTA reports 3 multi-layer calls. Dynamic comparison of the saved trace finds
170 total runtime callsites, 6 internal runtime callsites, and 14 internal pairs,
with no unresolved internal edges. Its two MLTA misses are:

- `src/unix/core.c:363 -> timer_close_cb`
- `src/unix/signal.c:481 -> uv__chld`

The existing dynamic `summary.csv` records 527 tests, 456 passed, 71 failed,
0 timed out, and 395,948 trace events. These dynamic observations were reused;
this packaging task did not rerun the dynamic suite. There were no numerical
discrepancies from the supplied libuv reference. These observations are
reported results, not constants or acceptance thresholds in the implementation.

A second-project check on libpng revision
`964b4135949703b705fc760fc3fb546b86e5ab47`, using the documented shared target,
selected 16 TUs and found 96 indirect calls in each mode. FLTA/one-layer/MLTA
produced 125/91/59 candidate pairs respectively. This was a static portability
check; no new libpng dynamic coverage claim is made.

In the supplied workspace, final artifacts are in
`/mnt/d/SRE_Bench_research/typedive-libuv-packaged` and
`/mnt/d/SRE_Bench_research/typedive-libpng-packaged`. Existing output directories
are deliberately refused; choose new destinations when repeating the commands.
