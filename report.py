#!/usr/bin/env python3

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path


RUNTIME_MARKERS = (
    "__libc_",
    "__cxa_",
    "_start",
    "ld-linux",
    "@glibc",
    "@gcc",
    "__gmon_start__",
)


def normalize_module(name: str) -> str:
    return os.path.basename(name.strip())


def normalize_offset(value) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def load_static(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    sites = {}

    for entry in data.get("indirect_callsites", []):
        module = normalize_module(entry["module"])
        offset = normalize_offset(entry["offset"])

        sites[(module, offset)] = {
            "module": module,
            "offset": offset,
            "instruction": entry.get("instruction", ""),
        }

    return data, sites


def load_dynamic(path: Path):
    executed = set()
    edges = {}

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        required = {
            "caller_module",
            "caller_offset",
            "target_module",
            "target_offset",
        }

        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                "dynamic.csv must contain columns: "
                "caller_module, caller_offset, target_module, target_offset"
            )

        for row in reader:
            caller = (
                normalize_module(row["caller_module"]),
                normalize_offset(row["caller_offset"]),
            )
            target = (
                normalize_module(row["target_module"]),
                normalize_offset(row["target_offset"]),
            )

            executed.add(caller)
            edges.setdefault(caller, {})
            edges[caller][target] = edges[caller].get(target, 0) + 1

    return executed, edges


