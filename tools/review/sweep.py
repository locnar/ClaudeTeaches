#!/usr/bin/env python3
"""Mechanical consistency sweep over chapters/*.md."""
import os, re, sys, collections
D = "chapters"
files = sorted(os.listdir(D))
chap = [f for f in files if re.match(r"^\d{2}-.*\.md$", f) and not f.startswith("00-")]
apps = [f for f in files if f.startswith("appendix-")]
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")

def lines_outside_fences(path):
    out=[]; inf=False
    for i,l in enumerate(open(path,encoding="utf-8"),1):
        l=l.rstrip("\n")
        if FENCE.match(l): inf = not inf; continue
        if not inf: out.append((i,l))
    return out

issues=collections.defaultdict(list)
titles={}
sections={}
for f in chap:
    n=int(f[:2]); p=os.path.join(D,f)
    ls=lines_outside_fences(p)
    text="\n".join(l for _,l in ls)
    # part header on line 1?
    first=[l for _,l in ls if l.strip()][:3]
    if not first or not re.match(r"^#\s+Part\s+[IVX]+\s*[—–-]", first[0]):
        issues[f].append(f"no Part header as first line: {first[:1]}")
    m=re.search(r"^#\s+Chapter\s+(\d+)\s*[—–-]\s*(.+)$", text, re.M)
    if not m: issues[f].append("no '# Chapter N — Title'")
    else:
        if int(m.group(1))!=n: issues[f].append(f"chapter number {m.group(1)} != file {n}")
        titles[n]=m.group(2).strip()
    # prerequisites blockquote
    if not re.search(r"^>\s*\*\*Prerequisites", text, re.M|re.I) and not re.search(r"^>.*Prerequisites", text, re.M):
        issues[f].append("no Prerequisites blockquote")
    if not re.search(r"Leads into|Leads-into", text):
        issues[f].append("no Leads-into")
    # sections
    secs=re.findall(r"^##\s+(\d+)\.(\d+)\s+(.+)$", text, re.M)
    subs=re.findall(r"^###\s+(\d+)\.(\d+)\.(\d+)\s+(.+)$", text, re.M)
    for c,s,t in secs:
        if int(c)!=n: issues[f].append(f"section {c}.{s} wrong chapter number")
    nums=[int(s) for c,s,t in secs]
    if nums!=list(range(1,len(nums)+1)): issues[f].append(f"section numbering gap: {nums}")
    for c,s,ss,t in subs:
        if int(c)!=n: issues[f].append(f"subsection {c}.{s}.{ss} wrong chapter number")
    sections[n]=[(int(s),t) for c,s,t in secs]
    # expected section names
    want=["Why it matters","Mental model","Measure it","Techniques","Pitfalls","Exercises","References","Additional Reading"]
    sec_titles=" | ".join(t for _,_,t in secs)
    for w in want:
        if w.lower() not in sec_titles.lower(): issues[f].append(f"missing section containing '{w}'")
    # teaser
    tail=[l for _,l in ls if l.strip()][-1] if ls else ""
    mt=re.search(r"\*Next: (?:Ch\.|Chapter|Appendix)\s*(\d+|[A-Z])\b", tail)
    if not mt: issues[f].append(f"no Next teaser at end: {tail[:80]!r}")
    else:
        nxt=mt.group(1)
        if nxt.isdigit() and int(nxt)!=n+1 and n!=76: issues[f].append(f"teaser points to {nxt}, expected {n+1}")
        if n==76 and nxt!="A": issues[f].append(f"ch76 teaser -> {nxt}")
    # cross refs out of range
    for r in re.findall(r"\bCh(?:\.|apter)\s*(\d+)", text):
        if int(r)<1 or int(r)>76: issues[f].append(f"cross-ref Ch. {r} out of range")
    for r in re.findall(r"§\s*(\d+)\.", text):
        if int(r)<1 or int(r)>76: issues[f].append(f"cross-ref §{r} out of range")
    for r in re.findall(r"Appendix\s+([A-Z])\b", text):
        if r>"L": issues[f].append(f"Appendix {r} out of range")
    # self-reference check: "Ch. N" where N == own number used as if other
    # hard-wrapped lines? (lines of prose < 100 chars ending mid-sentence) skip
    # reference-machine
    if 7<=n<=15 and "6326" not in text: issues[f].append("Part II chapter without Xeon Gold 6326 mention")
    if n==16 and "dual-socket" not in text.lower() and "2×" not in text and "2x" not in text.lower(): issues[f].append("NUMA chapter without dual-socket mention")

