#!/usr/bin/env python3
"""Asuka EVA-02 Japanese localizer for Claude Code (native Bun build).

Claude Code's native binary embeds Bun bytecode whose shared string table stores
UI literals as Latin-1 or UTF-16 records:

    [u32 count][u32 offset[count]]
    record = [u32 length | is8Bit<<31][u32 hash][chars][pad-to-4]

This tool rewrites selected Latin-1 records in place as UTF-16 Japanese.  A
record's padded byte size never changes, so the table's offset array and every
bytecode reference stay valid.  The hash is recomputed with WebKit's RapidHash
(the same algorithm StringImpl uses), and the binary is re-signed ad-hoc.

Usage:
    asuka_patch.py                 # patch the installed Claude Code binary
    asuka_patch.py --check         # report what would change, write nothing
    asuka_patch.py --restore       # restore the pre-patch backup
    asuka_patch.py --binary PATH   # target a specific executable
    asuka_patch.py --strings FILE  # use another translation table
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

MASK64 = (1 << 64) - 1
SECRET = (0x2D358DCCAA6C78A5, 0x8BB84B93962EACC9, 0x4B33A62ED433D4A3)


def _mix(a: int, b: int) -> int:
    r = a * b
    return ((r & MASK64) ^ (r >> 64)) & MASK64


def rapid_hash(data: bytes) -> int:
    """WebKit RapidHash over raw bytes (used for non-Latin-1 UTF-16 bodies)."""
    n = len(data)

    def read64(off: int) -> int:
        return int.from_bytes(data[off:off + 8], "little")

    def read32(off: int) -> int:
        return int.from_bytes(data[off:off + 4], "little")

    def read_small(off: int, k: int) -> int:
        return (data[off] << 56) | (data[off + (k >> 1)] << 32) | data[off + k - 1]

    seed = (_mix(SECRET[0], SECRET[1]) ^ n) & MASK64
    if n <= 16:
        if n >= 4:
            delta = 4 if n >= 8 else 0
            a = ((read32(0) << 32) | read32(n - 4)) & MASK64
            b = ((read32(delta) << 32) | read32(n - 4 - delta)) & MASK64
        elif n > 0:
            a, b = read_small(0, n), 0
        else:
            a = b = 0
    else:
        i, off = n, 0
        if i > 48:
            see1 = see2 = seed
            while True:
                seed = _mix(read64(off) ^ SECRET[0], read64(off + 8) ^ seed)
                see1 = _mix(read64(off + 16) ^ SECRET[1], read64(off + 24) ^ see1)
                see2 = _mix(read64(off + 32) ^ SECRET[2], read64(off + 40) ^ see2)
                off += 48
                i -= 48
                if i < 48:
                    break
            seed ^= see1 ^ see2
        if i > 16:
            seed = _mix(read64(off) ^ SECRET[2], read64(off + 8) ^ seed ^ SECRET[1])
            if i > 32:
                seed = _mix(read64(off + 16) ^ SECRET[2], read64(off + 24) ^ seed)
        a = read64(off + i - 16)
        b = read64(off + i - 8)

    a ^= SECRET[1]
    b ^= seed
    r = a * b
    return _mix(((r & MASK64) ^ SECRET[0] ^ n), ((r >> 64) ^ SECRET[1]))


def string_hash(data: bytes) -> int:
    """StringImpl's cached hash: RapidHash masked to 24 bits, never zero."""
    h = rapid_hash(data) & 0xFFFFFF
    return h if h else 0x800000


def record_size(length: int) -> int:
    return (8 + length + 3) & ~3


def find_records(data: bytes, text: str) -> list[tuple[int, str]]:
    """Find Latin-1 string-table records whose full text equals `text`."""
    enc = text.encode("latin-1")
    out: list[tuple[int, str]] = []
    start = 0
    while True:
        i = data.find(enc, start)
        if i < 0:
            break
        p = i - 8
        if p >= 0:
            header, stored = struct.unpack_from("<II", data, p)
            if (
                (header & 0x7FFFFFFF) == len(enc)
                and (header >> 31) == 1
                and stored == string_hash(enc)
            ):
                out.append((p, text))
        start = i + 1
    return out


def find_utf16_records(data: bytes, text: str) -> list[tuple[int, str]]:
    """Find already-localized 16-bit records whose full text equals `text`."""
    body = text.encode("utf-16-le")
    out: list[tuple[int, str]] = []
    start = 0
    while True:
        i = data.find(body, start)
        if i < 0:
            break
        p = i - 8
        if p >= 0:
            header, stored = struct.unpack_from("<II", data, p)
            if (
                (header & 0x7FFFFFFF) == len(text)
                and (header >> 31) == 0
                and stored == string_hash(body)
            ):
                out.append((p, text))
        start = i + 1
    return out


def rewrite_record(buf: bytearray, pos: int, old_len: int, new_text: str) -> None:
    size = record_size(old_len)
    body = new_text.encode("utf-16-le")
    if 8 + len(body) > size:
        raise ValueError(
            f"{new_text!r} needs {8 + len(body)} bytes, record only has {size}"
        )
    struct.pack_into("<I", buf, pos, len(new_text))          # 16-bit, length in UTF-16 units
    struct.pack_into("<I", buf, pos + 4, string_hash(body))
    buf[pos + 8:pos + 8 + len(body)] = body
    buf[pos + 8 + len(body):pos + size] = b"\x00" * (size - 8 - len(body))


