#!/usr/bin/env python3
"""Detect cross-references that still carry pre-Phase-3 chapter numbers.

For every 'Ch. N' / 'Ch.N' / '§N.M' reference, compare the surrounding words with the
title/section words of the CURRENT target chapter and of the target under the old→new
renumber map. 'STALE' = the mapped target matches the context and the current one does not.
Fenced-block refs are reported separately (near-certain); prose refs are a heuristic list.

usage: stale_crossrefs.py [chapters_dir] [--all]   (--all prints ambiguous/unknown too)
"""
import os, re, sys, collections
D = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "chapters"
ALL = "--all" in sys.argv
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
def old2new(n):
    if n <= 4: return n
    if n <= 37: return n + 1
    if n <= 41: return n + 2
    if n <= 45: return n + 3
    if n <= 47: return n + 4
    if n == 48: return 53
    if n == 49: return 55
    if n == 50: return 58
    if n <= 53: return n + 10
    if n <= 64: return n + 12
    return None
STOP = set("the and of for a an in on to vs with its it is from into at by your you how when what why not or as be this that are".split())
def words(s): return set(w for w in re.findall(r"[a-z0-9_+]+", s.lower()) if w not in STOP and len(w) > 2)
files = sorted(f for f in os.listdir(D) if f.endswith(".md") and f != "README.md")
kw, titles = {}, {}
for f in files:
    m = re.match(r"^(\d{2})-", f)
    if not m: continue
    n = int(m.group(1)); k = set()
    for l in open(os.path.join(D, f), encoding="utf-8"):
        mh = re.match(r"^#\s+Chapter\s+\d+\s*[—–-]\s*(.+)$", l)
        if mh: titles[n] = mh.group(1).strip(); k |= words(mh.group(1))
        ms = re.match(r"^#{2,3}\s+\d+\.\d+(?:\.\d+)?\s+(.+)$", l)
        if ms: k |= words(ms.group(1))
    kw[n] = k
out = collections.defaultdict(list)
for f in files:
    inf = False; own = int(f[:2]) if re.match(r"^\d{2}-", f) else None
    for i, l in enumerate(open(os.path.join(D, f), encoding="utf-8"), 1):
        if FENCE.match(l): inf = not inf; continue
        for m in re.finditer(r"(?:\bCh(?:\.|apter)s?\s?|§\s?)(\d+)(?:\.\d+)?", l):
            n = int(m.group(1))
            if n < 1 or n > 76: continue
            ctx = l[max(0, m.start() - 120):m.end() + 120]; cw = words(ctx)
            cur = bool(cw & kw.get(n, set())); mapped = old2new(n)
            if mapped == n: continue
            mp = bool(mapped in kw and (cw & kw[mapped]))
            selfref = m.group(0).startswith("§") and own is not None and mapped == own and n != own
            if selfref or (mp and not cur): cls = "STALE"
            elif cur and not mp: cls = "ok"
            elif cur and mp: cls = "ambiguous"
            else: cls = "unknown"
            if cls == "STALE" or ALL:
                out["FENCED" if inf else "PROSE"].append(f"{cls:<9} {f}:{i}: {m.group(0)} -> {mapped} ({titles.get(mapped,'')[:40]}) | {ctx.strip()[:160]}")
for sec in ("FENCED", "PROSE"):
    print(f"## {sec} ({len(out[sec])})")
    print("\n".join(out[sec]))
