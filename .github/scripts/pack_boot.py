#!/usr/bin/env python3
"""Pack the built Image.lz4 into a flashable boot.img, using the STOCK boot header as the template.

Why a template instead of mkbootimg: every field the bootloader sees (os_version, cmdline, reserved
words, header size/version, partition padding) must stay exactly as the stock device had it. Copying
the stock header verbatim and swapping only the kernel keeps that guarantee, needs no external tools,
and needs no network access in CI.

Layout (AOSP bootimg.h), header_version 3/4:
    offset 0    header   (4096-byte page; header_size=1580 v3 / 1584 v4)
    4096        kernel   (kernel_size bytes, padded to the page)
    ...         ramdisk  (ramdisk_size bytes, padded; ZERO on GKI devices that put it in init_boot)
    ...         signature (v4: signature_size bytes; ZERO when the vbmeta partition carries the chain)

Subcommands:
    extract-template --stock <stock boot.img> --outdir <dir> [--pad-to BYTES]
    pack             --template-dir <dir> --kernel <Image.lz4> --output <boot.img>

`pack` re-parses what it wrote and fails unless every field except kernel_size is byte-identical to the
template and the kernel payload hashes to the input kernel.
"""
# ==============================================================================================
# RETIRED - DO NOT USE FOR FLASHABLE IMAGES.
#
# The `pack` subcommand below rebuilds the image from the stock HEADER and ZERO-FILLS everything
# after the kernel blob. That deletes:
#     * the AVB vbmeta struct ("AVB0") at the page-aligned offset immediately after the kernel
#     * the AVB footer ("AVBf") in the last 64 bytes of the partition
# Measured: stock carries 4,709 non-zero bytes after the kernel, a magiskboot pack 1,140, and an
# image from this script ZERO - and the bootloader refuses those. Runs #16 and #21 were packed
# with this script: both compiled fine, both flashed cleanly, both BOOTLOOPED, and both kernels
# were innocent.
#
# The workflow now packs with magiskboot against the committed stock image
# (.github/boot-template/stock-boot.img.gz) and gates the result with
# .github/scripts/verify_boot_img.py. The header/layout notes here are still useful reading, and
# `extract-template` remains harmless, but `pack` must not be used again.
# ==============================================================================================
import argparse
import hashlib
import json
import os
import struct
import sys

PAGE = 4096
MAGIC = b"ANDROID!"
FIELDS_V4 = ["kernel_size", "ramdisk_size", "os_version", "header_size", "reserved",
             "header_version", "cmdline", "signature_size"]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def round_up(value, unit):
    return value + (-value % unit)


def parse_header(blob):
    """Parse an AOSP boot image header (v3/v4 only; v0-v2 lack the fields we must preserve)."""
    if blob[:8] != MAGIC:
        raise SystemExit("FATAL: not a boot image - first 8 bytes are %r, expected %r"
                         % (blob[:8], MAGIC))
    version = struct.unpack_from("<I", blob, 40)[0]
    if version not in (3, 4):
        raise SystemExit(
            "FATAL: boot header_version %d is not supported (only 3 and 4). This device uses 4; a "
            "different template would need the legacy v0-v2 layout added." % version)
    hdr = {
        "kernel_size": struct.unpack_from("<I", blob, 8)[0],
        "ramdisk_size": struct.unpack_from("<I", blob, 12)[0],
        "os_version": struct.unpack_from("<I", blob, 16)[0],
        "header_size": struct.unpack_from("<I", blob, 20)[0],
        "reserved": [struct.unpack_from("<I", blob, 24 + 4 * i)[0] for i in range(4)],
        "header_version": version,
        "cmdline": blob[44:44 + 512 + 1024].rstrip(b"\0").decode("ascii", "replace"),
        "signature_size": struct.unpack_from("<I", blob, 1580)[0] if version == 4 else 0,
    }
    return hdr


def layout(hdr):
    """Return (kernel_off, kernel_size, ramdisk_off, ramdisk_size, sig_off, sig_size)."""
    kernel_off = PAGE
    ramdisk_off = kernel_off + round_up(hdr["kernel_size"], PAGE)
    sig_off = ramdisk_off + round_up(hdr["ramdisk_size"], PAGE)
    return kernel_off, hdr["kernel_size"], ramdisk_off, hdr["ramdisk_size"], sig_off, hdr["signature_size"]


def cmd_extract_template(args):
    """Turn a stock boot.img (or a whole-partition readback) into a tiny, checkable template."""
    with open(args.stock, "rb") as fh:
        blob = fh.read()
    hdr = parse_header(blob)
    k_off, k_size, r_off, r_size, s_off, s_size = layout(hdr)
    os.makedirs(args.outdir, exist_ok=True)

    with open(os.path.join(args.outdir, "boot-header.bin"), "wb") as fh:
        fh.write(blob[:PAGE])
    if r_size:
        with open(os.path.join(args.outdir, "ramdisk.bin"), "wb") as fh:
            fh.write(blob[r_off:r_off + r_size])
    if s_size:
        with open(os.path.join(args.outdir, "signature.bin"), "wb") as fh:
            fh.write(blob[s_off:s_off + s_size])

    meta = dict(hdr,
                header_sha256=sha256(blob[:PAGE]),
                stock_kernel_size=k_size,
                stock_kernel_sha256=sha256(blob[k_off:k_off + k_size]),
                stock_file_size=len(blob),
                pad_to=args.pad_to or len(blob))
    with open(os.path.join(args.outdir, "template.json"), "w") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print("template written to %s" % args.outdir)
    print("  header_version   : %d" % hdr["header_version"])
    print("  header_size      : %d" % hdr["header_size"])
    print("  ramdisk_size     : %d%s" % (r_size, "  (ramdisk lives in init_boot)" if not r_size else ""))
    print("  signature_size   : %d%s" % (s_size, "  (chain is in the vbmeta partition)" if not s_size else ""))
    print("  stock kernel     : %d bytes sha256=%s" % (k_size, meta["stock_kernel_sha256"]))
    print("  stock image size : %d (also used as pad_to)" % meta["stock_file_size"])
    return 0


