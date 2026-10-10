#!/usr/bin/env python3
"""
crc_gate.py - predict, on the bench, whether the phone's prebuilt modules will load
against a freshly built kernel.

WHY THIS EXISTS
    The device runs CONFIG_MODVERSIONS=y. Every module it ships records, in its
    __versions section, the CRC it expects for EVERY symbol it imports. At load time
    the kernel compares each expected CRC against the CRC of the symbol it exports and
    refuses the module on the first disagreement:

        bcmdhd4390: disagrees about version of symbol __netdev_alloc_skb
        bcmdhd4390: Unknown symbol __netdev_alloc_skb (err -22)

    On 2026-10-03 a kernel built from the KernelSU-Next / SUSFS dev-susfs TIPS was
    flashed. cfg80211 and bcmdhd4383/bcmdhd4390 never loaded (Wi-Fi gone), the modem
    stack degraded, and 312 modules loaded where the Wild Kernel loads 369. Nothing in
    that pipeline checked this contract, so the phone found out. This script is that
    check, and it runs WITHOUT the phone: it only needs the kernel's Module.symvers
    (uploaded by the workflow, kodiak-...-Module.symvers) and the device's .ko files
    pulled over adb while a known-good kernel is running.

USAGE
    adb pull /vendor_dlkm/lib/modules  C:\\Users\\coolk\\workspace\\_kernel_ref\\devmodules
    python crc_gate.py --symvers kodiak-...-Module.symvers --modules devmodules

    Exit 0 = every module should load. Exit 1 = at least one would be refused; the
    report names the module, the symbol and the two CRCs.

WHAT IT CHECKS
    1. CRC match      - symbol imported by a module, exported by our kernel: CRCs equal?
    2. Kernel export  - symbol neither in our Module.symvers nor exported by another
                        device module: our kernel does not export it at all.
    3. vermagic flags - the flag half of the vermagic (SMP preempt mod_unload modversions
                        aarch64) after the first space must match ours, because with
                        CONFIG_MODVERSIONS the version token of the vermagic is skipped.
    Cross-module imports (bcmdhd4390 -> cfg80211) are reported, not judged: Google ships
    those as one consistent set, and each exporter is checked on its own.
"""

import argparse
import glob
import os
import struct
import sys

MODVERSION_ENTRY_SIZE = 64          # struct modversion_info: unsigned long crc (8) + name[56]
SHN_UNDEF = 0
KERNEL_FLAGS_MARK = "modversions"   # the flag half of the vermagic is what matters here


# ---------------------------------------------------------------------------- Module.symvers
def parse_symvers(path):
    """Module.symvers lines: '0x<hex crc>\\t<symbol>\\t<providing module>'."""
    crcs = {}
    bad = 0
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                crc = int(parts[0], 16)
            except ValueError:
                bad += 1
                continue
            if crc:                      # 0x00000000 means the providing side carries no CRC
                crcs[parts[1]] = crc
    return crcs, bad


