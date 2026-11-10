#!/usr/bin/env python3
"""check_refs.py — deterministic cross-reference validator for the book.

THE PRIMARY REFERENCE CHECK. It parses every chapter and appendix for its real headings
(`# Chapter N — Title`, `## N.k`, `### N.k.j`, `# Appendix X — Title`, `## X.n`) and the README for
the Part → chapter ranges, then validates every reference it can find in

    chapters/*.md (chapters, appendices, the preface, README.md), CLAUDE.md, tools/book-pdf/README.md

against that index. Nothing here is a heuristic about *meaning*: a finding says the target does not
exist (or the reference contradicts itself). Whether an existing target is the *right* one is the job
of tools/review/check_hints.py.

Exit code
    0   no ERROR findings (WARN findings may have been printed; `--strict` promotes them)
    1   at least one ERROR finding (prose or fenced)
    2   the book's structure could not be indexed (missing chapters dir, a chapter file without a
        `# Chapter N — Title` heading, …) or bad usage

Reference forms understood
    Ch. N · Chapter N · Chs. N · Chapters N           (N must be 1..MAX)
    lists and ranges:  Ch. 3, 5 and 7 · Ch. 8–12 · Ch. 41/42 · Ch. 3, Ch. 5
    §N.k · §N.k.j · §N.k.j–§N.k.m · §N.k.j–m · §N.k/§N.m    (the section heading must exist)
    Ch. N §N.k[.j]  and mixed lists  Ch. 42 §42.5, 43 §43.5  (the § prefix must equal N)
    Ch. N.k                                           (the retired spelling of §N.k: an ERROR)
    Appendix X · Appendices C, E and I · Appendix A–G · Appendix X.n · Appendix X §X.n · §X.n
    Part IV · Parts II–IV · "Part IX (Ch. 70–80)" / "**IX — Title** (70–80)" (range must equal README's)
    [text](NN-slug.md) links and bare `NN-slug.md` / `appendix-X-slug.md` file names

Findings (ERROR unless marked WARN)
    ch-missing         Ch. N where no chapter N exists
    sec-missing        §N.k / §N.k.j where chapter N has no such heading (or §X.n in an appendix)
    ch-sec-mismatch    Ch. N §M.k with N != M
    ch-dot-sec         "Ch. N.k" — write "§N.k" (or "Ch. N §N.k")
    range-order        Ch. a–b or §a–§b with a >= b
    app-missing        Appendix X beyond the last appendix on disk
    part-missing       Part numeral that is not one of the README's Parts
    part-range         a "Part X (a–b)" range that disagrees with the README
    link-missing       a (file.md) link or file name that does not exist
    ext-citation       a chapter/section of SOMEBODY ELSE'S book written "Ch. 9" / "§7.17" (after a title in a
                       References section, or after "Vol. 3A", "2nd ed.", "manual", "standard"): the insertion
                       tool renumbers those (it did, thirteen times, before this check existed) — write
                       "ch. 9" / "sec. 7.17"
    list-suspect WARN  a bare number continuing a chapter list that is out of range (probably not a
                       chapter number at all — read the line)
    list-implicit WARN a bare number continuing a chapter list after a parenthesis
                       ("Ch. 42 (threading), 43 (spinlocks)"): repeat the "Ch." — the insertion tool stops
                       at the parenthesis, so the bare number goes stale at the next insertion
References inside fenced code blocks are validated the same way but reported in their own section
(`--list-fenced` prints every fenced reference, valid or not, for a by-eye review).

Deliberately exempt
    * CLAUDE.md's historical `## Table of Contents` block and its dated insertion records
      (`- **2026-…` bullets under Status / Workflow): they cite the numbering of their day.
    * the review/ directory (never scanned).
    * §-numbers that belong to an external document (a standard, an RFC, a manual, a regulation):
      a `§` immediately preceded by one of EXTERNAL_BEFORE, or a line matched by EXTERNAL_LINES.
    * the README outline's own definition bullets (`- N.k Title`) — they are checked by sweep.py.

usage: check_refs.py [--root DIR] [--strict] [--list-fenced] [--dump-refs] [--quiet] [--self-test]
"""
import argparse
import collections
import os
import re
import sys

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV",
         "XVI", "XVII", "XVIII", "XIX", "XX"]
DASH = "–"

# A `§` preceded (within the same line, up to 60 characters back) by one of these belongs to an
# external document, not to the book.
EXTERNAL_BEFORE = re.compile(
    r"(?:SDM|ORM|RFC\s?\d*|ISO|IEEE|IEC|\bstandard|\bStandard|\[[a-z.]+\]|N\d{4}|P\d{4}R?\d*|Vol(?:ume|\.)\s?\d\w*|"
    r"\bspec(?:ification)?s?|\bSpec|\bmanual|\bManual|ABI|psABI|\bAPM|\bPPR|Rule|\bRTS\s?\d+|MiFID|Reg(?:ulation)?\s\w+|"
    r"\bSEC\b|CFR|15c3-5|draft|working paper|\bman\b|Handbook|Guide|TS\s?\d+|802\.\d\w*|1588\w*|G\.\d+|"
    r"JEP\s?\d+|PEP\s?\d+|Fog|Drepper|Hennessy|Patterson|Williams|McKenney|Knuth|Intel|AMD|Arm|ARM|"
    r"\bbook\b|\bpaper\b|et al\.|[’'\"”]\s?|\*,?\s?)[^§]{0,60}$")
EXTERNAL_LINES = []  # (file-suffix, compiled regex) pairs for one-off exemptions; keep empty if possible


