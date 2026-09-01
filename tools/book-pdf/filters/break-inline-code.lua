-- break-inline-code.lua
--
-- Pandoc renders inline `code` as \texttt{...} with non-breaking spaces and no break
-- points, so long inline code (e.g. `perf stat -e cycles,instructions,...` or
-- `MAP_SHARED/PRIVATE/ANONYMOUS/...`) overflows the right margin (Overfull \hbox).
--
-- This filter re-emits inline code as a \texttt{} in which:
--   * spaces are ordinary (breakable) interword spaces, and
--   * a break opportunity (\allowbreak) is inserted after common separators
--     ( / _ , - . : ; = | ),
-- so long inline code wraps to the text width instead of overflowing. Breaks are only
-- *taken* when needed, so short inline code is unaffected.

local SPECIAL = {
  ["\\"] = "\\textbackslash{}",
  ["{"] = "\\{",
  ["}"] = "\\}",
  ["$"] = "\\$",
  ["&"] = "\\&",
  ["#"] = "\\#",
  ["%"] = "\\%",
  ["_"] = "\\_",
  ["~"] = "\\textasciitilde{}",
  ["^"] = "\\textasciicircum{}",
}

local function esc(ch)
  return SPECIAL[ch] or ch
end

-- Break opportunities after common separators/operators so long inline code (feature-
-- test macros like `__has_cpp_attribute(likely)`, comparisons like `>= 202202`, and
-- qualified names like `boost::container::flat_map`) can wrap in narrow table cells.
local function is_break_after(ch)
  return ch:match("[/_,%-%.:;=|(){}<>+]") ~= nil
end

function Code(el)
  local pieces = {}
  local run = 0 -- characters since the last break opportunity
  for _, cp in utf8.codes(el.text) do
    local ch = utf8.char(cp)
    if ch == " " then
      pieces[#pieces + 1] = " " -- breakable interword space
      run = 0
    else
      -- Inside a long unbroken run (e.g. `multidimensional`) with no separator to break
      -- at, allow a break every 10 chars. \allowbreak inserts no hyphen and only breaks
      -- when the line is actually overfull, so short inline code is never split.
      if run >= 10 then
        pieces[#pieces + 1] = "\\allowbreak{}"
        run = 0
      end
      pieces[#pieces + 1] = esc(ch)
      run = run + 1
      if is_break_after(ch) then
        pieces[#pieces + 1] = "\\allowbreak{}"
        run = 0
      end
    end
  end
  return pandoc.RawInline("latex", "\\texttt{" .. table.concat(pieces) .. "}")
end
