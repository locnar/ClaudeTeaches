#!/usr/bin/env python3
"""
Preprocess the chapter markdown into a single book.md that Pandoc can turn into a
proper LaTeX book.

The source files are authored for the web/README index, not for LaTeX, so several
transforms are needed:

  * Each chapter file repeats its Part header (`# Part X — Name`). In a book that
    Part divider must appear exactly once, when the Part changes. We strip the per-file
    Part line and inject a raw `\\part{...}` only at Part boundaries.
  * Both the Part header and the Chapter title are level-1 (`#`) headings in the source.
    We drop the Part line and rewrite `# Chapter N — Title` to just `# Title` so that,
    with `--top-level-division=chapter`, it becomes a `\\chapter` that LaTeX auto-numbers.
  * Section/subsection headings carry manual numbers (`## 48.4 ...`, `### 48.4.1 ...`).
    We strip the manual number and let LaTeX number them — by construction the LaTeX
    numbers match the manual ones (48 is the 48th chapter, etc.), so the in-prose
    cross-references like "§48.4.1" stay correct.
  * The preface is emitted unnumbered; appendices are switched to letter numbering with
    a raw `\\appendix` and grouped under an "Appendices" part.
  * Thematic breaks (`---`) are dropped (the chapter/section structure separates content).

All heading transforms are skipped inside fenced code blocks, so `#` comments and
diagram content are never mangled.
"""

import os
import re
import sys

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
PART = re.compile(r"^#\s+Part\s+[IVXLCDM]+\s*[—–-]\s*(.+?)\s*$")
CHAP = re.compile(r"^#\s+Chapter\s+\d+\s*[—–-]\s*(.+?)\s*$")
APP = re.compile(r"^#\s+Appendix\s+[A-Z]\s*[—–-]\s*(.+?)\s*$")
# A leading section label: 5.1 / 48.4.1 / A.1 / A.2.1
SEC = re.compile(r"^(#{2,3})\s+(?:\d+(?:\.\d+)*|[A-Z]\.\d+(?:\.\d+)*)\s+(.+?)\s*$")
HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
HR = re.compile(r"^-{3,}\s*$")


def tex_escape(s):
    return (
        s.replace("\\", "\\textbackslash{}")
        .replace("&", "\\&")
        .replace("%", "\\%")
        .replace("#", "\\#")
        .replace("_", "\\_")
        .replace("$", "\\$")
    )


def raw_latex(*lines):
    return ["", "```{=latex}", *lines, "```", ""]


def chapter_files(chapters_dir):
    """Ordered list of (path, kind) for the whole book."""
    names = os.listdir(chapters_dir)
    numbered = sorted(
        (n for n in names if re.match(r"^\d{2}-.*\.md$", n) and not n.startswith("00-")),
        key=lambda n: int(n[:2]),
    )
    appendices = sorted(n for n in names if n.startswith("appendix-") and n.endswith(".md"))
    order = []
    if "00-preface.md" in names:
        order.append(("00-preface.md", "preface"))
    order += [(n, "chapter") for n in numbered]
    order += [(n, "appendix") for n in appendices]
    return [(os.path.join(chapters_dir, n), kind) for n, kind in order]


def emit_preface(path, out):
    in_fence = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if FENCE.match(line):
                in_fence = not in_fence
                out.append(line)
                continue
            if not in_fence:
                if HR.match(line):
                    continue
                m = HEAD.match(line)
                if m and "{." not in line:
                    out.append(f"{m.group(1)} {m.group(2)} {{.unnumbered}}")
                    continue
            out.append(line)


def emit_chapter(path, state, out):
    in_fence = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if FENCE.match(line):
                in_fence = not in_fence
                out.append(line)
                continue
            if not in_fence:
                m = PART.match(line)
                if m:
                    name = m.group(1)
                    if name != state["part"]:
                        state["part"] = name
                        out.extend(raw_latex("\\part{" + tex_escape(name) + "}"))
                    continue  # drop the per-file Part line
                m = CHAP.match(line)
                if m:
                    out.append("# " + m.group(1))
                    continue
                if HR.match(line):
                    continue
                m = SEC.match(line)
                if m:
                    out.append(f"{m.group(1)} {m.group(2)}")
                    continue
            out.append(line)


def emit_appendix(path, state, out):
    if not state["appendix_started"]:
        state["appendix_started"] = True
        out.extend(
            raw_latex(
                "\\appendix",
                "\\part*{Appendices}",
                "\\addcontentsline{toc}{part}{Appendices}",
            )
        )
    in_fence = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if FENCE.match(line):
                in_fence = not in_fence
                out.append(line)
                continue
            if not in_fence:
                m = APP.match(line)
                if m:
                    out.append("# " + m.group(1))
                    continue
                if HR.match(line):
                    continue
                m = SEC.match(line)
                if m:
                    out.append(f"{m.group(1)} {m.group(2)}")
                    continue
            out.append(line)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: preprocess.py <chapters-dir> [output.md]")
    chapters_dir = sys.argv[1]
    out = []
    state = {"part": None, "appendix_started": False}
    files = chapter_files(chapters_dir)
    if not files:
        sys.exit(f"no chapter files found in {chapters_dir}")
    for path, kind in files:
        if kind == "preface":
            emit_preface(path, out)
        elif kind == "chapter":
            emit_chapter(path, state, out)
        else:
            emit_appendix(path, state, out)
        out.append("")  # blank line between files
    text = "\n".join(out) + "\n"
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