class Index:
    """What actually exists on disk."""

    def __init__(self, root):
        self.root = root
        self.cdir = os.path.join(root, "chapters")
        self.chapters = {}   # n -> dict(file, title, part, part_title, secs{k:title}, subs{(k,j):title})
        self.appendices = {}  # L -> dict(file, title, secs{k:title}, subs{(k,j):title})
        self.parts = {}      # roman -> dict(title, lo, hi)
        self.problems = []
        self._build()

    def _build(self):
        if not os.path.isdir(self.cdir):
            self.problems.append(f"no chapters directory at {self.cdir}")
            return
        for f in sorted(os.listdir(self.cdir)):
            p = os.path.join(self.cdir, f)
            m = re.match(r"^(\d{2,3})-.*\.md$", f)
            a = re.match(r"^appendix-([A-Z])-.*\.md$", f)
            if m and int(m.group(1)) > 0:
                n = int(m.group(1))
                e = dict(file=f, title=None, part=None, part_title=None, secs={}, subs={})
                for _, l, inf in iter_lines(p):
                    if inf:
                        continue
                    h = re.match(r"^#\s+Part\s+([IVX]+)\s*[—–-]\s*(.+?)\s*$", l)
                    if h and e["part"] is None:
                        e["part"], e["part_title"] = h.group(1), h.group(2)
                    h = re.match(r"^#\s+Chapter\s+(\d+)\s*[—–-]\s*(.+?)\s*$", l)
                    if h:
                        if int(h.group(1)) != n:
                            self.problems.append(f"{f}: heading says Chapter {h.group(1)}, file name says {n}")
                        e["title"] = h.group(2)
                    h = re.match(r"^##\s+(\d+)\.(\d+)\s+(.+?)\s*$", l)
                    if h and int(h.group(1)) == n:
                        e["secs"][int(h.group(2))] = h.group(3)
                    h = re.match(r"^###\s+(\d+)\.(\d+)\.(\d+)\s+(.+?)\s*$", l)
                    if h and int(h.group(1)) == n:
                        e["subs"][(int(h.group(2)), int(h.group(3)))] = h.group(4)
                if e["title"] is None:
                    self.problems.append(f"{f}: no '# Chapter N — Title' heading")
                if n in self.chapters:
                    self.problems.append(f"two files for chapter {n}: {self.chapters[n]['file']} and {f}")
                self.chapters[n] = e
            elif a:
                L = a.group(1)
                e = dict(file=f, title=None, secs={}, subs={})
                for _, l, inf in iter_lines(p):
                    if inf:
                        continue
                    h = re.match(r"^#\s+Appendix\s+([A-Z])\s*[—–-]\s*(.+?)\s*$", l)
                    if h:
                        if h.group(1) != L:
                            self.problems.append(f"{f}: heading says Appendix {h.group(1)}, file name says {L}")
                        e["title"] = h.group(2)
                    h = re.match(r"^##\s+([A-Z])\.(\d+)\s+(.+?)\s*$", l)
                    if h and h.group(1) == L:
                        e["secs"][int(h.group(2))] = h.group(3)
                    h = re.match(r"^###\s+([A-Z])\.(\d+)\.(\d+)\s+(.+?)\s*$", l)
                    if h and h.group(1) == L:
                        e["subs"][(int(h.group(2)), int(h.group(3)))] = h.group(4)
                if e["title"] is None:
                    self.problems.append(f"{f}: no '# Appendix X — Title' heading")
                self.appendices[L] = e
        if not self.chapters:
            self.problems.append("no chapter files found")
            return
        self.maxch = max(self.chapters)
        missing = [n for n in range(1, self.maxch + 1) if n not in self.chapters]
        if missing:
            self.problems.append(f"chapter numbers missing on disk: {missing}")
        self.lastapp = max(self.appendices) if self.appendices else "@"
        # README: Part -> range, from the `## Part X — Title` headers and the `### [N. Title](file)` entries
        rp = os.path.join(self.cdir, "README.md")
        cur = None
        if os.path.exists(rp):
            for _, l, inf in iter_lines(rp):
                if inf:
                    continue
                h = re.match(r"^##\s+Part\s+([IVX]+)\s*[—–-]\s*(.+?)\s*$", l)
                if h:
                    cur = h.group(1)
                    self.parts[cur] = dict(title=h.group(2), lo=None, hi=None)
                    continue
                if re.match(r"^##\s", l):
                    cur = None
                h = re.match(r"^###\s+\[(\d+)\.\s", l)
                if h and cur:
                    n = int(h.group(1))
                    P = self.parts[cur]
                    P["lo"] = n if P["lo"] is None else min(P["lo"], n)
                    P["hi"] = n if P["hi"] is None else max(P["hi"], n)
        else:
            self.problems.append("chapters/README.md not found")
        if not self.parts:
            self.problems.append("README.md has no '## Part X — Title' headers")

    # --- existence queries -------------------------------------------------
    def has_ch(self, n):
        return n in self.chapters

    def has_sec(self, n, k, j=None):
        c = self.chapters.get(n)
        if not c:
            return False
        if j is None:
            return k in c["secs"]
        return (k, j) in c["subs"]

    def has_appsec(self, L, k, j=None):
        a = self.appendices.get(L)
        if not a:
            return False
        if j is None:
            return k in a["secs"]
        return (k, j) in a["subs"]

    def sec_title(self, n, k, j=None):
        c = self.chapters.get(n) or {}
        if j is None:
            return c.get("secs", {}).get(k)
        return c.get("subs", {}).get((k, j))


def iter_lines(path):
    """Yield (lineno, line, in_fence). Fence delimiter lines themselves are yielded with in_fence=True."""
    inf = False
    with open(path, encoding="utf-8") as fh:
        for i, l in enumerate(fh, 1):
            l = l.rstrip("\n")
            if FENCE.match(l):
                inf = not inf
                yield i, l, True
                continue
            yield i, l, inf


