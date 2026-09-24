"""Translate Linux x86-64 ELF virtual addresses to tracer module offsets."""

from pathlib import Path
import struct


def elf_image_base(binary: Path) -> int:
    """Return the lowest PT_LOAD virtual address rounded to a 4 KiB page.

    DynamoRIO's module start is this address plus the runtime load bias.
    Thus ELF VA - image base equals runtime PC - module start for both
    ET_EXEC and ET_DYN. Segment alignment (p_align) may exceed the Linux
    x86-64 page size and is not the rounding unit for the mapping start.
    """
    with binary.open("rb") as stream:
        header = stream.read(64)
        if (len(header) != 64 or header[:4] != b"\x7fELF"
                or header[4:7] != b"\x02\x01\x01"):
            raise ValueError(f"{binary}: expected a little-endian ELF64 file")
        fields = struct.unpack("<16sHHIQQQIHHHHHH", header)
        elf_type, machine, version = fields[1:4]
        phoff, phentsize, phnum = fields[5], fields[9], fields[10]
        if elf_type not in (2, 3) or machine != 62 or version != 1:
            raise ValueError(f"{binary}: expected an x86-64 ELF executable or shared object")
        if phentsize < 56 or phnum == 0 or phnum == 0xffff:
            raise ValueError(f"{binary}: unsupported ELF program header table")
        file_size = stream.seek(0, 2)
        if phoff + phentsize * phnum > file_size:
            raise ValueError(f"{binary}: truncated ELF program header table")

        bases = []
        for index in range(phnum):
            stream.seek(phoff + index * phentsize)
            segment = struct.unpack("<IIQQQQQQ", stream.read(56))
            if segment[0] == 1:  # PT_LOAD
                bases.append(segment[3] & ~0xfff)
        if not bases:
            raise ValueError(f"{binary}: ELF has no PT_LOAD segments")
        return min(bases)
