#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from elf_addresses import elf_image_base


# Example objdump lines:
#
# 0000000000001234 <foo>:
#     1240:       call   *%rax
#     1243:       call   *0x20(%rbx)
#     1247:       call   1100 <bar>
#
# We only want the first two.

INSTRUCTION_RE = re.compile(
    r"^\s*([0-9a-fA-F]+):\s+"
    r"(callq?|lcall)\s+"
    r"(.+?)\s*$"
)


def run_objdump(binary: Path) -> str:
    cmd = [
        "objdump",
        "-d",
        "--no-show-raw-insn",
        str(binary),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print("error: objdump not found", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"error: objdump failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)

    return result.stdout


def is_indirect_call(operand: str) -> bool:
    """
    AT&T syntax:

        call *%rax
        call *0x20(%rbx)

    Direct calls look like:

        call 1234 <foo>

    So '*' at the beginning means indirect.
    """
    operand = operand.strip()

    return operand.startswith("*")


def scan_indirect_calls(binary: Path):
    image_base = elf_image_base(binary)
    output = run_objdump(binary)

    callsites = []

    for line in output.splitlines():
        match = INSTRUCTION_RE.match(line)

        if not match:
            continue

        address_str = match.group(1)
        mnemonic = match.group(2)
        operand = match.group(3)

        if not is_indirect_call(operand):
            continue

        # objdump prints ELF virtual addresses; the tracer records offsets
        # from the module mapping start, independent of PIE/load bias.
        address = int(address_str, 16) - image_base

        callsites.append(
            {
                "module": binary.name,
                "offset": address,
                "offset_hex": f"0x{address:x}",
                "instruction": f"{mnemonic} {operand}",
            }
        )

    return callsites


def main():
    parser = argparse.ArgumentParser(
        description="Find indirect x86-64 callsites in an ELF binary."
    )

    parser.add_argument(
        "binary",
        help="ELF executable or shared library to scan",
    )

    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Write JSON output to this file",
    )

    args = parser.parse_args()

    binary = Path(args.binary).resolve()

    if not binary.exists():
        print(f"error: file not found: {binary}", file=sys.stderr)
        sys.exit(1)

    if not binary.is_file():
        print(f"error: not a file: {binary}", file=sys.stderr)
        sys.exit(1)

    try:
        callsites = scan_indirect_calls(binary)
        image_base = elf_image_base(binary)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    result = {
        "binary": str(binary),
        "module": binary.name,
        "address_coordinate": "module-relative",
        "elf_image_base": image_base,
        "indirect_callsites": callsites,
        "count": len(callsites),
    }

    print(f"Binary: {binary}")
    print(f"Found {len(callsites)} indirect callsites")

    for site in callsites[:20]:
        print(
            f"  {site['module']}+{site['offset_hex']}: "
            f"{site['instruction']}"
        )

    if len(callsites) > 20:
        print(f"  ... {len(callsites) - 20} more")

    if args.output:
        output_path = Path(args.output)

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with output_path.open("w") as f:
            json.dump(result, f, indent=2)

        print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()
