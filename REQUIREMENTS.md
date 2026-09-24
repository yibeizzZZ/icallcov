# Requirements and Setup

The supported environment is Linux on x86-64, analyzing one main ELF executable built as PIE or non-PIE. Tests should invoke native executables directly. Debug information is strongly recommended for useful classification and symbolization.

| Component | Purpose |
| --- | --- |
| Python 3.8 or newer | Runs `scan.py`, `run_suite.py`, and `report.py`; no third-party Python packages are required |
| GNU binutils: `objdump` | Disassembles the target binary for static discovery |
| GNU binutils: `addr2line` | Resolves ELF virtual addresses to source functions/files/lines when debug information exists |
| C compiler | Builds the DynamoRIO client |
| CMake 3.14 or newer | Configures the client build, as required by the existing `CMakeLists.txt` |
| DynamoRIO with `drmgr` | Builds and runs the dynamic tracing client |

Install these tools using the setup appropriate to your Linux environment. Build the program under test with debug information for useful source mapping, and keep the same binary for the scan, trace, and report. Target projects may have additional build requirements.

The scanner records module-relative offsets and the ELF image base in static JSON. Regenerate older static files with `scan.py`; the report rejects files without the coordinate metadata. See [Address coordinates](README.md#address-coordinates) for PIE/non-PIE conversion details.

Use a working directory in which the suite can create trace files and acquire its advisory lock. Suite runs sharing that directory cooperate through the lock; an unrelated tracer that ignores it must not run there concurrently. The suite terminates the test's process group before collecting newly created traces and preserves preexisting trace files. Tests whose descendants detach or escape the process group are unsupported.

CTest discovery additionally requires a `ctest` executable supporting `--show-only=json-v1` (CMake/CTest 3.14 or newer). Only simple native test commands are supported; the adapter does not reproduce complex CTest semantics or reliably detect launchers. Shared-library unload/module identity and same-PID `exec` tracing also remain limitations. See [Current Limitations](README.md#current-limitations).

## Optional LLVM IR scanner

The optional [LLVM IR scanner](llvm/README.md) additionally requires LLVM 21.1.x development headers/libraries and CMake package, a C++17 compiler, and matching Clang/LLVM tools to generate input. It was tested with LLVM 21.1.8. These dependencies are not needed for the existing binary scanning and tracing workflow.

## External DynamoRIO installation

DynamoRIO is an external dependency. Do not vendor its source tree, installation, binaries, or license into this repository. The repository's `dynamorio/` directory is the icallcov client, not a DynamoRIO distribution.

For example, keep your external DynamoRIO installation at:

```text
$HOME/tools/dynamorio
```

An example source-build layout places its CMake package at `$HOME/tools/dynamorio/build/cmake` and its runner at `$HOME/tools/dynamorio/build/bin64/drrun`. These are illustrative paths, not universal locations. Use the directory containing `DynamoRIOConfig.cmake` and the `drrun` executable from your installation. No particular DynamoRIO release compatibility range has been established here.

## Build the icallcov client

Starting in the icallcov repository root:

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

For the illustrated Linux build, the resulting client is `dynamorio/build/libicall_trace.so`. If your build configuration places it elsewhere, adjust the tracing command accordingly.

Continue with the [general usage](README.md#usage) or the [libuv example](examples/libuv.md).