def symbolize(binary: Path, offset: int):
    try:
        result = subprocess.run(
            [
                "addr2line",
                "-f",
                "-C",
                "-e",
                str(binary),
                hex(offset),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None, None

    lines = [x.strip() for x in result.stdout.splitlines() if x.strip()]

    if not lines:
        return None, None

    function = lines[0] if len(lines) >= 1 else None
    location = lines[1] if len(lines) >= 2 else None

    if function == "??":
        function = None

    if location in {"??:0", "??:?", None}:
        location = None

    return function, location


def normalize_location_path(location):
    if not location:
        return None

    # addr2line output is often "path/to/file.c:123" or "file.c:?"
    # Strip the final ":line" part without breaking weird filenames too much.
    if ":" in location:
        path_part, _ = location.rsplit(":", 1)
    else:
        path_part = location

    return os.path.normpath(path_part)


def is_under(path, directory):
    if not path or not directory:
        return False

    try:
        path = os.path.abspath(path)
        directory = os.path.abspath(directory)
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def classify_site(function, location, instruction,
                  project_root=None,
                  source_dirs=None,
                  test_dirs=None):
    fn = (function or "").lower()
    loc = (location or "").lower()
    ins = (instruction or "").lower()

    # Runtime / loader / libc-ish patterns first.
    if any(marker in fn for marker in RUNTIME_MARKERS):
        return "runtime"

    if any(marker in ins for marker in RUNTIME_MARKERS):
        return "runtime"

    location_path = normalize_location_path(location)

    # If addr2line gives an absolute or project-relative path, prefer
    # directory-based classification.
    if project_root and location_path:
        candidate = location_path

        if not os.path.isabs(candidate):
            candidate = os.path.join(project_root, candidate)

        for test_dir in test_dirs or []:
            full_test_dir = (
                test_dir
                if os.path.isabs(test_dir)
                else os.path.join(project_root, test_dir)
            )
            if is_under(candidate, full_test_dir):
                return "test"

        for source_dir in source_dirs or []:
            full_source_dir = (
                source_dir
                if os.path.isabs(source_dir)
                else os.path.join(project_root, source_dir)
            )
            if is_under(candidate, full_source_dir):
                return "project"

    # Fallback heuristics for incomplete debug info.
    basename = os.path.basename(location_path or "").lower()

    if (
        basename.startswith("test-")
        or "/test/" in loc
        or "/tests/" in loc
    ):
        return "test"

    # libuv-specific fallback for now; directory classification above
    # is preferred and is what makes this reusable.
    if fn.startswith("uv_") or fn.startswith("uv__"):
        return "project"

    return "unknown"


def build_site_records(static_sites, binary, project_root, source_dirs, test_dirs):
    records = {}

    for key, site in static_sites.items():
        module, offset = key

        function = None
        location = None

        if binary is not None and binary.name == module:
            function, location = symbolize(binary, offset)

        category = classify_site(
            function,
            location,
            site["instruction"],
            project_root=project_root,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
        )

        records[key] = {
            **site,
            "function": function,
            "location": location,
            "category": category,
        }

    return records


def print_site(record, dynamic_edges=None, show_targets=False):
    module = record["module"]
    offset = record["offset"]
    instruction = record.get("instruction", "")
    function = record.get("function")
    location = record.get("location")

    line = f"{module}+0x{offset:x}"

    if instruction:
        line += f"    {instruction}"

    print(line)

    if function:
        print(f"    {function}")

    if location:
        print(f"    {location}")

    if show_targets and dynamic_edges is not None:
        key = (module, offset)
        targets = dynamic_edges.get(key, {})

        for (target_module, target_offset), count in sorted(
            targets.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        ):
            print(
                f"    -> {target_module}+0x{target_offset:x} "
                f"({count} hits)"
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare static indirect callsites against DynamoRIO execution, "
            "classify callsites, and report project-only coverage."
        )
    )

    parser.add_argument("static_json", help="JSON generated by scan.py")
    parser.add_argument("dynamic_csv", help="CSV generated by the DynamoRIO tracer")

    parser.add_argument(
        "--binary",
        help="Binary used for addr2line symbolization",
    )

    parser.add_argument(
        "--project-root",
        help="Project source root, e.g. /path/to/libuv",
    )

    parser.add_argument(
        "--source-dir",
        action="append",
        default=[],
        help=(
            "Project source directory relative to --project-root. "
            "May be supplied multiple times. Example: --source-dir src"
        ),
    )

    parser.add_argument(
        "--test-dir",
        action="append",
        default=[],
        help=(
            "Test directory relative to --project-root. "
            "May be supplied multiple times. Example: --test-dir test"
        ),
    )

    parser.add_argument(
        "--show-filtered",
        action="store_true",
        help="Print test, runtime, and unknown callsites after the main report",
    )

    parser.add_argument(
        "--show-covered",
        action="store_true",
        help="Print covered project callsites",
    )

    parser.add_argument(
        "--show-targets",
        action="store_true",
        help="Print observed dynamic targets for covered callsites",
    )

    args = parser.parse_args()

    static_path = Path(args.static_json)
    dynamic_path = Path(args.dynamic_csv)

    if not static_path.exists():
        print(f"error: static file not found: {static_path}", file=sys.stderr)
        sys.exit(1)

    if not dynamic_path.exists():
        print(f"error: dynamic file not found: {dynamic_path}", file=sys.stderr)
        sys.exit(1)

    static_data, static_sites = load_static(static_path)
    dynamic_sites, dynamic_edges = load_dynamic(dynamic_path)

    binary = None

    if args.binary:
        binary = Path(args.binary).resolve()
    elif static_data.get("binary"):
        candidate = Path(static_data["binary"])
        if candidate.exists():
            binary = candidate.resolve()

    project_root = Path(args.project_root).resolve() if args.project_root else None

    source_dirs = args.source_dir or ["src"]
    test_dirs = args.test_dir or ["test"]

    records = build_site_records(
        static_sites,
        binary,
        str(project_root) if project_root else None,
        source_dirs,
        test_dirs,
    )

    categories = {
        "project": set(),
        "test": set(),
        "runtime": set(),
        "unknown": set(),
    }

    for key, record in records.items():
        categories[record["category"]].add(key)

    project_sites = categories["project"]
    project_covered = project_sites & dynamic_sites
    project_uncovered = project_sites - dynamic_sites

    raw_total = len(static_sites)
    project_total = len(project_sites)
    project_covered_count = len(project_covered)
    project_uncovered_count = len(project_uncovered)

    coverage = (
        project_covered_count / project_total * 100.0
        if project_total
        else 0.0
    )

    print()
    print("Indirect Call Coverage")
    print("=" * 48)
    print(f"Raw indirect callsites : {raw_total}")
    print()
    print("Classification")
    print("-" * 48)
    print(f"Project               : {len(categories['project'])}")
    print(f"Test                  : {len(categories['test'])}")
    print(f"Runtime               : {len(categories['runtime'])}")
    print(f"Unknown               : {len(categories['unknown'])}")
    print()
    print("Project Coverage")
    print("-" * 48)
    print(f"Total project sites   : {project_total}")
    print(f"Covered               : {project_covered_count}")
    print(f"Uncovered             : {project_uncovered_count}")
    print(f"Coverage              : {coverage:.1f}%")
    print()

    if project_uncovered:
        print("UNCOVERED PROJECT CALLSITES")
        print("-" * 48)

        for key in sorted(project_uncovered):
            print_site(records[key])

        print()

    if args.show_covered and project_covered:
        print("COVERED PROJECT CALLSITES")
        print("-" * 48)

        for key in sorted(project_covered):
            print_site(
                records[key],
                dynamic_edges=dynamic_edges,
                show_targets=args.show_targets,
            )

        print()

    if args.show_filtered:
        for category in ("test", "runtime", "unknown"):
            keys = categories[category]

            print(f"{category.upper()} CALLSITES")
            print("-" * 48)

            if not keys:
                print("(none)")
            else:
                for key in sorted(keys):
                    print_site(records[key])

            print()


if __name__ == "__main__":
    main()
