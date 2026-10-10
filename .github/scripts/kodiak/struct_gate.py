#!/usr/bin/env python3
"""
struct_gate.py <stock.btf> <built.btf> [report]

Automatic version of the struct-layout check. btf_structs.py diff compares EVERY struct, and between
two builds hundreds legitimately differ (SUSFS adds fields to getdents_callback, configs change
f2fs_sb_info, a newer sublevel reworks internals) - none of which a vendor module touches. So the
gate judges only the structs vendor modules really access, and only the two failures a CRC check
cannot see: an existing member moved to another offset, or the struct changed size. Members that were
added or removed are not failures (the kABI padding unions do exactly that, by design, with every
other offset unchanged).

Exit 0 = every watched struct kept its layout, 1 = one did not (do not flash), 2 = could not compare.
"""
import os
import re
import subprocess
import sys

WATCH = """
task_struct mm_struct vm_area_struct cred nsproxy file inode dentry super_block address_space
sk_buff sock socket net_device napi_struct tcp_sock inet_sock inet_connection_sock
sched_entity cfs_rq rq task_group device kobject platform_device irq_desc request_queue bio
""".split()


def main():
    base, new = sys.argv[1], sys.argv[2]
    report = sys.argv[3] if len(sys.argv) > 3 else None
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run([sys.executable, os.path.join(here, "btf_structs.py"), "diff", base, new, "--limit", "0"],
                       capture_output=True, text=True)
    if r.returncode == 2:
        print("::error::btf_structs.py could not compare the two BTF blobs:\n" + r.stdout[-400:] + r.stderr[-400:])
        return 2
    section, resized, moved = None, [], []
    for line in r.stdout.splitlines():
        if line.startswith("--- SIZE CHANGED"):
            section = "size"
        elif line.startswith("--- OFFSET MOVED"):
            section = "moved"
        elif line.startswith("---") or not line.startswith("  "):
            section = None
        elif section == "size":
            m = re.match(r"^\s+(\S+)\s+(\d+) -> (\d+)", line)
            if m:
                resized.append((m.group(1), line.strip()))
        elif section == "moved":
            m = re.match(r"^\s+(\S+)\s+(\S+)\s+(\d+) -> (\d+)", line)
            if m:
                moved.append((m.group(1), line.strip()))
    summary = [l for l in r.stdout.splitlines()
               if l.startswith(("size changed", "offset moved", "members added", "structs new"))]
    bad = [x for x in resized + moved if x[0] in WATCH]
    out = ["struct_gate - watched structs: " + " ".join(WATCH), "", "all structs (informational):"] + ["  " + s for s in summary] + [""]
    if bad:
        out.append("WATCHED STRUCT LAYOUT CHANGED:")
        out += ["  " + b[1] for b in bad[:30]]
        if len(bad) > 30:
            out.append("  ... and %d more lines (full diff: btf_structs.py diff)" % (len(bad) - 30))
        out.append("")
    out.append("GATE: %s" % ("RED - a struct vendor modules access changed layout; do NOT flash this kernel" if bad
                              else "GREEN - every watched struct kept its size and member offsets"))
    text = "\n".join(out)
    print(text)
    if report:
        open(report, "w", encoding="utf-8").write(text + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
