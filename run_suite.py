#!/usr/bin/env python3

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path


TRACE_HEADER = [
    "caller_module",
    "caller_offset",
    "target_module",
    "target_offset",
]


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def resolve_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def default_drrun() -> Path:
    dynamorio_home = os.environ.get("DYNAMORIO_HOME")

    if dynamorio_home:
        return resolve_path(
            os.path.join(dynamorio_home, "build", "bin64", "drrun")
        )

    return resolve_path("~/tools/dynamorio/build/bin64/drrun")


def parse_test_list(output: str):
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


def discover_tests(binary: Path):
    proc = subprocess.run(
        [str(binary), "--list"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "failed to list tests\n"
            f"command: {binary} --list\n"
            f"exit code: {proc.returncode}\n"
            f"stderr:\n{proc.stderr}"
        )

    tests = parse_test_list(proc.stdout)

    if not tests:
        raise RuntimeError(
            "test discovery returned no tests; "
            "expected libuv-style `--list` output"
        )

    return tests


def read_trace(trace_path: Path):
    rows = []

    with trace_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            return []

        missing = set(TRACE_HEADER) - set(reader.fieldnames)

        if missing:
            raise ValueError(
                f"{trace_path} is missing CSV columns: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            rows.append(
                {
                    key: row[key]
                    for key in TRACE_HEADER
                }
            )

    return rows


def collect_pid_traces(cwd: Path):
    trace_files = sorted(cwd.glob("dynamic.*.csv"))
    rows = []

    for trace_path in trace_files:
        rows.extend(read_trace(trace_path))

    return trace_files, rows


def cleanup_pid_traces(cwd: Path):
    for trace_path in cwd.glob("dynamic.*.csv"):
        try:
            trace_path.unlink()
        except FileNotFoundError:
            pass


def write_test_trace(destination: Path, rows):
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACE_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def write_suite_trace(destination: Path, suite_rows):
    destination.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["test_name"] + TRACE_HEADER

    with destination.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(suite_rows)


def write_summary(destination: Path, results):
    destination.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "test_name",
        "status",
        "returncode",
        "duration_seconds",
        "trace_events",
        "unique_callsites",
        "process_traces",
        "helpers",
    ]

    with destination.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for result in results:
            writer.writerow(
                {
                    "test_name": result["test_name"],
                    "status": result["status"],
                    "returncode": (
                        ""
                        if result["returncode"] is None
                        else result["returncode"]
                    ),
                    "duration_seconds": f"{result['duration_seconds']:.3f}",
                    "trace_events": result["trace_events"],
                    "unique_callsites": result["unique_callsites"],
                    "process_traces": result["process_traces"],
                    "helpers": " ".join(result["helpers"]),
                }
            )


