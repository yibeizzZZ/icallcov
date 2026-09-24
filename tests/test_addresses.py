"""Address and classification regressions; native tools are optional.

Run with: python3 -m unittest discover -s tests -p test_addresses.py -v
Native fixtures and DynamoRIO traces live only in temporary directories.
"""

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import report
import scan


ROOT = Path(__file__).resolve().parents[1]


class ClassificationTests(unittest.TestCase):
    def test_explicit_directories_override_runtime_names_and_operands(self):
        for directory, category in (("src", "project"), ("test", "test")):
            for function in ("worker_start", "__libc_adapter", "_start"):
                with self.subTest(directory=directory, function=function):
                    self.assertEqual(report.classify_site(
                        function, f"/project/{directory}/probe.c:12",
                        "call *0x20(%rax) # 123 <__libc_start_main@GLIBC_2.34>",
                        project_root="/project", source_dirs=["src"],
                        test_dirs=["test"]), category)

    def test_runtime_symbols_match_symbol_boundaries(self):
        cases = [
            ("worker_start", "call *%rax", "unknown"),
            (None, "call *%rax # 123 <worker_start>", "unknown"),
            ("_start", "call *%rax", "runtime"),
            (None, "call *%rax # 123 <_start+0x10>", "runtime"),
            (None, "call *%rax # 123 <__libc_start_main@GLIBC_2.34>", "runtime"),
            ("__cxa_finalize", "call *%rax", "runtime"),
        ]
        for function, instruction, expected in cases:
            with self.subTest(function=function, instruction=instruction):
                self.assertEqual(report.classify_site(function, None, instruction), expected)

    def test_legacy_static_json_requires_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "static.json"
            path.write_text(json.dumps({"indirect_callsites": []}))
            with self.assertRaisesRegex(ValueError, "[Rr]egenerate|regenerate"):
                report.load_static(path)


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "x86_64"
                     and all(shutil.which(tool) for tool in ("cc", "objdump", "addr2line")),
                     "Linux x86-64 cc/binutils required")
class NativeAddressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="icallcov-addresses-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        source = cls.directory / "src" / "probe.c"
        source.parent.mkdir()
        source.write_text("""
__attribute__((noinline)) void sink(void) { __asm__ volatile("" ::: "memory"); }
__attribute__((noinline)) void dispatch(void (*fn)(void)) { fn(); }
__attribute__((noinline)) void worker_start(void (*fn)(void)) { fn(); }
__attribute__((noinline)) void never_called(void (*fn)(void)) { fn(); }
int main(void) { dispatch(sink); worker_start(sink); return 0; }
""")
        cls.fixtures = []
        for label, flags, base in (
            ("pie", ["-fPIE", "-pie"], 0),
            ("nonpie", ["-fno-pie", "-no-pie"], 0x400000),
            ("shifted", ["-fno-pie", "-no-pie", "-Wl,-Ttext-segment=0x600000"], 0x600000),
        ):
            binary = cls.directory / label
            subprocess.run(["cc", "-O0", "-g", *flags, str(source), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            disassembly = subprocess.check_output(
                ["objdump", "-d", "--no-show-raw-insn", str(binary)], text=True)
            body = disassembly.split("<dispatch>:\n", 1)[1].split("\n\n", 1)[0]
            address = int(re.search(r"^\s*([0-9a-f]+):\s+call\s+\*", body, re.M)[1], 16)
            cls.fixtures.append((binary, base, address))

    def test_scan_uses_module_relative_offsets_and_records_coordinate(self):
        for binary, base, address in self.fixtures:
            with self.subTest(binary=binary.name):
                sites = scan.scan_indirect_calls(binary)
                self.assertIn(address - base, {site["offset"] for site in sites})
                if base:
                    self.assertNotIn(address, {site["offset"] for site in sites})
                output = binary.with_suffix(".json")
                subprocess.run([sys.executable, str(ROOT / "scan.py"), str(binary),
                                "-o", str(output)], check=True, capture_output=True, text=True)
                data = json.loads(output.read_text())
                self.assertEqual(data.get("address_coordinate"), "module-relative")
                self.assertEqual(data.get("elf_image_base"), base)
                _, loaded = report.load_static(output)
                self.assertIn((binary.name, address - base), loaded)

    def test_symbolization_converts_relative_offsets_back_to_elf_addresses(self):
        for binary, base, address in self.fixtures:
            with self.subTest(binary=binary.name):
                function, location = report.symbolize(binary, address - base)
                self.assertEqual(function, "dispatch")
                self.assertIn("/src/probe.c:", location)

    def test_native_coverage_and_edge_symbolization_match_for_all_elf_layouts(self):
        drrun = Path(os.environ.get("DRRUN", str(Path.home() / "tools/dynamorio/build/bin64/drrun")))
        client = Path(os.environ.get("ICALL_TRACE_CLIENT", str(ROOT / "dynamorio/build/libicall_trace.so")))
        if not drrun.is_file() or not client.is_file():
            self.skipTest("DynamoRIO and built icall_trace client required")
        for binary, _, _ in self.fixtures:
            for mode in ("fast", "edge"):
                with self.subTest(binary=binary.name, mode=mode):
                    trace_dir = self.directory / f"{binary.name}-{mode}"
                    trace_dir.mkdir()
                    result = subprocess.run(
                        [str(drrun), "-c", str(client), "-mode", mode, "--", str(binary)],
                        cwd=trace_dir, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    traces = list(trace_dir.glob("dynamic.*.csv"))
                    self.assertEqual(len(traces), 1)
                    dynamic, edges, skipped = report.load_dynamic(traces[0])
                    self.assertEqual(skipped, 0)
                    sites = scan.scan_indirect_calls(binary)
                    records = report.build_site_records(
                        {(s["module"], s["offset"]): s for s in sites}, binary,
                        str(self.directory), ["src"], ["test"])
                    project = {key for key, record in records.items()
                               if record["category"] == "project"}
                    self.assertEqual({records[key]["function"] for key in project},
                                     {"dispatch", "worker_start", "never_called"})
                    covered = project & dynamic
                    self.assertEqual({records[key]["function"] for key in covered},
                                     {"dispatch", "worker_start"})
                    exported = report.build_edge_export(project, covered, records, edges, binary, {})
                    for entry in exported:
                        if mode == "fast" or entry["caller_function"] == "never_called":
                            self.assertEqual(entry["targets"], [])
                        else:
                            self.assertEqual(len(entry["targets"]), 1)
                            target = entry["targets"][0]
                            self.assertEqual(target["target_function"], "sink")
                            self.assertIn("/src/probe.c:", target["target_location"])


if __name__ == "__main__":
    unittest.main()
