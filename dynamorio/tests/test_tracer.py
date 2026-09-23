"""Integration checks using a real DynamoRIO client and a small PIE fixture.

Run: python3 dynamorio/tests/test_tracer.py --client /absolute/libicall_trace.so
Override --drrun or --cc for a different local installation. No Python packages
are required. All binaries and traces are created in temporary directories.
"""

import argparse
import csv
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


HEADER = "caller_module,caller_offset,target_module,target_offset"
OPTIONS = None


class TracerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="icallcov-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root / "trace_fixture"
        subprocess.run(
            [OPTIONS.cc, "-O0", "-g", "-fPIE", "-pie", "-pthread",
             str(Path(__file__).with_name("trace_fixture.c")), "-o", str(cls.binary)],
            check=True,
        )
        symbols = subprocess.check_output(["nm", "-n", str(cls.binary)], text=True)
        cls.symbols = {
            parts[2]: int(parts[0], 16)
            for line in symbols.splitlines()
            if len(parts := line.split()) == 3
        }
        disassembly = subprocess.check_output(
            ["objdump", "-d", "--no-show-raw-insn", str(cls.binary)], text=True
        )
        cls.calls = {}
        for name in ("dispatch", "before_fork", "never_called"):
            body = disassembly.split(f"<{name}>:\n", 1)[1].split("\n\n", 1)[0]
            offsets = re.findall(r"^\s*([0-9a-f]+):\s+call\s+\*", body, re.M)
            assert len(offsets) == 1, (name, body)
            cls.calls[name] = int(offsets[0], 16)

    def run_trace(self, flags=(), app_args=(), success=True):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        result = subprocess.run(
            [str(OPTIONS.drrun), "-c", str(OPTIONS.client), *flags,
             "--", str(self.binary), *app_args],
            cwd=directory, capture_output=True, text=True, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, "invalid client arguments accepted")
            self.assertIn("Usage:", result.stderr)
            self.assertFalse(list(directory.glob("dynamic.*.csv")))
            return
        traces = {}
        for path in directory.glob("dynamic.*.csv"):
            self.assertRegex(path.name, r"^dynamic\.\d+\.csv$")
            with path.open() as stream:
                self.assertEqual(stream.readline(), HEADER + "\n")
                stream.seek(0)
                rows = list(csv.DictReader(stream))
            self.assertTrue(rows, path.name)
            for row in rows:
                self.assertEqual(set(row), set(HEADER.split(",")))
                for column in ("caller_offset", "target_offset"):
                    self.assertRegex(row[column], r"^0x[0-9a-f]+$")
            traces[int(path.name.split(".")[1])] = rows
        self.assertTrue(traces, "no per-PID traces")
        return result, traces

    def fixture_rows(self, rows, function):
        return [row for row in rows
                if row["caller_module"] == self.binary.name
                and int(row["caller_offset"], 16) == self.calls[function]]

    def test_fast_and_default_cover_callsites_without_targets(self):
        for flags in ((), ("-mode", "fast")):
            with self.subTest(flags=flags):
                _, traces = self.run_trace(flags)
                self.assertEqual(len(traces), 1)
                rows = next(iter(traces.values()))
                self.assertTrue(self.fixture_rows(rows, "dispatch"))
                self.assertFalse(self.fixture_rows(rows, "never_called"))
                self.assertTrue(all(row["target_module"] == "<not-recorded>"
                                    and row["target_offset"] == "0x0" for row in rows))

    def test_edge_records_exact_targets_and_threaded_hit_counts(self):
        _, traces = self.run_trace(("-mode", "edge"))
        self.assertEqual(len(traces), 1)
        rows = next(iter(traces.values()))
        self.assertFalse(self.fixture_rows(rows, "never_called"))
        edges = self.fixture_rows(rows, "dispatch")
        self.assertEqual(len(edges), 800)
        for target in ("target_a", "target_b"):
            hits = [row for row in edges
                    if row["target_module"] == self.binary.name
                    and int(row["target_offset"], 16) == self.symbols[target]]
            self.assertEqual(len(hits), 400, target)

    def test_fork_has_separate_pid_files_and_child_only_coverage(self):
        for mode in ("fast", "edge"):
            with self.subTest(mode=mode):
                result, traces = self.run_trace(("-mode", mode), ("fork",))
                pids = dict(line.split() for line in result.stdout.splitlines())
                self.assertEqual(set(traces), {int(pid) for pid in pids.values()})
                parent = traces[int(pids["parent"])]
                child = traces[int(pids["child"])]
                self.assertTrue(self.fixture_rows(parent, "before_fork"))
                self.assertFalse(self.fixture_rows(child, "before_fork"))
                for rows, target in ((parent, "target_a"), (child, "target_b")):
                    edges = self.fixture_rows(rows, "dispatch")
                    self.assertTrue(edges)
                    if mode == "edge":
                        self.assertEqual(len(edges), 1)
                        self.assertEqual(edges[0]["target_module"], self.binary.name)
                        self.assertEqual(int(edges[0]["target_offset"], 16),
                                         self.symbols[target])

    def test_invalid_arguments_fail_before_tracing(self):
        for flags in (("-mode",), ("-mode", "invalid"), ("-unknown",),
                      ("-mode", "fast", "-mode", "edge")):
            with self.subTest(flags=flags):
                self.run_trace(flags, success=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build/libicall_trace.so")
    parser.add_argument("--drrun", type=Path,
                        default=Path.home() / "tools/dynamorio/build/bin64/drrun")
    parser.add_argument("--cc", default="cc")
    OPTIONS, remaining = parser.parse_known_args()
    OPTIONS.client = OPTIONS.client.resolve()
    OPTIONS.drrun = OPTIONS.drrun.resolve()
    unittest.main(argv=[__file__, *remaining])
