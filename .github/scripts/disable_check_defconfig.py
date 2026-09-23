#!/usr/bin/env python3
"""
Force kleaf's check_defconfig to "disabled" in the synced ACK tree.

WHY
---
Runs #3 and #4 died with:
    ERROR: Creating kernel config kernel_aarch64_config failed
    ERROR: savedefconfig does not match common/arch/arm64/configs/gki_defconfig

In this vintage of kleaf, build/kernel/kleaf/common_kernels.bzl contains:

    check_defconfig = select({
        Label("//build/kernel/kleaf:gki_build_config_fragment_is_unset"):
            "match" if pre_defconfig_fragments else "minimized",
        "//conditions:default": "disabled",
    }),

So for a pure GKI build (no gki_build_config_fragment) it runs the strict comparison of
`make savedefconfig` against kernel_build.defconfig and errors on ANY difference. A hand-edited
gki_defconfig can never satisfy that: KernelSU-Next declares CONFIG_KSU as `default y`, and
savedefconfig never emits a value equal to its default, so it drops CONFIG_KSU=y even when the
option is in force.

Rather than fight it, force "disabled" and rely on the workflow's own check, which is both
stronger and much faster (~2 min instead of ~2 h): it greps the GENERATED .config for
CONFIG_KSU=y and CONFIG_KSU_SUSFS=y.

Usage: disable_check_defconfig.py <path-to-synced-repo-root>
"""
import os
import sys

KEY = "check_defconfig"
VALUE = '"disabled"'


def force_value(text):
    """Replace the value of every `check_defconfig = <expr>` with "disabled".

    The value may be a multi-line `select({...})`, so scan to the end of the expression using
    balanced delimiters, stopping at a top-level comma or the closing bracket of the call.
    """
    hits = 0
    idx = 0
    while True:
        i = text.find(KEY, idx)
        if i < 0:
            break
        j = i + len(KEY)
        while j < len(text) and text[j] in " \t":
            j += 1
        if j >= len(text) or text[j] != "=":
            idx = j
            continue
        j += 1
        while j < len(text) and text[j] in " \t":
            j += 1

        k, depth = j, 0
        while k < len(text):
            c = text[k]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            k += 1

        if text[j:k].strip() == VALUE:
            idx = k
            continue
        text = text[:j] + VALUE + text[k:]
        hits += 1
        idx = j + len(VALUE)
    return text, hits


def show(text, needle, before=2, after=8):
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if needle in l:
            lo, hi = max(0, i - before), min(len(lines), i + after)
            print(f"  ---- lines {lo + 1}-{hi} ----")
            for j in range(lo, hi):
                print(f"  {j + 1}: {lines[j]}")
            return
    print(f"  (no line containing {needle!r})")


def find_targets(root):
    primary = os.path.join(root, "build", "kernel", "kleaf", "common_kernels.bzl")
    if os.path.isfile(primary):
        return [primary]
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".bzl"):
                p = os.path.join(dirpath, f)
                try:
                    if KEY in open(p, encoding="utf-8", errors="replace").read():
                        found.append(p)
                except OSError:
                    pass
    return found


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    targets = find_targets(root)
    print(f"root: {root}")
    print(f"files mentioning {KEY}: {targets}")
    if not targets:
        print(f"!! no .bzl file mentions {KEY} - cannot disable the check")
        return 1

    total = 0
    for path in targets:
        src = open(path, encoding="utf-8", errors="replace").read()
        new, hits = force_value(src)
        print(f"\n{path}: {hits} change(s)")
        if hits:
            open(path, "w", encoding="utf-8", newline="").write(new)
            total += hits
        show(new, KEY)

    if not total:
        print(f"!! nothing changed - could not force {KEY} to {VALUE}")
        return 1
    print(f"\nOK - {KEY} forced to {VALUE} in {total} place(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
