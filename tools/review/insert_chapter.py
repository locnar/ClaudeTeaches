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
  6. Appends an ("insert", N) step to STEPS in tools/review/stale_crossrefs.py so the stale-reference
     heuristic keeps mapping pre-Phase-3 numbers through this insertion.
  7. Prints a per-file change summary and the list of suspicious lines, then `git diff --stat`.

After running: write the chapter, fill the README outline, rewrite chapter N's teaser and the new
chapter's Prerequisites, update CLAUDE.md's counts, then run tools/review/sweep.py and
tools/review/stale_crossrefs.py.
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
            98: "ninety-eight", 99: "ninety-nine"}

# Tokens that mean "the number before me was a quantity, not a chapter".
UNIT_GUARD = re.compile(
    r"\s?(?:-?(?:bytes?|bit|byte|way|core|cores|thread|threads|entry|entries|lane|lanes|"
    r"B|KiB|MiB|GiB|KB|MB|GB|ns|µs|us|ms|s|Hz|kHz|MHz|GHz|%|×|x)\b|-[A-Za-z])")

CH_PREFIX = re.compile(r"\b(Ch(?:apter)?s?\.?|Chs\.)(\s| )?(?=\d)")
NUM = re.compile(r"(\d{1,3})((?:\.\d+)*)")
CH_SEP = re.compile(r"(\s?[,/]\s?|\s?[–—-]\s?|\s?&\s?|,?\s(?:and|or)\s)")
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
                sm = CH_SEP.match(line, p)
                if sm and NUM.match(line, sm.end()):
                    nxt = int(NUM.match(line, sm.end()).group(1))
                    if self.is_ch(nxt):
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
        return re.sub(r"(?<![\w/])(\d{2})-([a-z0-9][a-z0-9-]*\.md)\b", rep, line)

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
    chap_files = {int(f[:2]): f for f in names if re.match(r"^\d{2}-.*\.md$", f) and not f.startswith("00-")}
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
    old_next_title = None
    if N + 1 in chap_files:
        with open(os.path.join(chdir, chap_files[N + 1]), encoding="utf-8") as fh:
            for line in fh:
                mo = re.match(r"^#\s+Chapter\s+\d+\s*[—–-]\s*(.+)$", line)
                if mo:
                    old_next_title = mo.group(1).strip()
                    break

    print(f"inserting '{args.title}' as Ch. {N + 1} ({new_name}); chapters {N + 1}..{maxch} -> {N + 2}..{maxch + 1}")

    # 1. renames, highest first
    renames = [(chap_files[m], f"{m + 1:02d}-{chap_files[m][3:]}") for m in sorted(chap_files) if m > N]
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
        n = R.changed - before
        if n:
            print(f"  {relpath}: {n} line(s)")
        if not args.dry_run and new != text:
            open(path, "w", encoding="utf-8").write(new)

    current = sorted(os.listdir(chdir)) if not args.dry_run else names
    for f in current:
        if re.match(r"^\d{2}-.*\.md$", f) and not f.startswith("00-"):
            rewrite(os.path.join("chapters", f))
        elif f.startswith("appendix-") and f.endswith(".md"):
            rewrite(os.path.join("chapters", f), headings=False)
    rewrite("chapters/00-preface.md", headings=False, counts=True)
    rewrite("chapters/README.md", readme=True, headings=False, counts=True)
    rewrite("tools/book-pdf/README.md", headings=False, counts=True)

    cpath = os.path.join(root, "CLAUDE.md")
    if os.path.exists(cpath):
        text = open(cpath, encoding="utf-8").read()
        before = R.changed
        new = R.process_claude_md(text)
        print(f"  CLAUDE.md: {R.changed - before} Part-range line(s)")
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
            rlines[end:end] = readme_block.rstrip("\n").split("\n") + [""]
            open(rpath, "w", encoding="utf-8").write("\n".join(rlines))

    # 5. stale_crossrefs.py step chain
    spath = os.path.join(root, "tools/review/stale_crossrefs.py")
    if os.path.exists(spath):
        stext = open(spath, encoding="utf-8").read()
        marker = "    # INSERT_STEPS_END"
        if marker in stext:
            stext = stext.replace(marker, f'    ("insert", {N}),  # Ch. {k} {args.slug}\n{marker}')
            print(f"  stale_crossrefs.py: appended ('insert', {N}) step")
            if not args.dry_run:
                open(spath, "w", encoding="utf-8").write(stext)
        else:
            print("  WARNING: stale_crossrefs.py has no INSERT_STEPS_END marker; add the step by hand")

    # 6. summary
    print(f"\n{R.changed} line(s) rewritten in total.")
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
