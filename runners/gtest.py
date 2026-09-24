"""Runner for GoogleTest-compatible binaries."""

import subprocess

from .base import TestRunner


class GTestRunner(TestRunner):
    """Discovery uses `--gtest_list_tests`. Execution filters a single
    test with `--gtest_filter=Suite.Test`, run directly by the test
    binary so DynamoRIO instruments it exactly like any other runner.
    """

    def __init__(self, binary):
        self.binary = binary

    def discover_tests(self):
        proc = subprocess.run(
            [str(self.binary), "--gtest_list_tests"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                "failed to list gtest tests\n"
                f"command: {self.binary} --gtest_list_tests\n"
                f"exit code: {proc.returncode}\n"
                f"stderr:\n{proc.stderr}"
            )

        tests = _parse_gtest_list(proc.stdout)

        if not tests:
            raise RuntimeError(
                "test discovery returned no tests; "
                "expected `--gtest_list_tests` output"
            )

        return tests

    def command_for_test(self, test):
        return [str(self.binary), f"--gtest_filter={test['name']}"]


def _parse_gtest_list(output):
    tests = []
    current_suite = None

    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue

        # Suite header lines are unindented (e.g. "TimerTest."); test-case
        # lines are indented under their suite (e.g. "  Starts"). Typed/
        # value-parameterized suites may append a "# TypeParam() = ..."
        # comment that isn't part of the suite name.
        if not raw_line[0].isspace():
            current_suite = raw_line.strip().split("#", 1)[0].strip()

            if current_suite.endswith("."):
                current_suite = current_suite[:-1]

            continue

        if current_suite is None:
            continue

        # Parameterized tests may append "# GetParam() = ..."; keep only
        # the case name itself.
        case = raw_line.strip().split("#", 1)[0].strip()

        if not case:
            continue

        tests.append(
            {
                "name": f"{current_suite}.{case}",
                "helpers": [],
            }
        )

    return tests
