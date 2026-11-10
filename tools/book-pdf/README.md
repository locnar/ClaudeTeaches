# Book PDF toolchain

Renders the chapter markdown in `../../chapters/` into a single print-ready PDF book:
a title page, a table of contents, parts and chapters, with **every chapter starting on a
right-hand (recto) page** and **all tables and ASCII diagrams sized to fit the page**.

## Quick start

```bash
cd tools/book-pdf
make            # -> ../../low-latency-cpp-on-linux.pdf  (repo top level)
make preview    # build and open it (macOS)
```

or directly:

```bash
./build.sh [output.pdf]
```

## Prerequisites

1. **Pandoc 2.10+** — `brew install pandoc` (macOS) / `apt-get install pandoc` (Linux).
2. **A Unicode-capable LaTeX engine.** Pick one:
   - **Tectonic** (lightest; a single binary that auto-downloads what it needs):
     `brew install tectonic`, or on Linux the static binary from the project's GitHub releases
     (`tectonic-<version>-x86_64-unknown-linux-musl.tar.gz`; put `tectonic` on `PATH` and run
     `ENGINE=tectonic ./build.sh`)
   - **XeLaTeX** via MacTeX/BasicTeX:
     `brew install --cask basictex` then
     `sudo tlmgr update --self && sudo tlmgr install fvextra fancyhdr emptypage microtype seqsplit etoolbox booktabs newunicodechar`
   - Linux: `apt-get install texlive-xetex texlive-latex-extra fonts-dejavu`
3. **A monospace font with box-drawing glyphs** (the diagrams use `─ │ ┌ ► ▲ …`):
   - macOS: **Menlo** (preinstalled) — used by default.
   - Linux: **DejaVu Sans Mono** (`fonts-dejavu`) — used by default.

`build.sh` checks for pandoc and an engine and prints install hints if either is missing.

## Options (environment variables)

| Variable    | Default                              | Meaning                                   |
|-------------|--------------------------------------|-------------------------------------------|
| `MONOFONT`  | Menlo (macOS) / DejaVu Sans Mono     | monospace font for code & diagrams        |
| `MAINFONT`  | engine default (Latin Modern)        | body text font                            |
| `TOC_DEPTH` | `1`                                  | `1` = parts+chapters, `2` = +sections (pandoc `--toc-depth`; `0` is rejected) |
| `AUTHOR`    | `git config user.name`               | name on the title page                    |
| `ENGINE`    | first of xelatex/lualatex/tectonic   | force a specific PDF engine               |

Example: `TOC_DEPTH=2 MONOFONT="DejaVu Sans Mono" ./build.sh ~/Desktop/book.pdf`

## How it works

```
chapters/*.md ──► preprocess.py ──► build/book.md ──► pandoc (+filters,+LaTeX) ──► PDF
```

- **`preprocess.py`** concatenates the files in reading order (preface, Ch. 1–120,
  Appendices A–S; discovered from the `chapters/` directory, not hard-coded) and fixes them up for LaTeX:
  - emits each **Part divider once** (the source repeats the Part header in every file);
  - rewrites `# Chapter N — Title` → `# Title` and strips manual section numbers
    (`## 48.4 …` → `## …`) so **LaTeX auto-numbers** them — the numbers come out identical
    to the manual ones, so in-prose cross-references like "§65.4.1" stay correct; a `##`/`###`
    heading that carries *no* manual number is emitted `{.unnumbered}` so it cannot shift the
    numbering of the headings after it;
  - emits the preface unnumbered and switches the appendices to letter numbering
    (`\appendix`) under an "Appendices" part;
  - skips all of this inside fenced code blocks, so `#` comments and diagrams are untouched.
- **`filters/fit-tables.lua`** gives every table content-proportional relative column
  widths, so wide tables **wrap and fit** the text block (Pandoc pipe tables otherwise
  overflow). `header.tex` also renders tables at `\footnotesize`.
- **`filters/break-table-cells.lua`** inserts a break opportunity after every `/` in text
  inside table cells only, so slash-compounds like `Spectre/retpoline` wrap instead of
  overflowing the column.
- **`filters/wrap-code.lua`** renders code/diagrams through fvextra's `Verbatim` with
  `breaklines` and a small font, so long lines wrap at a space and **Unicode box-drawing
  renders correctly** (which `listings`/plain `verbatim` do not). A block that contains a run
  with no space in it wider than the line (a long rule in a diagram, a long path) also gets
  `breakanywhere`; it is not a global default because of its memory cost (see Notes).
- **`filters/break-inline-code.lua`** re-emits inline `code` as breakable `\texttt`, so a
  long identifier or flag in running text can wrap at the line end instead of sticking out
  into the margin.
- **`assets/`** holds the Pandoc settings (`metadata.yaml`), the LaTeX preamble
  (`header.tex` — table sizing, code wrapping, running heads, blank-page handling), and
  the `titlepage.tex` template.
- **Recto chapter openings** come from the `book` class with the `openright` option
  (a blank verso page is inserted when needed; `emptypage` keeps those blanks truly blank).

## Inspecting the intermediate markdown

You don't need LaTeX to check the preprocessing step:

```bash
make book.md            # writes build/book.md
```

## Notes

- The benchmark numbers in the chapters are **representative** (see "About the benchmark numbers"
  in `chapters/00-preface.md` and the *Status / Workflow* section of `CLAUDE.md`); the PDF
  reproduces them as written.
- Syntax highlighting is intentionally dropped in favour of uniform, always-fitting
  monochrome code — appropriate for print. Remove `wrap-code.lua` (and, if you want inline
  code unbroken too, `break-inline-code.lua`) from `build.sh` to get Pandoc's default
  highlighting back (at the cost of Unicode-diagram robustness).
- A full build produces a very long PDF: **2,188 pages** at 7 × 10 in for the 120 chapters and
  19 appendices (measured 2026-11-10, Tectonic 0.15.0, DejaVu Sans Mono; ~7 MB). It takes under
  two minutes on a 4-core machine; the first Tectonic run also downloads its packages and fonts.
- **TeX's main memory is the limit to watch.** XeTeX (and therefore Tectonic, where it cannot be
  raised) has a fixed 5,000,000-word main memory. With `breakanywhere` enabled on every code
  block the book ran out four fifths of the way through (`TeX capacity exceeded, sorry [main
  memory size=5000000]`); `wrap-code.lua` therefore enables it only for the few dozen blocks
  that have an unbreakable run wider than the page. If the error returns as the book grows,
  look for a new construct that is applied to every block or every paragraph.
- A glyph the body font lacks is dropped silently from the PDF; the build log says
  `could not represent character`. `grep -o 'could not represent character "[^"]*"' <log> | sort | uniq -c`
  must print nothing — add a `\newunicodechar` line to `assets/header.tex` for anything it lists.
