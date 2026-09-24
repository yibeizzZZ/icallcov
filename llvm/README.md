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
