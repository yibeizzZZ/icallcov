#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from elf_addresses import elf_image_base


RUNTIME_SYMBOLS = {"_start", "__gmon_start__"}
RUNTIME_PREFIXES = ("__libc_", "__cxa_", "ld-linux")


def is_runtime_symbol(symbol):
    # Match names, not arbitrary instruction substrings: worker_start is
    # not _start. objdump annotations can append +0xNN and symbol versions.
    symbol = re.split(r"[+-]0x[0-9a-f]+$", symbol.lower())[0]
    name, _, version = symbol.partition("@")
    return (name in RUNTIME_SYMBOLS or name.startswith(RUNTIME_PREFIXES)
            or version.lstrip("@").startswith(("glibc_", "gcc_")))


def normalize_module(name: str) -> str:
    return os.path.basename(name.strip())


def normalize_offset(value) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def load_static(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Old scans used ELF VAs even though their field was called "offset".
    # Reject them rather than silently reporting incorrect non-PIE coverage.
    image_base = data.get("elf_image_base")
    if (data.get("address_coordinate") != "module-relative"
            or type(image_base) is not int or image_base < 0 or image_base % 4096):
        raise ValueError(
            "static JSON has missing or unsupported address metadata; "
            "regenerate it with scan.py"
        )

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
    skipped_rows = 0

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
                "dynamic CSV must contain columns: "
                "caller_module, caller_offset, target_module, target_offset"
            )

        has_test_name = "test_name" in reader.fieldnames

        for row in reader:
            caller_module = (row.get("caller_module") or "").strip()
            caller_offset_raw = (row.get("caller_offset") or "").strip()
            target_module = (row.get("target_module") or "").strip()
            target_offset_raw = (row.get("target_offset") or "").strip()

            test_name = (
                (row.get("test_name") or "").strip() if has_test_name else ""
            )

            # Some resumed/older per-test traces may contain an embedded CSV
            # header row. Ignore those rows instead of treating
            # "caller_offset" as a hexadecimal address.
            if (
                caller_module == "caller_module"
                and caller_offset_raw == "caller_offset"
            ):
                skipped_rows += 1
                continue

            if not caller_module or not caller_offset_raw:
                skipped_rows += 1
                continue

            try:
                caller = (
                    normalize_module(caller_module),
                    normalize_offset(caller_offset_raw),
                )
            except ValueError:
                skipped_rows += 1
                continue

            executed.add(caller)

            # Fast callsite-only tracing deliberately stores a placeholder
            # target. It is still valid for coverage, but there is no edge
            # information to record.
            if target_module == "<not-recorded>":
                continue

            if not target_module or not target_offset_raw:
                continue

            try:
                target = (
                    normalize_module(target_module),
                    normalize_offset(target_offset_raw),
                )
            except ValueError:
                skipped_rows += 1
                continue

            edges.setdefault(caller, {})
            edge_info = edges[caller].setdefault(
                target, {"count": 0, "tests": set()}
            )
            edge_info["count"] += 1

            if test_name:
                edge_info["tests"].add(test_name)

    return executed, edges, skipped_rows


def symbolize(binary: Path, offset: int):
    # addr2line consumes ELF VAs, whereas all coverage/edge keys are module
    # offsets. This conversion applies equally to callers and targets.
    address = elf_image_base(binary) + offset
    try:
        result = subprocess.run(
            [
                "addr2line",
                "-f",
                "-C",
                "-e",
                str(binary),
                hex(address),
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
                  test_dirs=None,
                  libuv_compat=False):
    fn = (function or "").lower()
    loc = (location or "").lower()

    location_path = normalize_location_path(location)

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

    # Explicit source/test directories are authoritative. Runtime name
    # heuristics are only a fallback for sites outside those directories.
    symbols = [fn, *re.findall(r"<([^<>]+)>", instruction or "")]
    if any(is_runtime_symbol(symbol) for symbol in symbols):
        return "runtime"

    basename = os.path.basename(location_path or "").lower()

    if (
        basename.startswith("test-")
        or "/test/" in loc
        or "/tests/" in loc
    ):
        return "test"

    # This is a libuv-specific fallback (its public/internal functions are
    # named uv_*/uv__*) and is intentionally NOT part of generic
    # classification; it only applies with --libuv-compat.
    if libuv_compat and (fn.startswith("uv_") or fn.startswith("uv__")):
        return "project"

    return "unknown"


def build_site_records(
    static_sites,
    binary,
    project_root,
    source_dirs,
    test_dirs,
    libuv_compat=False,
):
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
            libuv_compat=libuv_compat,
        )

        records[key] = {
            **site,
            "function": function,
            "location": location,
            "category": category,
        }

    return records