def run_test(
    *,
    test,
    binary: Path,
    drrun: Path,
    client: Path,
    mode: str,
    cwd: Path,
    log_path: Path,
    timeout: float,
):
    name = test["name"]
    helpers = test["helpers"]

    cleanup_pid_traces(cwd)

    # IMPORTANT:
    # Always invoke libuv through its normal test runner:
    #
    #     uv_run_tests_a TEST_NAME
    #
    # Do NOT use:
    #
    #     uv_run_tests_a TEST_NAME TEST_NAME
    #
    # The two-argument form calls run_test_part() directly and bypasses
    # libuv's normal process/test-runner semantics. Some valid tests
    # (for example fork_fs_events_child) can hang when invoked that way.
    #
    # Per-PID tracing means it is safe for the normal runner to create
    # child/helper processes: DynamoRIO follows them and each process
    # writes its own dynamic.<pid>.csv file.
    app_args = [str(binary), name]

    command = [
        str(drrun),
        "-c",
        str(client),
        "-mode",
        mode,
        "--",
        *app_args,
    ]

    started = time.monotonic()

    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout if timeout > 0 else None,
            check=False,
        )

        duration = time.monotonic() - started
        returncode = proc.returncode
        timed_out = False
        stdout = proc.stdout
        stderr = proc.stderr

    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        returncode = None
        timed_out = True

        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")

        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

    trace_files, rows = collect_pid_traces(cwd)

    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as f:
        f.write(f"test: {name}\n")
        f.write(f"helpers: {' '.join(helpers)}\n")
        f.write(f"command: {' '.join(command)}\n")
        f.write(f"returncode: {returncode}\n")
        f.write(f"timed_out: {timed_out}\n")
        f.write(f"duration_seconds: {duration:.3f}\n")
        f.write(f"process_traces: {len(trace_files)}\n")

        for trace_path in trace_files:
            f.write(f"trace_file: {trace_path.name}\n")

        f.write("\n===== STDOUT =====\n")
        f.write(stdout)

        f.write("\n===== STDERR =====\n")
        f.write(stderr)

    if timed_out:
        status = "timeout"
    elif returncode == 0:
        status = "passed"
    else:
        status = "failed"

    return {
        "test_name": name,
        "helpers": helpers,
        "status": status,
        "returncode": returncode,
        "duration_seconds": duration,
        "trace_files": trace_files,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run a libuv-style test suite under the icallcov DynamoRIO "
            "client and aggregate per-process indirect-call traces."
        )
    )

    parser.add_argument(
        "--binary",
        required=True,
        help="Path to the test binary",
    )

    parser.add_argument(
        "--drrun",
        default=str(default_drrun()),
        help=(
            "Path to DynamoRIO drrun "
            "(default: $DYNAMORIO_HOME/build/bin64/drrun or "
            "~/tools/dynamorio/build/bin64/drrun)"
        ),
    )

    parser.add_argument(
        "--client",
        default="./dynamorio/build/libicall_trace.so",
        help="Path to libicall_trace.so",
    )

    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory for test execution",
    )

    parser.add_argument(
        "--mode",
        choices=("fast", "edge"),
        default="fast",
        help="Tracing mode to request from the DynamoRIO client (default: fast)",
    )

    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory for traces, logs, suite.csv, and summary.csv "
            "(default: coverage/<mode>)"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Outer timeout per test in seconds; <= 0 disables it",
    )

    parser.add_argument(
        "--match",
        help="Only run test names matching this regular expression",
    )

    parser.add_argument(
        "--exclude",
        help="Skip test names matching this regular expression",
    )

    parser.add_argument(
        "--limit",
        type=int,
        help="Run at most this many selected tests",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip tests whose per-test CSV already exists",
    )

    args = parser.parse_args()

    binary = resolve_path(args.binary)
    drrun = resolve_path(args.drrun)
    client = resolve_path(args.client)
    cwd = resolve_path(args.cwd)
    output_dir = resolve_path(args.output_dir or f"coverage/{args.mode}")

    for description, path in (
        ("test binary", binary),
        ("drrun", drrun),
        ("DynamoRIO client", client),
    ):
        if not path.exists():
            print(
                f"error: {description} not found: {path}",
                file=sys.stderr,
            )
            sys.exit(1)

    if not cwd.exists() or not cwd.is_dir():
        print(
            f"error: cwd is not a directory: {cwd}",
            file=sys.stderr,
        )
        sys.exit(1)

    traces_dir = output_dir / "traces"
    logs_dir = output_dir / "logs"

    traces_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        tests = discover_tests(binary)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.match:
        pattern = re.compile(args.match)
        tests = [
            test
            for test in tests
            if pattern.search(test["name"])
        ]

    if args.exclude:
        pattern = re.compile(args.exclude)
        tests = [
            test
            for test in tests
            if not pattern.search(test["name"])
        ]

    if args.limit is not None:
        tests = tests[: args.limit]

    if not tests:
        print("No tests selected.")
        return

    print(f"Discovered/selected tests: {len(tests)}")
    print(f"Binary: {binary}")
    print(f"Mode: {args.mode}")
    print(f"Output: {output_dir}")
    print()

    results = []
    suite_rows = []

    for index, test in enumerate(tests, start=1):
        name = test["name"]
        stem = safe_name(name)

        trace_path = traces_dir / f"{stem}.csv"
        log_path = logs_dir / f"{stem}.log"

        if args.resume and trace_path.exists():
            rows = read_trace(trace_path)

            unique_callsites = {
                (row["caller_module"], row["caller_offset"])
                for row in rows
            }

            result = {
                "test_name": name,
                "helpers": test["helpers"],
                "status": "resumed",
                "returncode": None,
                "duration_seconds": 0.0,
                "rows": rows,
                "trace_events": len(rows),
                "unique_callsites": len(unique_callsites),
                "process_traces": 0,
            }

            results.append(result)

            for row in rows:
                suite_rows.append(
                    {
                        "test_name": name,
                        **row,
                    }
                )

            print(
                f"[{index}/{len(tests)}] {name}: "
                f"resumed ({len(rows)} events)"
            )
            continue

        helper_text = ""

        if test["helpers"]:
            helper_text = (
                f" [helpers: {' '.join(test['helpers'])}]"
            )

        print(
            f"[{index}/{len(tests)}] "
            f"{name}{helper_text}"
        )

        try:
            result = run_test(
                test=test,
                binary=binary,
                drrun=drrun,
                client=client,
                mode=args.mode,
                cwd=cwd,
                log_path=log_path,
                timeout=args.timeout,
            )
        except Exception as exc:
            result = {
                "test_name": name,
                "helpers": test["helpers"],
                "status": "runner-error",
                "returncode": None,
                "duration_seconds": 0.0,
                "trace_files": [],
                "rows": [],
            }

            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\nRUNNER ERROR:\n{exc}\n")

        rows = result["rows"]

        write_test_trace(trace_path, rows)

        unique_callsites = {
            (row["caller_module"], row["caller_offset"])
            for row in rows
        }

        result["trace_events"] = len(rows)
        result["unique_callsites"] = len(unique_callsites)
        result["process_traces"] = len(result["trace_files"])

        results.append(result)

        for row in rows:
            suite_rows.append(
                {
                    "test_name": name,
                    **row,
                }
            )

        print(
            f"    {result['status']}, "
            f"{result['duration_seconds']:.2f}s, "
            f"{len(rows)} events, "
            f"{len(unique_callsites)} unique callsites, "
            f"{len(result['trace_files'])} process trace(s)"
        )

        cleanup_pid_traces(cwd)

    suite_path = output_dir / "suite.csv"
    summary_path = output_dir / "summary.csv"

    write_suite_trace(suite_path, suite_rows)
    write_summary(summary_path, results)

    passed = sum(
        result["status"] == "passed"
        for result in results
    )

    failed = sum(
        result["status"] == "failed"
        for result in results
    )

    timed_out = sum(
        result["status"] == "timeout"
        for result in results
    )

    runner_errors = sum(
        result["status"] == "runner-error"
        for result in results
    )

    resumed = sum(
        result["status"] == "resumed"
        for result in results
    )

    all_callsites = {
        (row["caller_module"], row["caller_offset"])
        for row in suite_rows
    }

    print()
    print("Suite complete")
    print("=" * 56)
    print(f"Tests              : {len(results)}")
    print(f"Passed             : {passed}")
    print(f"Failed             : {failed}")
    print(f"Timed out          : {timed_out}")
    print(f"Runner errors      : {runner_errors}")
    print(f"Resumed            : {resumed}")
    print(f"Trace events       : {len(suite_rows)}")
    print(f"Unique callsites   : {len(all_callsites)}")
    print(f"Suite trace        : {suite_path}")
    print(f"Summary            : {summary_path}")


if __name__ == "__main__":
    main()
