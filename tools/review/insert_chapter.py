#!/usr/bin/env python3
"""Insert a new chapter after chapter N and renumber everything that follows.

usage (run from the repo root, on a CLEAN git tree):

    python3 tools/review/insert_chapter.py --after 45 \
        --slug hunting-residual-noise --title "Hunting Residual Noise on Isolated Cores" \
        [--part "Part VII — OS, Scheduling & Isolation"] [--dry-run] [--allow-dirty]

What it does, in order:

  1. Refuses to run on a dirty tree (`git status --porcelain` must be empty) unless
     --allow-dirty or --dry-run is given.
  2. `git mv` renames chapters/MM-*.md for every M in N+1..MAX to (M+1)-*.md, highest first.
  3. Rewrites, in every chapter file, every appendix, chapters/README.md, chapters/00-preface.md
     and tools/book-pdf/README.md (fenced code included, because these are explicit references):
       - `Ch. M`, `Ch.M`, `Chapter M`, `Chapters M–K`, ranges `Ch. M–K` / `Ch. M-K` (both ends),
         slash and comma lists `Ch. 41, 45, 57` / `Ch. 7/12/15`, `Ch. 34 and 37`, `Ch. 55 & 62`;
       - `§M.x`, `§M.x.y` and their lists/ranges (`§45.3–§45.4`, `§14.2.3 and §14.4`);
       - `MM-slug.md` file names/links (only names that exist as chapter files);
       - chapter-internal headings `# Chapter M — …`, `## M.x …`, `### M.x.y …`;
       - README outline lines `### [M. Title](MM-slug.md)`, `- M.x …`, `  - M.x.y …`;
       - the `The 76 chapters` legend count in README and the `seventy-six chapters` count in
         the preface.
     A list item may carry its own section (`Ch. 42 §42.5, 43 §43.5 ◆, 44`): the list continues past it.
     NOT seen (write them so the tool can see them — check_refs.py reports both): a bare number after a
     parenthesis (`Ch. 42 (threading), 43 (spinlocks)` — repeat the `Ch.`), and a citation of someone
     else's book (`*Title*, Ch. 9` would be renumbered — write `ch. 9` / `sec. 7.17` in lower case).
     Numbers <= N are never touched; numbers > MAX are never treated as chapter references; a
     list continuation that is followed by a unit-like token (`Ch. 12, 64 bytes`) is not treated as
     a chapter and is reported for manual review.
  4. In CLAUDE.md, rewrites ONLY the Phase-3 Part-range lines (`- **VII — …** (41–46)`); the
     historical TOC there is deliberately left alone. Chapter/appendix counts in CLAUDE.md are
     printed as TODOs (they are prose, update them by hand).
  5. Rewrites chapter N's closing `*Next: Ch. N+1 — …*` teaser to point at the new chapter
     (prose placeholder — rewrite it by hand) and creates a skeleton chapters/(N+1)-SLUG.md with the
     Part header (copied from chapter N unless --part is given), the Prerequisites/Leads-into block,
     the §1–§8 section headings and a teaser pointing at the (moved) chapter N+2; inserts a matching
     README outline block after chapter N's block.
  6. Appends an ("insert", N) step to STEPS in tools/review/renumber_history.py, the record that lets
     `renumber_history.py when FILE NEEDLE N…` date any number in the book and map it to today's.
  7. Prints a per-file change summary and the list of suspicious lines, then `git diff --stat`.

  --new-part (with --part): the inserted chapter OPENS A NEW PART. The Part's roman numeral R is read
  from --part; every existing Part numbered R or higher is shifted up by one everywhere the book names
  Parts outside code fences — chapter first-line headers (`# Part S — …`), README `## Part S — …`
  headers, the preface's `**Part S — …** (Ch. a–b)` bullets, CLAUDE.md's Phase-3 `- **S — …** (a–b)`
  lines, Appendix G's `**Part S (…)**` map and prose mentions (`Part S`, `Parts I–S`, `Parts (II, S)`);
  a README `## Part R — …` header (with a TODO intro), a preface bullet and a CLAUDE.md range line are
  inserted for the new Part, and the `twelve Parts` counts in README/preface/CLAUDE.md are bumped.
  Every shifted prose line is printed so the wording can be checked by hand.

After running: write the chapter, fill the README outline, rewrite chapter N's teaser and the new
chapter's Prerequisites, update CLAUDE.md's counts, then run the three checks:
tools/review/sweep.py (structure), tools/review/check_refs.py (every reference resolves) and
tools/review/check_hints.py (it resolves to the right topic). All three must exit 0.
"""
import argparse
import os
import re
import subprocess
import sys

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
NUMWORDS = {70: "seventy", 71: "seventy-one", 72: "seventy-two", 73: "seventy-three",
            74: "seventy-four", 75: "seventy-five", 76: "seventy-six", 77: "seventy-seven",
            78: "seventy-eight", 79: "seventy-nine", 80: "eighty", 81: "eighty-one",
            82: "eighty-two", 83: "eighty-three", 84: "eighty-four", 85: "eighty-five",
            86: "eighty-six", 87: "eighty-seven", 88: "eighty-eight", 89: "eighty-nine",
            90: "ninety", 91: "ninety-one", 92: "ninety-two", 93: "ninety-three",
            94: "ninety-four", 95: "ninety-five", 96: "ninety-six", 97: "ninety-seven",
            98: "ninety-eight", 99: "ninety-nine", 100: "one hundred", 101: "one hundred and one",
            102: "one hundred and two", 103: "one hundred and three", 104: "one hundred and four",
            105: "one hundred and five", 106: "one hundred and six", 107: "one hundred and seven",
            108: "one hundred and eight", 109: "one hundred and nine", 110: "one hundred and ten",
            111: "one hundred and eleven", 112: "one hundred and twelve", 113: "one hundred and thirteen",
            114: "one hundred and fourteen", 115: "one hundred and fifteen", 116: "one hundred and sixteen",
            117: "one hundred and seventeen", 118: "one hundred and eighteen", 119: "one hundred and nineteen",
            120: "one hundred and twenty"}

