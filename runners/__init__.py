"""Test-runner adapters used by run_suite.py.

Each runner isolates project-specific test discovery/execution behind the
minimal TestRunner interface in base.py. The DynamoRIO tracing core stays
generic and lives in run_suite.py.
"""

from .base import TestRunner
from .commands import CommandsRunner
from .ctest import CTestRunner
from .gtest import GTestRunner
from .libuv import LibuvRunner

RUNNER_NAMES = ("libuv", "ctest", "gtest", "commands")


def build_runner(name, *, binary=None, build_dir=None, ctest_bin="ctest",
                  tests_file=None):
    """Construct the runner named `name`, validating its required option."""
    if name == "libuv":
        if binary is None:
            raise ValueError("--binary is required for --runner libuv")
        return LibuvRunner(binary)

    if name == "gtest":
        if binary is None:
            raise ValueError("--binary is required for --runner gtest")
        return GTestRunner(binary)

    if name == "ctest":
        if build_dir is None:
            raise ValueError("--build-dir is required for --runner ctest")
        return CTestRunner(build_dir, ctest_bin=ctest_bin)

    if name == "commands":
        if tests_file is None:
            raise ValueError(
                "--tests-file is required for --runner commands"
            )
        return CommandsRunner(tests_file)

    raise ValueError(f"unknown runner: {name}")


__all__ = [
    "TestRunner",
    "LibuvRunner",
    "GTestRunner",
    "CTestRunner",
    "CommandsRunner",
    "RUNNER_NAMES",
    "build_runner",
]
