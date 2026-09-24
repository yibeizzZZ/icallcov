#!/usr/bin/env python3

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from runners import RUNNER_NAMES, build_runner


TRACE_HEADER = [
    "caller_module",
    "caller_offset",
    "target_module",
    "target_offset",
]


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def trace_stem(name: str) -> str:
    # Keep the original name in the digest: foo/bar and foo_bar differ.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
    return f"{safe_name(name)[:80]}-{digest}"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def executable_identity(path: Path):
    return {"path": str(path.resolve()), "sha256": file_digest(path)}


def resolve_executable(command, cwd):
    if not command:
        raise ValueError("empty test command")
    command = [str(arg) for arg in command]
    if "/" in command[0]:
        executable = Path(command[0])
        if not executable.is_absolute():
            executable = cwd / executable
    else:
        # Resolve relative PATH entries against the test's working directory,
        # just as exec would, rather than against the suite driver's cwd.
        search_path = os.pathsep.join(
            str(Path(entry) if os.path.isabs(entry) else cwd / entry)
            for entry in os.get_exec_path()
        )
        found = shutil.which(command[0], path=search_path)
        if found is None:
            raise FileNotFoundError(f"test executable not found: {command[0]}")
        executable = Path(found)
    # This path is for fingerprinting only. Execute the original argv: even
    # dereferencing a symlink can change argv[0] and multicall-program behavior.
    return executable


def trace_identity(test, runner, mode, command, cwd, client, drrun, timeout):
    # Store only a digest of the environment: it may contain credentials.
    environment = json.dumps(sorted(os.environ.items()), ensure_ascii=True)
    return {
        "test_name": test["name"],
        "helpers": test["helpers"],
        "runner": runner,
        "mode": mode,
        "command": command,
        "executable": executable_identity(resolve_executable(command, cwd)),
        "cwd": str(cwd),
        "client": executable_identity(client),
        "drrun": executable_identity(drrun),
        "environment_sha256": hashlib.sha256(environment.encode()).hexdigest(),
        "timeout": timeout,
    }


def load_resume(metadata_path, trace_path, identity):
    try:
        with metadata_path.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        if (metadata.get("version") != 1 or metadata.get("identity") != identity
                or metadata.get("trace_sha256") != file_digest(trace_path)):
            return None
        result = metadata["result"]
        if (result["status"] != "passed" or type(result["returncode"]) is not int
                or result["returncode"] != 0
                or result["test_name"] != identity["test_name"]
                or result["helpers"] != identity["helpers"]):
            return None
        if any(type(result[key]) is not int or result[key] < 0 for key in
               ("process_traces", "trace_events", "unique_callsites")):
            return None
        duration = result["duration_seconds"]
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or duration < 0 or result["process_traces"] < 1):
            return None
        rows = read_trace(trace_path)
        unique = len({(row["caller_module"], row["caller_offset"]) for row in rows})
        if len(rows) != result["trace_events"] or unique != result["unique_callsites"]:
            return None
        return {**result, "rows": rows, "resumed": True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, csv.Error):
        # Old CSVs, interrupted writes and incompatible records require a run.
        return None


def save_metadata(path, trace_path, identity, result):
    metadata = {
        "version": 1,
        "identity": identity,
        "trace_sha256": file_digest(trace_path),
        "result": {key: result[key] for key in (
            "test_name", "helpers", "status", "returncode", "duration_seconds",
            "trace_events", "unique_callsites", "process_traces",
        )},
    }
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def resolve_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def default_drrun() -> Path:
    dynamorio_home = os.environ.get("DYNAMORIO_HOME")

    if dynamorio_home:
        return resolve_path(
            os.path.join(dynamorio_home, "build", "bin64", "drrun")
        )

    return resolve_path("~/tools/dynamorio/build/bin64/drrun")


def read_trace(trace_path: Path):
    rows = []

    with trace_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ValueError(f"{trace_path} has no CSV header")

        missing = set(TRACE_HEADER) - set(reader.fieldnames)

        if missing:
            raise ValueError(
                f"{trace_path} is missing CSV columns: "
                + ", ".join(sorted(missing))
            )

        for row in reader:
            if None in row or any(not row.get(key) for key in TRACE_HEADER):
                raise ValueError(f"{trace_path} contains an incomplete CSV row")
            int(row["caller_offset"], 0)
            int(row["target_offset"], 0)
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


def cleanup_pid_traces(trace_files):
    # Delete only files owned by this run, never every trace in the cwd.
    for trace_path in trace_files:
        try:
            trace_path.unlink()
        except FileNotFoundError:
            pass


def write_test_trace(destination: Path, rows):
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACE_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(destination)


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
        "resumed",
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
                    "resumed": result.get("resumed", False),
                }
            )


