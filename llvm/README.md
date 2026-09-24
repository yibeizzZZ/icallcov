# LLVM IR callsite scanner

This optional tool discovers indirect callsites using LLVM's `CallBase::isIndirectCall()`. It accepts textual IR (`.ll`) and bitcode (`.bc`), including indirect `call` and `invoke` instructions. It does not estimate possible callees or map IR instructions to machine addresses. The existing `scan.py`, DynamoRIO tracing, and coverage report remain the binary-based workflow; **do not pass this tool's JSON to `report.py`**.

## Build and run

The initial supported toolchain is LLVM 21.1.x (tested with 21.1.8), its development headers/CMake package, a C++17 compiler, and CMake. Use matching Clang/LLVM tools to generate input. Other LLVM versions have not been validated. This tool builds separately from the C DynamoRIO client and is not required for binary scanning.

From the repository root:

```sh
cmake -S llvm -B llvm/build -DLLVM_DIR="$(llvm-config-21 --cmakedir)"
cmake --build llvm/build -j

clang-21 -O0 -g -emit-llvm -c examples/ir_callbacks.c -o llvm/build/ir_callbacks.bc
./llvm/build/icall_ir_scan llvm/build/ir_callbacks.bc -o static-ir.json
```

The example produces two indirect callsites in `apply_twice`. To produce textual IR, use `clang-21 -O0 -g -S -emit-llvm examples/ir_callbacks.c -o llvm/build/ir_callbacks.ll`.

Multiple inputs are supported: `./llvm/build/icall_ir_scan a.bc b.bc -o static-ir.json`. They are scanned separately, not linked or deduplicated by symbol. Identical input contents are rejected to prevent duplicate callsite IDs. Omitting `-o` writes JSON to standard output. Invalid inputs return a nonzero status without replacing an existing output file.

## Batch scan a project's compilation database

`scan_project_ir.py` automates bitcode generation and calls this scanner once for the selected compilation units. It is project-independent and uses Python's standard library. Build the LLVM scanner above first, and use Clang 21.1.x to match it.

For a CMake project, export the database and build the chosen target so any generated headers/sources exist:

```sh
cmake -S /path/to/project -B /path/to/project/build \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build /path/to/project/build --target target
```

From the icallcov repository root:

```sh
python3 scan_project_ir.py \
  --compdb /path/to/project/build/compile_commands.json \
  --output-match 'CMakeFiles/target.dir/' \
  --output-dir coverage/project-ir
```

Omit `--output-match` to scan all database entries. The filter is a **literal substring**, not a regular expression or glob, matched against the entry's optional `output` field, or its original `-o` path when that field is absent. It does not match source filenames. An empty selection is an error. The filter does not infer a target's linked dependencies; include their compilation units explicitly by using a broader selection when needed.

Custom tool locations can be supplied with `--clang /path/to/clang`, `--clangxx /path/to/clang++`, and `--scanner /path/to/icall_ir_scan`. Defaults are `clang-21`, `clang++-21`, and this repository's `llvm/build/icall_ir_scan`. Relative tool paths are resolved before entering the compilation working directory.

The output directory contains:

```text
coverage/project-ir/
├── callsites.json       # existing scanner JSON, unchanged schema
├── callsites.txt        # one line per indirect callsite
└── ir/run-*/unit-*.bc   # retained IR inputs referenced by the JSON
```

Readable lines look like `dispatch | /path/to/source.c:42 | %fn`. Without debug locations they use `<unknown>:0`; unnamed functions use `<unnamed>`. Newlines inside fields are escaped so one callsite always occupies one line. JSON retains full location and identity information.

