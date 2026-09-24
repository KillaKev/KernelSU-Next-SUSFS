#!/usr/bin/env python3
"""Gate: prove a packed boot.img still carries the AVB structures the loader needs.

Why this exists (the most expensive lesson in this repo's history)
------------------------------------------------------------------
`pack_boot.py` rebuilt the image from the stock HEADER and zero-filled everything after the
kernel. That silently deleted:

  * the AVB vbmeta struct ("AVB0") sitting at the page-aligned offset straight after the
    kernel, and
  * the AVB footer ("AVBf") in the last 64 bytes of the partition.

Measured on three real images:

    boot-stock.img      non-zero bytes after the kernel: 4,709   AVBf present   BOOTS
    custom_boot.img     non-zero bytes after the kernel: 1,140   AVBf present   BOOTS
    pack_boot.py image  non-zero bytes after the kernel: 0       no AVBf        BOOTLOOPS

Runs #16 and #21 were both packed that way: they compiled, they flashed, and they bootlooped -
because the bootloader refused the image before Linux ever ran. No kernel-side check (config
drift, release string, vermagic, symbol CRCs) could have caught it. This gate can, in a second.

Usage: verify_boot_img.py <boot.img> [--expect-kernel <Image>]
Exit 0 only if the header is sane, the kernel is present, the AVB0 struct is where the loader
expects it, the AVBf footer is intact, and (if given) the payload hashes to the built Image's
compressed form.
"""
import argparse
import hashlib
import struct
import sys

PAGE = 4096
AVB0 = b"AVB0"
AVBF = b"AVBf"
MIN_TAIL_BYTES = 1000  # stock has 4,709; a magiskboot pack has 1,140; a broken one has 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--expect-kernel", default=None,
                    help="raw kernel Image that should be inside (optional)")
    args = ap.parse_args()

    with open(args.image, "rb") as fh:
        d = fh.read()

    problems = []

    if d[:8] != b"ANDROID!":
        problems.append("no ANDROID! magic: %r" % d[:8])
    version = struct.unpack_from("<I", d, 40)[0]
    ks = struct.unpack_from("<I", d, 8)[0]
    rs = struct.unpack_from("<I", d, 12)[0]
    ss = struct.unpack_from("<I", d, 1580)[0] if version == 4 else 0
    k_end = PAGE + ks
    k_page_end = (k_end + PAGE - 1) // PAGE * PAGE
    tail = d[k_page_end:]
    non_zero = sum(1 for b in tail if b)

    print("image            : %s" % args.image)
    print("  size           : %d" % len(d))
    print("  header_version : %d" % version)
    print("  kernel_size    : %d   (kernel ends %d, page-aligned %d)" % (ks, k_end, k_page_end))
    print("  ramdisk/sig    : %d / %d" % (rs, ss))
    print("  sha256         : %s" % hashlib.sha256(d).hexdigest())
    print("  AFTER kernel   : %d non-zero bytes" % non_zero)
    avb0_here = bytes(d[k_page_end:k_page_end + 4]) == AVB0
    print("  AVB0 at %d : %s" % (k_page_end, "YES" if avb0_here else "NO"))
    if not avb0_here:
        # Diagnostic only - still a failure below. Stock and magiskboot both land the struct
        # exactly at the page-aligned kernel end; anything else means the layout drifted away
        # from the one image we know boots. Say where it actually went, so the log explains
        # itself instead of just saying "missing".
        found = d.find(AVB0, PAGE)
        print("  AVB0 actually at  : %s"
              % (found if found >= 0 else "<absent from the whole image>"))
    avbf_here = bytes(d[-64:-60]) == AVBF
    print("  AVBf in tail   : %s" % ("YES" if avbf_here else "NO"))
    if avbf_here:
        # AvbFooter is big-endian: original_image_size @12, vbmeta_offset @20, vbmeta_size @28
        print("  AVBf says vbmeta  : offset %d size %d"
              % (struct.unpack_from(">Q", d, len(d) - 64 + 20)[0],
                 struct.unpack_from(">Q", d, len(d) - 64 + 28)[0]))

    if version != 4:
        problems.append("header_version %d is not 4" % version)
    if not avb0_here:
        problems.append("AVB0 vbmeta struct missing at the page-aligned kernel end (%d)" % k_page_end)
    if not avbf_here:
        problems.append("AVBf footer missing from the last 64 bytes of the partition")
    if non_zero < MIN_TAIL_BYTES:
        problems.append("only %d non-zero bytes after the kernel (expected >= %d) - this image "
                        "has had its trailing structures zeroed and will not boot"
                        % (non_zero, MIN_TAIL_BYTES))
    if args.expect_kernel:
        with open(args.expect_kernel, "rb") as fh:
            raw = fh.read()
        # Decompress the payload exactly the way the bootloader will (lz4 legacy frame) and
        # compare it to the raw Image we built. This is the check that proves the image carries
        # THIS kernel and not, say, a stale blob left over from the template.
        blob = d[PAGE:k_end]
        got = b""
        try:
            import lz4.block as lb
            off, out = 4, bytearray()
            while off < len(blob):
                n = struct.unpack_from("<I", blob, off)[0]
                if n == 0:
                    break
                off += 4
                out += lb.decompress(blob[off:off + n], uncompressed_size=8 * 1024 * 1024)
                off += n
            got = bytes(out)
        except Exception as exc:  # pragma: no cover - diagnostic path
            print("   (could not decompress the payload: %s)" % exc)
        same = bool(got) and hashlib.sha256(got).hexdigest() == hashlib.sha256(raw).hexdigest()
        print("  raw Image      : %d bytes sha256 %s" % (len(raw), hashlib.sha256(raw).hexdigest()))
        print("  payload inside : %d bytes sha256 %s"
              % (len(got), hashlib.sha256(got).hexdigest() if got else "-"))
        print("  payload == built Image : %s" % ("YES" if same else "NO"))
        if not same:
            problems.append("the kernel inside the image does not decompress to the built Image")

    if problems:
        for p in problems:
            print("::error::%s" % p)
        print("RESULT: FAILED - this image would bootloop regardless of the kernel inside it")
        return 1
    print("RESULT: OK - AVB structures intact, this image is shaped like one that boots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
