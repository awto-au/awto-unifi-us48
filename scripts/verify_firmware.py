#!/usr/bin/env python3
"""Verify US48P500 vendor metadata, UBNT CRCs and legacy uImage CRCs.

Usage:
    python3 scripts/verify_firmware.py \
        private/firmware/US48P500-7.5.15.bin private/firmware/vendor-latest.json

This is an offline verifier, not a flasher. An ENDS footer is reported as
an RSA signature container; its cryptographic signature is not verified.
"""

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

PART = struct.Struct(">4s16s12sIIIIII")
UIMAGE = struct.Struct(">7I4B32s")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify(firmware: Path, metadata: Path) -> dict:
    data = firmware.read_bytes()
    entries = json.loads(metadata.read_text())["_embedded"]["firmware"]
    require(len(entries) == 1, "Expected exactly one vendor metadata entry")
    vendor = entries[0]
    require(vendor["platform"] == "US48P500", "Wrong vendor platform")
    require(vendor["channel"] == "release", "Not a release-channel image")
    require(len(data) == vendor["file_size"], "Vendor file-size mismatch")
    digest = hashlib.sha256(data).hexdigest()
    require(digest == vendor["sha256_checksum"], "Vendor SHA-256 mismatch")
    require(len(data) >= 268 and data[:4] == b"UBNT", "Invalid UBNT header")
    require(zlib.crc32(data[:260]) == struct.unpack_from(">I", data, 260)[0],
            "UBNT header CRC mismatch")
    version = data[4:260].split(b"\0", 1)[0].decode("ascii")
    require(f".{vendor['version']}." in version, "Version does not match metadata")
    parts = []
    pos = 268
    while data[pos:pos + 4] == b"PART":
        require(pos + PART.size <= len(data), "Truncated PART header")
        _, name, _, mem, index, base, entry, size, capacity = PART.unpack_from(data, pos)
        end = pos + PART.size + size
        require(end + 8 <= len(data), "Truncated PART payload/CRC")
        require(0 < size <= capacity, "Invalid PART size/capacity")
        require(zlib.crc32(data[pos:end]) == struct.unpack_from(">I", data, end)[0],
                f"PART {index} CRC mismatch")
        body = data[pos + PART.size:end]
        record = {
            "name": name.split(b"\0", 1)[0].decode("ascii"),
            "index": index, "container_offset": pos, "bytes": size,
            "partition_bytes": capacity, "memory_address": mem,
            "flash_address": base, "entry_address": entry, "crc_verified": True,
        }
        if body[:4] == b"\x27\x05\x19\x56":
            require(len(body) >= UIMAGE.size, "Truncated uImage header")
            fields = UIMAGE.unpack_from(body)
            header = bytearray(body[:64])
            header[4:8] = b"\0" * 4
            require(zlib.crc32(header) == fields[1], "uImage header CRC mismatch")
            require(fields[3] == len(body) - 64, "uImage payload-size mismatch")
            require(zlib.crc32(body[64:]) == fields[6], "uImage data CRC mismatch")
            record["uimage"] = {
                "name": fields[11].split(b"\0", 1)[0].decode("ascii"),
                "load_address": fields[4], "entry_address": fields[5],
                "os": fields[7], "architecture": fields[8],
                "type": fields[9], "compression": fields[10],
                "crc_verified": True,
            }
        parts.append(record)
        pos = end + 8
    require([p["name"] for p in parts] == ["u-boot", "kernel0"],
            "Unexpected partition sequence for this firmware")
    require(data[pos:pos + 4] == b"ENDS" and len(data) - pos == 264,
            "Unexpected signed-footer shape")
    require(data[-4:] == b"\0" * 4, "Nonzero signed-footer padding")
    return {
        "version": version, "vendor_version": vendor["version"],
        "sha256": digest, "bytes": len(data), "parts": parts,
        "signature_footer": {
            "magic": "ENDS", "offset": pos, "bytes": 264,
            "rsa_signature_bytes": 256, "cryptographically_verified": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.firmware, args.metadata)
    except (OSError, ValueError, KeyError, struct.error) as error:
        print(f"Firmware verification failed: {error}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
