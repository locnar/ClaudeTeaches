-- break-table-cells.lua
--
-- Inside table cells, narrow columns can't break slash-compounds like "Spectre/retpoline"
-- or "maker/taker" (a slash is not a hyphenation point), so they overflow the cell.
-- This filter inserts a break opportunity (\allowbreak) after every "/" in text *inside
-- tables only* — prose like "A/B feed" elsewhere is left untouched. Combined with
-- \RaggedRight (hyphenation) in header.tex, table cells now wrap to fit.

local function break_on_slash(el)
  if not el.text:find("/") then return nil end
  local out = {}
  local first = true
  for seg in (el.text .. "/"):gmatch("([^/]*)/") do
    if not first then
      out[#out + 1] = pandoc.Str("/")
      out[#out + 1] = pandoc.RawInline("latex", "\\allowbreak{}")
    end
    if seg ~= "" then out[#out + 1] = pandoc.Str(seg) end
    first = false
  end
  -- handle a trailing slash (rare): the loop above appends segments between slashes;
  -- a literal trailing "/" in the source is preserved by the sentinel split.
  return out
end

function Table(tbl)
  return pandoc.walk_block(tbl, { Str = break_on_slash })
end
