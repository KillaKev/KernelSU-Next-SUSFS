#!/usr/bin/env python3
"""Fail the build unless the generated kernel config matches the STOCK kernel's config.

Where the stock config comes from: pulled off the device itself with
    adb pull /proc/config.gz  ->  gunzip  ->  .github/boot-template/stock-config.txt
Nothing is more authoritative about what the vendor modules were built against.

Why this gate exists: run #16's kernel flashed cleanly and then bootlooped. Its config differed from
stock in CONFIG_LTO_CLANG_FULL vs THIN, a missing CONFIG_MODULE_SCMVERSION, and LOCALVERSION handling
that masked the fact the source tree was not the release commit. Any of those can change the exported
symbol set / CFI / module acceptance, and a kernel whose modules refuse to load boots to a loop.

Target state: identical, except the KernelSU/SUSFS options we add on purpose.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STOCK = os.path.normpath(os.path.join(HERE, "..", "boot-template", "stock-config.txt"))

# Deliberate differences, and only these:
#   CONFIG_KSU*          our KernelSU-Next + SUSFS options
#   CONFIG_LOCALVERSION  we pin the full suffix here because kleaf's non-stamp path disables
#                        CONFIG_LOCALVERSION_AUTO; the two cancel out and the release string is stock
#   UNUSED_KSYMS_WHITELIST   the path to the generated ABI symbol list is inherently build-machine
ALLOWED = re.compile(
    r"^CONFIG_(KSU|KSU_SUSFS|KSU_THRONE|LOCALVERSION|LOCALVERSION_AUTO|UNUSED_KSYMS_WHITELIST)")


def parse(text):
    """Map CONFIG name -> its line, so 'not set' and '=y' compare correctly."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(?:# )?(CONFIG_[A-Za-z0-9_]+)(?: is not set|=)", line)
        if m:
            out[m.group(1)] = line.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="the generated .config from this build")
    ap.add_argument("--stock", default=DEFAULT_STOCK, help="reference config (default: stock)")
    ap.add_argument("--allow-regex", default=None, help="override the allowed-difference pattern")
    args = ap.parse_args()

    if not os.path.exists(args.stock):
        print("::error::reference config not found: %s" % args.stock)
        return 1
    if not os.path.exists(args.config):
        print("::error::generated config not found: %s" % args.config)
        return 1

    with open(args.config, encoding="utf-8", errors="replace") as fh:
        ours = parse(fh.read())
    with open(args.stock, encoding="utf-8", errors="replace") as fh:
        stock = parse(fh.read())
    allowed = re.compile(args.allow_regex) if args.allow_regex else ALLOWED

    only_stock = sorted(set(stock) - set(ours))
    only_ours = sorted(set(ours) - set(stock))
    changed = sorted(k for k in set(stock) & set(ours) if stock[k] != ours[k])

    print("=== generated config vs STOCK kernel config ===")
    print("ours: %d entries    stock: %d entries" % (len(ours), len(stock)))

    problems = []
    for title, keys, side in (("present in STOCK, absent from ours", only_stock, stock),
                              ("present in OURS, absent from stock", only_ours, ours)):
        print("\n--- %s (%d) ---" % (title, len(keys)))
        for k in keys:
            print("  " + side[k])
            if not allowed.match(k):
                problems.append("%s -> %s" % (title, side[k]))

    print("\n--- value differs (%d) ---" % len(changed))
    for k in changed:
        print("  stock: " + stock[k])
        print("  ours : " + ours[k])
        if not allowed.match(k):
            problems.append("value differs: stock %r vs ours %r" % (stock[k], ours[k]))

    print()
    if problems:
        print("::error::%d config difference(s) outside the allowed KernelSU/SUSFS set:"
              % len(problems))
        for p in problems:
            print("  " + p)
        print("Any of these can change exported symbol CRCs / CFI / module acceptance, which makes the")
        print("vendor modules refuse to load - and a kernel whose modules will not load bootloops.")
        return 1
    print("OK   config matches stock apart from the intended KernelSU/SUSFS options")
    return 0


if __name__ == "__main__":
    sys.exit(main())