for f in apps:
    p=os.path.join(D,f); ls=lines_outside_fences(p); text="\n".join(l for _,l in ls)
    L=f.split("-")[1]
    m=re.search(r"^#\s+Appendix\s+([A-Z])\s*[—–-]\s*(.+)$", text, re.M)
    if not m: issues[f].append("no '# Appendix X — Title'")
    elif m.group(1)!=L: issues[f].append(f"appendix letter {m.group(1)} != file {L}")
    secs=re.findall(r"^##\s+([A-Z])\.(\d+)\s+(.+)$", text, re.M)
    for c,s,t in secs:
        if c!=L: issues[f].append(f"section {c}.{s} wrong letter")
    nums=[int(s) for c,s,t in secs]
    if nums!=list(range(1,len(nums)+1)): issues[f].append(f"section numbering gap: {nums}")
    for r in re.findall(r"\bCh(?:\.|apter)\s*(\d+)", text):
        if int(r)<1 or int(r)>76: issues[f].append(f"cross-ref Ch. {r} out of range")
    tail=[l for _,l in ls if l.strip()][-1] if ls else ""
    if "Next:" not in tail and L!="L": issues[f].append(f"no Next teaser: {tail[:80]!r}")
    sections[L]=[(int(s),t) for c,s,t in secs]
    titles[L]=m.group(2).strip() if m else None

# README checks
R=open(os.path.join(D,"README.md"),encoding="utf-8").read()
links=re.findall(r"\]\(([^)]+\.md)\)", R)
for l in links:
    if not os.path.exists(os.path.join(D,l)): issues["README.md"].append(f"broken link {l}")
for f in chap+apps:
    if f not in links: issues["README.md"].append(f"file not linked: {f}")
# README titles vs chapter titles
for n,t in titles.items():
    if isinstance(n,int):
        m=re.search(r"^### \[%d\. (.+?)\]\(" % n, R, re.M)
        if not m: issues["README.md"].append(f"no README entry for ch {n}")
        elif m.group(1).strip()!=t: issues["README.md"].append(f"title mismatch ch {n}: README={m.group(1)!r} chapter={t!r}")
    else:
        m=re.search(r"^### \[Appendix %s — (.+?)\]\(" % n, R, re.M)
        if not m: issues["README.md"].append(f"no README entry for appendix {n}")
        elif t and m.group(1).strip()!=t: issues["README.md"].append(f"title mismatch app {n}: README={m.group(1)!r} chapter={t!r}")
# README section outline vs chapter sections (N.k lines)
for n,secs in sections.items():
    if not isinstance(n,int): continue
    # README bullets like "- N.k Title: ..." 
    rsec=re.findall(r"^- %d\.(\d+) ([^:\n]+)" % n, R, re.M)
    rmap={int(k):t.strip() for k,t in rsec}
    for k,t in secs:
        if k in rmap:
            # compare on leading words
            a=re.sub(r"[`*]","",rmap[k]).lower(); b=re.sub(r"[`*]","",t).lower()
            if not (a.startswith(b[:12]) or b.startswith(a[:12])):
                issues["README-outline"].append(f"ch {n} §{n}.{k}: README={rmap[k]!r} vs chapter={t!r}")
        else:
            if k<=7: issues["README-outline"].append(f"ch {n} §{n}.{k} {t!r} not in README outline")
    # subsections 2.x and 4.x
    text="\n".join(l for _,l in lines_outside_fences(os.path.join(D,[f for f in chap if int(f[:2])==n][0])))
    subs=re.findall(r"^###\s+%d\.(\d+)\.(\d+)\s+(.+)$" % n, text, re.M)
    rsubs=re.findall(r"^  - %d\.(\d+)\.(\d+) (.+)$" % n, R, re.M)
    rs={(int(a),int(b)):t for a,b,t in rsubs}
    cs={(int(a),int(b)):t for a,b,t in subs}
    for k in cs:
        if k[0] in (2,4) and k not in rs: issues["README-outline"].append(f"ch {n} §{n}.{k[0]}.{k[1]} {cs[k]!r} missing from README")
    for k in rs:
        if k not in cs: issues["README-outline"].append(f"ch {n} README has {n}.{k[0]}.{k[1]} {rs[k]!r} not in chapter")

for f in sorted(issues):
    print(f"== {f}")
    for i in issues[f]: print("   -", i)
print("\nchapters:",len(chap),"appendices:",len(apps))