def claude_exempt_lines(path):
    """Line numbers of CLAUDE.md that are historical by design: the `## Table of Contents` block and
    the dated insertion records under Status / Workflow."""
    ex = set()
    in_toc = False
    in_rec = False
    with open(path, encoding="utf-8") as fh:
        for i, l in enumerate(fh, 1):
            if l.startswith("## "):
                in_toc = l.startswith("## Table of Contents")
                in_rec = False
            if in_toc:
                ex.add(i)
                continue
            if re.match(r"^- \*\*20\d\d-\d\d-\d\d ", l):
                in_rec = True
            elif re.match(r"^(- |#)", l):
                in_rec = False
            if in_rec:
                ex.add(i)
    return ex


Ref = collections.namedtuple("Ref", "kind a b c start end text flags")
# kind: 'ch' (a=n) · 'sec' (a=n, b=k, c=j or None) · 'app' (a=L) · 'appsec' (a=L, b=k, c=j) ·
#       'part' (a=roman) · 'partrange' (a=roman, b=lo, c=hi) · 'link' (a=target) · 'chrange' (a=lo, b=hi) ·
#       'secrange' (a=(n,k,j), b=(n,k,j)) · 'chdot' (a=n, b=k, c=j)
# flags: set of strings: 'mismatch:<N>' (a § directly attached to Ch. N), 'weak' (uncertain list item),
#        'cont' (list continuation), 'ctxch:<N>' (the chapter a § was attached to)

CH_KW = re.compile(r"(?<![A-Za-z])(Chs?\.|Chapters?)(?:\s| |&nbsp;)?(?=\d)")
NUM = re.compile(r"(\d{1,3})(?!\d)")
SEC = re.compile(r"§\s?(\d{1,3})\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)")
SEC_RANGE_FULL = re.compile(r"\s?[–-]\s?§?\s?(\d{1,3})\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)")
SEC_RANGE_SHORT = re.compile(r"[–-](\d{1,2})(?![\d.]|\s?(?:ns|µs|us|ms|%|×|x\b|bit|byte|KB|MB|GB))")
APPSEC = re.compile(r"§\s?([A-Z])\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)")
APP_KW = re.compile(r"(?<![A-Za-z])Appendi(x|ces)\s+(?=[A-Z](?![A-Za-z]))")
PART_KW = re.compile(r"(?<![A-Za-z])Parts?\s+(?=[IVX]+(?![A-Za-z]))")
ROMAN_TOK = re.compile(r"([IVX]+)(?![A-Za-z])")
LINK = re.compile(r"\]\(([^)\s#]+\.md)(#[^)\s]*)?\)")
BAREFILE = re.compile(r"(?<![\w/(-])((?:chapters/)?(?:\d{2,3}|appendix-[A-Z])-[a-z0-9][a-z0-9-]*\.md)\b")
APPSEC_CONT = re.compile(r"(–|/|,\s(?:and\s|or\s)?|\s(?:and|or|&)\s)§?([A-Z])\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)")
LIST_SEP = re.compile(r"(,\s(?:and\s|or\s)?|\s(?:and|or|&)\s|\s?/\s?)")
RANGE_SEP = re.compile(r"[–-](?=\d)")
# what may follow a list-continuation number for it to be accepted as a chapter number outright
STRONG_AFTER = re.compile(r"(?:$|[)\];:,*|'’\"”}]|\.(?:$|[\s)*'’\"”])|\s?§|[–/]|\s(?:and|or|&)\s|\s[—|(])")
IMPLICIT_ITEM = re.compile(r"(?:\s?§[\d.]+)?\s\([^()]{1,200}\)(?:,\s(?:and\s)?|\s(?:and|&)\s)(\d{1,3})(?=\s\(|[),;|]|\s\||,\s\d|–\d)")
EXT_STRONG = r"\d(?:st|nd|rd|th)\sed\.\)?|Vol\.\s?\d+[A-Z]?|[Mm]anual|(?:C\d\d|C\+\+\d\d|ISO\s[^,;]{0,20})\sstandard|Supplement|Handbook"
EXT_CITE = re.compile(r"((?<![*:])\*|\*\*|" + EXT_STRONG + r")[,;:]?\s((?:Chs?\.|Chapters?)\s?\d+(?:[–-]\d+)?|§\s?\d+\.\d+(?:\.\d+)*)")
# "… Wiley 1991 — Ch. 2", "2nd ed. — Ch. 13", and a chapter number followed by a quoted chapter title: Ch. 2 "Methodologies"
EXT_CITE_STRONG = re.compile(r"(?:(?:(?:19|20)\d{2}|\d(?:st|nd|rd|th)\sed\.|Press|Wiley|Reilly|Wesley|Kaufmann|Springer|Pearson)\)?,?\s(?:[—–-]\s)?(?=Ch))"
                             r"((?:Chs?\.|Chapters?)\s\d+(?:[–-]\d+)?)")
EXT_CITE_QUOTED = re.compile(r"((?:Chs?\.|Chapters?)\s\d+(?:[–-]\d+)?)(?=\s[\"“])")   # only meaningful in a References section
PART_RANGE_PAREN = re.compile(r"\*{0,2}\s\((?:Ch\.\s|Chapters\s)?(\d{1,3})\s?[–-]\s?(\d{1,3})\)")


