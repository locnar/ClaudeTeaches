-- fit-tables.lua
--
-- Make every table fit the text block. Pandoc pipe tables carry no column-width
-- information, so LaTeX lays them out at their natural width and wide tables overflow
-- the margin. We assign each column a *relative* width proportional to the longest
-- cell in that column (normalised to ~0.96 of the text width). With explicit relative
-- widths, Pandoc emits wrapping `p{...}` columns inside longtable, so text wraps and the
-- table always fits. Combined with the \footnotesize applied to longtable in header.tex.

local stringify = pandoc.utils.stringify

local function cell_length(cell)
  local ok, s = pcall(stringify, cell.contents)
  if not ok or not s then return 1 end
  local n = (utf8 and utf8.len(s)) or #s
  return n or 1
end

local function scan_rows(rows, maxlen, ncols)
  for _, row in ipairs(rows) do
    local cells = row.cells or row
    for i = 1, ncols do
      local c = cells[i]
      if c then
        local l = cell_length(c)
        if l > maxlen[i] then maxlen[i] = l end
      end
    end
  end
end

function Table(tbl)
  local ncols = #tbl.colspecs
  if ncols == 0 then return nil end

  local maxlen = {}
  for i = 1, ncols do maxlen[i] = 3 end

  if tbl.head and tbl.head.rows then
    scan_rows(tbl.head.rows, maxlen, ncols)
  end
  for _, body in ipairs(tbl.bodies) do
    if body.body then scan_rows(body.body, maxlen, ncols) end
    if body.head then scan_rows(body.head, maxlen, ncols) end
  end

  -- Flatten the distribution: a column whose content is much shorter than another's
  -- (e.g. a numeric "~Latency" column next to a long "Trading relevance" column) would
  -- otherwise be starved so narrow that its *header* collides with the next column.
  -- Adding a constant to each weight gives short columns a fairer minimum share while
  -- still letting long-content columns dominate.
  local PAD = 8
  local weight = {}
  local total = 0
  for i = 1, ncols do
    weight[i] = maxlen[i] + PAD
    total = total + weight[i]
  end
  if total == 0 then return nil end

  -- Pandoc already subtracts inter-column padding from \linewidth before applying
  -- these fractions, so we can use almost the full width.
  local usable = 0.99
  local min_w = 0.06
  -- Especially wide tables (many columns — e.g. the C++ feature matrix's 6-column
  -- Feature/Paper/GCC/Clang/Macro/Fallback rows) can't fit at \footnotesize even with
  -- proportional widths and cell breaking. Drop those to \scriptsize with tighter
  -- inter-column padding, and give short columns a smaller floor so the long content
  -- columns get the room they need.
  local wide = ncols >= 6
  if wide then min_w = 0.045 end
  -- First pass: proportional widths with a per-column floor.
  local w = {}
  local wsum = 0
  for i = 1, ncols do
    w[i] = (weight[i] / total) * usable
    if w[i] < min_w then w[i] = min_w end
    wsum = wsum + w[i]
  end
  -- The floor can push the total above `usable` (→ a uniform per-row overflow); scale
  -- back down so the column fractions always sum to the target and the table fits.
  if wsum > usable then
    local s = usable / wsum
    for i = 1, ncols do w[i] = w[i] * s end
  end
  for i = 1, ncols do
    tbl.colspecs[i] = { tbl.colspecs[i][1], w[i] }
  end
  if wide then
    return {
      pandoc.RawBlock("latex",
        "\\renewcommand{\\tablefontsize}{\\scriptsize}\\renewcommand{\\tablecolsep}{3pt}"),
      tbl,
      pandoc.RawBlock("latex",
        "\\renewcommand{\\tablefontsize}{\\footnotesize}\\renewcommand{\\tablecolsep}{4pt}"),
    }
  end
  return tbl
end
