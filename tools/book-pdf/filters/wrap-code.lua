-- wrap-code.lua
--
-- Render every code block / ASCII diagram through fvextra's Verbatim with line
-- breaking and a small font. Two reasons:
--   1. The chapters contain Unicode box-drawing diagrams (─ │ ┌ ► ▲ …). Under
--      xelatex/lualatex, fvextra's Verbatim renders these correctly given a mono
--      font that has the glyphs (Menlo on macOS, DejaVu Sans Mono on Linux);
--      the `listings` package and plain `verbatim` do not handle them gracefully.
--   2. `breaklines` makes over-wide code/diagram lines wrap instead of overflowing
--      the margin, so everything fits the page.
--
-- Syntax highlighting is intentionally dropped in favour of robust, uniform,
-- always-fitting monochrome code — appropriate for a print book.

local function verbatim(text)
  -- Content is kept fully literal (no commandchars), so backslashes/braces in code
  -- and diagrams are shown verbatim.
  local opts = "fontsize=\\footnotesize,breaklines=true,breakanywhere=true,breaksymbolleft={}"
  return pandoc.RawBlock(
    "latex",
    "\\begin{Verbatim}[" .. opts .. "]\n" .. text .. "\n\\end{Verbatim}"
  )
end

function CodeBlock(el)
  return verbatim(el.text)
end
