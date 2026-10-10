#!/usr/bin/env python3
"""
susfs_smaps_port.py <common-dir>

Hand-port of the two fs/proc/task_mmu.c hunks of the SUSFS kernel patch that stop applying once
show_smaps_rollup() has been reworked upstream (seen on 6.12.92; 6.12.69 still applies as-is).

The reworked function walks VMAs with proc_get_vma() and decides in one place which stats call to
make:

    if (vma->vm_start < last_vma_end) { ...smap_gather_stats_range(...) }
    else                              { ...smap_gather_stats(...) }

SUSFS wants a hidden mapping (SUSFS_IS_INODE_SUS_MAP) to be left out of smaps_rollup. The old patch
did that around each smap_gather_stats() call; here one guard in front of the if/else does the same:

    #ifdef CONFIG_KSU_SUSFS_SUS_MAP
        if (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))) {
            /* SUSFS: hidden mapping, not counted */
        } else
    #endif
        if (vma->vm_start < last_vma_end) { ...

last_vma_end is still advanced afterwards, so the walk is unchanged for the other VMAs.

Exit 0 = ported (or nothing to do); non-zero = the function does not look like the one this port was
written against, so the build must stop rather than ship a half-applied SUSFS.
"""
import os
import sys

root = sys.argv[1]
path = os.path.join(root, "fs/proc/task_mmu.c")
rej = path + ".rej"
src = open(path, encoding="utf-8").read()

if "SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))" in src and "show_smaps_rollup" in src:
    # Is the rollup function itself already guarded (old-style patch applied)?
    body = src[src.index("static int show_smaps_rollup"):]
    body = body[:body.index("\n}\n")]
    if "SUSFS_IS_INODE_SUS_MAP" in body:
        print("show_smaps_rollup already carries the SUSFS guard - nothing to port")
        if os.path.exists(rej):
            os.remove(rej)
        sys.exit(0)

anchor = (
    "\t\tif (vma->vm_start < last_vma_end) {\n"
    "\t\t\t/*\n"
    "\t\t\t * After retaking the lock, already reported VMA grew\n"
)
if src.count(anchor) != 1:
    print("::error::show_smaps_rollup does not look like the 6.12.92 shape (anchor found %d times)" % src.count(anchor))
    sys.exit(1)

guard = (
    "#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
    "\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file))) {\n"
    "\t\t\t/* SUSFS: hidden mapping, not counted in smaps_rollup */\n"
    "\t\t} else\n"
    "#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n"
)
out = src.replace(anchor, guard + anchor)
if out.count("SUSFS: hidden mapping, not counted in smaps_rollup") != 1:
    print("::error::port did not land exactly once")
    sys.exit(1)
open(path, "w", encoding="utf-8", newline="\n").write(out)
if os.path.exists(rej):
    os.remove(rej)
orig = path + ".orig"
if os.path.exists(orig):
    os.remove(orig)
print("ported the SUSFS_SUS_MAP guard into show_smaps_rollup (fs/proc/task_mmu.c)")
