#!/usr/bin/env python3
"""
extract_stock.py --stock DIR --out DIR

Turns the pieces of the factory image that the resolve step kept (DIR/vendor_kernel_boot.img,
vendor_boot.img, vendor_dlkm.img, system_dlkm.img, Image) into what the two automatic gates need:

  OUT/modules/ramdisk/*.ko      first-stage modules inside the vendor_kernel_boot ramdisk
  OUT/modules/vendor_dlkm/...   the vendor_dlkm partition's modules
  OUT/modules/system_dlkm/...   the system_dlkm partition's modules
  OUT/stock.btf                 the stock kernel's BTF blob, cut out of its Image

Together the three module sets are what the phone loads (on a Pixel 11 Pro XL that is ~370
modules, of which only ~200 are visible on the running filesystem - the rest live in the ramdisk).

BTF is found by scanning the Image for a header that is internally consistent (magic 0xEB9F,
version 1, hdr_len 24, and type/string sections that tile the blob exactly), because the stock
kernel's .BTF sits in the loaded image (it is what /sys/kernel/btf/vmlinux serves).

Needs: python3 + the `lz4` module (pip install lz4), and for the erofs partitions
erofs support: the kernel's erofs driver via a loop mount (sudo), or `fsck.erofs` (apt: erofs-utils).
"""
import argparse
import gzip
import os
import struct
import subprocess
import sys

try:
    import lz4.block
    import lz4.frame
except ImportError:  # the CI installs it; fail with a useful message otherwise
    lz4 = None


def die(msg):
    print("::error::" + msg, flush=True)
    sys.exit(1)


