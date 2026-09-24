"""Universal fallback runner: run explicit commands from a text file.

Each non-blank, non-comment (`#`) line in the file is treated as one test.
Two line formats are supported:

    ./build/test_timer --case foo

or, to give the test an explicit name (tab-separated):

    timer<TAB>./build/test_runner timer

When no explicit name is given, a deterministic name is derived from the
command text so it is safe to use as a trace filename and as the
test_name column in suite.csv/summary.csv.
"""

import os
import re
import shlex

from .base import TestRunner


class CommandsRunner(TestRunner):
    def __init__(self, tests_file):
        self.tests_file = tests_file

    def discover_tests(self):
        tests = []
        seen_names = {}

        with open(self.tests_file, "r", encoding="utf-8") as f:
            for line_number, raw_line in enumerate(f, start=1):
                line = raw_line.rstrip("\n")
                stripped = line.strip()

                if not stripped or stripped.startswith("#"):
                    continue

                name = None
                command_text = stripped

                if "\t" in line:
                    name_part, _, rest = line.partition("\t")

                    if name_part.strip():
                        name = name_part.strip()
                        command_text = rest.strip()

                try:
                    argv = shlex.split(command_text)
                except ValueError as exc:
                    raise RuntimeError(
                        f"{self.tests_file}:{line_number}: "
                        f"failed to parse command: {exc}"
                    ) from exc

                if not argv:
                    continue

                if not name:
                    name = _default_test_name(argv)

                if name in seen_names:
                    seen_names[name] += 1
                    name = f"{name}_{seen_names[name]}"
                else:
                    seen_names[name] = 0

                tests.append(
                    {
                        "name": name,
                        "helpers": [],
                        "command": argv,
                    }
                )

        if not tests:
            raise RuntimeError(f"no test commands found in {self.tests_file}")

        return tests

    def command_for_test(self, test):
        return test["command"]


def _default_test_name(argv):
    base = os.path.basename(argv[0])
    rest = "_".join(argv[1:])
    name = base if not rest else f"{base}_{rest}"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return name or "test"