def cmd_pack(args):
    tmpl = args.template_dir
    with open(os.path.join(tmpl, "boot-header.bin"), "rb") as fh:
        header = bytearray(fh.read())
    if len(header) != PAGE:
        raise SystemExit("FATAL: boot-header.bin is %d bytes, expected one %d-byte page"
                         % (len(header), PAGE))
    with open(os.path.join(tmpl, "template.json")) as fh:
        meta = json.load(fh)

    # The packer copies this header VERBATIM and then compares the result against it, so a template
    # that was mangled in transit (line-ending or encoding damage) would compare equal to itself and
    # pass silently. Pin it to the hash recorded when it was extracted from the device.
    want = meta.get("header_sha256")
    if want and sha256(bytes(header)) != want:
        raise SystemExit("FATAL: boot-header.bin does not match template.json (header_sha256 %s vs %s) - "
                         "the committed template was damaged, re-extract it from the stock boot.img"
                         % (sha256(bytes(header)), want))
    with open(args.kernel, "rb") as fh:
        kernel = fh.read()

    tmpl_hdr = parse_header(bytes(header))
    pad_to = args.pad_to or meta.get("pad_to") or 0

    if tmpl_hdr["ramdisk_size"]:
        raise SystemExit("FATAL: this template carries a ramdisk (%d bytes); the packer does not rebuild "
                         "another partition's payload" % tmpl_hdr["ramdisk_size"])
    signature = b""
    if tmpl_hdr["header_version"] == 4 and tmpl_hdr["signature_size"]:
        with open(os.path.join(tmpl, "signature.bin"), "rb") as fh:
            signature = fh.read()
        if len(signature) != tmpl_hdr["signature_size"]:
            raise SystemExit("FATAL: signature.bin is %d bytes, header says %d"
                             % (len(signature), tmpl_hdr["signature_size"]))

    struct.pack_into("<I", header, 8, len(kernel))          # THE ONLY FIELD THAT CHANGES
    out = bytearray()
    out += header[:PAGE]
    out += kernel + b"\0" * (-len(kernel) % PAGE)
    out += signature
    if pad_to and len(out) < pad_to:
        out += b"\0" * (pad_to - len(out))
    with open(args.output, "wb") as fh:
        fh.write(out)

    # ---- verify what we just wrote, the same way a bootloader reads it --------------------
    got = parse_header(bytes(out[:PAGE]))
    problems = []
    for field in FIELDS_V4:
        if field != "kernel_size" and got[field] != tmpl_hdr[field]:
            problems.append("field %s changed: template=%r written=%r"
                            % (field, tmpl_hdr[field], got[field]))
    if got["kernel_size"] != len(kernel):
        problems.append("kernel_size is %d but the kernel is %d bytes" % (got["kernel_size"], len(kernel)))
    g_koff, g_ksize, _, _, _, _ = layout(got)
    if sha256(out[g_koff:g_koff + g_ksize]) != sha256(kernel):
        problems.append("the kernel payload inside boot.img does not hash to the input kernel")

    expected_size = PAGE + round_up(len(kernel), PAGE) + len(signature)
    if pad_to:
        expected_size = max(expected_size, pad_to)
    if len(out) != expected_size:
        problems.append("file is %d bytes, expected %d" % (len(out), expected_size))
    if meta.get("stock_kernel_size") and len(kernel) > round_up(meta["stock_kernel_size"], PAGE):
        print("NOTE: our kernel (%d B) exceeds the stock kernel's page allocation (%d B); the image is "
              "rebuilt rather than patched in place, so this is fine."
              % (len(kernel), round_up(meta["stock_kernel_size"], PAGE)))

    print("================= boot.img verification =================")
    print("  header magic       : %s" % ("ANDROID! OK" if got else "BAD"))
    print("  header_version     : %d (template %d)" % (got["header_version"], tmpl_hdr["header_version"]))
    print("  os_version/cmdline : preserved (0x%08x / %r)" % (got["os_version"], got["cmdline"]))
    print("  reserved words     : %s" % ["0x%08x" % r for r in got["reserved"]])
    print("  kernel offset      : %d (page %d)" % (g_koff, PAGE))
    print("  kernel_size        : %d" % got["kernel_size"])
    print("  kernel sha256      : %s" % sha256(kernel))
    print("  ramdisk_size       : %d" % got["ramdisk_size"])
    print("  signature_size     : %d" % got["signature_size"])
    print("  boot.img size      : %d bytes" % len(out))
    print("  boot.img sha256    : %s" % sha256(bytes(out)))
    if problems:
        for p in problems:
            print("::error::%s" % p)
        return 1
    print("  RESULT             : OK - every field except kernel_size is identical to the stock template")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract-template")
    e.add_argument("--stock", required=True)
    e.add_argument("--outdir", required=True)
    e.add_argument("--pad-to", type=int, default=0)
    e.set_defaults(func=cmd_extract_template)

    p = sub.add_parser("pack")
    p.add_argument("--template-dir", required=True)
    p.add_argument("--kernel", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--pad-to", type=int, default=0)
    p.set_defaults(func=cmd_pack)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


