#!/usr/bin/env python3
"""Drop dependency testbench tops from bender's VCS compile script.

vlogan puts all files of one invocation in a single $unit, so unrelated
dependency testbenches clash (e.g. class icache_request defined twice).
None of them are part of the Occamy design or testharness.
"""
import re, sys

TB = re.compile(r'/\.bender/git/checkouts/[^/]+/test/(tb_[^/]*|[^/]*_tb)\.sv"')
SRC = re.compile(r'\.(sv|v|svh|vp)"')

# Split into commands: a command runs until a line without a trailing backslash.
cmds, cur = [], []
for l in open(sys.argv[1]).read().split('\n'):
    cur.append(l)
    if not l.rstrip().endswith('\\'):
        cmds.append(cur)
        cur = []
if cur:
    cmds.append(cur)

out, ndropped = [], 0
for c in cmds:
    if not c[0].startswith('vlogan'):
        out += c
        continue
    keep = []
    for l in c:
        if TB.search(l) and not l.rstrip().rstrip('\\').rstrip().endswith('_pkg.sv"'):
            print('dropped', l.strip(), file=sys.stderr)
            ndropped += 1
        else:
            keep.append(l)
    if not any(SRC.search(l) for l in keep):
        print('dropped empty vlogan call', file=sys.stderr)
        continue
    keep = [l.rstrip().rstrip('\\').rstrip() + ' \\' for l in keep]
    keep[-1] = keep[-1][:-2]
    out += keep
open(sys.argv[1], 'w').write('\n'.join(out))
print(f'dropped {ndropped} files', file=sys.stderr)