PARTWORDS = {10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
             16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty"}
_ROMAN = [(50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def roman_to_int(r):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50}
    total, prev = 0, 0
    for ch in reversed(r):
        v = vals[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def int_to_roman(n):
    out = ""
    for v, sym in _ROMAN:
        while n >= v:
            out += sym
            n -= v
    return out


PART_LIST = re.compile(r"\b(Parts?)(\s+\(?)([IVXL]+(?:\s*(?:[,/–—-]|,?\s(?:and|or)\s|&)\s*[IVXL]+)*)")
ROMAN_TOK = re.compile(r"[IVXL]+")


class PartShifter:
    """Shift every Part numeral >= first up by one, outside code fences; records the lines it touched."""

    def __init__(self, first):
        self.first = first
        self.touched = []
        self.changed = 0

    def _shift_list(self, lst):
        def rep(mo):
            n = roman_to_int(mo.group(0))
            return int_to_roman(n + 1) if n >= self.first and 1 <= n <= 30 else mo.group(0)
        return ROMAN_TOK.sub(rep, lst)

    def line(self, line, where):
        new = PART_LIST.sub(lambda mo: mo.group(1) + mo.group(2) + self._shift_list(mo.group(3)), line)
        # CLAUDE.md Phase-3 range lines name the Part by numeral alone: `- **XII — Title** (73–84)`
        mo = re.match(r"^(\s*- \*\*)([IVXL]+)( — [^*]+\*\*\s*\(\d+–\d+\))", line if new == line else new)
        if mo and roman_to_int(mo.group(2)) >= self.first:
            new = f"{mo.group(1)}{int_to_roman(roman_to_int(mo.group(2)) + 1)}{mo.group(3)}{(line if new == line else new)[mo.end():]}"
        if new != line:
            self.changed += 1
            self.touched.append((where, new.strip()[:160]))
        return new

    def text(self, text, where):
        out, inf = [], False
        for i, l in enumerate(text.split("\n"), 1):
            if FENCE.match(l):
                inf = not inf
            elif not inf:
                l = self.line(l, f"{where}:{i}")
            out.append(l)
        return "\n".join(out)

    @staticmethod
    def bump_counts(text, nparts):
        old, new = nparts, nparts + 1
        if old in PARTWORDS and new in PARTWORDS:
            text = re.sub(rf"\b{PARTWORDS[old]} Parts\b", f"{PARTWORDS[new]} Parts", text)
            text = re.sub(rf"\b{PARTWORDS[old].capitalize()} Parts\b", f"{PARTWORDS[new].capitalize()} Parts", text)
        return re.sub(rf"\b{old} Parts\b", f"{new} Parts", text)


# Tokens that mean "the number before me was a quantity, not a chapter".
UNIT_GUARD = re.compile(
    r"\s?(?:-?(?:bytes?|bit|byte|way|core|cores|thread|threads|entry|entries|lane|lanes|"
    r"B|KiB|MiB|GiB|KB|MB|GB|ns|µs|us|ms|s|Hz|kHz|MHz|GHz|%|×|x)\b|-[A-Za-z])")

CH_PREFIX = re.compile(r"\b(Ch(?:apter)?s?\.?|Chs\.)(\s| )?(?=\d)")
NUM = re.compile(r"(\d{1,3})((?:\.\d+)*)")
CH_SEP = re.compile(r"(\s?[,/]\s?|\s?[–—-]\s?|\s?&\s?|,?\s(?:and|or)\s)")
# " §42.5", " §42.5–§42.6", " §42.5, §42.7", optionally followed by Appendix O's " ◆" marker
SEC_ATTACH = re.compile(r"(?:\s?§\s?\d{1,3}(?:\.\d+)+(?:\s?[–-]\s?§?\d{1,3}(?:\.\d+)+)?(?:(?:,\s|/)§\d{1,3}(?:\.\d+)+)*)?(?:\s◆)?")
SEC_PREFIX = re.compile(r"§§?\s?(?=\d)")
SEC_NUM = re.compile(r"(\d{1,3})((?:\.\d+)+)")
SEC_SEP = re.compile(r"(\s?[,/]\s?§?|\s?[–—-]\s?§?|\s?&\s?§?|,?\s(?:and|or)\s§?)")


class Renumberer:
    def __init__(self, after, maxch, slugs):
        self.after = after
        self.maxch = maxch
        self.slugs = slugs  # {"46-keeping-the-hot-path-warm.md", ...} (pre-move names)
        self.suspicious = []
        self.changed = 0

    def m(self, n):
        return n + 1 if n > self.after else n

    def is_ch(self, n):
        return 1 <= n <= self.maxch

    # --- the three reference rules -----------------------------------------------------------
    def rewrite_ch_refs(self, line, where):
        out, pos = [], 0
        while True:
            pm = CH_PREFIX.search(line, pos)
            if not pm:
                out.append(line[pos:])
                break
            out.append(line[pos:pm.end()])
            p = pm.end()
            first = True
            while True:
                nm = NUM.match(line, p)
                if not nm:
                    break
                n = int(nm.group(1))
                if not self.is_ch(n):
                    if first:
                        pass  # "Ch. 128"? leave it
                    break
                if not first and UNIT_GUARD.match(line, nm.end()):
                    self.suspicious.append((where, line.strip()[:160]))
                    break
                out.append(str(self.m(n)) + nm.group(2))
                p = nm.end()
                first = False
                # A section attached to the item ("Ch. 42 §42.5, 43 §43.5 ◆, 44") must not end the list:
                # copy it through untouched (rewrite_sec_refs renumbers it afterwards) and keep going.
                # Before 2026-11-10 the list stopped here and every later bare number went stale.
                am = SEC_ATTACH.match(line, p)
                q = am.end() if am else p
                sm = CH_SEP.match(line, q)
                if sm and NUM.match(line, sm.end()):
                    nxt = int(NUM.match(line, sm.end()).group(1))
                    if self.is_ch(nxt) and not (am and am.end() > p and UNIT_GUARD.match(line, NUM.match(line, sm.end()).end())):
                        out.append(line[p:q])
                        out.append(sm.group(0))
                        p = sm.end()
                        continue
                break
            pos = p
        return "".join(out)

    def rewrite_sec_refs(self, line):
        out, pos = [], 0
        while True:
            pm = SEC_PREFIX.search(line, pos)
            if not pm:
                out.append(line[pos:])
                break
            out.append(line[pos:pm.end()])
            p = pm.end()
            while True:
                nm = SEC_NUM.match(line, p)
                if not nm:
                    break
                n = int(nm.group(1))
                if not self.is_ch(n):
                    break
                out.append(str(self.m(n)) + nm.group(2))
                p = nm.end()
                sm = SEC_SEP.match(line, p)
                if sm and SEC_NUM.match(line, sm.end()):
                    out.append(sm.group(0))
                    p = sm.end()
                    continue
                break
            pos = p
        return "".join(out)

    def rewrite_filenames(self, line):
        def rep(mo):
            name = mo.group(0)
            if name not in self.slugs:
                return name
            n = int(mo.group(1))
            return f"{self.m(n):02d}-{mo.group(2)}"
        return re.sub(r"(?<![\w/])(\d{2,3})-([a-z0-9][a-z0-9-]*\.md)\b", rep, line)

    def rewrite_headings(self, line):
        """Returns (line, done). done=True means no other rule may touch the line (the
        '# Chapter N — …' heading would otherwise be remapped a second time by the Ch. rule)."""
        mo = re.match(r"^(#\s+Chapter\s+)(\d+)(\s*[—–-])", line)
        if mo:
            return f"{mo.group(1)}{self.m(int(mo.group(2)))}{mo.group(3)}{line[mo.end():]}", True
        mo = re.match(r"^(#{2,3}\s+)(\d+)((?:\.\d+)+\s)", line)
        if mo and self.is_ch(int(mo.group(2))):
            return f"{mo.group(1)}{self.m(int(mo.group(2)))}{mo.group(3)}{line[mo.end():]}", False
        return line, False

    def rewrite_readme_outline(self, line):
        mo = re.match(r"^(### \[)(\d+)(\. )", line)
        if mo:
            return f"{mo.group(1)}{self.m(int(mo.group(2)))}{mo.group(3)}{line[mo.end():]}"
        mo = re.match(r"^(### )(\d+)(\. )", line)
        if mo:
            return f"{mo.group(1)}{self.m(int(mo.group(2)))}{mo.group(3)}{line[mo.end():]}"
        mo = re.match(r"^(\s*- )(\d+)((?:\.\d+)+ )", line)
        if mo and self.is_ch(int(mo.group(2))):
            return f"{mo.group(1)}{self.m(int(mo.group(2)))}{mo.group(3)}{line[mo.end():]}"
        return line

    def rewrite_counts(self, line):
        old, new = self.maxch, self.maxch + 1
        line = re.sub(rf"\b{old} chapters\b", f"{new} chapters", line)
        if old in NUMWORDS and new in NUMWORDS:
            line = re.sub(rf"\b{NUMWORDS[old]} chapters\b", f"{NUMWORDS[new]} chapters", line)
            line = re.sub(rf"\b{NUMWORDS[old].capitalize()} chapters\b",
                          f"{NUMWORDS[new].capitalize()} chapters", line)
        return line

    # --- per-file drivers -------------------------------------------------------------------
    def process_text(self, text, where, readme=False, headings=True, counts=False):
        out, inf = [], False
        for i, line in enumerate(text.split("\n"), 1):
            orig = line
            if FENCE.match(line):
                inf = not inf
            else:
                done = False
                if not inf:
                    if headings:
                        line, done = self.rewrite_headings(line)
                    if readme:
                        line = self.rewrite_readme_outline(line)
                    if counts:
                        line = self.rewrite_counts(line)
                if not done:
                    line = self.rewrite_ch_refs(line, f"{where}:{i}")
                    line = self.rewrite_sec_refs(line)
                    line = self.rewrite_filenames(line)
            if line != orig:
                self.changed += 1
            out.append(line)
        return "\n".join(out)

    def process_claude_md(self, text):
        def rep(mo):
            a, b = int(mo.group(2)), int(mo.group(3))
            return f"{mo.group(1)}({self.m(a)}–{self.m(b)})"
        out = []
        for line in text.split("\n"):
            new = re.sub(r"^(\s*- \*\*[IVXL]+ — [^*]+\*\*\s*)\((\d+)–(\d+)\)", rep, line)
            if new != line:
                self.changed += 1
            out.append(new)
        return "\n".join(out)


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--after", type=int, required=True, help="the new chapter follows chapter N")
    ap.add_argument("--slug", required=True, help="file slug for the new chapter (no number, no .md)")
    ap.add_argument("--title", required=True, help="chapter title (without the number)")
    ap.add_argument("--part", help="Part header line text (default: copied from chapter N)")
    ap.add_argument("--new-part", action="store_true",
                    help="the new chapter opens a new Part named by --part; shift every later Part's numeral (see doc)")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    chdir = os.path.join(root, "chapters")
    if not os.path.isdir(chdir):
        sys.exit(f"no chapters/ under {root}")
    if not args.dry_run and not args.allow_dirty:
        dirty = run(["git", "status", "--porcelain"], root).strip()
        if dirty:
            sys.exit("refusing to run on a dirty tree:\n" + dirty)

    names = sorted(os.listdir(chdir))
    chap_files = {int(f.split("-", 1)[0]): f for f in names if re.match(r"^\d{2,3}-.*\.md$", f) and not f.startswith("00-")}
    maxch = max(chap_files)
    N = args.after
    if not 1 <= N <= maxch:
        sys.exit(f"--after must be in 1..{maxch}")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", args.slug):
        sys.exit("slug must be lowercase [a-z0-9-]")
    new_name = f"{N + 1:02d}-{args.slug}.md"
    R = Renumberer(N, maxch, set(chap_files.values()))

    # Part header for the new chapter.
    part_line = None
    with open(os.path.join(chdir, chap_files[N]), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("# Part "):
                part_line = line.rstrip("\n")
                break
    if args.part:
        part_line = args.part if args.part.startswith("# ") else "# " + args.part
    if not part_line:
        sys.exit(f"could not find a Part header in {chap_files[N]}; pass --part")
    shifter = None
    if args.new_part:
        if not args.part:
            sys.exit("--new-part requires --part")
        pm = re.match(r"^#\s+Part\s+([IVXL]+)\s*[—–-]\s*(.+)$", part_line)
        if not pm:
            sys.exit("--part must look like 'Part XII — Title' for --new-part")
        new_part_num, new_part_title = roman_to_int(pm.group(1)), pm.group(2).strip()
        # the Part chapter N belongs to must be exactly R-1, or the numbering would not be contiguous
        with open(os.path.join(chdir, chap_files[N]), encoding="utf-8") as fh:
            prev = next((roman_to_int(m.group(1)) for m in (re.match(r"^#\s+Part\s+([IVXL]+)", l) for l in fh) if m), None)
        if prev != new_part_num - 1:
            sys.exit(f"chapter {N} is in Part {int_to_roman(prev) if prev else '?'}; a new Part after it must be Part {int_to_roman((prev or 0) + 1)}")
        shifter = PartShifter(new_part_num)
        print(f"opening new Part {pm.group(1)} — {new_part_title}; Parts {pm.group(1)} and later shift up by one")
    old_next_title = None
    if N + 1 in chap_files:
        with open(os.path.join(chdir, chap_files[N + 1]), encoding="utf-8") as fh:
            for line in fh:
                mo = re.match(r"^#\s+Chapter\s+\d+\s*[—–-]\s*(.+)$", line)
                if mo:
                    old_next_title = mo.group(1).strip()
                    break

    print(f"inserting '{args.title}' as Ch. {N + 1} ({new_name}); chapters {N + 1}..{maxch} -> {N + 2}..{maxch + 1}")
    nparts_before = 0
    rp0 = os.path.join(chdir, "README.md")
    if os.path.exists(rp0):
        nparts_before = len(re.findall(r"^## Part [IVXL]+ ", open(rp0, encoding="utf-8").read(), re.M))

    # 1. renames, highest first
    renames = [(chap_files[m], f"{m + 1:02d}-{chap_files[m].split('-', 1)[1]}") for m in sorted(chap_files) if m > N]
    for old, new in reversed(renames):
        print(f"  git mv {old} -> {new}")
        if not args.dry_run:
            run(["git", "mv", os.path.join("chapters", old), os.path.join("chapters", new)], root)

    # 2. text rewrites
    def rewrite(relpath, **kw):
        path = os.path.join(root, relpath)
        if not os.path.exists(path):
            return
        text = open(path, encoding="utf-8").read()
        before = R.changed
        new = R.process_text(text, relpath, **kw)
        if shifter:
            sb = shifter.changed
            new = shifter.text(new, relpath)
            if kw.get("counts"):
                new = shifter.bump_counts(new, nparts_before)
            n_parts = shifter.changed - sb
        else:
            n_parts = 0
        n = R.changed - before
        if n or n_parts:
            print(f"  {relpath}: {n} line(s)" + (f", {n_parts} Part-numeral line(s)" if n_parts else ""))
        if not args.dry_run and new != text:
            open(path, "w", encoding="utf-8").write(new)

    current = sorted(os.listdir(chdir)) if not args.dry_run else names
    for f in current:
        if re.match(r"^\d{2,3}-.*\.md$", f) and not f.startswith("00-"):
            rewrite(os.path.join("chapters", f))
        elif f.startswith("appendix-") and f.endswith(".md"):
            rewrite(os.path.join("chapters", f), headings=False)
    rewrite("chapters/00-preface.md", headings=False, counts=True)
    rewrite("chapters/README.md", readme=True, headings=False, counts=True)
    if shifter and not args.dry_run:
        ppath = os.path.join(chdir, "00-preface.md")
        if os.path.exists(ppath):
            ptext = open(ppath, encoding="utf-8").read().split("\n")
            prev_roman = int_to_roman(shifter.first - 1)
            idx = next((i for i, l in enumerate(ptext) if re.match(rf"^- \*\*Part {prev_roman} — ", l)), None)
            if idx is None:
                print("  WARNING: preface has no Part bullet for the preceding Part; add the new Part's bullet by hand")
            else:
                ptext[idx + 1:idx + 1] = [f"- **Part {int_to_roman(shifter.first)} — {new_part_title}** (Ch. {N + 1}–{N + 1}): TODO."]
                open(ppath, "w", encoding="utf-8").write("\n".join(ptext))
                print("  00-preface.md: new Part bullet inserted (TODO: describe it)")
    rewrite("tools/book-pdf/README.md", headings=False, counts=True)

    cpath = os.path.join(root, "CLAUDE.md")
    if os.path.exists(cpath):
        text = open(cpath, encoding="utf-8").read()
        before = R.changed
        new = R.process_claude_md(text)
        print(f"  CLAUDE.md: {R.changed - before} Part-range line(s)")
        if shifter:
            new = shifter.text(new, "CLAUDE.md")
            new = shifter.bump_counts(new, nparts_before)
            prev_roman = int_to_roman(shifter.first - 1)
            clines = new.split("\n")
            idx = next((i for i, l in enumerate(clines) if re.match(rf"^\s*- \*\*{prev_roman} — ", l)), None)
            if idx is None:
                print("  WARNING: CLAUDE.md has no Phase-3 range line for the preceding Part; add the new Part's line by hand")
            else:
                j = idx + 1
                while j < len(clines) and clines[j].startswith("    "):
                    j += 1
                clines[j:j] = [f"  - **{int_to_roman(shifter.first)} — {new_part_title}** ({N + 1}–{N + 1}): TODO."]
                new = "\n".join(clines)
            print(f"  CLAUDE.md: Part numerals shifted; new Part {int_to_roman(shifter.first)} range line inserted (TODO: describe it)")
        if not args.dry_run and new != text:
            open(cpath, "w", encoding="utf-8").write(new)

    # 3. chapter N's teaser
    npath = os.path.join(chdir, chap_files[N])
    text = open(npath, encoding="utf-8").read()
    lines = text.rstrip("\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            if re.match(r"^\*Next: Ch\. \d+ ", lines[i]):
                lines[i] = f"*Next: Ch. {N + 1} — {args.title}.*"
                print(f"  {chap_files[N]}: teaser now points at Ch. {N + 1} (TODO: rewrite the teaser prose)")
                if not args.dry_run:
                    open(npath, "w", encoding="utf-8").write("\n".join(lines) + "\n")
            else:
                print(f"  WARNING: {chap_files[N]} has no '*Next: Ch. …*' teaser as its last line")
            break

    # 4. skeleton chapter + README block
    k = N + 1
    nxt = f"Ch. {k + 1} — {old_next_title}" if old_next_title else "Appendix A"
    skeleton = f"""{part_line}

# Chapter {k} — {args.title}

> **Prerequisites:** TODO.
>
> **Leads into:** TODO.

---

## {k}.1 Why it matters: TODO

TODO

## {k}.2 Mental model: TODO

TODO

## {k}.3 Measure it: TODO

TODO

## {k}.4 Techniques

### {k}.4.1 TODO

TODO

## {k}.5 Pitfalls & anti-patterns: TODO

TODO

## {k}.6 Exercises & checklist

TODO

## {k}.7 References

TODO

## {k}.8 Additional Reading

TODO

---

*Next: {nxt}.*
"""
    readme_block = f"""### [{k}. {args.title}]({new_name})
- {k}.1 Why it matters: TODO
- {k}.2 Mental model: TODO
- {k}.3 Measure it: TODO
- {k}.4 Techniques
  - {k}.4.1 TODO
- {k}.5 Pitfalls & anti-patterns: TODO
- {k}.6 Exercises & checklist

"""
    print(f"  creating skeleton chapters/{new_name} and its README outline block")
    if not args.dry_run:
        open(os.path.join(chdir, new_name), "w", encoding="utf-8").write(skeleton)
        run(["git", "add", os.path.join("chapters", new_name)], root)
        rpath = os.path.join(chdir, "README.md")
        rtext = open(rpath, encoding="utf-8").read()
        rlines = rtext.split("\n")
        start = next((i for i, l in enumerate(rlines) if re.match(rf"^### \[?{N}\. ", l)), None)
        if start is None:
            print(f"  WARNING: README has no block for chapter {N}; add the outline block by hand")
        else:
            end = next((i for i in range(start + 1, len(rlines)) if rlines[i].startswith("### ") or rlines[i].startswith("## ")), len(rlines))
            block = readme_block.rstrip("\n").split("\n") + [""]
            if shifter:
                block = ["---", "", f"## {part_line.lstrip('# ').strip()}", "", "*TODO: one-line Part intro.*", ""] + block
                print(f"  README.md: new '## {part_line.lstrip('# ').strip()}' header inserted before the chapter block (TODO: intro)")
            rlines[end:end] = block
            open(rpath, "w", encoding="utf-8").write("\n".join(rlines))

    # 5. renumber_history.py step chain
    spath = os.path.join(root, "tools/review/renumber_history.py")
    if os.path.exists(spath):
        stext = open(spath, encoding="utf-8").read()
        marker = "    # INSERT_STEPS_END"
        if marker in stext:
            stext = stext.replace(marker, f'    ("insert", {N}),  # Ch. {k} {args.slug}\n{marker}')
            print(f"  renumber_history.py: appended ('insert', {N}) step")
            if not args.dry_run:
                open(spath, "w", encoding="utf-8").write(stext)
        else:
            print("  WARNING: renumber_history.py has no INSERT_STEPS_END marker; add the step by hand")

    # 6. summary
    print(f"\n{R.changed} line(s) rewritten in total.")
    if shifter and shifter.touched:
        print(f"\n{len(shifter.touched)} Part-numeral line(s) shifted (check the wording by hand):")
        for where, ctx in shifter.touched:
            print(f"  {where}: {ctx}")
    if R.suspicious:
        print(f"\n{len(R.suspicious)} suspicious list continuation(s) NOT remapped (review by hand):")
        for where, ctx in R.suspicious:
            print(f"  {where}: {ctx}")
    print("\nTODO by hand: write the chapter; fill its README outline; rewrite chapter N's teaser prose and the new "
          "chapter's Prerequisites/Leads-into; update the chapter count and Part ranges in CLAUDE.md's Phase-3 block; "
          "update the next chapter's Prerequisites if it should cite the new one.")
    if not args.dry_run:
        print("\n" + run(["git", "diff", "--stat", "HEAD"], root))


if __name__ == "__main__":
    main()
