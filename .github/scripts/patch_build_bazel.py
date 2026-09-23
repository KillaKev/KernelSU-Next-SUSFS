#!/usr/bin/env python3
"""
Turn off kleaf's `check_defconfig` for the GKI kernel_build target.

WHY
---
Run #3 died with:
    ERROR: common/BUILD.bazel:54:22: Creating kernel config kernel_aarch64_config failed
    ERROR: savedefconfig does not match common/arch/arm64/configs/gki_defconfig

kleaf's check_defconfig runs `make savedefconfig` and compares the result against
kernel_build.defconfig. That makes ANY hand-edit of gki_defconfig fail, because savedefconfig emits
only values that differ from their Kconfig default - and KernelSU-Next declares

    config KSU
            tristate "KernelSU function support"
            depends on KPROBES && EXT4_FS
            default y

so `savedefconfig` OMITS `CONFIG_KSU=y` even when it is in force. (CONFIG_MODULE_SCMVERSION
behaves the same way.) The diff in the log therefore did NOT mean KSU was off.

The check is replaced by something stronger and much faster: the workflow greps the generated
.config for CONFIG_KSU=y / CONFIG_KSU_SUSFS=y in the smoke-test step, which fails in ~2 minutes
instead of after a ~2 hour build.

Usage: patch_build_bazel.py <path-to-common-dir>
"""
import os
import re
import sys


def show(lines, needle, before=6, after=14):
    for i, l in enumerate(lines):
        if needle in l:
            lo, hi = max(0, i - before), min(len(lines), i + after)
            print(f"  ---- lines {lo + 1}-{hi} ----")
            for j in range(lo, hi):
                print(f"  {j + 1}: {lines[j]}")
            return True
    print(f"  (no line containing {needle!r})")
    return False


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, "BUILD.bazel")
    if not os.path.isfile(path):
        print(f"!! {path} does not exist")
        return 1

    src = open(path, encoding="utf-8", errors="replace").read()
    lines = src.split("\n")

    # 1) flip an explicit "check_defconfig = True"
    new, n = re.subn(r"(check_defconfig\s*=\s*)True\b", r"\1False", src)
    if n:
        src = new
        print(f"OK - set {n} 'check_defconfig = True' occurrence(s) to False")
    elif re.search(r"check_defconfig\s*=\s*False\b", src):
        print("OK - check_defconfig is already False")
    else:
        # 2) attribute absent - add it to the kernel_build call
        m = re.search(r'(?m)^([ \t]*)name[ \t]*=[ \t]*"kernel_aarch64"[ \t]*,?[ \t]*$', src)
        if not m:
            print("!! could not find a 'name = \"kernel_aarch64\"' line to attach it to.")
            print("   Here is what BUILD.bazel declares:")
            show(lines, "kernel_build(")
            return 1
        indent = m.group(1)
        add = f'{indent}check_defconfig = False,  # CI: added by patch_build_bazel.py'
        src = src[:m.end()] + "\n" + add + src[m.end():]
        print("OK - inserted 'check_defconfig = False' after name = \"kernel_aarch64\"")

    open(path, "w", encoding="utf-8", newline="").write(src)

    print("--- result ---")
    out = src.split("\n")
    show(out, "check_defconfig", before=8, after=4)

    if "check_defconfig = False" not in src:
        print("!! verification failed: 'check_defconfig = False' is not in the file")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
