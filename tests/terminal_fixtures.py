"""Tiny PE-shaped bytes for inspection tests, never launched or distributed.

Zero-filled section contents deliberately provide no useful executable program.
Passing structural inspection is not evidence Windows could safely load/run it.
"""

import struct


def console_pe_bytes(*, machine=0x8664, subsystem=3, dll=False):
    image = bytearray(1024)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 128)
    image[128:132] = b"PE\x00\x00"
    optional_size = 224 if machine == 0x014C else 240
    struct.pack_into("<HHIIIHH", image, 132, machine, 1, 0, 0, 0,
                     optional_size, 0x0002 | (0x2000 if dll else 0))
    optional = 152
    struct.pack_into("<H", image, optional, 0x10B if machine == 0x014C else 0x20B)
    struct.pack_into("<I", image, optional + 16, 0x1000)
    struct.pack_into("<II", image, optional + 32, 0x1000, 0x200)
    struct.pack_into("<II", image, optional + 56, 0x2000, 0x200)
    struct.pack_into("<H", image, optional + 68, subsystem)
    directories = 96 if machine == 0x014C else 112
    struct.pack_into("<I", image, optional + directories - 4, 16)
    section = optional + optional_size
    image[section:section + 8] = b".text\x00\x00\x00"
    struct.pack_into("<IIII", image, section + 8, 1, 0x1000, 0x200, 0x200)
    struct.pack_into("<I", image, section + 36, 0x60000020)
    return bytes(image)