def looks_native_bun(path: str) -> bool:
    """True for a native single-file build (Mach-O / ELF), not an npm script."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head in (
        b"\xcf\xfa\xed\xfe",  # Mach-O 64
        b"\xce\xfa\xed\xfe",  # Mach-O 32
        b"\xca\xfe\xba\xbe",  # Mach-O universal
        b"\x7fELF",           # ELF
    )


def resolve_binary(explicit: str | None) -> str:
    if explicit:
        return os.path.realpath(explicit)
    candidates: list[str] = []
    which = shutil.which("claude")
    if which:
        candidates.append(os.path.realpath(which))
    versions = os.path.expanduser("~/.local/share/claude/versions")
    if os.path.isdir(versions):
        names = [n for n in os.listdir(versions) if n[0].isdigit()]
        names.sort(key=lambda n: [int(x) for x in n.split(".") if x.isdigit()])
        candidates.extend(os.path.join(versions, n) for n in reversed(names))
    for path in candidates:
        if os.path.isfile(path) and looks_native_bun(path):
            return path
    raise SystemExit(
        "Claude Code native build not found (npm installs are not supported); "
        "pass --binary PATH"
    )


def codesign(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(
            ["codesign", "--force", "--sign", "-", path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def load_strings(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("strings", doc)


def marker_file() -> str:
    return os.path.expanduser("~/.claude/asuka/.patched.json")


def fingerprint(binary: str, strings: str) -> dict:
    st = os.stat(binary)
    with open(strings, "rb") as f:
        table = hashlib.sha256(f.read()).hexdigest()
    return {
        "binary": os.path.realpath(binary),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "table": table,
    }


def marker_matches(want: dict) -> bool:
    try:
        with open(marker_file(), encoding="utf-8") as f:
            return json.load(f) == want
    except (OSError, ValueError):
        return False


def write_marker(want: dict) -> None:
    path = marker_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(want, f)


def clear_marker() -> None:
    try:
        os.unlink(marker_file())
    except OSError:
        pass


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Japanese localizer for Claude Code")
    ap.add_argument("--binary")
    ap.add_argument("--strings", default=os.path.join(here, "asuka-strings.json"))
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--restore", action="store_true", help="restore backup")
    ap.add_argument("--force", action="store_true", help="write even if nothing matched")
    ap.add_argument("--auto", action="store_true",
                    help="session-start mode: quiet, skip when the fingerprint matches")
    args = ap.parse_args()

    binary = resolve_binary(args.binary)
    backup = binary + ".asuka-orig"

    if args.restore:
        if not os.path.exists(backup):
            raise SystemExit(f"no backup at {backup}")
        tmp = binary + ".restore-tmp"
        shutil.copy2(backup, tmp)
        os.chmod(tmp, 0o755)
        codesign(tmp)
        os.replace(tmp, binary)
        clear_marker()
        print(f"restored {binary}")
        return 0

    if not os.path.exists(binary):
        if args.auto:
            return 0
        raise SystemExit(f"not found: {binary}")

    want = fingerprint(binary, args.strings)
    if args.auto and marker_matches(want):
        return 0

    entries = load_strings(args.strings)
    if not args.auto:
        print(f"binary : {binary}")
        print(f"table  : {args.strings} ({len(entries)} entries)")

    with open(binary, "rb") as f:
        original = f.read()

    if b"Image in clipboard" not in original:
        if args.auto:
            return 0
        raise SystemExit("this does not look like a supported Claude Code native build")
    buf = bytearray(original)

    patched = missing = already = 0
    errors = []
    for item in entries:
        en, ja = item["en"], item["ja"]
        max_chars = (record_size(len(en)) - 8) // 2
        if len(ja) > max_chars:
            errors.append(f"{en!r}: {len(ja)} chars > {max_chars} allowed")
            continue
        recs = find_records(original, en)
        if not recs:
            if find_utf16_records(original, ja):
                already += 1
            else:
                missing += 1
                if not args.auto:
                    print(f"  missing: {en!r}")
            continue
        for pos, _ in recs:
            rewrite_record(buf, pos, len(en), ja)
            patched += 1
        if not args.auto:
            print(f"  {len(recs)}x  {en[:58]!r} -> {ja!r}")

    if errors:
        print("\nlength errors:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 2

    if not args.auto:
        print(f"\nrecords rewritten: {patched}, already localized: {already}, not found: {missing}")

    if args.check:
        return 0 if patched or already else 1
    if not patched and not args.force:
        if already:
            write_marker(want)
        if not args.auto:
            print("nothing to patch (already localized, or a different Claude Code build)")
        return 0

    if not os.path.exists(backup):
        shutil.copy2(binary, backup)
        if not args.auto:
            print(f"backup : {backup}")

    fd, tmp = tempfile.mkstemp(prefix=".asuka-", dir=os.path.dirname(binary))
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(buf)
        os.chmod(tmp, 0o755)
        codesign(tmp)
        os.replace(tmp, binary)  # atomic; a running process keeps the old inode
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    write_marker(fingerprint(binary, args.strings))
    if not args.auto:
        print("patched and re-signed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
