-- wrap-code.lua
--
-- Render every code block / ASCII diagram through fvextra's Verbatim with line
-- breaking and a small font. Two reasons:
--   1. The chapters contain Unicode box-drawing diagrams (─ │ ┌ ► ▲ …). Under
--      xelatex/lualatex, fvextra's Verbatim renders these correctly given a mono
--      font that has the glyphs (Menlo on macOS, DejaVu Sans Mono on Linux);
--      the `listings` package and plain `verbatim` do not handle them gracefully.
--   2. `breaklines` (and, where a block needs it, `breakanywhere`) makes over-wide
--      code/diagram lines wrap instead of overflowing the margin, so everything fits the page.
--
-- Syntax highlighting is intentionally dropped in favour of robust, uniform,
-- always-fitting monochrome code — appropriate for a print book.

-- How many monospace characters fit on one line of the text block at \footnotesize
-- (5.15 in text width, DejaVu Sans Mono / Menlo advance 0.602 em at 9 pt ≈ 68; a margin is kept).
local LINE_CAPACITY = 64

-- The longest run of non-space characters in the block, in characters (not bytes).
local function longest_unbreakable_run(text)
  local longest = 0
  for run in text:gmatch("[^%s]+") do
    local n = (utf8 and utf8.len(run)) or #run
    if n > longest then longest = n end
  end
  return longest
end

local function verbatim(text)
  -- Content is kept fully literal (no commandchars), so backslashes/braces in code
  -- and diagrams are shown verbatim.
  --
  -- `breaklines` wraps an over-wide line at a space. `breakanywhere` is added ONLY when
  -- the block has a run with no space in it that is wider than the line (a long rule in a
  -- diagram, a long path or hex string): fvextra's break-anywhere scanner costs TeX main
  -- memory for every character it handles, and with it switched on for all ~750 blocks the
  -- book overran XeTeX's fixed 5,000,000-word memory four fifths of the way through
  -- ("TeX capacity exceeded, sorry [main memory size=5000000]", 2026-11-10, 120 chapters).
  local opts = "fontsize=\\footnotesize,breaklines=true,breaksymbolleft={}"
  if longest_unbreakable_run(text) > LINE_CAPACITY then
    opts = opts .. ",breakanywhere=true"
  end
  return pandoc.RawBlock(
    "latex",
    "\\begin{Verbatim}[" .. opts .. "]\n" .. text .. "\n\\end{Verbatim}"
  )
end

function CodeBlock(el)
  return verbatim(el.text)
end