Each selected entry is compiled in its database `directory` with its include paths, defines, language standard, debug flags, optimization settings, and other compatible compiler flags retained. `arguments` takes precedence over `command`; command strings use POSIX quoting without shell expansion or execution, following the [compilation database format](https://clang.llvm.org/docs/JSONCompilationDatabase.html). Relative database directories are resolved against the database's parent. GNU-style `@response` files are expanded relative to the compilation directory, including nested files; missing or recursive files fail clearly.

The script replaces the compiler with Clang/Clang++, removes the old output and compilation/preprocessing modes, dependency-file outputs, save-temporaries/time-trace and optimization-record output flags, and LTO output modes, then emits bitcode with `-emit-llvm -c`. It preserves the original source argument spelling and compiles exactly one source per entry. It does not add `-g` or change optimization levels: configure your project's flags accordingly before exporting the database. Existing object files, ordinary dependency files, and optimization-record reports are not overwritten by these options. Forwarded optimization-record output options through `-Xclang`/`-mllvm` are rejected, including joined `=value` spellings; unrecognized compiler flags are still subject to the compatibility limits below.

Repeated source paths are **not** collapsed: each selected command is compiled with its own flags and a distinct IR filename. If different commands produce byte-identical bitcode, the script explicitly reports this and scans that content once, because the scanner's content-based identity rejects duplicate artifacts. This does not skip compilation failures or merge different bitcode. All remaining modules are passed to the scanner in one invocation.

Any selected-unit compilation failure, missing bitcode, or scanner failure aborts with a nonzero status and diagnostics. Compiler failures include the entry, source, working directory, and rewritten command. Reports are replaced only after successful compilation and scanning; prior reports survive these failures, and the failed run's temporary IR is removed. Each run uses a fresh IR directory, so previous bitcode cannot contaminate a new result. Successful run directories remain for inspection. Report publication checks destination types and backs up existing reports, restoring replaced files if a later replacement fails. Once publication starts, its IR directory is retained on failure as well: if rollback also fails, the error identifies the recovery directory, and any published JSON still points to retained IR. Remove old directories manually when no longer needed. Use separate output directories for concurrent runs; publishing the two report files is not a crash-atomic or cross-process transaction.

This first version supports **native C/C++ databases with GNU-style GCC/Clang driver arguments**. It recognizes plain `ccache`/`sccache` launchers. It does not replay arbitrary shell setup, custom compiler wrappers, assembly units, PCH/modules, coverage side outputs, or compiler plugins. Unsupported selected inputs/options fail rather than being silently discarded; Clang-specific compatibility errors are reported. When an excluded entry has no `output` field, its command/response files must still be readable to obtain its `-o` path. Generated files must already exist. The database does not capture the original environment or compiler's implicit defaults; use a compatible Clang toolchain and environment. Cross-compilation toolchain emulation and automatic GCC-specific flag translation are not implemented.

The batch workflow does not link modules, estimate callees with SVF/PhASAR, map IR to runtime addresses, or alter `scan.py`, DynamoRIO, or `report.py`.

## Output and identity

The versioned JSON document has `kind: "llvm-ir-callsites"`, `schema_version: 1`, `llvm_version`, `build_id`, `id_scope`, `modules`, `count`, and `indirect_callsites`.

Each callsite records:

- `callsite_id`: SHA-256 of the input bytes followed by function, basic-block, and instruction ordinals, e.g. `<digest>:f3:b0:i8`. Ordinals count all corresponding IR objects, not only indirect calls. Two calls on the same source line have different IDs.
- `module_id`: the input content digest; `function`: its IR name (or `null` when unnamed) and module-qualified ID.
- `basic_block_index`, `instruction_index`, `opcode`, and `called_operand`: the IR location and operand for inspection.
- `source_location`: debug filename, directory, line, column, discriminator, and `inlined_at` chain, or `null` when no debug location exists. Debug metadata can itself contain incomplete locations; filenames are not guaranteed to be absolute or available locally.
- `target_analysis`: `{"status": "not_analyzed", "backend": null, "possible_callees": null}`. Null means no analysis has run, not that the possible target set is empty.

IDs are unique within the supplied IR artifact set, assuming SHA-256 collision resistance. `build_id` fingerprints the sorted set of input digests and is independent of input order or filenames. It is **not an ELF build ID**, and does not prove correspondence to an executable. Identical bytes at a different path retain IDs; recompilation, optimization, textual edits, and conversion between `.ll` and `.bc` can change IDs. They are not persistent source-level identities across builds.

Debug information (`-g`) is strongly recommended. Optimization can inline, remove, or turn indirect calls into direct calls; this tool reports the IR supplied to it. It does not recover missing compilation units or compiler-generated machine callsites. The example uses `-O0` for predictable demonstration, not as a guarantee of correspondence with an independently optimized executable.

Static callee estimation and reliable IR-to-runtime identity mapping remain future work. This output alone cannot calculate static-edge coverage.

## Validation

```sh
ICALL_IR_SCANNER="$PWD/llvm/build/icall_ir_scan" \
  python3 -B -m unittest discover -s tests -p test_llvm_scanner.py -v
```

The bitcode test uses `llvm-as-21` (override with `LLVM_AS`). Without a built scanner, ordinary repository test discovery skips these optional tests; an explicit invalid `ICALL_IR_SCANNER` is an error.

Run the batch workflow tests with `python3 -B -m unittest discover -s tests -p test_project_ir.py -v`. Parsing and rewriting tests need only Python; native integration cases additionally use `clang-21`, `clang++-21`, and the built scanner.