class TraceIsolationError(RuntimeError):
    """The suite must stop rather than let a process leak into the next test."""


@contextmanager
def trace_workspace(cwd):
    # This lock coordinates icallcov suites sharing a cwd. Never unlink the
    # lock file: another suite could otherwise lock a different inode.
    with (cwd / ".icallcov-trace.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TraceIsolationError(f"another suite is tracing in {cwd}") from exc
        # Preserve old traces outside the tracer's filename namespace while
        # running. A reused PID can then neither overwrite an old trace nor
        # cause us to mistake its new output for an old file.
        existing = list(cwd.glob("dynamic.*.csv"))
        backup = Path(tempfile.mkdtemp(prefix=".icallcov-preserved-", dir=cwd))
        moved = []
        ready = False
        try:
            for path in existing:
                path.rename(backup / path.name)
                moved.append(path)
            ready = True
            yield
        finally:
            try:
                try:
                    if ready:
                        cleanup_pid_traces(cwd.glob("dynamic.*.csv"))
                finally:
                    for path in moved:
                        (backup / path.name).replace(path)
                    backup.rmdir()
            except OSError as exc:
                raise TraceIsolationError(
                    f"trace cleanup/restoration failed in {cwd}; "
                    f"preserved files may remain in {backup}: {exc}"
                ) from exc


def group_is_running(pgid):
    # Linux /proc lets us distinguish surviving writers from unreaped zombies.
    # killpg(pgid, 0) alone considers zombies alive indefinitely.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return False


def stop_process_group(proc):
    # start_new_session makes the child PID its session and process-group ID.
    # Also remove residual children after normal leader exit, before collecting.
    for sig, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            proc.wait()
            return
        deadline = time.monotonic() + grace
        while True:
            proc.poll()  # Reap the leader; orphan zombies cannot write traces.
            if not group_is_running(proc.pid):
                proc.wait()
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    raise TraceIsolationError(f"cannot stop test process group {proc.pid}; aborting suite")


def run_test(
    *,
    test,
    app_command,
    drrun: Path,
    client: Path,
    mode: str,
    cwd: Path,
    log_path: Path,
    timeout: float,
):
    name = test["name"]
    helpers = test["helpers"]
    command = [str(drrun), "-c", str(client), "-mode", mode, "--", *app_command]
    started = time.monotonic()
    timed_out = False

    with trace_workspace(cwd):
        # Files avoid communicate() hanging on pipes inherited by descendants.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
            proc = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err,
                                    start_new_session=True)
            try:
                proc.wait(timeout=timeout if timeout > 0 else None)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    stop_process_group(proc)
                except Exception as exc:
                    raise TraceIsolationError(
                        f"process-group cleanup failed for {proc.pid}: {exc}"
                    ) from exc
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(), err.read()

        # Collection is allowed only after the entire test group has stopped.
        trace_files, rows = collect_pid_traces(cwd)
        duration = time.monotonic() - started
        returncode = None if timed_out else proc.returncode
        status = "timeout" if timed_out else ("passed" if returncode == 0 else "failed")
        if status == "passed" and not trace_files:
            status = "runner-error"
            stderr += "\nicallcov: test produced no per-process trace files\n"

        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as stream:
            stream.write(f"test: {name}\nhelpers: {' '.join(helpers)}\n")
            stream.write(f"command: {' '.join(command)}\nreturncode: {returncode}\n")
            stream.write(f"timed_out: {timed_out}\nduration_seconds: {duration:.3f}\n")
            stream.write(f"process_traces: {len(trace_files)}\n")
            for path in trace_files:
                stream.write(f"trace_file: {path.name}\n")
            stream.write(f"\n===== STDOUT =====\n{stdout}\n===== STDERR =====\n{stderr}")

        return {
            "test_name": name, "helpers": helpers, "status": status,
            "returncode": returncode, "duration_seconds": duration,
            "trace_files": trace_files, "rows": rows, "resumed": False,
        }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run a test suite (via a pluggable runner adapter) under the "
            "icallcov DynamoRIO client and aggregate per-process "
            "indirect-call traces."
        )
    )

    parser.add_argument(
        "--runner",
        choices=RUNNER_NAMES,
        default="libuv",
        help=(
            "Test-runner adapter to use (default: libuv, kept for "
            "backward compatibility)"
        ),
    )

    parser.add_argument(
        "--binary",
        help="Path to the test binary (required for --runner libuv/gtest)",
    )

    parser.add_argument(
        "--build-dir",
        help="CMake build directory (required for --runner ctest)",
    )

    parser.add_argument(
        "--ctest-bin",
        default="ctest",
        help="Path to the ctest executable (default: ctest)",
    )

    parser.add_argument(
        "--tests-file",
        help=(
            "File of one test command per line "
            "(required for --runner commands)"
        ),
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
        help=(
            "Working directory for test execution "
            "(a runner may override this per test, e.g. CTest)"
        ),
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
        help="Reuse only verified successful traces with matching execution metadata",
    )

    args = parser.parse_args()

    if args.runner in ("libuv", "gtest") and not args.binary:
        print(
            f"error: --binary is required for --runner {args.runner}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.runner == "ctest" and not args.build_dir:
        print(
            "error: --build-dir is required for --runner ctest",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.runner == "commands" and not args.tests_file:
        print(
            "error: --tests-file is required for --runner commands",
            file=sys.stderr,
        )
        sys.exit(1)

    binary = resolve_path(args.binary) if args.binary else None
    build_dir = resolve_path(args.build_dir) if args.build_dir else None
    tests_file = resolve_path(args.tests_file) if args.tests_file else None
    drrun = resolve_path(args.drrun)
    client = resolve_path(args.client)
    cwd = resolve_path(args.cwd)
    output_dir = resolve_path(args.output_dir or f"coverage/{args.mode}")

    path_checks = [("drrun", drrun), ("DynamoRIO client", client)]

    if binary is not None:
        path_checks.append(("test binary", binary))

    if build_dir is not None:
        path_checks.append(("build dir", build_dir))

    if tests_file is not None:
        path_checks.append(("tests file", tests_file))

    for description, path in path_checks:
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

    try:
        runner = build_runner(
            args.runner,
            binary=binary,
            build_dir=build_dir,
            ctest_bin=args.ctest_bin,
            tests_file=tests_file,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    traces_dir = output_dir / "traces"
    logs_dir = output_dir / "logs"

    traces_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        tests = runner.discover_tests()
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
    print(f"Runner: {args.runner}")

    if binary is not None:
        print(f"Binary: {binary}")

    print(f"Mode: {args.mode}")
    print(f"Output: {output_dir}")
    print()

    stems = [trace_stem(test["name"]) for test in tests]
    if len(set(stems)) != len(stems):
        parser.error("test names must be unique; duplicate trace identities discovered")

    results = []
    suite_rows = []

    for index, test in enumerate(tests, start=1):
        name = test["name"]
        stem = trace_stem(name)
        trace_path = traces_dir / f"{stem}.csv"
        metadata_path = traces_dir / f"{stem}.json"
        log_path = logs_dir / f"{stem}.log"
        identity = None
        print(f"[{index}/{len(tests)}] {name}")

        try:
            working_dir = runner.working_dir_for_test(test)
            test_cwd = resolve_path(working_dir) if working_dir else cwd
            app_command = [str(arg) for arg in runner.command_for_test(test)]
            identity = trace_identity(test, args.runner, args.mode, app_command,
                                      test_cwd, client, drrun, args.timeout)
            result = load_resume(metadata_path, trace_path, identity) if args.resume else None
            if result is None:
                # Invalidate first, so interruption cannot leave an old success
                # sidecar next to a new or partially written CSV.
                metadata_path.unlink(missing_ok=True)
                result = run_test(
                    test=test, app_command=app_command, drrun=drrun, client=client,
                    mode=args.mode, cwd=test_cwd, log_path=log_path, timeout=args.timeout,
                )
        except TraceIsolationError as exc:
            metadata_path.unlink(missing_ok=True)
            # A group that cannot be stopped makes subsequent collection unsafe.
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            metadata_path.unlink(missing_ok=True)
            result = {
                "test_name": name, "helpers": test["helpers"], "status": "runner-error",
                "returncode": None, "duration_seconds": 0.0,
                "trace_files": [], "rows": [], "resumed": False,
            }
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"\nRUNNER ERROR:\n{exc}\n")

        rows = result["rows"]
        if not result.get("resumed", False):
            write_test_trace(trace_path, rows)
            result["trace_events"] = len(rows)
            result["unique_callsites"] = len({
                (row["caller_module"], row["caller_offset"]) for row in rows
            })
            result["process_traces"] = len(result["trace_files"])
            save_metadata(metadata_path, trace_path, identity, result)

        results.append(result)
        suite_rows.extend({"test_name": name, **row} for row in rows)
        reused = " (reused verified trace)" if result.get("resumed", False) else ""
        print(
            f"    {result['status']}{reused}, {result['duration_seconds']:.2f}s, "
            f"{len(rows)} events, {result['unique_callsites']} unique callsites, "
            f"{result['process_traces']} process trace(s)"
        )

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
        result.get("resumed", False)
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

    if failed or timed_out or runner_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