# ------------------------------------------------------------------------------ decompression
def unlz4_legacy(data):
    """LZ4 legacy frame (magic 02 21 4c 18): repeated [u32 compressed size][block], 8 MiB each."""
    out, pos = [], 4
    while pos + 4 <= len(data):
        (n,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if n == 0x184C2102:      # concatenated legacy stream
            continue
        if n == 0 or pos + n > len(data):
            break
        out.append(lz4.block.decompress(data[pos:pos + n], uncompressed_size=8 * 1024 * 1024))
        pos += n
    return b"".join(out)


def decompress(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:4] == bytes.fromhex("02214c18"):
        return unlz4_legacy(data)
    if data[:4] == bytes.fromhex("04224d18"):
        return lz4.frame.decompress(data)
    return data


# ------------------------------------------------------------------------------ vendor_boot
def vendor_ramdisks(path):
    """Decompressed ramdisk fragments of a vendor_boot v3/v4 image."""
    d = open(path, "rb").read()
    if d[:8] != b"VNDRBOOT":
        die("%s is not a vendor_boot image" % path)
    ver, page, _ka, _ra, rd_size = struct.unpack_from("<IIIII", d, 8)
    hdr_size = struct.unpack_from("<I", d, 2096)[0]
    dtb_size = struct.unpack_from("<I", d, 2100)[0]
    up = lambda n: (n + page - 1) // page * page
    rd_off = up(hdr_size)
    frags = []
    if ver >= 4:
        tbl_size, tbl_num, tbl_esz, _bc = struct.unpack_from("<IIII", d, 2112)
        tbl_off = rd_off + up(rd_size) + up(dtb_size)
        for i in range(tbl_num):
            sz, off, typ = struct.unpack_from("<III", d, tbl_off + i * tbl_esz)
            frags.append(d[rd_off + off:rd_off + off + sz])
    else:
        frags.append(d[rd_off:rd_off + rd_size])
    return [decompress(f) for f in frags]


def cpio_modules(cpio):
    """(path, bytes) of every *.ko in a newc cpio archive."""
    pos = 0
    while pos + 110 <= len(cpio) and cpio[pos:pos + 6] == b"070701":
        f = [int(cpio[pos + 6 + 8 * i:pos + 14 + 8 * i], 16) for i in range(13)]
        size, namesize = f[6], f[11]
        name = cpio[pos + 110:pos + 110 + namesize - 1].decode("utf-8", "replace")
        data_off = (pos + 110 + namesize + 3) & ~3
        if name == "TRAILER!!!":
            return
        if name.endswith(".ko"):
            yield name, cpio[data_off:data_off + size]
        pos = (data_off + size + 3) & ~3


# ------------------------------------------------------------------------------ erofs
def elf_complete(path):
    """True if every section the ELF header describes lies inside the file (i.e. not truncated)."""
    d = open(path, "rb").read()
    if len(d) < 64 or d[:4] != b"\x7fELF":
        return False
    shoff, = struct.unpack_from("<Q", d, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", d, 0x3A)
    if shoff + shentsize * shnum > len(d):
        return False
    for i in range(shnum):
        b = shoff + i * shentsize
        stype, = struct.unpack_from("<I", d, b + 4)
        off, size = struct.unpack_from("<QQ", d, b + 0x18)
        if stype != 8 and off + size > len(d):      # 8 = SHT_NOBITS (no file data)
            return False
    return True


def bad_modules(dest):
    return [os.path.join(r, f) for r, _d, fs in os.walk(dest) for f in fs
            if f.endswith(".ko") and not elf_complete(os.path.join(r, f))]


def count_modules(dest):
    return sum(1 for _r, _d, fs in os.walk(dest) for f in fs if f.endswith(".ko"))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def extract_with_mount(img, dest):
    """Kernel EROFS driver (loop mount): the most faithful reader. Needs sudo + the erofs module."""
    mnt = dest + ".mnt"
    os.makedirs(mnt, exist_ok=True)
    run(["sudo", "modprobe", "erofs"])
    r = run(["sudo", "mount", "-t", "erofs", "-o", "ro,loop", img, mnt])
    if r.returncode != 0:
        print("  loop mount failed: %s" % (r.stderr.strip() or r.stdout.strip())[:200])
        return False
    try:
        os.makedirs(dest, exist_ok=True)
        r = run(["sudo", "cp", "-r", "--no-preserve=ownership", mnt + "/.", dest])
        run(["sudo", "chown", "-R", "%d:%d" % (os.getuid(), os.getgid()), dest])
        return r.returncode == 0
    finally:
        run(["sudo", "umount", mnt])


def extract_with_fsck(img, dest):
    os.makedirs(dest, exist_ok=True)
    try:
        r = run(["fsck.erofs", "--extract=" + dest, img])
    except FileNotFoundError:
        print("  fsck.erofs is not installed")
        return False
    print("  fsck.erofs rc=%d" % r.returncode)
    return True


def extract_erofs(img, dest):
    """Extract an erofs partition and prove every module came out whole.

    fsck.erofs on the runner's erofs-utils was seen to drop the tail of some compressed files (9 of
    ~280 modules came out short, which made the CRC gate fail on files that were simply incomplete), so
    the kernel driver is tried first and the result of EVERY method is verified before it is trusted."""
    name = os.path.basename(img)
    for label, fn in (("kernel erofs mount", extract_with_mount), ("fsck.erofs", extract_with_fsck)):
        shutil_rmtree(dest)
        print("%s: trying %s" % (name, label))
        if not fn(img, dest):
            continue
        n, bad = count_modules(dest), bad_modules(dest)
        print("  %d modules, %d incomplete" % (n, len(bad)))
        if n and not bad:
            return n
        for b in bad[:5]:
            print("  incomplete: %s" % os.path.basename(b))
    die("could not extract %s completely - every method left truncated or no modules" % name)


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------------------------ BTF
def find_btf(img):
    """Cut the BTF blob out of a kernel Image: the one header whose sections tile it exactly."""
    needle = bytes.fromhex("9feb0100")      # magic 0xEB9F (LE) + version 1
    best, pos = None, 0
    while True:
        i = img.find(needle, pos)
        if i < 0:
            break
        pos = i + 1
        if i + 24 > len(img):
            continue
        _flags, hdr_len, type_off, type_len, str_off, str_len = struct.unpack_from("<BIIIII", img, i + 3)
        if hdr_len != 24 or type_off != 0 or str_off != type_len or type_len == 0 or str_len == 0:
            continue
        total = hdr_len + type_len + str_len
        if i + total > len(img):
            continue
        if best is None or total > best[1]:
            best = (i, total)
    if not best:
        die("no BTF blob found in the kernel image")
    return img[best[0]:best[0] + best[1]]


# ------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if lz4 is None:
        die("python module 'lz4' is missing (pip install lz4)")
    mods = os.path.join(a.out, "modules")

    n = 0
    rd = os.path.join(mods, "ramdisk")
    os.makedirs(rd, exist_ok=True)
    # first-stage modules: vendor_kernel_boot on current Pixels, vendor_boot on older layouts
    for img in ("vendor_kernel_boot.img", "vendor_boot.img"):
        p = os.path.join(a.stock, img)
        if not os.path.exists(p):
            continue
        for frag in vendor_ramdisks(p):
            for name, blob in cpio_modules(frag):
                with open(os.path.join(rd, os.path.basename(name)), "wb") as f:
                    f.write(blob)
                n += 1
        print("%s ramdisk: %d modules so far" % (img, n))
    if n == 0:
        die("no first-stage modules found in vendor_kernel_boot/vendor_boot")

    for part in ("vendor_dlkm", "system_dlkm"):
        img = os.path.join(a.stock, part + ".img")
        if os.path.exists(img):
            extract_erofs(img, os.path.join(mods, part))

    k = os.path.join(a.stock, "Image")
    if os.path.exists(k):
        blob = find_btf(open(k, "rb").read())
        with open(os.path.join(a.out, "stock.btf"), "wb") as f:
            f.write(blob)
        print("stock BTF: %d bytes" % len(blob))


if __name__ == "__main__":
    main()