def _parse_secs(line, pos, refs, attached_to, taken):
    """At `pos` there may be ' §N.k[.j]' (+ ranges/lists). Append refs; return new pos."""
    m = re.compile(r"\s?(?=§)").match(line, pos)
    q = m.end() if m else pos
    first = True
    while True:
        s = SEC.match(line, q)
        if not s:
            return pos
        n, k, j = int(s.group(1)), int(s.group(2)), int(s.group(3)) if s.group(3) else None
        fl = set()
        if attached_to is not None:
            fl.add(f"ctxch:{attached_to}")
            if first and n != attached_to:
                fl.add(f"mismatch:{attached_to}")
        refs.append(Ref("sec", n, k, j, s.start(), s.end(), s.group(0), frozenset(fl)))
        taken.append((s.start(), s.end()))
        pos = s.end()
        first = False
        # range tail
        r = SEC_RANGE_FULL.match(line, pos)
        if r:
            n2, k2, j2 = int(r.group(1)), int(r.group(2)), int(r.group(3)) if r.group(3) else None
            refs.append(Ref("sec", n2, k2, j2, r.start(), r.end(), r.group(0).strip(), frozenset(fl | {"cont"})))
            refs.append(Ref("secrange", (n, k, j), (n2, k2, j2), None, s.start(), r.end(), line[s.start():r.end()], frozenset()))
            taken.append((r.start(), r.end()))
            pos = r.end()
        else:
            r = SEC_RANGE_SHORT.match(line, pos)
            if r:
                e = int(r.group(1))
                n2, k2, j2 = (n, k, e) if j is not None else (n, e, None)
                refs.append(Ref("sec", n2, k2, j2, r.start(), r.end(), r.group(0), frozenset(fl | {"cont"})))
                refs.append(Ref("secrange", (n, k, j), (n2, k2, j2), None, s.start(), r.end(), line[s.start():r.end()], frozenset()))
                taken.append((r.start(), r.end()))
                pos = r.end()
        mk = re.compile(r"\s◆").match(line, pos)      # Appendix O's "added by the design-pattern chapters" marker
        if mk:
            pos = mk.end()
        # list of further § directly after a separator
        m2 = re.compile(r"(?:,\s(?:and\s)?|\s(?:and|or|&)\s|/)(?=§)").match(line, pos)
        if not m2:
            return pos
        q = m2.end()