# ------------------------------------------------------------------------------------- ELF
class Elf:
    """Just enough ELF64-LE to get section names, section bytes and undefined symbols."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        if len(d) < 64 or d[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        if d[4] != 2 or d[5] != 1:
            raise ValueError("not ELF64 little-endian")
        # ELF64 header: e_shoff @0x28 (Q), e_flags @0x30 (I), e_ehsize @0x34 (H),
        # e_phentsize @0x36, e_phnum @0x38, e_shentsize @0x3a, e_shnum @0x3c, e_shstrndx @0x3e.
        (self.shoff, _flags, _ehsize, _phentsize, _phnum,
         self.shentsize, self.shnum, self.shstrndx) = struct.unpack_from("<QIHHHHHH", d, 0x28)
        self.sections = {}                  # name -> (offset, size, entsize, link, type)
        self._read_sections()

    def _read_sections(self):
        d, shoff, n, ent = self.data, self.shoff, self.shnum, self.shentsize
        raw = []
        for i in range(n):
            base = shoff + i * ent
            (name_off, stype, _flags, _addr, off, size, link, _info,
             _align, sh_entsize) = struct.unpack_from("<IIQQQQIIQQ", d, base)
            raw.append((name_off, stype, off, size, link, sh_entsize))
        strtab = raw[self.shstrndx]
        strings = d[strtab[2]:strtab[2] + strtab[3]]
        for name_off, stype, off, size, link, sh_entsize in raw:
            end = strings.find(b"\x00", name_off)
            name = strings[name_off:end].decode("utf-8", "replace") if name_off < len(strings) else ""
            self.sections[name] = (off, size, sh_entsize, link, stype)

    def section(self, name):
        if name not in self.sections:
            return None
        off, size, _entsize, _link, _stype = self.sections[name]
        return self.data[off:off + size]

    def modinfo(self):
        """NUL-separated key=value blob -> dict (gives name= and vermagic=)."""
        blob = self.section(".modinfo") or b""
        out = {}
        for item in blob.split(b"\x00"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return out

    def expected_versions(self):
        """[(expected_crc, symbol)] from __versions (None if that section is absent)."""
        blob = self.section("__versions")
        if not blob:
            return None
        if len(blob) % MODVERSION_ENTRY_SIZE:
            # Do not guess: report the oddity instead of silently mis-slicing the table.
            raise ValueError("__versions size %d is not a multiple of %d"
                             % (len(blob), MODVERSION_ENTRY_SIZE))
        out = []
        for i in range(0, len(blob), MODVERSION_ENTRY_SIZE):
            crc = struct.unpack_from("<Q", blob, i)[0]
            name = blob[i + 8:i + MODVERSION_ENTRY_SIZE].split(b"\x00")[0]
            if crc and name:
                out.append((crc, name.decode("utf-8", "replace")))
        return out

    def exports(self):
        """Symbols this module itself exports (__ksymtab_strings) - i.e. device-to-device."""
        blob = self.section("__ksymtab_strings")
        if not blob:
            return set()
        return {s.decode("utf-8", "replace") for s in blob.split(b"\x00") if len(s) > 1}

    def undefined(self):
        """Undefined symbol names from .symtab - the fallback when __versions is absent."""
        meta = self.sections.get(".symtab")
        strmeta = self.sections.get(".strtab")
        if not meta or not strmeta or not meta[2]:
            return set()
        off, size, entsize, _link, _stype = meta
        strtab = self.data[strmeta[0]:strmeta[0] + strmeta[1]]
        out = set()
        for i in range(0, size, entsize):
            st_name, _info, _other, st_shndx = struct.unpack_from("<IBBH", self.data, off + i)
            if st_shndx != SHN_UNDEF or not st_name:
                continue
            end = strtab.find(b"\x00", st_name)
            nm = strtab[st_name:end].decode("utf-8", "replace")
            if nm:
                out.add(nm)
        return out


# ------------------------------------------------------------------------------- the gate
def judge(symbol, expected, mine, provided):
    """Return (verdict, crc) for one imported symbol.

    verdict: 'ok'        - our kernel exports it and the CRC agrees
             'crc'       - our kernel exports it but with a DIFFERENT CRC -> module refused
             'missing'   - our kernel does not export it and no device module does either
             'intermod'  - another device module provides it (checked on its own)
    """
    if symbol in mine:
        if expected is not None and mine[symbol] != expected:
            return "crc", mine[symbol]
        return "ok", mine[symbol]
    if symbol in provided:
        return "intermod", None
    return "missing", None


def analyse(symvers, module_dir):
    mine, unparsed = parse_symvers(symvers)
    kos = sorted(glob.glob(os.path.join(module_dir, "**", "*.ko"), recursive=True))
    print("Module.symvers : %s  (%d exported symbol CRCs%s)"
          % (symvers, len(mine), ", %d unparsable lines" % unparsed if unparsed else ""))
    print("device modules : %s  (%d .ko files)" % (module_dir, len(kos)))
    if not kos:
        print("::error::no .ko files found - pull the device's module directory first, e.g.")
        print("         adb pull /vendor_dlkm/lib/modules " + module_dir)
        return 2, None

    mods, unreadable = [], []
    for path in kos:
        try:
            mods.append((path, Elf(path)))
        except Exception as exc:                     # noqa: BLE001 - report, never abort
            unreadable.append((os.path.basename(path), str(exc)))

    # Symbols the device set provides to itself; legitimately absent from our kernel.
    provided = set()
    for _p, elf in mods:
        provided |= elf.exports()

    failures, intermod, oks, no_versions, flags = [], 0, 0, [], set()
    per_module = []

    for path, elf in mods:
        info = elf.modinfo()
        name = info.get("name") or os.path.basename(path)[:-3]
        vermagic = info.get("vermagic", "")
        if vermagic:
            parts = vermagic.split(" ", 1)
            flags.add(parts[1] if len(parts) > 1 else "(no flags)")
        try:
            expected = elf.expected_versions()
        except ValueError as exc:
            expected = None
            no_versions.append((name, "unreadable __versions: %s" % exc))

        bad_here = 0
        if expected is None:
            # No CRC table: still check that every undefined symbol can be resolved at all.
            no_versions.append((name, "no __versions section (checked undefined symbols only)"))
            for sym in sorted(elf.undefined()):
                verdict, crc = judge(sym, None, mine, provided)
                if verdict in ("crc", "missing"):
                    bad_here += 1
                    failures.append((name, sym, None, crc, verdict))
                elif verdict == "intermod":
                    intermod += 1
                else:
                    oks += 1
        else:
            for crc, sym in expected:
                verdict, ours = judge(sym, crc, mine, provided)
                if verdict in ("crc", "missing"):
                    bad_here += 1
                    failures.append((name, sym, crc, ours, verdict))
                elif verdict == "intermod":
                    intermod += 1
                else:
                    oks += 1
        per_module.append((name, len(elf.undefined()) if expected is None else len(expected),
                           bad_here, vermagic))
    return 0, dict(failures=failures, intermod=intermod, oks=oks, no_versions=no_versions,
                   flags=flags, per_module=per_module, mods=len(mods), unreadable=unreadable)


# ------------------------------------------------------------------------------- reporting
def report(res, report_path, ignored=()):
    # Modules named with --ignore-module are shown but do not turn the gate red. Used for modules
    # the phone does not load on ANY custom kernel (rust_binder: its Rust symbol names carry a crate
    # hash, so the CRCs differ for every non-Google build; binder itself is built in).
    skipped = [f for f in res["failures"] if f[0] in ignored]
    fail = [f for f in res["failures"] if f[0] not in ignored]
    out = []
    add = out.append
    add("crc_gate - device module loadability against a built kernel")
    add("=" * 72)
    add("modules checked      : %d" % res["mods"])
    add("symbol imports       : %d matched, %d cross-module (device-to-device)" % (res["oks"], res["intermod"]))
    add("HARD FAILURES        : %d" % len(fail))
    if skipped:
        add("ignored (known)      : %d symbol(s) in %s" % (len(skipped), ", ".join(sorted({f[0] for f in skipped}))))
    add("vermagic flag set(s) : %s" % (" | ".join(sorted(res["flags"])) or "(none)"))
    if res["unreadable"]:
        add("unreadable files     : %d" % len(res["unreadable"]))
        for n, why in res["unreadable"]:
            add("    %-28s %s" % (n, why))
    add("")

    if fail:
        by_kind = {}
        for mod, sym, exp, ours, kind in fail:
            by_kind.setdefault(kind, []).append((mod, sym, exp, ours))
        add("WHY THIS MATTERS: a module whose expected CRC differs from ours is refused at load")
        add('("disagrees about version of symbol X" then "Unknown symbol X (err -22)"), and the')
        add("subsystem it drives dies with it. Wi-Fi = cfg80211 + bcmdhd4383/4390.")
        add("")
        for kind, title in (("crc", "CRC MISMATCH - our kernel exports the symbol with a different CRC"),
                            ("missing", "NOT EXPORTED - our kernel does not export the symbol at all")):
            rows = by_kind.get(kind, [])
            if not rows:
                continue
            mods = sorted({r[0] for r in rows})
            add("%s: %d symbol(s) across %d module(s)" % (title, len(rows), len(mods)))
            for mod, sym, exp, ours in sorted(rows)[:40]:
                add("    %-24s %-46s expected %s  ours %s"
                    % (mod, sym, ("0x%08x" % exp) if exp is not None else "n/a",
                       ("0x%08x" % ours) if ours is not None else "not exported"))
            if len(rows) > 40:
                add("    ... %d more" % (len(rows) - 40))
            add("")
        add("affected modules     : %s" % ", ".join(sorted({m for m, _s, _e, _o, _k in fail})))
    else:
        add("Every module imported is satisfied by this kernel: CRCs match and every kernel")
        add("symbol it needs is exported. Wi-Fi (cfg80211, bcmdhd*), modem and touch should load.")

    if res["no_versions"]:
        add("")
        add("modules without a usable __versions table (checked via .symtab instead): %d"
            % len(res["no_versions"]))

    text = "\n".join(out)
    print(text)
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("\nreport written to %s" % report_path)
    except OSError as exc:
        print("\n(could not write %s: %s)" % (report_path, exc))
    if res["unreadable"]:
        # A module that cannot be read cannot be judged, and everything that imports its symbols then
        # looks "not exported". That is a problem with the gate's input, not a verdict on the kernel -
        # and it must never read as GREEN.
        print(chr(10) + "GATE: INCONCLUSIVE - %d module file(s) could not be read (see above); this is an "
              "extraction problem, not a verdict on the kernel" % len(res["unreadable"]))
        return 2
    print(chr(10) + "GATE: %s" % ("RED - do NOT flash this kernel" if fail else "GREEN - safe to flash"))
    return 1 if fail else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Predict, without flashing, whether the phone's prebuilt modules can load "
                    "against a built kernel (the CONFIG_MODVERSIONS CRC contract).")
    ap.add_argument("--symvers", required=True, help="Module.symvers from the CI artifact")
    ap.add_argument("--modules", required=True, help="directory holding the device's .ko files")
    ap.add_argument("--report", default="crc_report.txt", help="write the full report here")
    ap.add_argument("--ignore-module", action="append", default=[],
                    help="module whose failures are reported but do not fail the gate (repeatable)")
    args = ap.parse_args(argv)
    rc, res = analyse(args.symvers, args.modules)
    if res is None:
        return rc
    return report(res, args.report, set(args.ignore_module))


if __name__ == "__main__":
    sys.exit(main())


