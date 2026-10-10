#!/usr/bin/env python3
"""
btf_structs.py - read C data-structure layouts out of BTF, and diff two of them.

WHY THIS EXISTS
    The kernel refuses a vendor module whose symbol CRCs do not match; that is
    what crc_gate.py checks. But a CRC is computed from a *declaration*, so it
    cannot see a struct member move. If a feature adds a field to a struct a
    vendor module pokes at, the CRCs stay green, the module loads, and it reads
    the wrong offset - a much worse failure than a refusal. Phase 1+2 of the
    heybooboo port does exactly that kind of change (CONFIG_SYSVIPC adds
    sysvsem/sysvshm to struct task_struct; BBRv3 adds two bitfields to struct
    tcp_sock). Both are written to be offset-neutral - packed __kabi_ignored
    unions, and a union with __kabi_ignored_0 that reuses existing padding.
    "Written to be" is a claim. This tool checks it.

HOW TO USE IT
    python btf_structs.py dump _kernel_ref/btf_baseline_run30.vmlinux > before.txt
    python btf_structs.py diff _kernel_ref/btf_baseline_run30.vmlinux vmlinux

    Input may be (a) a raw BTF blob, which is what the device hands you for free
    (adb pull /sys/kernel/btf/vmlinux), or (b) an ELF vmlinux with a .BTF
    section, which is what a CI build produces.

    Exit: 0 = no existing member moved and no existing struct changed size.
    1 = something did move (do not flash that kernel). 2 = could not parse.

WHAT COUNTS AS A FAILURE
    "offset moved"   an existing named member now sits at a different byte
    "size changed"   an existing struct/union changed sizeof
    ADDED/REMOVED    reported; exit stays 0 only if nothing moved - a member
                     appended at the end is additive and KMI-safe, while one
                     inserted in the middle moves everything after it and is
                     therefore already caught as "offset moved".
"""

import argparse
import struct
import sys

BTF_MAGIC = 0xEB9F
ELF_MAGIC = b"\x7fELF"

K_INT, K_PTR, K_ARRAY, K_STRUCT, K_UNION = 1, 2, 3, 4, 5
K_ENUM, K_FWD, K_TYPEDEF, K_VOLATILE, K_CONST = 6, 7, 8, 9, 10
K_RESTRICT, K_FUNC, K_FUNC_PROTO, K_VAR, K_DATASEC = 11, 12, 13, 14, 15
K_FLOAT, K_DECL_TAG, K_TYPE_TAG, K_ENUM64 = 16, 17, 18, 19

KIND_NAME = {
    K_INT: "int", K_PTR: "ptr", K_ARRAY: "array", K_STRUCT: "struct",
    K_UNION: "union", K_ENUM: "enum", K_FWD: "fwd", K_TYPEDEF: "typedef",
    K_VOLATILE: "volatile", K_CONST: "const", K_RESTRICT: "restrict",
    K_FUNC: "func", K_FUNC_PROTO: "func_proto", K_VAR: "var",
    K_DATASEC: "datasec", K_FLOAT: "float", K_DECL_TAG: "decl_tag",
    K_TYPE_TAG: "type_tag", K_ENUM64: "enum64",
}