def scan_line(line):
    """Return every reference on the line (order of appearance)."""
    refs = []
    taken = []   # spans already consumed by a structured parse
    # --- chapter references with their attached sections and list continuations
    for kw in CH_KW.finditer(line):
        pos = kw.end()
        if any(a <= kw.start() < b for a, b in taken):
            continue
        m = NUM.match(line, pos)
        if not m:
            continue
        n = int(m.group(1))
        # the retired "Ch. N.k[.j]" spelling
        d = re.compile(r"\.(\d{1,2})(?:\.(\d{1,2}))?(?![\d])").match(line, m.end())
        if d and not re.match(r"\s?(?:ns|µs|us|ms|%|×|GHz|GB|MB|KB)", line[d.end():]):
            refs.append(Ref("chdot", n, int(d.group(1)), int(d.group(2)) if d.group(2) else None,
                            kw.start(), d.end(), line[kw.start():d.end()], frozenset()))
            taken.append((kw.start(), d.end()))
            continue
        refs.append(Ref("ch", n, None, None, kw.start(), m.end(), line[kw.start():m.end()], frozenset()))
        taken.append((kw.start(), m.end()))
        pos = _parse_secs(line, m.end(), refs, n, taken)
        prev = n
        while True:
            r = RANGE_SEP.match(line, pos)
            if r:
                m2 = NUM.match(line, r.end())
                if not m2 or re.match(r"\.\d", line[m2.end():]):
                    break
                hi = int(m2.group(1))
                refs.append(Ref("ch", hi, None, None, m2.start(), m2.end(), m2.group(0), frozenset({"cont"})))
                refs.append(Ref("chrange", prev, hi, None, kw.start(), m2.end(), line[kw.start():m2.end()], frozenset()))
                taken.append((r.start(), m2.end()))
                pos = _parse_secs(line, m2.end(), refs, hi, taken)
                prev = hi
                continue
            sp = LIST_SEP.match(line, pos)
            if not sp:
                break
            q = sp.end()
            k2 = CH_KW.match(line, q)
            if k2:
                break  # an explicit new "Ch." — the outer loop picks it up
            m2 = NUM.match(line, q)
            if not m2:
                break
            after = line[m2.end():]
            d2 = re.match(r"\.(\d{1,2})(?:\.(\d{1,2}))?(?![\d])(?!\s?(?:ns|µs|us|ms|%|×|GHz|GB|MB|KB))", after)
            if d2 and int(m2.group(1)) >= 10:      # "Ch. 6, 101.4.4" — the retired spelling inside a list
                refs.append(Ref("chdot", int(m2.group(1)), int(d2.group(1)), int(d2.group(2)) if d2.group(2) else None,
                                m2.start(), m2.end() + d2.end(), line[m2.start():m2.end() + d2.end()], frozenset({"cont"})))
                taken.append((m2.start(), m2.end() + d2.end()))
                break
            if re.match(r"\.\d", after) or re.match(r"\s?(?:ns|µs|us|ms|%|×|GHz|GB|MB|KB|bytes?|bits?|cycles?|cores?)\b", after):
                break
            strong = bool(STRONG_AFTER.match(after))
            if not strong and not re.match(r"\s", after):
                break
            hi = int(m2.group(1))
            fl = {"cont"} | (set() if strong else {"weak"})
            refs.append(Ref("ch", hi, None, None, m2.start(), m2.end(), m2.group(0), frozenset(fl)))
            taken.append((m2.start(), m2.end()))
            pos = _parse_secs(line, m2.end(), refs, hi, taken)
            prev = hi
    # --- a bare number continuing a chapter list AFTER a parenthesis: "Ch. 42 (threading), 43 (spinlocks)"
    for r0 in [r for r in refs if r.kind == "ch"]:
        pm = IMPLICIT_ITEM.match(line, r0.end)
        if pm and not any(a <= pm.start(1) < b for a, b in taken):
            refs.append(Ref("implicit", int(pm.group(1)), None, None, pm.start(1), pm.end(1), pm.group(1), frozenset()))
    # --- a citation of somebody else's book spelled like one of ours: "*Title*, Ch. 9", "SDM Vol. 3A §8.10.1"
    for xm in EXT_CITE.finditer(line):
        if not xm.group(1).startswith("**"):
            refs.append(Ref("extcite", xm.group(2), None, None, xm.start(2), xm.end(2), xm.group(2), frozenset()))
    for rx, fl in ((EXT_CITE_STRONG, "strong"), (EXT_CITE_QUOTED, "quoted")):
        for xm in rx.finditer(line):
            if not any(r.kind == "extcite" and r.start == xm.start(1) for r in refs):
                refs.append(Ref("extcite", xm.group(1), None, None, xm.start(1), xm.end(1), xm.group(1), frozenset({fl})))
    # --- standalone § references
    for s in SEC.finditer(line):
        if any(a <= s.start() < b for a, b in taken):
            continue
        _parse_secs(line, s.start(), refs, None, taken)
    # --- appendix references
    for kw in APP_KW.finditer(line):
        pos = kw.end()
        first = True
        while True:
            L = line[pos]
            if not ("A" <= L <= "Z") or re.match(r"[A-Za-z]", line[pos + 1:pos + 2] or ""):
                break
            end = pos + 1
            refs.append(Ref("app", L, None, None, kw.start() if first else pos, end, line[(kw.start() if first else pos):end],
                            frozenset() if first else frozenset({"cont"})))
            first = False
            d = re.compile(r"\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)").match(line, end)
            if d:
                refs.append(Ref("appsec", L, int(d.group(1)), int(d.group(2)) if d.group(2) else None, pos, d.end(),
                                line[pos:d.end()], frozenset()))
                taken.append((pos, d.end()))
                end = d.end()
            else:
                d = re.compile(r",?\s§\s?([A-Z])\.(\d{1,2})(?:\.(\d{1,2}))?(?!\d)").match(line, end)
                if d:
                    fl = frozenset() if d.group(1) == L else frozenset({f"mismatch:{L}"})
                    refs.append(Ref("appsec", d.group(1), int(d.group(2)), int(d.group(3)) if d.group(3) else None,
                                    d.start(), d.end(), d.group(0).strip(), fl))
                    taken.append((d.start(), d.end()))
                    end = d.end()
            # section lists/ranges on the same appendix or another:  "C.1–C.3 and C.5" · "M.2/M.6" · "H.2–3"
            while end > pos + 1:
                d = APPSEC_CONT.match(line, end)
                if d:
                    L2 = d.group(2)
                    refs.append(Ref("app", L2, None, None, d.start(2), d.start(2) + 1, L2, frozenset({"cont"})))
                    refs.append(Ref("appsec", L2, int(d.group(3)), int(d.group(4)) if d.group(4) else None,
                                    d.start(2), d.end(), line[d.start(2):d.end()], frozenset({"cont"})))
                    taken.append((d.start(), d.end()))
                    end = d.end()
                    continue
                d = re.compile(r"–(\d{1,2})(?![\d.])").match(line, end)
                if d and "." in line[pos:end]:
                    prev = [r for r in refs if r.kind == "appsec"][-1]
                    k2, j2 = (prev.b, int(d.group(1))) if prev.c is not None else (int(d.group(1)), None)
                    refs.append(Ref("appsec", prev.a, k2, j2, d.start(), d.end(), d.group(0), frozenset({"cont"})))
                    end = d.end()
                    continue
                break
            # continuation:  ", C.6" · " and I" · "/I" · "–G"
            c = re.compile(r"(,\s(?:and\s|or\s)?|\s(?:and|or|&)\s|/|–)(?=[A-Z](?:\.\d|[)\],;:*]|\.(?:\s|$)|\s(?:and|or|&)\s|[/–]|$))").match(line, end)
            if not c:
                break
            if c.group(1) == "–":
                refs.append(Ref("apprange", L, line[c.end()], None, pos, c.end() + 1, line[pos:c.end() + 1], frozenset()))
            pos = c.end()
    for s in APPSEC.finditer(line):
        if any(a <= s.start() < b for a, b in taken):
            continue
        refs.append(Ref("appsec", s.group(1), int(s.group(2)), int(s.group(3)) if s.group(3) else None,
                        s.start(), s.end(), s.group(0), frozenset({"bare"})))
    # --- Part references
    for kw in PART_KW.finditer(line):
        pos = kw.end()
        while True:
            t = ROMAN_TOK.match(line, pos)
            if not t:
                break
            refs.append(Ref("part", t.group(1), None, None, kw.start(), t.end(), line[kw.start():t.end()], frozenset()))
            pr = re.compile(r"(?:\s[—–-]\s[^()|]{1,90}?)?" + PART_RANGE_PAREN.pattern).match(line, t.end())
            if pr:
                refs.append(Ref("partrange", t.group(1), int(pr.group(1)), int(pr.group(2)), kw.start(), pr.end(),
                                line[kw.start():pr.end()], frozenset()))
            c = re.compile(r"(,\s(?:and\s)?|\s(?:and|or|&)\s|[–/])(?=[IVX]+(?![A-Za-z]))").match(line, t.end())
            if not c:
                break
            pos = c.end()
    # `- **IX — Title** (70–80)` — the Part-range lines of CLAUDE.md's Phase-3 block and the preface
    m = re.match(r"^\s*-\s+\*\*(?:Part\s+)?([IVX]+)\s+[—–-]\s+[^*]+\*\*\s+\((?:Ch\.\s|Chapters\s)?(\d{1,3})\s?[–-]\s?(\d{1,3})[,)]", line)
    if m and not any(r.kind == "partrange" for r in refs):
        refs.append(Ref("partrange", m.group(1), int(m.group(2)), int(m.group(3)), m.start(1), m.end(), m.group(0).strip(), frozenset()))
    # --- links and bare file names
    for l in LINK.finditer(line):
        refs.append(Ref("link", l.group(1), None, None, l.start(), l.end(), l.group(0), frozenset()))
        taken.append((l.start(), l.end()))
    for b in BAREFILE.finditer(line):
        if any(a <= b.start() < e for a, e in taken):
            continue
        refs.append(Ref("link", b.group(1), None, None, b.start(), b.end(), b.group(0), frozenset({"bare"})))
    refs.sort(key=lambda r: (r.start, r.end))
    return refs


