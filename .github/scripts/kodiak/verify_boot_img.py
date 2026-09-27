#!/usr/bin/env python3
"""
verify_boot_img.py  <built-boot.img> <reference-header.bin>

Answers "is this boot.img header built the way Google builds them on this device?" by parsing
both and comparing the header field conventions.

The reference is the first 4 KB of **`boot.img` from Google's factory image for this exact build**
(`kodiak-cd1a.260905.001.b1`). It is not taken from `boot_a` on the phone (both slots hold the same
patched Wild Kernel) and it is not taken from `init_boot`, because Android writes `os_version` into
`init_boot` and leaves it **zero** in `boot` - a distinction that fooled an earlier revision of this
file in both directions. Measured values:

    boot.img       hv=4  hsize=1584  kernel_size=20230712  ramdisk_size=0  os_version=0x0        cmdline=''  sig=0
    init_boot.img  hv=4  hsize=1584  kernel_size=0         ramdisk_size=2667913  os_version=0x220001a9  cmdline=''  sig=0

Provenance and history: boot-template/kodiak/SOURCE.md.

Field layout (include/uapi/linux/android/bootimg.h):
    0    magic[8]          "ANDROID!"
    8    kernel_size       u32
    12   ramdisk_size      u32
    16   os_version        u32
    20   header_size       u32   (1580 = v3, 1584 = v4)
    24   reserved[4]       16 bytes
    40   header_version    u32
    44   cmdline[1536]
    1580 signature_size    u32   (v4 only)

Exit 0 only when every applicable field matches.
"""
import struct
import sys

MAGIC = b"ANDROID!"
HDR_FIELDS = ("magic", "kernel_size", "ramdisk_size", "os_version",
              "header_size", "reserved", "header_version", "cmdline",
              "signature_size")


def parse(path):
    with open(path, "rb") as fh:
        blob = fh.read(4096)
    if len(blob) < 1580:
        raise SystemExit("::error::%s is too short to hold a boot header (%d bytes)"
                         % (path, len(blob)))
    if blob[0:8] != MAGIC:
        raise SystemExit("::error::%s does not start with ANDROID! (got %r)"
                         % (path, blob[0:8]))
    kernel_size, ramdisk_size, os_version, header_size = struct.unpack_from("<4I", blob, 8)
    reserved = blob[24:40]
    header_version = struct.unpack_from("<I", blob, 40)[0]
    cmdline = blob[44:44 + 1536].split(b"\x00", 1)[0].decode(errors="replace")
    sig = struct.unpack_from("<I", blob, 1580)[0] if header_version >= 4 and len(blob) >= 1584 else None
    return {
        "magic": blob[0:8],
        "kernel_size": kernel_size,
        "ramdisk_size": ramdisk_size,
        "os_version": os_version,
        "header_size": header_size,
        "reserved": reserved,
        "header_version": header_version,
        "cmdline": cmdline,
        "signature_size": sig,
    }


def os_str(raw):
    """Decode the packed os_version field.

    mkbootimg stores --os_version A.B.C as (A << 25) | (B << 18) | (C << 11) and
    --os_patch_level YYYY-MM as ((YYYY - 2000) << 4) | MM. The "year" in the version part is the
    ANDROID version, not a calendar year: only the patch part is offset from 2000. Decoding the
    version part as 2000 + value is how you print "2017.0.0" for Android 17.0.0 - which is exactly
    the bug this function previously had.
    """
    a, b, c = raw >> 25, (raw >> 18) & 0x7F, (raw >> 11) & 0x7F
    py, pm = (raw >> 4) & 0x7F, raw & 0xF
    return "%d.%d.%d / patch %04d-%02d" % (a, b, c, 2000 + py, pm)


def show(tag, h):
    print("  %-16s v%d  header_size=%d  kernel_size=%d  ramdisk_size=%d  sig_size=%s"
          % (tag, h["header_version"], h["header_size"], h["kernel_size"],
             h["ramdisk_size"], h["signature_size"]))
    print("  %-16s os_version: %s   cmdline: %r"
          % ("", os_str(h["os_version"]), h["cmdline"]))


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_boot_img.py <built-boot.img> <reference-header.bin>")
    built_path, ref_path = sys.argv[1], sys.argv[2]

    try:
        built = parse(built_path)
    except FileNotFoundError:
        raise SystemExit("::error::%s does not exist" % built_path)
    reference = parse(ref_path)

    print("===== built boot.img vs Google-authored reference header =====")
    print("  reference: %s" % ref_path)
    show("built:", built)
    show("reference:", reference)

    problems = []

    # Field conventions that MUST match the Google-authored reference.
    # Payload sizes are deliberately NOT compared: the reference is an init_boot image (which
    # carries a ramdisk, no kernel) while this is a boot image (kernel, no ramdisk), so comparing
    # kernel_size/ramdisk_size would be meaningless. The device-specific rule below covers
    # ramdisk_size instead.
    for field in ("magic", "header_version", "header_size",
                  "os_version", "cmdline", "signature_size"):
        if built[field] != reference[field]:
            problems.append("%s: built=%r reference=%r" % (field, built[field], reference[field]))

    # reserved[4] is MBZ ("must be zero") in every header version. Assert that on the
    # built image rather than comparing it, so a stray byte in the reference cannot
    # bless a malformed header.
    if built["reserved"] != b"\x00" * 16:
        problems.append("reserved[] must be zero, got %s" % built["reserved"].hex())

    # Hard requirements for this device, independent of the reference.
    if built["header_version"] != 4:
        problems.append("header_version must be 4 for kodiak (got %d)" % built["header_version"])
    # NOTE: there is deliberately no "os_version must be non-zero" rule here. That rule existed for one
    # revision of this repo, based on the belief that a zero os_version was the packer's signature - and
    # Google's factory boot.img proved it wrong: stock kodiak boot.img HAS os_version 0, while stock
    # init_boot.img carries 17.0.0 / 2026-09. The field is therefore checked purely by equality with the
    # reference (see the loop above), which is the only authority on what stock means for this build.
    if built["ramdisk_size"] != 0:
        problems.append("ramdisk_size must be 0 - on kodiak the ramdisk lives in init_boot, "
                        "and a boot.img that carries one would be a second, wrong ramdisk")
    if built["cmdline"].strip():
        problems.append("cmdline must be empty (the reference is empty); got %r" % built["cmdline"])
    if built["kernel_size"] <= 0:
        problems.append("kernel_size is 0 - no kernel in the image")

    # Sanity: the payload we put in must actually be there.
    import os
    size = os.path.getsize(built_path)
    print("  file size: %d bytes" % size)
    if size < built["kernel_size"]:
        problems.append("file (%d B) is smaller than the declared kernel_size (%d B)"
                        % (size, built["kernel_size"]))

    print("===== result =====")
    if problems:
        for p in problems:
            print("::error::" + p)
        print("::error::boot.img header verification FAILED - do not flash it")
        return 1
    print("OK - header follows the reference conventions (v4, os_version %s, empty cmdline, "
          "ramdisk_size 0)" % os_str(reference["os_version"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
