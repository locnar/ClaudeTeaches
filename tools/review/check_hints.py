#!/usr/bin/env python3
"""check_hints.py — does each cross-reference point at the RIGHT chapter or section?

check_refs.py proves that every reference resolves; it cannot tell that `Ch. 57 (RT scheduling)`
resolves to the SMT chapter. This tool extracts every reference that carries a topic hint and compares
the hint's words with what the target actually is:

    after the reference      Ch. 57 (RT scheduling) · Ch. 27 (*Build Toolchain for Speed*) · Ch. 44's false-sharing fix
                             Ch. 2 — Measure First · §32.4.4 (slot maps) · Appendix L.3 (the reference machine)
    before the reference     the NUMA chapter (Ch. 18) · false sharing (Ch. 44) · huge pages — Ch. 17
    Part references          Part IX (Market Data, NIC & Fabric) · **Part III — Compile-Time …**

Each hint word is weighted by its rarity across the book (IDF). The TARGET scores a word in full if it
is in one of its headings (chapter title, `## N.k`, `### N.k.j`), and by frequency if it is only in its
body; for a `§N.k[.j]` reference the section's own headings and body are tried first. Every OTHER
chapter scores the hint by its headings alone. The report is driven by the gap between the best other
chapter and the target:

    MISMATCH  the target shows no trace of the hint (score <= LOW_EVIDENCE), or another chapter's headings
              claim the hint by STRONG_GAP more than the target does
    note      a smaller gap with some evidence for the target (listed with --all; almost always fine)

Scores do NOT separate true errors from asides ("Ch. 54's hunt", "Ch. 3's promise") — when this tool was
calibrated against the tree before the 2026-11-10 pass, the two dozen real wrong-chapter references
(PCIe cited as the transport chapter, tracing cited as the Rust chapter, …) sat among ~300 harmless
rows at the same scores. So every MISMATCH is meant to be READ once, and then either fixed or recorded.

This is a heuristic with a deterministic output, kept honest by a baseline: tools/review/hints-baseline.txt
lists the reviewed false positives (file, reference, hint — no line numbers, so it survives edits). The
exit code is about NEW findings only.

Exit code
    0   no MISMATCH outside the baseline
    1   at least one new MISMATCH (read each one; fix the reference, or re-run with --write-baseline once
        every new row has been read and found right)
    2   the book could not be indexed / bad usage

usage: check_hints.py [--root DIR] [--all] [--write-baseline] [--no-baseline] [--stats]
    --all             also print the note class and the baselined rows
    --write-baseline  rewrite the baseline from the current MISMATCH rows (do this only after reviewing them)
    --no-baseline     ignore the baseline (show everything)
Scope and exemptions are check_refs.py's (chapters/, CLAUDE.md minus its historical blocks,
tools/book-pdf/README.md); fenced code is included, since diagrams carry references too.
"""
import argparse
import collections
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_refs as C  # noqa: E402

LOW_EVIDENCE = 0.5        # at or below this score the target shows (almost) no trace of the hint's words
STRONG_GAP = 3.0          # (best other chapter's heading score) − (target's score), hint AFTER the reference
STRONG_GAP_BEFORE = 4.5   # same, hint taken from the words BEFORE "(Ch. N)" — looser prose, so a higher bar
WEAK_GAP = 1.5            # below this, and with any evidence for the target, a reference is not reported at all
STOP = set("""a an the and or of for in on to vs with its it is are was be been being this that these those from into at
by as how when what why not no nor but if then than so such via per each every all any some more most less least very
also only just here there where which who whom whose their they them he she his her our we you your i me my us can may
might must should would could will shall do does did done has have had having one two three four five six seven eight
nine ten first second third next last previous above below see cf e g i.e eg ie etc ch chapter chapters section sections
appendix part both either neither between through over under after before during while again once same other another
own too out up down off about against because until both few new old use used using uses make makes made get gets got
give gives given take takes took show shows shown showed shown again still even much many little lot lots way ways
thing things kind sort case cases example examples point points whole full half rest part parts side sides
book chapter's treatment detail details discussion covers covered cover material topic topics measured measure
measures built build builds develops developed describes described explains explained introduced introduces defined
defines there's that's it's what's here's""".split())