def scan_files(root):
    """The files in scope: (label, path, exempt-line-set)."""
    out = []
    cdir = os.path.join(root, "chapters")
    for f in sorted(os.listdir(cdir)):
        if f.endswith(".md"):
            out.append((f"chapters/{f}", os.path.join(cdir, f), set()))
    c = os.path.join(root, "CLAUDE.md")
    if os.path.exists(c):
        out.append(("CLAUDE.md", c, claude_exempt_lines(c)))
    for extra in ("tools/book-pdf/README.md",):
        p = os.path.join(root, extra)
        if os.path.exists(p):
            out.append((extra, p, set()))
    return out


def iter_refs(root):
    """Yield (label, path, lineno, line, in_fence, Ref) for every in-scope reference."""
    for label, path, exempt in scan_files(root):
        for i, line, inf in iter_lines(path):
            if i in exempt:
                continue
            for r in scan_line(line):
                yield label, path, i, line, inf, r


def own_id(label):
    """Chapter number or appendix letter of a chapters/ file, else None."""
    b = os.path.basename(label)
    m = re.match(r"^(\d{2,3})-", b)
    if m and int(m.group(1)) > 0:
        return int(m.group(1))
    m = re.match(r"^appendix-([A-Z])-", b)
    return m.group(1) if m else None


