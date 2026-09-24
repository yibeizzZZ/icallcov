"""Runner for CMake/CTest projects.

LIMITATION: wrapping `ctest` itself with DynamoRIO does not reliably
instrument the child test executable that ctest spawns, because ctest is a
separate driver process and the real test binary runs underneath it. To
trace the actual code under test, this runner resolves each test's real
invocation command via `ctest --show-only=json-v1` (CMake/CTest >= 3.14)
and runs that resolved command directly under drrun, instead of wrapping
`ctest -R '^NAME$'`.

This is reliable for the common case of plain `add_test(NAME ... COMMAND
<exe> ...)` entries. It is NOT reliable, and will raise a clear error
instead of silently mis-tracing, when:

- CTest/CMake predates `--show-only=json-v1` support, or
- a test's resolved "command" is itself a wrapper/launcher (for example a
  cross-compilation emulator or custom test driver) rather than the real
  test binary.

In either case, use the `commands` runner with an explicit invocation of
the real test binary instead.
"""

import json
import subprocess

from .base import TestRunner


class CTestRunner(TestRunner):
    def __init__(self, build_dir, ctest_bin="ctest"):
        self.build_dir = build_dir
        self.ctest_bin = ctest_bin
        self._commands = {}
        self._working_dirs = {}

    def discover_tests(self):
        proc = subprocess.run(
            [self.ctest_bin, "--show-only=json-v1"],
            cwd=str(self.build_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(
                "failed to discover CTest tests via `--show-only=json-v1`; "
                "this requires CMake/CTest >= 3.14\n"
                f"command: {self.ctest_bin} --show-only=json-v1 "
                f"(cwd={self.build_dir})\n"
                f"exit code: {proc.returncode}\n"
                f"stderr:\n{proc.stderr}\n"
                "If your CTest predates json-v1 support, or its tests are "
                "not resolvable to a real executable, use the `commands` "
                "runner with an explicit --tests-file instead."
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"failed to parse `ctest --show-only=json-v1` output: {exc}"
            ) from exc

        tests = []

        for entry in data.get("tests", []):
            name = entry.get("name")
            command = entry.get("command")

            if not name or not command:
                continue

            self._commands[name] = [str(part) for part in command]
            self._working_dirs[name] = _extract_working_dir(
                entry, default=str(self.build_dir)
            )

            tests.append(
                {
                    "name": name,
                    "helpers": [],
                }
            )

        if not tests:
            raise RuntimeError(
                f"CTest reported no tests; checked build dir: {self.build_dir}"
            )

        return tests

    def command_for_test(self, test):
        name = test["name"]
        command = self._commands.get(name)

        if not command:
            raise RuntimeError(
                f"no resolved executable command for CTest test {name!r}; "
                "re-run discovery or use the `commands` runner instead"
            )

        return command

    def working_dir_for_test(self, test):
        return self._working_dirs.get(test["name"])


def _extract_working_dir(entry, default):
    # `ctest --show-only=json-v1` reports per-test WORKING_DIRECTORY (when
    # explicitly set) inside "properties" as a {"name", "value"} pair; it
    # is not a top-level key. Fall back to the build directory, which is
    # CTest's own default working directory for tests that don't set one.
    for prop in entry.get("properties", []):
        if prop.get("name") == "WORKING_DIRECTORY":
            value = prop.get("value")
            if value:
                return value

    return default