def stem(w):
    for suf in ("ization", "isation", "ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)] + ("y" if suf == "ies" else "")
    return w


def words(s):
    s = re.sub(r"`", " ", s.lower())
    s = re.sub(r"['’]s\b", " ", s)
    out = []
    for w in re.findall(r"[a-z][a-z0-9_+]*(?:-[a-z0-9_+]+)*", s):
        parts = [w] + (w.split("-") if "-" in w else []) + (w.split("_") if "_" in w else [])
        for p in parts:
            if len(p) >= 3 and p not in STOP and not p.isdigit():
                out.append(stem(p))
    return out


class Vocab:
    """Per chapter / appendix: heading words, body term counts, and the same per section and subsection."""

    def __init__(self, ix):
        self.ix = ix
        self.unit = {}        # key -> (heading word set, Counter of body words)
        self.title_words = {}  # chapter n / appendix L -> set of title words
        df = collections.Counter()
        for key, e in list(ix.chapters.items()) + list(ix.appendices.items()):
            path = os.path.join(ix.cdir, e["file"])
            cur_sec = cur_sub = None
            heads = {(): set()}
            body = {(): collections.Counter()}
            for _, l, inf in C.iter_lines(path):
                m = None if inf else re.match(r"^(#{1,3})\s+(.*)$", l)
                if m:
                    hw = set(words(re.sub(r"^(?:Chapter\s+\d+|Appendix\s+[A-Z]|Part\s+[IVX]+)\s*[—–-]\s*", "", m.group(2))))
                    if m.group(1) == "#" and m.group(2).startswith("Part "):
                        continue
                    num = re.match(r"^(?:\d+|[A-Z])\.(\d+)(?:\.(\d+))?\s", m.group(2))
                    if m.group(1) == "##" and num:
                        cur_sec, cur_sub = int(num.group(1)), None
                        heads.setdefault((cur_sec,), set()).update(hw)
                        body.setdefault((cur_sec,), collections.Counter())
                    elif m.group(1) == "###" and num and num.group(2):
                        cur_sub = int(num.group(2))
                        heads.setdefault((cur_sec, cur_sub), set()).update(hw)
                        heads.setdefault((cur_sec,), set()).update(hw)
                        body.setdefault((cur_sec, cur_sub), collections.Counter())
                    heads[()].update(hw)
                    continue
                ws = words(l)
                body[()].update(ws)
                if cur_sec is not None:
                    body.setdefault((cur_sec,), collections.Counter()).update(ws)
                    if cur_sub is not None:
                        body.setdefault((cur_sec, cur_sub), collections.Counter()).update(ws)
            for k in heads:
                self.unit[(key,) + k] = (heads[k], body.get(k, collections.Counter()))
            self.title_words[key] = set(words(e["title"] or ""))
            df.update(set(body[()]) | heads[()])
        n = len(ix.chapters) + len(ix.appendices)
        self.idf = {w: math.log(n / c) for w, c in df.items()}

    def wscore(self, key, sec, hint_words):
        """IDF-weighted evidence that the hint is about this target: a heading word counts in full, a body
        word by how often it occurs. The most specific unit that scores is used (section, then chapter)."""
        best = 0.0
        for k in ([(key,) + sec] if sec else []) + [(key,)]:
            if k not in self.unit:
                continue
            heads, body = self.unit[k]
            sc = 0.0
            for w in set(hint_words):
                tf = body.get(w, 0)
                wt = 1.0 if w in heads else 0.8 if tf >= 5 else 0.5 if tf >= 2 else 0.2 if tf == 1 else 0.0
                sc += wt * self.idf.get(w, 1.0)
            best = max(best, sc)
        return best

    def best_other(self, hint_words, exclude):
        """The other chapter/appendix whose HEADINGS claim the hint best: title words in full, section
        titles at half weight."""
        best = (0.0, None)
        hs = set(hint_words)
        for key in self.title_words:
            if key == exclude or (isinstance(exclude, set) and key in exclude):
                continue
            heads = self.unit[(key,)][0]
            tw = self.title_words[key]
            sc = sum(self.idf.get(w, 1.0) * (1.0 if w in tw else 0.5) for w in hs if w in heads)
            if sc > best[0]:
                best = (sc, key)
        return best


AFTER_PAREN = re.compile(r"\s\(([^()]{3,160})\)")
AFTER_POSS = re.compile(r"['’]s\s+((?:[^\s.,;:()|]+\s?){1,5})")
AFTER_DASH = re.compile(r"\s[—–:]\s\*{0,2}([^.;()|*]{3,90})")
AFTER_ITAL = re.compile(r"[,]?\s\*{1,2}([^*]{3,90})\*{1,2}")
BEFORE = re.compile(r"([A-Za-z`*][^()|;:!?]{2,90}?)\s(?:\((?:see\s|cf\.\s|→\s|e\.g\.,?\s|ties\s|from\s|in\s)?|[—–]\s)$")


def clean(h):
    h = re.sub(r"\*+|`", "", h)
    h = re.split(r"\s[—–]\s|;|\bCh\.|§|Appendix\s[A-Z]\b", h)[0]
    return h.strip(" ,.:")


def hints_for(line, refs, idx):
    """Yield (kind, hint text) for refs[idx]. Looks after the reference's attached sections and before it."""
    r = refs[idx]
    end = r.end
    j = idx + 1
    secs = []
    # "Ch. 81–85 (…)" / "Ch. 105, 106 (…)": the hint describes the whole list, so skip to its end
    while j < len(refs) and (refs[j].kind == "chrange" or (refs[j].kind in ("ch", "sec") and "cont" in refs[j].flags)) \
            and refs[j].start <= end + 6:
        end = max(end, refs[j].end)
        j += 1
    while j < len(refs) and refs[j].kind in ("sec", "appsec", "secrange") and refs[j].start <= end + 3:
        if refs[j].kind != "secrange":
            secs.append(refs[j])
        end = max(end, refs[j].end)
        j += 1
    tail = line[end:]
    nxt = refs[j].start - end if j < len(refs) else 10 ** 6
    for kind, rx in (("after-paren", AFTER_PAREN), ("after-poss", AFTER_POSS), ("after-dash", AFTER_DASH), ("after-ital", AFTER_ITAL)):
        m = rx.match(tail)
        if m and m.start(1) < nxt + 2:
            h = clean(m.group(1))
            if h and not re.match(r"^[\d~≈<>±−-]", h):
                yield kind, h, secs
            break
    else:
        # nothing after: try the phrase in front, but only for the first reference of a parenthesis / dash clause
        m = BEFORE.search(line[:r.start])
        if m and not any(x.end > m.start(1) and x.end <= r.start and x.kind in ("ch", "sec", "app", "appsec") for x in refs):
            ws = m.group(1).split()
            yield "before", clean(" ".join(ws[-6:])), secs


def collect(root, ix, vocab):
    rows = []
    stats = collections.Counter()
    for label, path, exempt in C.scan_files(root):
        own = C.own_id(label)
        for i, line, inf in C.iter_lines(path):
            if i in exempt:
                continue
            if label == "chapters/README.md" and re.match(r"^(\s*- (?:\d+|[A-Z])\.\d|### \[)", line):
                continue   # outline definitions, not references
            if re.match(r"^#{1,3}\s", line) and not inf:
                continue   # the headings themselves
            refs = C.scan_line(line)
            for idx, r in enumerate(refs):
                if r.kind == "part":
                    m = re.match(r"\*{0,2}\s(?:[—–-]\s([^()|*]{3,80})|\(([^()]{3,80})\))", line[r.end:])
                    if m and r.a in ix.parts:
                        hint = clean(m.group(1) or m.group(2))
                        hw = words(hint)
                        if not hw or re.match(r"^(?:Ch\.|\d)", hint):
                            continue
                        stats["part-hints"] += 1
                        tw = set(words(ix.parts[r.a]["title"]))
                        if not (set(hw) & tw):
                            other = [(len(set(hw) & set(words(p["title"]))), k) for k, p in ix.parts.items() if k != r.a]
                            best = max(other)
                            if best[0] >= 1:
                                rows.append(("STRONG", label, i, f"Part {r.a}", hint, f"Part {r.a} — {ix.parts[r.a]['title']}",
                                             f"Part {best[1]} — {ix.parts[best[1]]['title']}", line))
                    continue
                if r.kind not in ("ch", "sec", "app", "appsec") or "weak" in r.flags:
                    continue
                if r.kind == "appsec" and "bare" in r.flags and r.a in ("N", "M", "X", "Y") and own != r.a:
                    continue          # "§N.3", "§M.x": the book's placeholder spelling for "section 3 of any chapter"
                if r.kind == "ch" and "cont" in r.flags:
                    continue          # a list member: the list is judged once, from its first reference
                if r.kind in ("sec", "appsec") and any(f.startswith("ctxch") for f in r.flags):
                    continue          # judged with its chapter reference
                if r.kind == "appsec" and "bare" not in r.flags and idx and refs[idx - 1].kind == "app" and refs[idx - 1].end >= r.start:
                    continue          # "Appendix L.3" is judged once, from the 'app' reference
                key = r.a
                if key not in ix.chapters and key not in ix.appendices:
                    continue
                for kind, hint, secs in hints_for(line, refs, idx):
                    hw = words(hint)
                    if not hw:
                        continue
                    stats["hints"] += 1
                    sec = ()
                    if r.kind in ("sec", "appsec"):
                        sec = (r.b,) + ((r.c,) if r.c is not None else ())
                    elif secs and secs[0].a == key:
                        sec = (secs[0].b,) + ((secs[0].c,) if secs[0].c is not None else ())
                    if key == own and not sec:
                        continue      # "this chapter (Ch. N)" — nothing to compare
                    tsc = vocab.wscore(key, sec, hw)
                    # a list or range is judged as a whole: any member may own the hint
                    members, k = {key}, idx + 1
                    while k < len(refs) and (refs[k].kind == "chrange" or (refs[k].kind in ("ch", "sec") and "cont" in refs[k].flags)):
                        if refs[k].kind == "ch":
                            members.add(refs[k].a)
                        elif refs[k].kind == "chrange" and isinstance(refs[k].a, int) and refs[k].a < refs[k].b:
                            members.update(range(refs[k].a, refs[k].b + 1))
                        k += 1
                    for mkey in members - {key}:
                        if mkey in ix.chapters:
                            tsc = max(tsc, vocab.wscore(mkey, (), hw))
                    osc, other = vocab.best_other(hw, members)
                    gap = osc - tsc
                    if tsc > 0 and gap < WEAK_GAP:
                        continue
                    e = ix.chapters.get(key) or ix.appendices.get(key)
                    tname = (f"Ch. {key}" if isinstance(key, int) else f"Appendix {key}") + f" — {e['title']}"
                    if sec:
                        st = (e["secs"].get(sec[0]) if len(sec) == 1 else e["subs"].get(sec))
                        tname += f" / §{key}.{'.'.join(map(str, sec))} {st or '?'}"
                    oname = ""
                    if other is not None:
                        oe = ix.chapters.get(other) or ix.appendices.get(other)
                        oname = (f"Ch. {other}" if isinstance(other, int) else f"Appendix {other}") + f" — {oe['title']}"
                    sev = "STRONG" if tsc <= LOW_EVIDENCE or (gap >= STRONG_GAP and kind != "before") or gap >= STRONG_GAP_BEFORE else "weak"
                    rows.append((sev, label, i, r.text.strip() + ("" if not secs else " " + secs[0].text.strip()), hint,
                                 tname + f"  [{tsc:.1f}]", oname + f"  [{osc:.1f}]", line))
    return rows, stats


def bkey(row):
    return f"{row[1]}\t{row[3]}\t{row[4]}"


def main():
    ap = argparse.ArgumentParser(description="Compare each reference's topic hint with its target's headings and vocabulary.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    root = a.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ix = C.Index(root)
    if ix.problems:
        for p in ix.problems:
            print("STRUCTURE:", p)
        return 2
    vocab = Vocab(ix)
    rows, stats = collect(root, ix, vocab)
    bpath = os.path.join(root, "tools", "review", "hints-baseline.txt")
    base = set()
    if os.path.exists(bpath) and not a.no_baseline:
        base = {l.rstrip("\n") for l in open(bpath, encoding="utf-8") if l.strip() and not l.startswith("#")}
    strong = [r for r in rows if r[0] == "STRONG"]
    weak = [r for r in rows if r[0] == "weak"]
    if a.write_baseline:
        with open(bpath, "w", encoding="utf-8") as fh:
            fh.write("# Reviewed rows of tools/review/check_hints.py (file<TAB>reference<TAB>hint). Regenerate with --write-baseline\n"
                     "# ONLY after reading every new MISMATCH row; a row here means \"read by a person: the target is right\".\n")
            for k in sorted({bkey(r) for r in strong}):
                fh.write(k + "\n")
        print(f"baseline written: {len({bkey(r) for r in strong})} rows")
        return 0
    new = [r for r in strong if bkey(r) not in base]
    for sev, label, i, ref, hint, tname, oname, line in new:
        print(f"MISMATCH {label}:{i}: {ref} «{hint}»\n       target: {tname}\n       hint fits: {oname}")
    if a.all:
        for sev, label, i, ref, hint, tname, oname, line in [r for r in strong if bkey(r) in base]:
            print(f"baselined {label}:{i}: {ref} «{hint}» | target: {tname} | hint fits: {oname}")
        for sev, label, i, ref, hint, tname, oname, line in weak:
            print(f"note   {label}:{i}: {ref} «{hint}» | target: {tname} | hint fits: {oname}")
    stale = base - {bkey(r) for r in strong}
    print(f"check_hints: {stats['hints']} chapter/section/appendix hints and {stats['part-hints']} Part hints compared; "
          f"{len(strong)} MISMATCH ({len(strong) - len(new)} reviewed in the baseline, {len(new)} new), {len(weak)} notes"
          + (f"; {len(stale)} baseline rows no longer occur (prune with --write-baseline)" if stale else ""))
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