def build_edge_export(
    project_sites,
    project_covered,
    records,
    dynamic_edges,
    binary,
    target_cache,
):
    """Build a machine-readable record of observed runtime targets per
    project callsite. This is strictly an observed edge set (from dynamic
    tracing) and must not be mistaken for a complete/legal target set."""
    export = []

    for key in sorted(project_sites):
        module, offset = key
        record = records[key]

        status = "observed" if key in project_covered else "unobserved"
        targets_map = dynamic_edges.get(key, {})

        targets = []

        for (target_module, target_offset), info in sorted(
            targets_map.items(),
            key=lambda item: (item[0][0], item[0][1]),
        ):
            target_function = None
            target_location = None

            if binary is not None and binary.name == target_module:
                cache_key = (target_module, target_offset)

                if cache_key not in target_cache:
                    target_cache[cache_key] = symbolize(binary, target_offset)

                target_function, target_location = target_cache[cache_key]

            targets.append(
                {
                    "target_module": target_module,
                    "target_offset": f"0x{target_offset:x}",
                    "target_function": target_function,
                    "target_location": target_location,
                    "total_hits": info["count"],
                    "tests": sorted(info["tests"]),
                }
            )

        export.append(
            {
                "caller_module": module,
                "caller_offset": f"0x{offset:x}",
                "caller_function": record.get("function"),
                "caller_location": record.get("location"),
                "instruction": record.get("instruction", ""),
                "status": status,
                "observed_target_count": len(targets),
                "targets": targets,
            }
        )

    return export


def print_site(
    record,
    dynamic_edges=None,
    show_targets=False,
    binary=None,
    target_cache=None,
):
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

        # Fast-mode traces never populate the edges map (their rows have no
        # recorded target), so `targets` is simply empty here; that is
        # expected and not an error.
        if targets:
            print(
                f"    observed targets (dynamic, not a complete legal "
                f"target set): {len(targets)} unique"
            )

            for (target_module, target_offset), info in sorted(
                targets.items(),
                key=lambda item: (
                    -item[1]["count"],
                    item[0][0],
                    item[0][1],
                ),
            ):
                count = info["count"]
                target_line = (
                    f"    -> {target_module}+0x{target_offset:x} "
                    f"({count} hits)"
                )

                target_function = None
                target_location = None

                if (
                    target_cache is not None
                    and binary is not None
                    and binary.name == target_module
                ):
                    cache_key = (target_module, target_offset)

                    if cache_key not in target_cache:
                        target_cache[cache_key] = symbolize(
                            binary, target_offset
                        )

                    target_function, target_location = target_cache[cache_key]

                print(target_line)

                if target_function:
                    print(f"        {target_function}")

                if target_location:
                    print(f"        {target_location}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare static indirect callsites against DynamoRIO execution, "
            "classify callsites, and report project-only coverage."
        )
    )

    parser.add_argument("static_json", help="JSON generated by scan.py")
    parser.add_argument("dynamic_csv", help="CSV generated by the DynamoRIO tracer")

    parser.add_argument("--binary", help="Binary used for addr2line symbolization")
    parser.add_argument("--project-root", help="Project source root")

    parser.add_argument(
        "--source-dir",
        action="append",
        default=[],
        help="Project source directory relative to --project-root",
    )

    parser.add_argument(
        "--test-dir",
        action="append",
        default=[],
        help="Test directory relative to --project-root",
    )

    parser.add_argument(
        "--libuv-compat",
        action="store_true",
        help=(
            "Enable a libuv-specific classification fallback that treats "
            "uv_*/uv__* function names as project code. Off by default; "
            "generic classification relies on --project-root/--source-dir/"
            "--test-dir instead."
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
        help="Print observed dynamic targets when target tracing is available",
    )

    parser.add_argument(
        "--export-edges",
        help=(
            "Write a JSON file with one record per project callsite "
            "listing its observed (not legal/complete) runtime targets, "
            "aggregated hit counts, and observing tests"
        ),
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

    try:
        static_data, static_sites = load_static(static_path)
        dynamic_sites, dynamic_edges, skipped_rows = load_dynamic(dynamic_path)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    binary = None

    if args.binary:
        binary = Path(args.binary).resolve()
    elif static_data.get("binary"):
        candidate = Path(static_data["binary"])
        if candidate.exists():
            binary = candidate.resolve()

    if binary is not None:
        try:
            if elf_image_base(binary) != static_data["elf_image_base"]:
                raise ValueError("binary ELF image base differs from static JSON; "
                                 "regenerate it with scan.py for this binary")
        except (OSError, ValueError) as error:
            parser.error(str(error))

    project_root = Path(args.project_root).resolve() if args.project_root else None
    source_dirs = args.source_dir or ["src"]
    test_dirs = args.test_dir or ["test"]

    records = build_site_records(
        static_sites,
        binary,
        str(project_root) if project_root else None,
        source_dirs,
        test_dirs,
        libuv_compat=args.libuv_compat,
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

    if skipped_rows:
        print(f"Skipped malformed rows: {skipped_rows}")

    print()

    target_cache = {}

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
                binary=binary,
                target_cache=target_cache,
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

    if args.export_edges:
        edge_export = build_edge_export(
            project_sites,
            project_covered,
            records,
            dynamic_edges,
            binary,
            target_cache,
        )

        export_path = Path(args.export_edges)
        export_path.parent.mkdir(parents=True, exist_ok=True)

        with export_path.open("w", encoding="utf-8") as f:
            json.dump(edge_export, f, indent=2)
            f.write("\n")

        print(f"Observed edge set written to: {export_path}")


if __name__ == "__main__":
    main()