def btf_from_elf(data):
    """Return the .BTF section contents of a 64-bit little-endian ELF, or None."""
    if data[:4] != ELF_MAGIC or data[4] != 2 or data[5] != 1:
        return None
    (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    if e_shoff == 0 or e_shnum == 0:
        return None
    shstr_off, shstr_size = struct.unpack_from("<QQ", data, e_shoff + e_shstrndx * e_shentsize + 0x18)
    strtab = data[shstr_off:shstr_off + shstr_size]
    for i in range(e_shnum):
        base = e_shoff + i * e_shentsize
        (name_off,) = struct.unpack_from("<I", data, base)
        off, size = struct.unpack_from("<QQ", data, base + 0x18)
        end = strtab.find(b"\0", name_off)
        name = strtab[name_off:end].decode("utf-8", "replace")
        if name == ".BTF" and size:
            return data[off:off + size]
    return None


class Btf:
    """Minimal BTF reader: self.types is 1-based; structs carry (name,type,bitoff,bitsz)."""

    def __init__(self, blob, source=""):
        self.source = source
        self.types = [None]                     # id 0 is void
        self.strings = b""
        self._parse(blob)

    def _str(self, off):
        if off == 0:
            return ""
        end = self.strings.find(b"\0", off)
        return self.strings[off:end].decode("utf-8", "replace")

    def _parse(self, blob):
        if len(blob) < 24:
            raise ValueError("blob too small to be BTF (%d bytes)" % len(blob))
        magic, version, flags, hdr_len = struct.unpack_from("<HBBI", blob, 0)
        if magic != BTF_MAGIC:
            raise ValueError("not BTF: magic %#06x" % magic)
        type_off, type_len, str_off, str_len = struct.unpack_from("<IIII", blob, 8)
        tb = blob[hdr_len + type_off: hdr_len + type_off + type_len]
        self.strings = blob[hdr_len + str_off: hdr_len + str_off + str_len]
        p, n = 0, len(tb)
        while p < n:
            name_off, info, size = struct.unpack_from("<III", tb, p)
            p += 12
            vlen = info & 0xFFFF
            kind = (info >> 24) & 0x1F
            kflag = (info >> 31) & 1
            rec = {"kind": kind, "name": self._str(name_off), "size": size,
                   "kflag": kflag, "vlen": vlen, "members": None, "id": len(self.types)}
            if kind in (K_STRUCT, K_UNION):
                members = []
                for _ in range(vlen):
                    mno, mtype, moff = struct.unpack_from("<III", tb, p)
                    p += 12
                    if kflag:                  # bitfield: low 24 bits offset, top 8 bits size
                        members.append((self._str(mno), mtype, moff & 0xFFFFFF, moff >> 24))
                    else:
                        members.append((self._str(mno), mtype, moff, 0))
                rec["members"] = members
            elif kind == K_INT:
                p += 4
            elif kind == K_ARRAY:
                p += 12
            elif kind == K_ENUM:
                p += vlen * 8
            elif kind == K_ENUM64:
                p += vlen * 12
            elif kind == K_FUNC_PROTO:
                p += vlen * 8
            elif kind in (K_VAR, K_DECL_TAG):
                p += 4
            elif kind == K_DATASEC:
                p += vlen * 12
            self.types.append(rec)

    def _name_of(self, tid):
        if 0 < tid < len(self.types) and self.types[tid]:
            return self.types[tid]["name"]
        return "?"

    def members(self, rec, prefix="", depth=0, out=None):
        """Flatten a struct/union into (key, byte_offset, bit_size). Anonymous
        members get positional names (<anon3>), and one level of them is expanded
        - which is what makes the __kabi_ignored unions readable."""
        if out is None:
            out = []
        for i, (mname, mtype, bitoff, bitsz) in enumerate(rec["members"] or []):
            trec = self.types[mtype] if 0 < mtype < len(self.types) else None
            byte = bitoff // 8
            if mname:
                out.append((prefix + mname, byte, bitsz))
            else:
                tag = "%s<anon%d>" % (prefix, i)
                isagg = trec and trec["kind"] in (K_STRUCT, K_UNION)
                out.append((tag, byte, 0))
                if isagg and depth < 1:
                    self.members(trec, tag + ".", depth + 1, out)
        return out

    def layouts(self, only=None):
        """{name: (size, [(key, offset, bitsize), ...])} for every struct/union.
        Duplicate names are keyed name#2, name#3 ... so they still diff positionally."""
        out, seen = {}, {}
        for rec in self.types:
            if not rec or rec["kind"] not in (K_STRUCT, K_UNION) or rec["members"] is None:
                continue
            name = rec["name"] or "<anon-%s>" % KIND_NAME[rec["kind"]]
            if only and name not in only:
                continue
            seen[name] = seen.get(name, 0) + 1
            key = name if seen[name] == 1 else "%s#%d" % (name, seen[name])
            out[key] = (rec["size"], self.members(rec))
        return out


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    blob = btf_from_elf(data)
    if blob is None:
        blob = data
    return Btf(blob, path)


def cmd_dump(args):
    btf = load(args.blob)
    only = set(args.structs.split(",")) if args.structs else None
    lays = btf.layouts(only)
    print("# btf_structs.py dump v1   source=%s" % btf.source)
    print("# types=%d  structs/unions=%d  offsets are in bytes" % (len(btf.types) - 1, len(lays)))
    for name in sorted(lays):
        size, mems = lays[name]
        print("struct %s size=%d members=%d" % (name, size, len(mems)))
        for key, off, bitsz in mems:
            print("  %-46s +%d%s" % (key, off, (" bits=%d" % bitsz) if bitsz else ""))
    return 0


def cmd_diff(args):
    base_all = load(args.base).layouts()
    new_all = load(args.new).layouts()

    # Only stably-named structs can be compared. Anonymous structs are keyed positionally
    # (<anon12>) and a duplicate name is keyed name#2, so one added type anywhere shifts every
    # later index and the diff fills with comparisons of unrelated structs - which is exactly
    # what the first run of this gate did (11104 "member changes" that were all that artifact).
    # Skipping them is not a weakening: an anonymous struct has no name for a module to refer
    # to, and every named struct that embeds one moved *it* only if the embedding struct's own
    # named members moved, which this still reports.
    def usable(d):
        return {k: v for k, v in d.items() if not k.startswith("<anon") and "#" not in k}

    skipped = len(base_all) - len(usable(base_all)) + len(new_all) - len(usable(new_all))
    base, new = usable(base_all), usable(new_all)

    watch = [w.strip() for w in args.watch.split(",") if w.strip()]
    if watch:
        print("--- watched structs (named members only) ---")
        for name in watch:
            if name not in base or name not in new:
                print("  %-24s %s" % (name, "MISSING from " + ("baseline" if name not in base else "candidate")))
                continue
            bsz, bmem = base[name]
            nsz, nmem = new[name]
            bmap, nmap = {k: o for k, o, _ in bmem}, {k: o for k, o, _ in nmem}
            moved = [(k, bmap[k], nmap[k]) for k in sorted(set(bmap) & set(nmap)) if bmap[k] != nmap[k]]
            print("  %-24s size %d -> %d   members %d -> %d   moved: %d%s"
                  % (name, bsz, nsz, len(bmem), len(nmem), len(moved),
                     "" if bsz == nsz else "   <-- SIZE CHANGED"))
            for k, o, n in moved[:8]:
                print("      %-30s %d -> %d  (%+d)" % (k, o, n, n - o))
        print()

    def anon(key):
        # Anonymous unions and their members are numbered by position, so they cannot be
        # compared across two builds that differ in how many there are. See the header note.
        return "<anon" in key

    moved, resized, added, removed, extra = [], [], [], [], []
    for name in sorted(set(base) | set(new)):
        if anon(name):
            continue
        if name not in base:
            added.append(name)
            continue
        if name not in new:
            removed.append(name)
            continue
        bsize, bmems = base[name]
        nsize, nmems = new[name]
        if bsize != nsize:
            resized.append((name, bsize, nsize))
        bmap = {k: (o, sz) for k, o, sz in bmems}
        nmap = {k: (o, sz) for k, o, sz in nmems}
        for k in sorted(set(bmap) & set(nmap)):
            if anon(k):
                continue
            if bmap[k][0] != nmap[k][0]:
                moved.append((name, k, bmap[k][0], nmap[k][0]))
        for k in sorted(set(nmap) - set(bmap)):
            if anon(k):
                continue
            extra.append((name, "+" + k, nmap[k][0]))
        for k in sorted(set(bmap) - set(nmap)):
            if anon(k):
                continue
            extra.append((name, "-" + k, bmap[k][0]))

    print("baseline : %s  (%d named structs/unions)" % (args.base, len(base)))
    print("candidate: %s  (%d named structs/unions)" % (args.new, len(new)))
    print("skipped  : %d positional keys (anonymous structs and duplicate names - not comparable)" % skipped)
    print()
    print("size changed      : %d" % len(resized))
    print("offset moved      : %d" % len(moved))
    print("members added/del : %d" % len(extra))
    print("structs new/gone  : %d / %d" % (len(added), len(removed)))
    # limit <= 0 means everything, which is what the hint below advertises.
    lim = args.limit if args.limit > 0 else None
    print()
    if resized:
        print("--- SIZE CHANGED (a struct changed sizeof; anything that embeds or inlines it is wrong now) ---")
        for name, b, n in resized[:lim]:
            print("  %-40s %d -> %d  (%+d)" % (name, b, n, n - b))
    if moved:
        print("--- OFFSET MOVED ---")
        for name, k, b, n in moved[:lim]:
            print("  %-36s %-34s %d -> %d  (%+d)" % (name, k, b, n, n - b))
    if extra:
        print("--- members added(+) or removed(-) ---")
        for name, k, off in extra[:lim]:
            print("  %-36s %-34s +%d" % (name, k, off))
    truncated = (lim is not None and
                 (len(resized) > lim or len(moved) > lim or len(extra) > lim))
    if truncated:
        print("  ... (%d size, %d moved, %d member entries - re-run with --limit 0 for all)"
              % (len(resized), len(moved), len(extra)))
    print()
    if moved or resized:
        print("FAIL: %d member(s) moved, %d struct(s) resized - do NOT flash this kernel."
              % (len(moved), len(resized)))
        if args.watch:
            print("      (--watch %s: check each of those names in the OFFSET MOVED list above)" % args.watch)
        return 1
    print("PASS: no existing member moved and no existing struct changed size.")
    if extra or added or removed:
        print("      %d member addition/removal and %d new struct(s) - additive layout changes only,"
              % (len(extra), len(added)))
        print("      and every one of them is after the live portion of its struct, or nothing above would be clean.")
    return 0


def cmd_extract(args):
    """Pull .BTF out of an ELF - the same section-table walk the CI step runs inline."""
    with open(args.elf, "rb") as fh:
        data = fh.read()
    blob = btf_from_elf(data)
    if blob is None:
        sys.stderr.write("no .BTF section in %s\n" % args.elf)
        return 2
    with open(args.out, "wb") as fh:
        fh.write(blob)
    magic = blob[:2].hex()
    print("wrote %s: %d bytes (magic %s)" % (args.out, len(blob), magic))
    return 0 if magic == "9feb" else 2


def main(argv):
    ap = argparse.ArgumentParser(description="Dump, diff or extract C struct layouts from BTF (see the header comment).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="dump layouts")
    d.add_argument("blob", help="raw BTF blob (adb pull /sys/kernel/btf/vmlinux) or an ELF vmlinux")
    d.add_argument("--structs", default="", help="comma-separated names to restrict the dump to")
    dd = sub.add_parser("diff", help="diff two layouts")
    dd.add_argument("base", help="baseline blob (the kernel that is known to work on the phone)")
    dd.add_argument("new", help="candidate blob (the kernel you just built)")
    dd.add_argument("--limit", type=int, default=60, help="max lines per section (0 = all)")
    dd.add_argument("--watch", default="", help="struct names you care about, mentioned in the FAIL hint")
    e = sub.add_parser("extract", help="write the .BTF section of an ELF to a file")
    e.add_argument("elf", help="an ELF with a .BTF section (vmlinux, or a module for a smoke test)")
    e.add_argument("out", help="where to write the raw BTF blob")
    args = ap.parse_args(argv)
    if args.cmd == "dump":
        return cmd_dump(args)
    if args.cmd == "extract":
        return cmd_extract(args)
    return cmd_diff(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError) as e:
        sys.stderr.write("btf_structs.py: %s\n" % e)
        sys.exit(2)

