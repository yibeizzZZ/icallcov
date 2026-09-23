# Requirements and Setup

The expected environment is Linux on x86-64, analyzing ELF binaries.

| Component | Purpose |
| --- | --- |
| Python 3 | Runs `scan.py` and `report.py`; no third-party Python packages are currently required |
| GNU binutils: `objdump` | Disassembles the target binary for static discovery |
| GNU binutils: `addr2line` | Resolves offsets to source functions/files/lines when debug information exists |
| C compiler | Builds the DynamoRIO client |
| CMake 3.14 or newer | Configures the client build, as required by the existing `CMakeLists.txt` |
| DynamoRIO with `drmgr` | Builds and runs the dynamic tracing client |

Install these tools using the setup appropriate to your Linux environment. Build the program under test with debug information for useful source mapping, and keep the same binary for the scan, trace, and report. Target projects may have additional build requirements.

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
