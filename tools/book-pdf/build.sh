#!/usr/bin/env bash
#
# Build the book as a PDF.
#
#   ./build.sh [output.pdf]
#
# Environment overrides:
#   MONOFONT   monospace font (default: Menlo on macOS, "DejaVu Sans Mono" on Linux)
#   MAINFONT   body font (default: the engine's default — Latin Modern)
#   TOC_DEPTH  0 = parts+chapters, 1 = +sections (default 1)
#   AUTHOR     name on the title page (default: git user.name)
#   ENGINE     force a pdf engine (xelatex|lualatex|tectonic)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CHAPTERS="$ROOT/chapters"
BUILD="$HERE/build"
OUT="${1:-$ROOT/low-latency-cpp-on-linux.pdf}"
mkdir -p "$BUILD"

# ---- dependency checks ------------------------------------------------------------
if ! command -v pandoc >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: pandoc not found.
  macOS:  brew install pandoc
  Linux:  apt-get install pandoc   (or download from https://pandoc.org/installing.html)
Pandoc 2.10+ is required (for the table column-width filter).
EOF
  exit 1
fi

ENGINE="${ENGINE:-}"
if [ -z "$ENGINE" ]; then
  for e in xelatex lualatex tectonic; do
    if command -v "$e" >/dev/null 2>&1; then ENGINE="$e"; break; fi
  done
fi
if [ -z "$ENGINE" ]; then
  cat >&2 <<'EOF'
ERROR: no Unicode-capable LaTeX engine found (need xelatex, lualatex, or tectonic).
  Lightest option (self-contained, auto-fetches packages):
      macOS/Linux:  brew install tectonic
  Full TeX (xelatex):
      macOS:  brew install --cask mactex-no-gui      (large) or  brew install --cask basictex
              then: sudo tlmgr update --self && sudo tlmgr install fvextra fancyhdr emptypage \
                          microtype seqsplit etoolbox booktabs
      Linux:  apt-get install texlive-xetex texlive-latex-extra fonts-dejavu
EOF
  exit 1
fi

# ---- fonts (must contain box-drawing glyphs for the ASCII diagrams) ----------------
case "$(uname -s)" in
  Darwin) MONO="${MONOFONT:-Menlo}" ;;
  *)      MONO="${MONOFONT:-DejaVu Sans Mono}" ;;
esac
MAIN="${MAINFONT:-}"
TOC_DEPTH="${TOC_DEPTH:-1}"
AUTHOR="${AUTHOR:-$(git -C "$ROOT" config user.name 2>/dev/null || echo 'Anonymous')}"

echo ">> preprocessing chapters -> $BUILD/book.md"
python3 "$HERE/preprocess.py" "$CHAPTERS" "$BUILD/book.md"

echo ">> rendering title page (author: $AUTHOR)"
sed -e "s/{{AUTHOR}}/$(printf '%s' "$AUTHOR" | sed 's/[&/\]/\\&/g')/g" \
    -e "s/{{DATE}}/$(date '+%B %d, %Y')/g" \
    "$HERE/assets/titlepage.tex" > "$BUILD/titlepage.tex"

echo ">> pandoc (engine: $ENGINE, mono: $MONO, toc-depth: $TOC_DEPTH)"
ARGS=(
  "$BUILD/book.md"
  --from=markdown+raw_tex
  --pdf-engine="$ENGINE"
  --top-level-division=chapter
  --number-sections
  --toc --toc-depth="$TOC_DEPTH"
  --metadata-file="$HERE/assets/metadata.yaml"
  --include-in-header="$HERE/assets/header.tex"
  --include-before-body="$BUILD/titlepage.tex"
  --lua-filter="$HERE/filters/fit-tables.lua"
  --lua-filter="$HERE/filters/break-table-cells.lua"
  --lua-filter="$HERE/filters/wrap-code.lua"
  --lua-filter="$HERE/filters/break-inline-code.lua"
  -V monofont="$MONO"
  -o "$OUT"
)
[ -n "$MAIN" ] && ARGS+=( -V mainfont="$MAIN" )

pandoc "${ARGS[@]}"
echo ">> done: $OUT"