def validate(ix, root):
    findings = []   # (severity, cls, label, lineno, in_fence, message, context)
    counts = collections.Counter()
    fenced_all = []

    def add(sev, cls, label, i, inf, msg, line, r):
        ctx = line[max(0, r.start - 70):r.end + 70].strip()
        findings.append((sev, cls, label, i, inf, msg, ctx))

    refs_from = {}   # label -> first line of its References / Additional Reading / bibliography part
    for label, path, exempt in scan_files(root):
        for i, line, inf in iter_lines(path):
            if not inf and re.match(r"^## (?:\d+|[A-Z])\.\d+ (?:References|Additional Reading)", line):
                refs_from[label] = i
                break
        if label.endswith("appendix-G-annotated-bibliography.md"):
            refs_from[label] = 1
    for label, path, i, line, inf, r in iter_refs(root):
        counts[r.kind] += 1
        in_refs_section = i >= refs_from.get(label, 10 ** 9)
        if inf:
            fenced_all.append((label, i, r.text, line.strip()[:150]))
        own = own_id(label)
        is_readme_outline = label == "chapters/README.md" and re.match(r"^\s*- (?:\d+|[A-Z])\.\d+(?:\.\d+)? ", line)
        if r.kind == "ch":
            if not ix.has_ch(r.a):
                if "weak" in r.flags:
                    add("WARN", "list-suspect", label, i, inf, f"'{r.text}' continues a chapter list but there is no chapter {r.a}", line, r)
                else:
                    add("ERROR", "ch-missing", label, i, inf, f"Ch. {r.a} does not exist (1..{ix.maxch})", line, r)
        elif r.kind == "chrange":
            if r.a >= r.b:
                add("ERROR", "range-order", label, i, inf, f"range {r.a}–{r.b} is not ascending", line, r)
        elif r.kind == "chdot":
            tgt = f"§{r.a}.{r.b}" + (f".{r.c}" if r.c is not None else "")
            ok = ix.has_sec(r.a, r.b, r.c)
            add("ERROR", "ch-dot-sec", label, i, inf,
                f"'{r.text}' — write '{tgt}'" + ("" if ok else " (and that section does not exist)"), line, r)
        elif r.kind == "implicit":
            add("WARN", "list-implicit", label, i, inf,
                f"bare '{r.a}' continues a chapter list after a parenthesis — write 'Ch. {r.a}' (insert_chapter.py cannot see it)", line, r)
        elif r.kind == "extcite":
            before = line[:r.start]
            strong = re.search(r"(?:" + EXT_STRONG + r")[,;:]?\s$", before)
            num = re.search(r"\d+", r.a).group(0)
            ours = re.search(r"Ch(?:\.|apter)\s?%s\s\([^()]{0,80}$" % num, before)   # "Ch. 6 (*System Setup*, §6.2.3)"
            if "quoted" in r.flags:
                if not in_refs_section:
                    continue
                ours = None
            if strong or "strong" in r.flags or (in_refs_section and not ours):
                add("ERROR", "ext-citation", label, i, inf,
                    f"'{r.a}' cites an external work in the book's own reference spelling — write it in lower case "
                    f"('ch. N', 'sec. N.M') so insert_chapter.py never renumbers it", line, r)
        elif r.kind == "sec":
            if r.start == 0 and is_readme_outline:
                continue
            before = line[:r.start]
            if "ctxch" not in "".join(r.flags) and EXTERNAL_BEFORE.search(before[-60:]) and not ix.has_sec(r.a, r.b, r.c):
                counts["sec-external"] += 1
                continue
            if any(label.endswith(sfx) and rx.search(line) for sfx, rx in EXTERNAL_LINES):
                counts["sec-external"] += 1
                continue
            for fl in r.flags:
                if fl.startswith("mismatch:"):
                    add("ERROR", "ch-sec-mismatch", label, i, inf,
                        f"Ch. {fl.split(':')[1]} is followed by §{r.a}.{r.b}" + (f".{r.c}" if r.c is not None else ""), line, r)
            if not ix.has_ch(r.a):
                add("ERROR", "sec-missing", label, i, inf, f"{r.text.strip()}: there is no chapter {r.a}", line, r)
            elif not ix.has_sec(r.a, r.b, r.c):
                what = f"§{r.a}.{r.b}" + (f".{r.c}" if r.c is not None else "")
                c = ix.chapters[r.a]
                have = (f"chapter {r.a} has §{r.a}.1–§{r.a}.{max(c['secs'])}" if r.c is None else
                        f"§{r.a}.{r.b} has " + (", ".join(f"{r.a}.{k}.{j}" for (k, j) in sorted(c['subs']) if k == r.b) or "no numbered subsections"))
                add("ERROR", "sec-missing", label, i, inf, f"{what} does not exist ({have})", line, r)
        elif r.kind == "secrange":
            (n1, k1, j1), (n2, k2, j2) = r.a, r.b
            if n1 == n2 and (k1, j1 or 0) >= (k2, j2 or 0):
                add("ERROR", "range-order", label, i, inf, f"section range '{r.text}' is not ascending", line, r)
        elif r.kind == "app":
            if r.a not in ix.appendices:
                add("ERROR", "app-missing", label, i, inf, f"Appendix {r.a} does not exist (A..{ix.lastapp})", line, r)
        elif r.kind == "apprange":
            if r.a not in ix.appendices or r.b not in ix.appendices or r.a >= r.b:
                add("ERROR", "app-missing", label, i, inf, f"appendix range {r.a}–{r.b} is not within A..{ix.lastapp}", line, r)
        elif r.kind == "appsec":
            if "bare" in r.flags:
                # a bare §X.n: §N.k / §M.k are also the book's placeholder spelling ("§N.3", "Ch. N §M.x")
                if r.a in ("N", "M", "X", "Y") and own != r.a and not ix.has_appsec(r.a, r.b, r.c):
                    counts["placeholder"] += 1
                    continue
                if r.a not in ix.appendices:
                    if EXTERNAL_BEFORE.search(line[:r.start][-60:]):
                        counts["sec-external"] += 1
                        continue
            for fl in r.flags:
                if fl.startswith("mismatch:"):
                    add("ERROR", "ch-sec-mismatch", label, i, inf, f"Appendix {fl.split(':')[1]} is followed by §{r.a}.{r.b}", line, r)
            if r.a not in ix.appendices:
                add("ERROR", "app-missing", label, i, inf, f"{r.text}: there is no Appendix {r.a}", line, r)
            elif not ix.has_appsec(r.a, r.b, r.c):
                a = ix.appendices[r.a]
                what = f"{r.a}.{r.b}" + (f".{r.c}" if r.c is not None else "")
                add("ERROR", "sec-missing", label, i, inf,
                    f"Appendix section {what} does not exist (Appendix {r.a} has {r.a}.1–{r.a}.{max(a['secs']) if a['secs'] else 0})", line, r)
        elif r.kind == "part":
            if r.a not in ix.parts:
                add("ERROR", "part-missing", label, i, inf, f"Part {r.a} is not one of the README's Parts (I..{ROMAN[len(ix.parts) - 1]})", line, r)
        elif r.kind == "partrange":
            P = ix.parts.get(r.a)
            if P and (P["lo"], P["hi"]) != (r.b, r.c):
                add("ERROR", "part-range", label, i, inf,
                    f"Part {r.a} is given as {r.b}–{r.c}; the README has {P['lo']}–{P['hi']}", line, r)
        elif r.kind == "link":
            t = r.a
            if t.startswith("http") or re.search(r"(?:^|/)(?:NN|MM|appendix-X)-", t):
                continue   # an external URL, or the convention's own placeholder (`NN-slug.md`)
            base = os.path.dirname(path)
            cands = [os.path.join(base, t), os.path.join(root, t), os.path.join(root, "chapters", t)]
            if not any(os.path.exists(c) for c in cands):
                add("ERROR", "link-missing", label, i, inf, f"'{t}' does not exist", line, r)
    return findings, counts, fenced_all


