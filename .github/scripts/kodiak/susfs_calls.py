#!/usr/bin/env python3
"""Print every susfs_*() identifier that KernelSU-Next actually calls in code.

Comments and string/char literals are removed first. The dev-susfs fork documents its
SUSFS port in long comments ("upstream's susfs_run_sus_path_loop() ...") and looks symbols
up by name (find_kernel_symbol_exact("susfs_is_inode_sus_path")); a plain grep counted
those as calls and failed the "declared by SUSFS or by the fork" gate for no reason.

usage: susfs_calls.py <KernelSU-Next/kernel dir>
"""
import os
import re
import sys

STRIP = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
CALL = re.compile(r'\b(susfs_[a-z0-9_]+)\s*\(')

found = set()
for root, _, files in os.walk(sys.argv[1]):
    for name in files:
        if name.endswith(('.c', '.h')):
            with open(os.path.join(root, name), errors='replace') as fh:
                found.update(CALL.findall(STRIP.sub(' ', fh.read())))
print('\n'.join(sorted(found)))
