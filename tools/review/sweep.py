#!/usr/bin/env python3
"""sweep.py — structural consistency sweep over chapters/, the README index, the preface and CLAUDE.md.

One of the three checks every change must pass (run from anywhere; --root defaults to the repo):

    tools/review/sweep.py         structure            (this file)
    tools/review/check_refs.py    every reference resolves
    tools/review/check_hints.py   every reference resolves to the right topic

Exit code: 0 = no issues; 1 = at least one issue (NOTE lines never change the exit code); 2 = bad usage.

What it checks
  Chapter files (chapters/NN-slug.md)
    * first line is the Part header `# Part X — Title`, and it is the README's Part for that number
    * `# Chapter N — Title` matches the file name; the Prerequisites / Leads-into blockquote is present
    * the sections are exactly one of the two shapes, numbered without gaps and in this order:
        9-section:  Why it matters · Mental model · Measure it · Techniques · Verify the codegen ·
                    Pitfalls · Exercises · References · Additional Reading
        8-section:  the same without "Verify the codegen" (the §N.5 exemption: a chapter where the
                    optimizer is not the subject has Pitfalls at §N.5 and References at §N.7)
    * `### N.k.j` subsections carry the chapter's number, sit under their own `## N.k`, and count 1, 2, 3 …
    * the closing teaser is `*Next: Ch. N+1 — <that chapter's title>…*` (the last chapter → Appendix A)
    * no cross-reference outside 1..MAX / A..LAST (check_refs.py does the thorough job)
    * every code fence is closed; no placeholder text (TODO, TBD, FIXME, @PLACEHOLDER@, "skeleton chapter");
      no hard-wrapped paragraph (a prose line directly followed by another prose line)
    * Part II chapters name the reference machine (Xeon Gold 6326) or cite Appendix L.3; the NUMA
      chapter names the dual-socket box
  Appendix files
    * `# Appendix X — Title` matches the file name; `## X.n` sections count 1, 2, 3 … with the right letter
    * the closing teaser chain A → B → … → LAST (`*Next: Appendix <next letter> — <its title>…`); the
      last appendix points at README.md
    * fences, placeholders and hard wraps as for chapters
  README.md (the topic index)
    * every chapter/appendix file is linked exactly once and every link resolves; titles match the files
    * every chapter's outline mirrors the chapter: one `- N.k …` line per `## N.k` heading up to Exercises
      (References / Additional Reading are deliberately NOT listed) and one `  - N.2.x` / `  - N.4.x`
      sub-bullet per `### N.2.x` / `### N.4.x` heading, in order, nothing extra. Each line BEGINS WITH the
      heading's text; it may continue with a gloss for the searcher ("… (`ixgbe`, `mlx5`, `sfc`)")
    * every appendix's outline lists `X.1 … X.n` in order, each line beginning with the section's title
      (an appendix line may add a parenthetical gloss), again without References / Additional Reading
    * chapters appear in numeric order under the right `## Part` header, Parts are I..N without gaps
  Counts stated in prose (README legend, preface, CLAUDE.md status block, tools/book-pdf/README.md)
    * "<N> chapters", "<N> appendices (A–X)", "<N> Parts" — digits or words — agree with the files on
      disk; the preface's Part bullets and CLAUDE.md's Phase-3 Part lines agree with the README

--fix-readme rewrites the README outline lines that do not begin with their heading (the line becomes the
heading text), inserts missing lines and removes extra ones; glossed lines that are right are kept.
Review the diff.

usage: sweep.py [--root DIR] [--fix-readme] [--quiet]
"""
import argparse
import collections
import os
import re
import sys

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI"]
SHAPE9 = ["Why it matters", "Mental model", "Measure it", "Techniques", "Verify the codegen", "Pitfalls", "Exercises",
          "References", "Additional Reading"]
