"""Runner for libuv's built-in test binary (e.g. `uv_run_tests_a`)."""

import re
import subprocess

from .base import TestRunner


class LibuvRunner(TestRunner):
    """Discovery uses the binary's `--list` output. Execution always
    invokes the binary's normal single-test-name entry point:

        <binary> TEST_NAME

    Never the two-argument `<binary> TEST_NAME TEST_NAME` form, which
    calls run_test_part() directly and bypasses libuv's normal
    process/test-runner semantics; some valid tests (for example
    fork_fs_events_child) can hang when invoked that way.

    Per-PID tracing means it is safe for the normal runner to create
    child/helper processes: DynamoRIO follows them and each process
    writes its own dynamic.<pid>.csv file.
    """

    def __init__(self, binary):
        self.binary = binary

    def discover_tests(self):
        proc = subprocess.run(
            [str(self.binary), "--list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                "failed to list tests\n"
                f"command: {self.binary} --list\n"
                f"exit code: {proc.returncode}\n"
                f"stderr:\n{proc.stderr}"
            )

        tests = _parse_test_list(proc.stdout)

        if not tests:
            raise RuntimeError(
                "test discovery returned no tests; "
                "expected libuv-style `--list` output"
            )

        return tests

    def command_for_test(self, test):
        return [str(self.binary), test["name"]]


def _parse_test_list(output):
    tests = []

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        match = re.match(
            r"^(\S+)(?:\s+\(helpers:\s*(.*?)\))?$",
            line,
        )

        if not match:
            continue

        name = match.group(1)
        helper_text = match.group(2)
        helpers = helper_text.split() if helper_text else []

        tests.append(
            {
                "name": name,
                "helpers": helpers,
            }
        )

    return tests
