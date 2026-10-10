#!/usr/bin/env python3
"""
verify_boot_img.py <new boot.img> <factory boot.img>

Both images are the same device's boot partition image, so the new one must look like the factory
one in everything except the kernel. Hard failures (the image would not flash or would not boot):
  * size differs from the factory file (the partition-sized layout was lost)
  * not an ANDROID! boot image
  * the kernel is stored in a different format than the factory kernel (e.g. raw vs lz4)
  * the factory image ends in an AVB footer and the new one does not
Header differences other than kernel_size are reported as warnings.
"""
import struct
import sys

HDR = 1584  # boot header v4


def main():
    new = open(sys.argv[1], "rb").read()
    old = open(sys.argv[2], "rb").read()
    bad = []
    print("size   new %d  factory %d" % (len(new), len(old)))
    if len(new) != len(old):
        bad.append("size differs from the factory boot.img")
    if new[:8] != b"ANDROID!":
        bad.append("not an ANDROID! boot image")
    else:
        (ver,) = struct.unpack_from("<I", new, 40)
        (kn,) = struct.unpack_from("<I", new, 8)
        (ko,) = struct.unpack_from("<I", old, 8)
        print("header version %d, kernel_size new %d factory %d" % (ver, kn, ko))
        diff = [i for i in range(min(HDR, len(new))) if not 8 <= i < 12 and new[i] != old[i]]
        if diff:
            print("::warning::boot header differs from the factory image at offsets %s" % diff[:20])
        else:
            print("OK   header identical to the factory header (apart from kernel_size)")
        a, b = new[4096:4100], old[4096:4100]
        n = 2 if old[4096:4098] == b"MZ" else 4
        print("kernel magic new %s factory %s" % (a.hex(), b.hex()))
        if a[:n] != b[:n]:
            bad.append("kernel is stored in a different format than the factory kernel")
        if 4096 + kn > len(new):
            bad.append("kernel_size runs past the end of the image")
    has_old = old[-64:-60] == b"AVBf"
    has_new = new[-64:-60] == b"AVBf"
    print("AVB footer  factory %s  new %s" % (has_old, has_new))
    if has_old and not has_new:
        bad.append("the factory image has an AVB footer and the new one lost it")
    if bad:
        for m in bad:
            print("::error::" + m)
        sys.exit(1)
    print("OK   boot.img agrees with the factory image")


if __name__ == "__main__":
    main()