SELF_TEST = [
    # (line placed in chapter 2 of a 3-chapter, 2-appendix, 2-Part toy book, expected finding classes)
    ("See Ch. 1, Ch. 3 §3.2 and §3.4.1; Appendix A.1 and B.", []),
    ("See Ch. 4.", ["ch-missing"]),
    ("See Ch. 1–3, 9).", ["ch-missing"]),
    ("See §3.9 and §3.4.7.", ["sec-missing", "sec-missing"]),
    ("See Ch. 1 §3.2.", ["ch-sec-mismatch"]),
    ("See Ch. 3 §3.2, 1 §3.4.1 ◆, 2 §2.1.", ["ch-sec-mismatch"]),
    ("See Ch. 3.2.", ["ch-dot-sec"]),
    ("See Ch. 1, 12.4.1 and Ch. 2, 3.5 ns.", ["ch-dot-sec"]),
    ("See Ch. 3–1.", ["range-order"]),
    ("See Appendix C and Appendix A.7.", ["app-missing", "sec-missing"]),
    ("See Appendix A.1–A.2, B.1 and §B.9.", ["sec-missing"]),
    ("Part III is next; Part I (Ch. 1–2) and **Part II** (Ch. 2–3).", ["part-missing", "part-range"]),
    ("A [link](09-nope.md) and `03-three.md`.", ["link-missing"]),
    ("Ch. 1 (first), 3 (third) and Ch. 2.", ["list-implicit"]),
    ("Intel SDM Vol. 3A §8.10.1 and the C11 standard §7.17; Smith, *A Book*, 2nd ed., Ch. 3.", ["ext-citation"] * 3),
    ("Intel SDM Vol. 3A sec. 8.10.1; Smith, *A Book*, 2nd ed., ch. 9; RFC 793 §3.7.", []),
    ("- Jones, R. *Another Book*, Wiley 1991 — Ch. 2 \"Common Mistakes\"; the Ch. 3 \"keep it off the chain\" rule; IEEE 1588 — Ch. 3", ["ext-citation"]),
    ("the generic §N.3 block, and Ch. 12, 64 bytes wide", ["ch-missing"]),
]


def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "chapters"))
        def w(name, text):
            with open(os.path.join(d, "chapters", name), "w", encoding="utf-8") as fh:
                fh.write(text)
        for n, part in ((1, "I"), (2, "I"), (3, "II")):
            w(f"0{n}-{['one', 'two', 'three'][n - 1]}.md",
              f"# Part {part} — P{part}\n\n# Chapter {n} — T{n}\n\n## {n}.1 A\n\n## {n}.2 B\n\n## {n}.3 C\n\n## {n}.4 D\n\n### {n}.4.1 E\n")
        w("appendix-A-a.md", "# Appendix A — AA\n\n## A.1 X\n\n## A.2 Y\n")
        w("appendix-B-b.md", "# Appendix B — BB\n\n## B.1 X\n")
        w("README.md", "## Part I — PI\n\n### [1. T1](01-one.md)\n\n### [2. T2](02-two.md)\n\n## Part II — PII\n\n### [3. T3](03-three.md)\n")
        ix = Index(d)
        assert not ix.problems, ix.problems
        base = open(os.path.join(d, "chapters", "02-two.md"), encoding="utf-8").read()
        bad = 0
        for text, want in SELF_TEST:
            w("02-two.md", base + "\n" + text + "\n")
            got = sorted(f[1] for f in validate(ix, d)[0])
            if got != sorted(want):
                bad += 1
                print(f"SELF-TEST FAILED: {text!r}\n   expected {sorted(want)}\n   got      {got}")
        print(f"check_refs self-test: {len(SELF_TEST) - bad}/{len(SELF_TEST)} cases pass")
        return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="Validate every cross-reference in the book against the headings on disk.")
    ap.add_argument("--root", default=None, help="repository root (default: two levels above this file)")
    ap.add_argument("--strict", action="store_true", help="WARN findings also make the exit code 1")
    ap.add_argument("--list-fenced", action="store_true", help="print every reference found inside fenced code")
    ap.add_argument("--dump-refs", action="store_true", help="print every reference as TSV and exit")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    ap.add_argument("--self-test", action="store_true", help="run the built-in cases against a toy book and exit")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    root = a.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ix = Index(root)
    if ix.problems:
        for p in ix.problems:
            print("STRUCTURE:", p)
        return 2
    if a.dump_refs:
        for label, path, i, line, inf, r in iter_refs(root):
            print("\t".join(map(str, (label, i, "fenced" if inf else "prose", r.kind, r.a, r.b, r.c, ",".join(sorted(r.flags)), r.text))))
        return 0
    findings, counts, fenced_all = validate(ix, root)
    err = [f for f in findings if f[0] == "ERROR"]
    warn = [f for f in findings if f[0] == "WARN"]
    if not a.quiet:
        for title, sel in (("PROSE", lambda f: not f[4]), ("FENCED CODE", lambda f: f[4])):
            rows = [f for f in findings if sel(f)]
            print(f"## {title}: {sum(1 for f in rows if f[0] == 'ERROR')} error(s), {sum(1 for f in rows if f[0] == 'WARN')} warning(s)")
            for sev, cls, label, i, inf, msg, ctx in rows:
                print(f"{sev:<5} {cls:<16} {label}:{i}: {msg}\n      | {ctx}")
        if a.list_fenced:
            print(f"## ALL REFERENCES INSIDE FENCED CODE ({len(fenced_all)})")
            for label, i, text, ctx in fenced_all:
                print(f"{label}:{i}: {text}  | {ctx}")
    nfen = len(fenced_all)
    print(f"check_refs: {len(ix.chapters)} chapters, {len(ix.appendices)} appendices (A–{ix.lastapp}), {len(ix.parts)} Parts; "
          f"checked {counts['ch']} chapter refs, {counts['sec']} § refs, {counts['app']} appendix refs, "
          f"{counts['appsec']} appendix-section refs, {counts['part']} Part refs, {counts['partrange']} Part ranges, "
          f"{counts['link']} links/file names ({nfen} refs inside fenced code; {counts['sec-external']} external-document § and "
          f"{counts['placeholder']} placeholder § skipped): {len(err)} error(s), {len(warn)} warning(s)")
    return 1 if err or (a.strict and warn) else 0


if __name__ == "__main__":
    sys.exit(main())
