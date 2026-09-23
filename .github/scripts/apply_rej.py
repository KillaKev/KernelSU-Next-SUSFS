#!/usr/bin/env python3
"""
Apply rejected kernel-patch hunks (.rej) so they are not silently lost.

WHY THIS EXISTS
---------------
The SUSFS kernel patch is written against a slightly different android13-5.15
revision than the one we build from (2026-03). `patch` therefore rejects a few
hunks. The SUSFS README says to fix those by hand; this does it automatically.

WHAT IT DOES
------------
For each .rej next to a target file:
  * pure-insertion hunks (no '-' lines) - find the context line that precedes the
    insertion, then insert the '+' lines after it. This is the common case: the
    rejected hunks are #include / extern / #define blocks near the top of a file.
  * modifying hunks (have '-' lines) - rebuild the "old" text from context+'-'
    and the "new" text from context+'+', then substitute.
  * if an anchor cannot be found, the hunk is printed and the script exits 1, so
    a broken integration fails loudly instead of producing a kernel that will not
    compile.

Usage: apply_rej.py <kernel-tree-root>
"""
import os
import re
import sys


def parse_hunks(text):
    hunks = []
    for part in re.split(r"(?m)^@@ ", text)[1:]:
        lines = part.split("\n")
        body = lines[1:]                      # drop the rest of the @@ header line
        seq = []
        for ln in body:
            if ln[:1] in (" ", "+", "-") and ln.strip() != "":
                seq.append((ln[0], ln[1:]))
            elif ln[:1] in (" ", "+", "-"):
                seq.append((ln[0], ""))
        hunks.append(seq)
    return hunks


def find_line(lines, needle):
    """Exact match, then whitespace-insensitive, then header-name, then substring.

    Never matches a blank needle: an empty needle would "find" the file's first empty line.
    """
    if needle.strip() == "":
        return -1
    for i, l in enumerate(lines):
        if l == needle:
            return i
    stripped = needle.strip()
    for i, l in enumerate(lines):
        if l.strip() == stripped:
            return i
    # '#include <linux/foo.h>' / '#include "foo.h"' - match on the header name alone so a moved
    # or reformatted include block still resolves.
    m = re.match(r"#\s*include\s+[<\"]([^>\"]+)[>\"]", stripped)
    if m:
        hdr = m.group(1)
        for i, l in enumerate(lines):
            if re.match(r"#\s*include\s+[<\"]", l.strip()) and hdr in l:
                return i
    if len(stripped) >= 12:
        for i, l in enumerate(lines):
            if stripped in l:
                return i
    return -1


def apply_pure_insert(lines, seq, where):
    """Insert each '+' group after the last NON-BLANK context line preceding it.

    The blank-line detail is the whole point: these hunks have a blank context line directly
    before the inserted block, and anchoring on "" would match the file's first empty line -
    dropping the code into the top of the file (inside the license comment).
    """
    anchor = None
    pending = []
    groups = []
    for kind, text in seq:
        if kind == " ":
            if pending:
                groups.append((anchor, pending))
                pending = []
            if text.strip() != "":
                anchor = text
        elif kind == "+":
            pending.append(text)
    if pending:
        groups.append((anchor, pending))

    added = 0
    for anchor_text, adds in groups:
        if anchor_text is None:
            print(f"    !! no usable anchor for {len(adds)} added line(s) in {where}")
            return -1
        idx = find_line(lines, anchor_text)
        if idx < 0:
            # Last resort: after the final #include. Safe for the preprocessor / extern /
            # #define blocks these rejected hunks contain.
            last_inc = max((i for i, l in enumerate(lines)
                            if l.lstrip().startswith("#include")), default=-1)
            if last_inc >= 0:
                print(f"    ~ anchor not found ({anchor_text.strip()[:50]!r}) - "
                      f"falling back to after the last #include (line {last_inc + 1})")
                idx = last_inc
            else:
                print(f"    !! anchor not found in {where}: {anchor_text.strip()[:70]!r}")
                print("       wanted to insert:")
                for a in adds:
                    print(f"         + {a}")
                return -1
        lines[idx + 1:idx + 1] = adds
        print(f"    + inserted {len(adds)} line(s) after line {idx + 1} ({anchor_text.strip()[:46]})")
        added += len(adds)
    return added


def apply_modify(lines, seq, where):
    old, new = [], []
    for kind, text in seq:
        if kind == " ":
            old.append(text)
            new.append(text)
        elif kind == "-":
            old.append(text)
        elif kind == "+":
            new.append(text)
    n = len(old)
    for i in range(len(lines) - n + 1):
        if lines[i:i + n] == old:
            lines[i:i + n] = new
            print(f"    ~ replaced {n} line(s) at line {i + 1} ({where})")
            return 1
    print(f"    !! context not found for a modifying hunk at {where}")
    return -1


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    rejs = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".rej"):
                rejs.append(os.path.join(dirpath, f))
    rejs.sort()

    if not rejs:
        print("no .rej files - clean apply")
        return 0

    failures = 0
    for rej in rejs:
        target = rej[:-4]
        rel = os.path.relpath(rej, root)
        print(f"\n=== {rel} ===")
        if not os.path.isfile(target):
            print(f"    !! target missing: {target}")
            failures += 1
            continue
        lines = open(target, "r", errors="surrogateescape").read().split("\n")
        hunks = parse_hunks(open(rej, "r", errors="surrogateescape").read())
        touched = False
        for h in hunks:
            if not any(k == "+" for k, _ in h):
                continue
            if any(k == "-" for k, _ in h):
                rc = apply_modify(lines, h, rel)
            else:
                rc = apply_pure_insert(lines, h, rel)
            if rc < 0:
                failures += 1
            else:
                touched = True
        if touched:
            open(target, "w", errors="surrogateescape").write("\n".join(lines))
            os.remove(rej)
            print(f"    wrote {target} and removed the .rej")

    print(f"\n{len(rejs)} .rej processed, {failures} problem hunk(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
