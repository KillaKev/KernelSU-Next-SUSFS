#!/usr/bin/env python3
"""Make a repacked boot.img use the STOCK boot-vbmeta image_size, because this device's
bootloader rejects the value a newer magiskboot recomputes.

Measured on the Nothing Phone 2a (pacman), booting vs bootlooping, same kernel:

    stock template        descriptor image_size = 25,063,424   footer original_image_size = 25,088,000
    e159716 magiskboot    descriptor image_size = 25,088,000   (= the stock FOOTER value)   BOOTS
    nightly magiskboot    descriptor image_size = 21,606,400   (= its OWN footer value)     BOOTLOOP
    nightly + this script descriptor image_size = 25,088,000                                BOOTS

So: the field must keep the value inherited from the stock template. Where it lives:
    AVB0 struct        at the page-aligned end of the kernel
    aux block          struct + 256 + authentication_data_block_size
    hash descriptor    image_size (u64, BIG-ENDIAN) is the 8 bytes immediately before the
                       "sha256" algorithm string

Usage:  normalize_vbmeta.py --stock <stock-boot.img[.gz]> --image <packed boot.img>
Exit 0 only if a sane value was found and the write re-reads back correctly.
"""
import argparse
import gzip
import struct
import sys

PAGE = 4096
AVB0 = b"AVB0"
STRUCT = 1664
PAGE_MASK = PAGE - 1


def load(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read()
    with open(path, "rb") as fh:
        return fh.read()


def stock_target(stock_path):
    """The value the working packer ends up with: the stock image's footer original_image_size."""
    d = load(stock_path)
    footer = d[-64:]
    if footer[0:4] != b"AVBf":
        raise SystemExit("::error::%s has no AVBf footer - cannot read the stock target" % stock_path)
    return struct.unpack_from(">Q", footer, 12)[0]


def field_offset(d):
    """Absolute offset of the boot vbmeta hash descriptor's image_size field."""
    ks = struct.unpack_from("<I", d, 8)[0]
    struct_off = (PAGE + ks + PAGE_MASK) & ~PAGE_MASK
    if d[struct_off:struct_off + 4] != AVB0:
        raise SystemExit("::error::no AVB0 struct at the page-aligned kernel end (%d)" % struct_off)
    auth = struct.unpack_from(">Q", d, struct_off + 12)[0]
    aux = struct_off + 256 + auth
    if aux + STRUCT > len(d):
        raise SystemExit("::error::aux block at %d runs past the image" % aux)
    idx = d.find(b"sha256", aux, aux + STRUCT)
    if idx < 0:
        raise SystemExit("::error::no 'sha256' algorithm string in the aux block at %d" % aux)
    return aux, idx - 8, struct_off


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, help="stock template (the committed stock-boot.img.gz)")
    ap.add_argument("--image", required=True, help="the repacked boot.img to normalise")
    args = ap.parse_args()

    target = stock_target(args.stock)
    if target <= 0 or target & PAGE_MASK:
        raise SystemExit("::error::stock original_image_size %d is not a sane page-aligned size" % target)

    with open(args.image, "rb") as fh:
        d = bytearray(fh.read())

    aux, off, struct_off = field_offset(d)
    old = struct.unpack_from(">Q", d, off)[0]
    if old & PAGE_MASK:
        raise SystemExit("::error::existing image_size %d at %d is not page-aligned - layout changed, "
                         "refusing to patch blindly" % (old, off))

    print("stock target image_size : %d" % target)
    print("AVB0 struct             : %d   aux block %d" % (struct_off, aux))
    print("descriptor image_size   : %d   old value %d" % (off, old))

    if old == target:
        print("OK   already %d - nothing to do (the packer behaved like the known-good one)" % target)
        return 0

    struct.pack_into(">Q", d, off, target)
    with open(args.image, "wb") as fh:
        fh.write(bytes(d))

    with open(args.image, "rb") as fh:
        again = fh.read()
    got = struct.unpack_from(">Q", again, off)[0]
    if got != target:
        raise SystemExit("::error::wrote %d but re-read %d" % (target, got))
    print("OK   image_size %d -> %d (re-read %d) - matches the template the device boots" % (old, target, got))
    return 0


if __name__ == "__main__":
    sys.exit(main())