SHAPE8 = [s for s in SHAPE9 if s != "Verify the codegen"]
# what insert_chapter.py's skeleton and an unfinished draft leave behind ("skeleton" alone is an ordinary word here:
# the benchmark skeleton of Appendix L — so only the phrases that name an unwritten chapter are matched)
PLACEHOLDER = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|@[A-Z][A-Z0-9_]{2,}@|\bskeleton chapter\b|\bchapter skeleton\b|"
                         r"\bprose placeholder\b|\blorem ipsum\b", re.I)


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[`*]", "", s)).strip().lower()


def num_word(n):
    ones = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    def below100(k):
        return ones[k] if k < 20 else tens[k // 10] + ("-" + ones[k % 10] if k % 10 else "")
    if n < 100:
        return below100(n)
    rest = n % 100
    return "one hundred" + (" and " + below100(rest) if rest else "")


def read_lines(path):
    """([(lineno, line, in_fence)], fence_left_open) — fence delimiter lines are in_fence=True."""
    out, inf = [], False
    for i, l in enumerate(open(path, encoding="utf-8"), 1):
        l = l.rstrip("\n")
        if FENCE.match(l):
            inf = not inf
            out.append((i, l, True))
        else:
            out.append((i, l, inf))
    return out, inf


def is_prose(l):
    """A paragraph line: not blank, not a heading/list/table/quote/rule/HTML/indented line."""
    if not l.strip():
        return False
    if re.match(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||<|---+\s*$|\*\*\*+\s*$|\[\^|:\s)", l):
        return False
    if l.startswith("    ") or l.startswith("\t"):
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Structural consistency sweep (see the module docstring).")
    ap.add_argument("--root", default=None)
    ap.add_argument("--fix-readme", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    root = a.root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    D = os.path.join(root, "chapters")
    files = sorted(os.listdir(D))
    chnum = lambda f: int(f.split("-", 1)[0])
    chap = sorted([f for f in files if re.match(r"^\d{2,3}-.*\.md$", f) and not f.startswith("00-")], key=chnum)
    apps = sorted(f for f in files if re.match(r"^appendix-[A-Z]-.*\.md$", f))
    MAXCH = max(chnum(f) for f in chap)
    LASTAPP = max(f.split("-")[1] for f in apps)
    issues = collections.defaultdict(list)
    notes = collections.defaultdict(list)
    titles, heads, subs = {}, {}, {}

    # ------------------------------------------------------------------ README: Parts and chapter order
    rpath = os.path.join(D, "README.md")
    rlines = open(rpath, encoding="utf-8").read().split("\n")
    R = "\n".join(rlines)
    readme_part = {}                       # chapter number -> (roman, title)
    part_range = collections.OrderedDict()  # roman -> [lo, hi, title]
    cur, order = None, []
    for l in rlines:
        m = re.match(r"^##\s+Part\s+([IVX]+)\s*[—–-]\s*(.+?)\s*$", l)
        if m:
            cur = (m.group(1), m.group(2))
            part_range[cur[0]] = [None, None, cur[1]]
            continue
        if re.match(r"^##\s", l):
            cur = None
        m = re.match(r"^### \[(\d+)\. ", l)
        if m:
            n = int(m.group(1))
            order.append(n)
            if cur is None:
                issues["README.md"].append(f"chapter {n} is not under a '## Part' header")
            else:
                readme_part[n] = cur
                pr = part_range[cur[0]]
                pr[0] = n if pr[0] is None else pr[0]
                pr[1] = n
    if order != list(range(1, MAXCH + 1)):
        issues["README.md"].append(f"chapter blocks are not 1..{MAXCH} in order")
    if list(part_range) != ROMAN[:len(part_range)]:
        issues["README.md"].append(f"Part headers are not I..{ROMAN[len(part_range) - 1]} in order: {list(part_range)}")
    prev_hi = 0
    for r, (lo, hi, _) in part_range.items():
        if lo != prev_hi + 1:
            issues["README.md"].append(f"Part {r} starts at {lo}, expected {prev_hi + 1}")
        prev_hi = hi or prev_hi
    if prev_hi != MAXCH:
        issues["README.md"].append(f"the last Part ends at {prev_hi}, but there are {MAXCH} chapters")

    # ------------------------------------------------------------------ checks shared by chapters and appendices
    def common(f, ls, unclosed):
        if unclosed:
            issues[f].append("a code fence is opened and never closed")
        in_run = False
        for k, (i, l, inf) in enumerate(ls):
            if inf:
                in_run = False
                continue
            m = PLACEHOLDER.search(l)
            if m:
                issues[f].append(f"line {i}: placeholder text {m.group(0)!r}: {l.strip()[:90]}")
            nxt = ls[k + 1] if k + 1 < len(ls) else (0, "", False)
            wrapped = is_prose(l) and not nxt[2] and is_prose(nxt[1]) and not l.endswith("  ") and not l.endswith("\\")
            if wrapped and not in_run:
                issues[f].append(f"line {i}: hard-wrapped paragraph (a prose line directly followed by another): {l.strip()[:70]}")
            in_run = wrapped or (in_run and is_prose(l))

    # ------------------------------------------------------------------ chapters
    for f in chap:
        n = chnum(f)
        ls, unclosed = read_lines(os.path.join(D, f))
        out = [(i, l) for i, l, inf in ls if not inf]
        text = "\n".join(l for _, l in out)
        common(f, ls, unclosed)
        first = [l for _, l in out if l.strip()][:1]
        m = re.match(r"^#\s+Part\s+([IVX]+)\s*[—–-]\s*(.+?)\s*$", first[0]) if first else None
        if not m:
            issues[f].append(f"no Part header as first line: {first}")
        elif n in readme_part and readme_part[n] != (m.group(1), m.group(2)):
            issues[f].append(f"Part header '{m.group(1)} — {m.group(2)}' but the README files chapter {n} under "
                             f"'{readme_part[n][0]} — {readme_part[n][1]}'")
        m = re.search(r"^#\s+Chapter\s+(\d+)\s*[—–-]\s*(.+)$", text, re.M)
        if not m:
            issues[f].append("no '# Chapter N — Title'")
        else:
            if int(m.group(1)) != n:
                issues[f].append(f"chapter number {m.group(1)} != file {n}")
            titles[n] = m.group(2).strip()
        if not re.search(r"^>\s*\*\*Prerequisites", text, re.M | re.I):
            issues[f].append("no '> **Prerequisites' blockquote")
        if not re.search(r"^>\s*\*\*Leads[ -]into", text, re.M | re.I):
            issues[f].append("no '> **Leads into' blockquote")
        secs = [(int(c), int(s), t.strip()) for c, s, t in re.findall(r"^##\s+(\d+)\.(\d+)\s+(.+)$", text, re.M)]
        sb = [(int(c), int(s), int(ss), t.strip()) for c, s, ss, t in re.findall(r"^###\s+(\d+)\.(\d+)\.(\d+)\s+(.+)$", text, re.M)]
        for c, s, t in secs:
            if c != n:
                issues[f].append(f"section {c}.{s} carries the wrong chapter number")
        nums = [s for c, s, t in secs]
        if nums != list(range(1, len(nums) + 1)):
            issues[f].append(f"section numbering is not 1..{len(nums)}: {nums}")
        shape = SHAPE9 if len(secs) == 9 else SHAPE8 if len(secs) == 8 else None
        if shape is None:
            issues[f].append(f"{len(secs)} sections; expected 9 (with 'Verify the codegen') or 8 (without)")
        else:
            for (c, s, t), want in zip(secs, shape):
                if not t.lower().startswith(want.lower()):
                    issues[f].append(f"§{n}.{s} is '{t[:40]}'; in the {len(shape)}-section shape it must be '{want}…'")
        seen = collections.defaultdict(list)
        for c, s, ss, t in sb:
            if c != n:
                issues[f].append(f"subsection {c}.{s}.{ss} carries the wrong chapter number")
            seen[s].append(ss)
        for s, lst in seen.items():
            if lst != list(range(1, len(lst) + 1)):
                issues[f].append(f"subsections of §{n}.{s} are not 1..{len(lst)} in order: {lst}")
        cur_s = None
        for i, l in out:
            m2 = re.match(r"^##\s+\d+\.(\d+)\s", l)
            if m2:
                cur_s = int(m2.group(1))
            m3 = re.match(r"^###\s+\d+\.(\d+)\.(\d+)\s", l)
            if m3 and int(m3.group(1)) != cur_s:
                issues[f].append(f"line {i}: '### {n}.{m3.group(1)}.{m3.group(2)}' appears under §{n}.{cur_s}")
            m4 = re.match(r"^###\s+(?!\d+\.\d+\.\d+\s)(.+)$", l)
            if m4:
                notes[f].append(f"line {i}: unnumbered '### {m4.group(1)[:50]}' (not in the README outline; the PDF build marks it unnumbered)")
        heads[n] = [(s, t) for c, s, t in secs]
        subs[n] = [(s, ss, t) for c, s, ss, t in sb]
        tail = [l for _, l in out if l.strip()][-1] if out else ""
        mt = re.match(r"^\*Next: (?:Ch\.|Chapter|Appendix)\s*(\d+|[A-Z])\s*[—–:-]\s*(.*)$", tail)
        if not mt:
            issues[f].append(f"no '*Next: Ch. N — …*' teaser as the last line: {tail[:80]!r}")
        else:
            nxt = mt.group(1)
            if n == MAXCH:
                if nxt != "A":
                    issues[f].append(f"last chapter's teaser points at {nxt}, expected Appendix A")
            elif not nxt.isdigit() or int(nxt) != n + 1:
                issues[f].append(f"teaser points at {nxt}, expected Ch. {n + 1}")
            titles[("teaser", n)] = mt.group(2)
        for r in re.findall(r"\bCh(?:\.|apter)\s*(\d+)", text):
            if int(r) < 1 or int(r) > MAXCH:
                issues[f].append(f"cross-ref Ch. {r} out of range")
        for r in re.findall(r"Appendix\s+([A-Z])\b", text):
            if r > LASTAPP:
                issues[f].append(f"Appendix {r} out of range")
        p2 = part_range.get("II")
        if p2 and p2[0] is not None and p2[0] <= n <= p2[1]:
            if "6326" not in text and "Appendix L.3" not in text:
                issues[f].append("Part II chapter names neither the reference machine (Xeon Gold 6326) nor Appendix L.3")
            if titles.get(n, "").startswith("NUMA") and "dual-socket" not in text.lower():
                issues[f].append("the NUMA chapter does not name the dual-socket reference box")
    for n in range(1, MAXCH):
        t = titles.get(("teaser", n))
        if t is not None and n + 1 in titles and not norm(t).startswith(norm(titles[n + 1])[:28]):
            issues[chap[n - 1]].append(f"teaser names 'Ch. {n + 1} — {t[:50]}…' but Chapter {n + 1} is titled '{titles[n + 1]}'")
    last_teaser = titles.get(("teaser", MAXCH))

    # ------------------------------------------------------------------ appendices
    aheads = {}
    letters = [f.split("-")[1] for f in apps]
    for k, f in enumerate(apps):
        L = letters[k]
        ls, unclosed = read_lines(os.path.join(D, f))
        out = [(i, l) for i, l, inf in ls if not inf]
        text = "\n".join(l for _, l in out)
        common(f, ls, unclosed)
        m = re.search(r"^#\s+Appendix\s+([A-Z])\s*[—–-]\s*(.+)$", text, re.M)
        if not m:
            issues[f].append("no '# Appendix X — Title'")
        else:
            if m.group(1) != L:
                issues[f].append(f"appendix letter {m.group(1)} != file {L}")
            titles[L] = m.group(2).strip()
        secs = re.findall(r"^##\s+([A-Z])\.(\d+)\s+(.+)$", text, re.M)
        for c, s, t in secs:
            if c != L:
                issues[f].append(f"section {c}.{s} carries the wrong letter")
        nums = [int(s) for c, s, t in secs]
        if nums != list(range(1, len(nums) + 1)):
            issues[f].append(f"section numbering is not 1..{len(nums)}: {nums}")
        aheads[L] = [(int(s), t.strip()) for c, s, t in secs]
        for r in re.findall(r"\bCh(?:\.|apter)\s*(\d+)", text):
            if int(r) < 1 or int(r) > MAXCH:
                issues[f].append(f"cross-ref Ch. {r} out of range")
        tail = [l for _, l in out if l.strip()][-1] if out else ""
        if L != LASTAPP:
            want = letters[k + 1]
            mt = re.match(r"^\*Next: Appendix ([A-Z])\s*[—–:-]\s*(.*)$", tail)
            if not mt:
                issues[f].append(f"no '*Next: Appendix {want} — …*' teaser as the last line: {tail[:70]!r}")
            elif mt.group(1) != want:
                issues[f].append(f"teaser points at Appendix {mt.group(1)}, expected Appendix {want}")
            else:
                titles[("ateaser", L)] = (want, mt.group(2))
        elif "README" not in tail:
            issues[f].append(f"the last appendix should close by pointing at README.md: {tail[:70]!r}")
    for k, f in enumerate(apps):
        v = titles.get(("ateaser", letters[k]))
        if v and v[0] in titles and not norm(v[1]).startswith(norm(titles[v[0]])[:22]):
            issues[f].append(f"teaser names 'Appendix {v[0]} — {v[1][:40]}…' but that appendix is titled '{titles[v[0]]}'")
    if last_teaser is not None and "A" in titles and not norm(last_teaser).startswith(norm(titles["A"])[:22]):
        issues[chap[-1]].append(f"teaser names 'Appendix A — {last_teaser[:40]}…' but Appendix A is titled '{titles['A']}'")
    if letters != [chr(ord("A") + i) for i in range(len(letters))]:
        issues["chapters/"].append(f"appendix letters are not A..{LASTAPP} without gaps: {letters}")
    if sorted(chnum(f) for f in chap) != list(range(1, MAXCH + 1)):
        issues["chapters/"].append("chapter numbers on disk are not 1..MAX without gaps or duplicates")

    # ------------------------------------------------------------------ README links, titles, outline
    links = re.findall(r"\]\(([^)]+\.md)\)", R)
    for l in links:
        if not os.path.exists(os.path.join(D, l)):
            issues["README.md"].append(f"broken link {l}")
    for f in chap + apps + ["00-preface.md"]:
        c = links.count(f)
        if c != 1:
            issues["README.md"].append(f"{f} is linked {c} times (expected once)")
    for n in range(1, MAXCH + 1):
        m = re.search(r"^### \[%d\. (.+?)\]\((.+?)\)" % n, R, re.M)
        if not m:
            issues["README.md"].append(f"no README entry for chapter {n}")
        else:
            if m.group(1).strip() != titles.get(n):
                issues["README.md"].append(f"title mismatch ch {n}: README={m.group(1)!r} chapter={titles.get(n)!r}")
            if m.group(2) != chap[n - 1]:
                issues["README.md"].append(f"chapter {n} links to {m.group(2)}, the file is {chap[n - 1]}")
    for L in letters:
        m = re.search(r"^### \[Appendix %s — (.+?)\]\((.+?)\)" % L, R, re.M)
        if not m:
            issues["README.md"].append(f"no README entry for appendix {L}")
        elif m.group(1).strip() != titles.get(L):
            issues["README.md"].append(f"title mismatch appendix {L}: README={m.group(1)!r} file={titles.get(L)!r}")

    def block(key):
        """(start, end) line indices of a README outline block (the bullet lines after its ### header)."""
        pat = (r"^### \[%d\. " % key) if isinstance(key, int) else (r"^### \[Appendix %s — " % key)
        st = next((i for i, l in enumerate(rlines) if re.match(pat, l)), None)
        if st is None:
            return None
        en = next((i for i in range(st + 1, len(rlines)) if not re.match(r"^\s*- ", rlines[i])), len(rlines))
        return st + 1, en

    fixes = 0
    for n in range(MAXCH, 0, -1):          # bottom-up, so --fix-readme edits do not shift later blocks
        if n not in heads or block(n) is None:
            continue
        want = []                          # [(indent, number, heading text)]
        for s, t in heads[n]:
            if t.startswith("References") or t.startswith("Additional Reading"):
                continue
            want.append(("", f"{n}.{s}", t))
            if s in (2, 4):
                want += [("  ", f"{n}.{x}.{y}", tt) for x, y, tt in subs[n] if x == s]
        st, en = block(n)
        have = rlines[st:en]
        hmap = {}
        for x in have:
            m = re.match(r"^(\s*)- (\d+(?:\.\d+)+) (.*)$", x)
            if m:
                hmap[m.group(2)] = (m.group(1), m.group(3), x)
            else:
                issues["README-outline"].append(f"ch {n}: unparseable outline line: {x.strip()[:90]}")
        new, bad = [], False
        for ind, num, t in want:
            h = hmap.get(num)
            # a README line is the heading, optionally followed by a gloss:  "N.k Title (gloss)" / "N.k Title; more"
            if h and h[0] == ind and norm(h[1]).startswith(norm(t)):
                new.append(h[2])
            else:
                bad = True
                issues["README-outline"].append(f"ch {n}: the line for {num} should begin '{num} {t[:80]}'"
                                                + (f" (has: {h[1][:80]!r})" if h else " (missing)"))
                new.append(f"{ind}- {num} {t}")
        wnums = [w[1] for w in want]
        for num in hmap:
            if num not in wnums:
                bad = True
                issues["README-outline"].append(f"ch {n}: README lists {num} '{hmap[num][1][:60]}', which the chapter does not have "
                                                f"(only §{n}.2.x / §{n}.4.x subsections and the sections up to Exercises are listed)")
        if not bad and [x for x in have] != new:
            bad = True
            issues["README-outline"].append(f"ch {n}: outline lines are in a different order from the chapter's sections")
        if bad and a.fix_readme:
            rlines[st:en] = new
            fixes += 1
    for L in reversed(letters):
        if L not in aheads or block(L) is None:
            continue
        st, en = block(L)
        have = rlines[st:en]
        want_secs = [(s, t) for s, t in aheads[L] if not (t.startswith("References") or t.startswith("Additional Reading"))]
        ok = len(have) == len(want_secs)
        new = []
        for idx, (s, t) in enumerate(want_secs):
            line = have[idx] if idx < len(have) else ""
            m = re.match(r"^- %s\.(\d+) (.*)$" % L, line)
            stem = re.split(r":| \(", t)[0]      # an appendix line may reword the gloss after the section's name
            if m and int(m.group(1)) == s and norm(m.group(2)).startswith(norm(stem)):
                new.append(line)
            else:
                ok = False
                issues["README-outline"].append(f"appendix {L}: the line for {L}.{s} should begin '{L}.{s} {t[:70]}' (has: {line.strip()[:70]!r})")
                new.append(f"- {L}.{s} {t}")
        for x in have[len(want_secs):]:
            issues["README-outline"].append(f"appendix {L}: README has an extra line: {x.strip()[:90]}")
        if not ok and a.fix_readme:
            rlines[st:en] = new
            fixes += 1
    if a.fix_readme and fixes:
        open(rpath, "w", encoding="utf-8").write("\n".join(rlines))
        print(f"--fix-readme: rewrote {fixes} outline block(s) in chapters/README.md — review `git diff chapters/README.md`, then re-run")

    # ------------------------------------------------------------------ counts stated in prose
    nparts = len(part_range)
    facts = [("chapters", MAXCH), ("appendices", len(apps)), ("Parts", nparts)]
    HIST = re.compile(r"\b(?:was|were|had|originally|then|since|first|from|extended|grew|historical|before|so far|preceding|previous|"
                      r"other|these|those|later|earlier|following|few|several|many|some|all|most|both|new|six|two|three|four|five)\b")

    def check_counts(label, text, skip=lambda l: False):
        for i, l in enumerate(text.split("\n"), 1):
            if skip(l):
                continue
            for word, val in facts:
                for m in re.finditer(r"(?<![\w.–-])(\d{2,3}|(?:[Oo]ne hundred(?: and)? )?[A-Za-z]+(?:-[a-z]+)?)\s+%s\b" % word, l):
                    tok = m.group(1)
                    if tok.isdigit():
                        got = int(tok)
                    else:
                        got = next((v for v in range(139, 9, -1) if tok.lower() == num_word(v)), None)
                        if got is None:
                            continue
                    if got != val and not HIST.search(l[max(0, m.start() - 50):m.start()]):
                        issues[label].append(f"line {i}: says '{m.group(0)}', the files say {val} {word}: {l.strip()[:100]}")
            for m in re.finditer(r"appendices\s+\(\*{0,2}A–([A-Z])\*{0,2}\)", l):
                if m.group(1) != LASTAPP and not HIST.search(l[max(0, m.start() - 50):m.start()]):
                    issues[label].append(f"line {i}: says appendices A–{m.group(1)}, the files end at {LASTAPP}")

    check_counts("README.md", "\n".join(rlines[:12]))
    ptext = open(os.path.join(D, "00-preface.md"), encoding="utf-8").read()
    check_counts("00-preface.md", ptext)
    p = os.path.join(root, "tools/book-pdf/README.md")
    if os.path.exists(p):
        check_counts("tools/book-pdf/README.md", open(p, encoding="utf-8").read())
    cpath = os.path.join(root, "CLAUDE.md")
    if os.path.exists(cpath):
        cl = open(cpath, encoding="utf-8").read().split("\n")
        s0 = next((i for i, l in enumerate(cl) if l.startswith("## Status / Workflow")), None)
        if s0 is None:
            issues["CLAUDE.md"].append("no '## Status / Workflow' section")
        else:
            # the status block up to the first dated insertion record; the records themselves are history
            e0 = next((i for i in range(s0, len(cl)) if re.match(r"^- \*\*20\d\d-\d\d-\d\d ", cl[i])), len(cl))
            blk = cl[s0:e0]
            check_counts("CLAUDE.md", "\n".join(blk), skip=lambda l: "Phase 1" in l or "twelve topical Parts" in l)
            got = []
            for l in blk:
                m = re.match(r"^\s*- \*\*([IVX]+) — ([^*]+)\*\* \((\d+)–(\d+)", l)
                if m:
                    got.append(m.group(1))
                    pr = part_range.get(m.group(1))
                    if not pr or (pr[0], pr[1]) != (int(m.group(3)), int(m.group(4))) or norm(pr[2]) != norm(m.group(2)):
                        issues["CLAUDE.md"].append(f"Phase-3 line '{m.group(1)} — {m.group(2)} ({m.group(3)}–{m.group(4)})' disagrees with the README "
                                                   f"({pr[2] if pr else '?'} {pr[0] if pr else '?'}–{pr[1] if pr else '?'})")
            if got != list(part_range):
                issues["CLAUDE.md"].append(f"Phase-3 Part lines are {got}, the README has {list(part_range)}")
    got = re.findall(r"^- \*\*Part ([IVX]+) — ([^*]+)\*\* \(Ch\. (\d+)–(\d+)\)", ptext, re.M)
    if [g[0] for g in got] != list(part_range):
        issues["00-preface.md"].append(f"Part bullets are {[g[0] for g in got]}, the README has {list(part_range)}")
    for r, t, lo, hi in got:
        pr = part_range.get(r)
        if pr and ((pr[0], pr[1]) != (int(lo), int(hi)) or norm(pr[2]) != norm(t)):
            issues["00-preface.md"].append(f"Part {r} bullet '{t} (Ch. {lo}–{hi})' disagrees with the README ({pr[2]} {pr[0]}–{pr[1]})")

    # ------------------------------------------------------------------ report
    total = sum(len(v) for v in issues.values())
    if not a.quiet:
        for f in sorted(issues):
            print(f"== {f}")
            for i in issues[f]:
                print("   -", i)
        if notes:
            print("== notes (informational, do not affect the exit code)")
            for f in sorted(notes):
                for i in notes[f]:
                    print(f"   NOTE {f}: {i}")
    print(f"sweep: {len(chap)} chapters (1–{MAXCH}), {len(apps)} appendices (A–{LASTAPP}), {nparts} Parts "
          f"({', '.join(f'{r} {lo}–{hi}' for r, (lo, hi, _) in part_range.items())}): {total} issue(s), "
          f"{sum(len(v) for v in notes.values())} note(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
