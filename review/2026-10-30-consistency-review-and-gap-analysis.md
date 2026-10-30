# Consistency & Accuracy Review + Gap Analysis — 2026-10-30

**Scope.** All 76 chapters, 12 appendices, the preface, `chapters/README.md`, `CLAUDE.md` and `tools/book-pdf/README.md` (≈307,000 words).

**Method.**
1. A scripted structural sweep over every file (headers, section numbering, teaser chain, every `Ch. N` / `§N.M` / `Appendix X` reference, README outline vs. real headings).
2. An agent review: eight *book-wide* checks (cross-reference targets, chapter chain, reference-machine consistency, recurring latency numbers, duplication/placement, glossary & bibliography, Appendices C/D/H/I accuracy) plus a *two-lens per-chapter review* (technical-accuracy lens and editorial/conventions lens). The per-chapter pass completed for **Ch. 1–17** before the author asked for the review workflow to be wrapped up; **Ch. 18–76 received only the book-wide checks** and still need the per-chapter pass.
3. Verification: the planned adversarial-verifier stage did not run. Instead, every quoted passage (538 of 541) was mechanically confirmed to exist in the named file, and the author of this report personally re-verified the headline items marked ✅ below (by reading the passage, by compiling/running the listing with GCC 13.3 / Clang 18, or by web lookup). Everything else is a reviewer finding of stated confidence, listed in full in [`2026-10-30-findings-full.md`](2026-10-30-findings-full.md).

**Totals.** 541 findings: 49 high, 213 medium, 279 low. The book-wide checks alone produced 212 of them, which is why most of the systemic issues below affect every Part, not just Ch. 1–17.

---

## 1. Headline findings (fix these first)

| # | Finding | Where | Verified |
|---|---|---|---|
| 1 | **The Phase-3 renumber never touched fenced blocks.** 140 of the 269 cross-references inside code/ASCII-diagram/result blocks still carry pre-reorg chapter numbers (only 25 are clearly current). ~170 prose references are also flagged, in 71 files; Appendices H–L were written against an *intermediate* numbering and are wrong in a different pattern. | book-wide; list in [`2026-10-30-stale-crossrefs.md`](2026-10-30-stale-crossrefs.md) | ✅ mechanical |
| 2 | **Stale book-shape metadata**: "seven appendices (A–G)" in README legend, preface and Ch. 76 close; "the book has 64 chapters" in Ch. 1; `tools/book-pdf/README.md` says "Ch. 1–64, Appendices A–G"; CLAUDE.md status block contradicts itself and points at a "Drafted:" line that no longer exists. | `README.md:5`, `00-preface.md:46`, `01:97`, `76:146`, `CLAUDE.md:415/485/497`, `tools/book-pdf/README.md` | ✅ |
| 3 | **Ch. 1's worked example does not build as instructed.** `// Build: g++ -std=c++20 …` but the listing uses `<print>`/`std::println` (C++23); GCC 13 has no `<print>` at all. The Prerequisites line ("GCC 11+ or Clang 14+ is enough") is therefore wrong too. | `01:5, 01:111–155` | ✅ compiled |
| 4 | **Ch. 13's headline experiment measures nothing as written.** With the stated `g++ -O2 … -march=native` the conditional-sum loop is auto-vectorized (`-fopt-info-vec`: "loop vectorized"), so the data-dependent branch does not exist in the binary and sorted ≈ shuffled. The "see note" promising a fix gives no flags. | `13:70–117` | ✅ compiled, GCC 13 |
| 5 | **Ch. 10's prefetch sweep cannot produce its table.** The gather `sum += data[index[i]]` has no cross-iteration dependency, so the OoO core already overlaps ~10 misses; the listing as written gives a flat ≈18–20 ns/elem at every PD (reproduced here), not the claimed 62 → 19.5 ns 3.2× U-curve. | `10:89–131` | ✅ reproduced |
| 6 | **GCC does not accept `-flto=thin`.** Ch. 22 and Appendix D present ThinLTO as "Clang/GCC"; `cc1plus: error: unrecognized argument to '-flto=' option: 'thin'`. Similarly `__builtin_assume` (App. H:44), `__builtin_nontemporal_store` (Ch. 10:189/221/240), `-fsanitize=cfi`/`-fsanitize=fuzzer` (App. D:81–82) are Clang-only but presented as portable. | `22:109/141/157`, `appendix-D:64`, `appendix-H:44`, `10:189`, `appendix-D:81` | ✅ compiled |
| 7 | **Copy-paste-broken commands.** Eight `perf stat -e …` event lists contain a space after a comma (the shell splits them and perf tries to *execute* the second half); Appendix I's `perf stat` continuation lines are indented after `\` (same effect); Appendix I:150 `objdump -drwС` uses a **Cyrillic С (U+0421)**, so the flag is rejected; `grep -E 'cpu[0-9]' /proc/interrupts` matches nothing; `nm --defined-only` counts the wrong symbols. | `11:134, 12:271, 13:105/244, 14:133/280, 15:109/173`, `appendix-I:33–37, 104, 150, 152` | ✅ |
| 8 | **Recurring numbers disagree across chapters.** Branch mispredict "~10–20 ns" (Ch. 1:243) vs "~15–20 cycles ≈ 5–7 ns" everywhere else; kernel-bypass RX "~250 ns p50" (Ch. 76:47, Ch. 1:90) vs "~0.8–1 µs" (Ch. 62:62–63) vs "~1–2 µs" (App. E:31); "DRAM is ~200× an L1 hit" (Ch. 7 title, README, Ch. 6 teaser) vs ~80× in Ch. 7's own table and Ch. 8; null syscall ~50–100 ns (App. E) vs ~80–400 ns (Ch. 41); minor fault ~1–5 µs (App. E) vs ~0.3–1 µs (Ch. 23/26); L1 chase 1.10 ns (Ch. 7:146) is < 5 cycles @ 2.9 GHz (1.72 ns), physically impossible. | see §4 | ✅ read |
| 9 | **Reference-machine facts drift.** Xeon Gold 6326 L3 is 24 MiB, but Ch. 44:49 says "~30 MB"; Ch. 43 and Ch. 6 use SMT sibling "CPU 34"/"CPUs 0 and 32", which only exist on the dual-socket box; ns↔cycles converted at ~3 GHz in Ch. 7 but 2.9 GHz elsewhere; compiler version named in only 6 chapters, kernel version nowhere but as "Linux 6.x". | `44:49`, `43:61/98/113`, `06:115`, `07:17` | ✅ read |
| 10 | **Fabricated or wrong bibliography entries.** Appendix G cites a non-existent Pikus title ("*C++ Optimization*"; his books are *The Art of Writing Efficient Programs* and *Hands-On Design Patterns with C++*) and a "Mark Newman" FIX/microstructure text that does not exist; "Cliff Click et al. on lock-free hash tables" is attributed to Ch. 34, which never cites him. | `appendix-G:18/37/42` | ✅ web (Pikus) |
| 11 | **Ch. 51 and Ch. 52 teach the same material end to end** (Nagle/delayed-ACK, the 40 ms stall experiment, NODELAY/QUICKACK/warming techniques, the same pitfalls). Several other near-verbatim duplications exist (see §6). | `51:21–135` vs `52:27–104` | ✅ read |
| 12 | **Chain/teaser errors that send readers to the wrong chapter**: Ch. 30's Leads-into list is off by one for five chapters ("31 (spinlocks), 32 (false sharing), 33 (lock-free), 34 (seqlocks), 35 (reclamation)"); Ch. 19, Ch. 14 and the Ch. 19 teaser promise `std::function`/type-erasure coverage in Ch. 20, which contains neither (0 hits); Ch. 17 sends readers to Ch. 6 six times for clocksource setup that Ch. 6 does not contain; Ch. 60 sends them to Ch. 6 for colocation (Appendix K). | `30:7`, `19:7/271`, `14:7/311`, `17:7/29/54/137/170/192`, `60:13/66/88/120/127` | ✅ read |
| 13 | **Appendix H's version table is wrong in several rows** (the appendix CLAUDE.md already flagged for checking): `std::mdspan` is *not* in libstdc++ 13/14; `flat_map`/`flat_set` landed in libc++ 20/21 (table says "not yet"); `add_sat` in libc++ 18; `std::jthread` only unguarded in libc++ 20; `consteval` complete in Clang 17 (not 11); P2128 multidimensional `operator[]` is Clang 15 (not 17); "D.2" pointer for `-std`/`-stdlib` is dangling. | `appendix-H:27–75, 101, 112` | agent-verified (web); see §7 |

---

## 2. Structural sweep (clean)

The scripted sweep found the *structure* in good shape — these are not problems:

- Every chapter opens with its Part header, `# Chapter N — Title`, and a Prerequisites/Leads-into blockquote; sections are numbered `N.1 … N.9` (or `N.8` where there is no *Verify the codegen*) with no gaps; the teaser chain 1→2→…→76 is intact.
- All **2,467** `§N.M[.K]` references resolve to an existing heading; all **9,735** chapter/appendix references are in range (1–76, A–L).
- `chapters/README.md` links every file, titles match, Part headers match, and every `N.2.x`/`N.4.x` sub-bullet matches a real `###` heading.
- Part header strings are identical across all files of a Part.

Minor structural nits: Ch. 5 labels its forward references "**Connects forward to:**" where the other 75 chapters say "**Leads into:**"; Ch. 76 has no closing `*Next:*` line; Ch. 9 is the only Part II chapter that never names the reference machine; Ch. 4:7 lists the chapters with a *Verify the codegen* section as "Ch. 9, 13–14, 18–21, 27–29" but the real set is 9–14, 19–21, 27–30, 39, 53, 71 (Ch. 18 has none).

---

## 3. Systemic issue: stale cross-references after the Phase-3 renumber

**What happened.** The Phase-3 reorganisation inserted twelve chapters (5, 39, 44, 49, 52, 54, 56, 57, 59, 60, 64, 65) and renumbered the rest. The scripted remap skipped everything between ``` fences — code comments, ASCII diagrams, benchmark-output annotations — and also missed the no-space spelling `Ch.NN`. Appendices H–L were drafted later against an intermediate numbering and were never remapped at all.

**Evidence (mechanical).** For each reference, the surrounding words were compared with the title/section words of the *current* target and of the target under the old→new map:

| Location | Refs | Clearly stale (mapped target fits, current does not) | Clearly current | Ambiguous/unknown |
|---|---|---|---|---|
| Inside fenced blocks | 269 | **140** (113 + 27 `§` self-references to the chapter's own old number) | 25 | 85 |
| Prose | 9,081 | ~172 flagged (heuristic; reviewers confirmed ≈55 of a 493-line suspicious subset) | 4,196 | 4,713 |

54 files have at least one clearly-stale fenced reference; 71 files have a flagged prose reference. Typical examples (all confirmed by reading): `07:40 "L1i (instructions; Ch.11)"` → 12; `07:46 "remote NUMA … Ch.15"` → 16; `07:48 "NVMe/disk, Ch.63"` → 75; `11:37` diagram "fetch (L1i, Ch.11)" → 12; `13:131 "(§12.5)"` → §13.5; `16:132 "pinned to its node (Ch. 40)"` → 42; `41:106 "KPTI … Ch. 5"` → 6; `45:59–72 "Ch.40 / Ch.41 / Ch.5"` → 42/43/6; `47:100–106 "Ch.39 / Ch.45 / Ch.52"` → 41/48/62; `62:59–64` whole RX table; `76:26–32 "Ch.51 eBPF, Ch.62–63 replay, Ch.63 capture"` → 61/74–75/75; Appendix D:96–98 build-line comments; Appendix H:3/60/63 "`std::execution` (Ch. 40)" → 39 and "`<bit>` (Ch. 27)" → 28; Appendix I:135–142 "PTP (Ch. 56)" → 58; Appendix J:29/68 "seqlock (Ch. 34)" → 35; Appendix K:46/77 "PTP (Ch. 56)" → 58, "FPGA (Ch. 68)" → 69; Appendix L:46 "branch predictor — Ch. 12" → 13.

Prose stale references confirmed by reviewers include blockquotes and tables, e.g. `22:5 "Ch. 11 & 20 (vectorization and aliasing)"` → 29 & 21 and `"Ch. 14 & 19 (…-fno-exceptions/-fno-rtti)"` → 14 & 20; `02:100` and `03:223` code comments (Ch. 21→22, Ch. 16→17); `04:165 "the Ch.10 trick"` → 11; `06:66 "Ch.22"` → 23/26; `28:7 "Companion to Ch. 13 & 26"` → 27; `31:5`/`32:5 "32-preview (false sharing)"` → 33; `57:7 "Ch. 64 (the fabric)"` → 60; `37:187` Aeron → Ch. 65; `32:7/202` futex → Ch. 41 not 47; `40:24` buffer overflows → not Ch. 9; `75:35` WAL → not Ch. 23; Appendix C:45 RPS/XPS → 55/57; Appendix G:30 "Parts IX" → Part XI.

**The old→new map** (derived from CLAUDE.md's original TOC vs `chapters/README.md`): 1–4 unchanged; 5–37 → +1; 38–41 → +2; 42–45 → +3; 46–47 → +4; 48 → 53; 49 → 55; 50 → 58; 51–53 → +10; 54–64 → +12.

**Recommended fix.**
1. Re-run the remap over fenced content only, in the 64 original chapter files, matching `Ch\.\s?(\d+)` (including ranges/lists such as `Ch.62-63`, `Ch.16,50`) and `§(\d+)\.`; refs to numbers ≤ 64 in those files were never remapped, so one application is safe. Hand-check the 85 ambiguous ones from the list.
2. Hand-fix Appendices H–L (their map is different: PTP 56→58, security 60→72, FPGA 68→69, tooling 38→40, false sharing 32→33, seqlock 34→35, `std::execution` 40→39, logging 59→71, bit tricks 27→28).
3. Extend the consistency sweep so fenced references are checked, and add the title-keyword heuristic used here as a standing test.

---

## 4. Systemic issue: numbers that disagree with each other

| Quantity | Statements that disagree | Suggested single value |
|---|---|---|
| Branch mispredict | `01:243` "~10–20 ns"; `02:203`, `03:33`, `13:11`, `11:58` "~15–20 cycles"; `17:15` "~6 ns"; App. E "~3–5 ns / 10–20 cycles" | ~15–20 cycles ≈ 5–7 ns @ 2.9–3.5 GHz |
| Kernel-bypass RX wire→app | `76:47` ~250 ns p50 / 400 ns p99.9 and `01:90` 250–800 ns; `62:62–63` DPDK ~1 µs, ef_vi ~0.8 µs; `55:60`; App. E:31 ~1–2 µs | pick one tier definition; App. E and Ch. 62 vs Ch. 76 differ 3–4× |
| DRAM vs L1 | `07:11` title, README:103, `06:187` "~200×"; `07:22`, `08:5` "~80×"; App. E:5 "~20×", E:20 "~20–100×", E:41 "1:100" | ~80–100× |
| L1 hit latency | `07:146` 1.10 ns @ 2.9 GHz turbo-off (= 3.2 cycles; Ice Lake L1d is 5 cycles → ≥ 1.72 ns); `07:17` table converts at "~3 GHz", other chapters at 2.9 GHz | 5 cycles ≈ 1.7 ns; convert at 2.9 GHz book-wide |
| Null syscall | App. E:27/44 ~50–100 ns; `41:106/114` ~80–400 ns; `01:242` "hundreds of ns" | state mitigations-on/off ranges identically |
| Minor page fault | App. E:29 ~1–5 µs; `23:36/41`, `26:112` ~0.3–1 µs | ~0.3–1 µs (4 KiB), several µs for a zeroed 2 MiB THP |
| Xeon Gold 6326 L3 | `07:143` 24 MiB; `44:49` "~30 MB, 12-way" | 24 MiB (16 × 1.5 MiB), 12-way |
| SMT sibling of CPU 2 | `43:61/98/113` "2,34"; `06:115` "CPUs 0 and 32" (dual-socket layout) on the single-socket box | 2,18 / 0,16 on the 16-core box |
| Ch. 8 AoS→SoA speed-up | `08:148/157` "~12×"; `08:161/216` "8×" | ≈8× (bandwidth ratio) |
| Ch. 13 shuffled cost | `13:109` 1.6 ns/elem implies ~7-cycle penalty vs the chapter's 15–20 cycles; `13:111` "~50% branch-misses/branch" would read ~25% in perf (loop back-edge counted) | re-derive |
| Ch. 11 accumulators | `11:145` "chains=4 … saturating FP-add throughput (~2/cyc)" but 0.36 ns/elem = ~1 add/cycle and §11.4.2 says 8 are needed; `11:182/239/261` "latency ÷ throughput = 4 ÷ 2 ⇒ 8" (should be latency × throughput) | fix wording and claim |
| Ch. 15 TLB reach | `15:17/51` "64 × 2 MB = 128 MB", "STLB 2048 × 2 MB = 4 GB"; Sunny Cove has 32 L1-dTLB entries for 2 MB pages and ≤1024 STLB entries for 2 MB | 64 MB / ~2 GB |
| Ch. 2 perf table | `02:161` identical instruction counts in `seq` and `rand` although only `rand` runs `std::shuffle` over 32 Mi elements; `02:207` "131 LLC-MPKI" is the raw miss count (131.9 M / 1.074 G instr = ~123 MPKI) | confine counting to the loop; recompute |

---

## 5. Listings, commands and asm that are wrong as printed (Ch. 1–17 + appendices)

- `01:111` build line `-std=c++20` with `<print>` (needs `-std=c++23`, GCC 14+/libc++ 17+). ✅
- `03:217` `hdr_init(1, 10'000'000, 3, &h)` caps trackable values at 10 ms and the loop ignores `hdr_record_value`'s `false` — the >10 ms tail events the section is about are silently dropped.
- `03:199` "measure in cycles via `rdtscp`" — the invariant TSC ticks at a fixed rate, so it cannot be a frequency-independent cross-check; only the PMU `cycles` event is. (Contradicts Ch. 17 §17.2.1.)
- `04:130` "verified" asm: `je .done` before `xor eax, eax` returns an uninitialised `rax` for `n == 0`; no compiler emits this. ✅ read
- `04:192` `cmovs` annotation inverts the flag logic (SF after `neg` means x > 0).
- `06:122` `idle=poll` does not "cap at C1"; it replaces the idle loop with a spin and defeats "leave the SMT sibling idle".
- `06:75` "ondemand gov" does not exist under `intel_pstate`.
- `07:125`, `10:103` `std::shuffle` without `<algorithm>` (compiles on libstdc++ 13 transitively; fails on libc++).
- `08:82` "`// ~20 bytes -> 3 per cache line`" — `sizeof(LevelHot)` is 24 (Ch. 9's own `pahole` output), 2⅔ per line; `09:104` `static_assert(sizeof(LevelHot) == 16)` would fail to compile; `09:123` "24 bytes (3 per cache line)" contradicts `09:17/137`.
- `10:171–184` "verified `-march=x86-64-v3`" listing shows `prefetchw`, but x86-64-v3 does not include PRFCHW; GCC/Clang emit `prefetcht2` with those flags.
- `11:134`, `12:271`, `13:105/244`, `14:133/280`, `15:109/173` `perf stat -e` lists with an embedded space (8 occurrences). ✅ mechanical
- `12:226` `imul rax, qword ptr [rdi + 16]` reads 8 bytes of a 4-byte `qty`; `12:243` `readelf -S a.out | grep .text.hot` shows nothing on a normally linked executable (GNU ld merges the input sections).
- `15:70` "transparent HP off for this region — see note": no note exists and nothing in `tlb.cpp` opts out of THP (`madvise(MADV_NOHUGEPAGE)` missing), so the 4 K baseline silently runs on 2 MB pages where THP is `always`.
- Appendix I: `:33–37` `perf stat` continuation lines indented after `\` (shell splits the event list) ✅; `:104` `grep -E 'cpu[0-9]' /proc/interrupts` matches nothing; `:150` Cyrillic `С` in `objdump -drwС` ✅; `:152` `nm --defined-only` counts static symbols, not exports; `:155` BOLT recipe lacks `-Wl,--emit-relocs` and `-fno-reorder-blocks-and-partition`.
- Appendix D: `:64` `-flto=thin` "Clang/modern GCC" ✅ (GCC rejects); `:55/93` `-freorder-blocks-and-partition` is already on at `-O2` in GCC and rejected by Clang, and conflicts with the BOLT step at `:98`; `:14` `-Ofast` omits `-fallow-store-data-races` (breaks the Ch. 30–35 memory-model guarantees); `:81–82` `-fsanitize=cfi` / `-fsanitize=fuzzer` are Clang-only.
- Appendix C: `:42` writing `/proc/irq/<N>/smp_affinity` fails for kernel-managed (multi-queue NIC/NVMe) IRQs; needs `isolcpus=…,managed_irq`.

---

## 6. Experiments whose stated results the code cannot produce

| Chapter | Claim | Why it fails | Status |
|---|---|---|---|
| Ch. 13 §13.3 | sorted 0.4 ns vs shuffled 1.6 ns from `branch.cpp` | loop is auto-vectorized at `-O2` (GCC ≥ 12) or if-converted to `cmov`; the branch is gone | ✅ compiled: `loop vectorized using 32 byte vectors`; needs `-fno-tree-vectorize -fno-tree-loop-if-convert -fno-if-conversion -fno-if-conversion2` or an opaque barrier |
| Ch. 10 §10.3 | PD sweep 62 → 19.5 → 58 ns/elem | independent gathers are already MLP-overlapped; prefetch cannot beat the LFB limit; the "right side" rise has no mechanism (128 lines = 8 KiB, L1-resident) | ✅ reproduced flat ≈18–20 ns at every PD |
| Ch. 12 §12.3 | BLOAT build 2× slower with elevated `l1i_miss` | the "bloat" is a never-executed ~hundreds-of-bytes arm that the compiler places after the hot loop; L1i/DSB only hold fetched/decoded bytes, so both builds have the same front-end footprint | two reviewers independently; one compiled and inspected layout |
| Ch. 12 §12.4.1 | `[[unlikely]]` "physically moves the arm to a cold region" | alone it only reorders blocks within the function; `.text.unlikely` needs `[[gnu::cold]]`/PGO | reviewer compiled GCC 13 / Clang 18 |
| Ch. 2 §2.3 | identical instruction counts for `seq` and `rand` | only `rand` runs `std::shuffle` (32 Mi elements) inside the counted process | ✅ read listing |
| Ch. 11 §11.3 | `chains=4` "same instructions, same FP adds retired, saturating 2/cycle" | the 4-accumulator loop vectorizes to `vaddpd ymm` (§11.5 says so), so `scalar_double` counts differ 4×; 0.36 ns/elem is 1 add/cycle, not 2 | reviewer compiled |
| Ch. 8 §8.3 | ~12× AoS→SoA, instructions "~same" | bandwidth ratio is 8×; SoA sum vectorizes at `-O2 -march=native`; whole-process timing includes 8× more page-fault setup | reviewer ran listing |
| Ch. 7 §7.3 | 1.10 ns L1 plateau | 3.2 cycles < 5-cycle L1d load-to-use on Ice Lake | arithmetic |
| Ch. 15 §15.3 | ~75 ns (4 K) vs ~45 ns (2 M) "dependent-ish probe" | probe is not dependent (only the add chains); MLP makes it throughput-bound | reviewer reasoning |
| Ch. 16 §16.3 | ~85 ns local DRAM from a 512 MiB chase on 4 KiB pages | every access also pays an STLB miss + page walk unless the buffer is THP-backed (unstated) | reviewer reasoning |
| Ch. 17 §17.3 | "TSC calibrates to base 2.9 GHz, confirming TSC ticks ≠ core cycles" | the run is turbo-off, so core clock *is* 2.9 GHz; proves nothing (use `cycles` vs `ref-cycles` with turbo on) | ✅ read |
| Ch. 29 §29.3 | downclock table "@ 3.5 GHz (full turbo)" under a "turbo off" header | self-contradictory environment | ✅ read |

---

## 7. Technical inaccuracies (reviewer findings, high confidence; ✅ = re-verified here)

- **Ch. 3:154** — "at N = 4096 … the flat array doesn't fit in L1": 4096 × 4 B = 16 KiB fits the 48 KiB L1d with room to spare; the binary search loses to ~12 unpredictable dependent branches, not cache misses. Two lenses independently. ✅ read
- **Ch. 5:62** — uprobes and *enabled* USDT markers are an `int3` trap per hit (~1–3 µs), not "nanosecond-scale, safe to run in production" (Ch. 61 §61.3 says so itself).
- **Ch. 6:52** — retpoline is a kernel-side compile-time mitigation; it does not slow your process's virtual calls unless you compile with `-mretpoline`. **Ch. 6:53** — Ice Lake-SP reports Meltdown/L1TF/MDS as *Not affected*, so KPTI and the MDS flushes are not active on the reference machine.
- **Ch. 6** never covers SMIs (`hwlat`, `turbostat --show SMI`) or the clocksource, although Ch. 45, Appendix C and Ch. 17 (six times) send readers to Ch. 6 for exactly that.
- **Ch. 9:151** — AArch64 tolerates ordinary misaligned loads/stores to normal memory; only exclusives/LDAR/STLR/atomics fault.
- **Ch. 9:195** — packing per se does not block vectorization; the 17-byte stride (not a multiple of 8) does.
- **Ch. 10:240/189/221** — `__builtin_nontemporal_store` is Clang-only; **Ch. 10** relies on Ch. 30 for the `sfence` requirement, but Ch. 30 never mentions `sfence`/weakly-ordered stores, and C++ release fences do **not** order `movnt*` stores (a trap the chapter should name).
- **Ch. 11:182/239/261** — "latency ÷ throughput = 4 ÷ 2 ⇒ 8" (should be latency × throughput in ops/cycle). ✅ read
- **Ch. 12:257** — "even if never executed, its bytes evict the hot loop" misstates the mechanism (only fetched/decoded bytes occupy L1i/DSB).
- **Ch. 14:62** — `std::function` is one indirect call to a type-specific invoker with the callable inlined, not "two indirect hops" (verified by disassembly). **Ch. 14:207** — libstdc++'s `std::visit` over 3 alternatives lowers to a compare/branch chain, not a jump table; the §14.5 "verified Clang output" is not what Clang produces.
- **Ch. 15:17/51** — L1-dTLB 2 MB entries are 32, STLB 2 MB ≤ 1024 (reach 64 MB / ~2 GB, not 128 MB / 4 GB).
- **Ch. 16:56** — glibc `calloc` of a large buffer returns fresh mmap pages without touching them; it does not first-touch on the allocating node.
- **Ch. 2:80** — `perf stat --topdown` on Ice Lake exposes only Level-1 buckets (Level 2 arrived with Sapphire Rapids; L3/L4 need `toplev`). **Ch. 2:185** — `LLC-load-misses:pp` is not a PEBS-precise event; use `mem_load_retired.l3_miss:pp`.
- **Ch. 44:49** — "~30 MB L3" for the 6326 (24 MiB). ✅ read
- **Appendix H** (CLAUDE.md asked for this check): `std::mdspan` row "GCC 13 (libstdc++ 14)" is wrong — libstdc++ 13/14 have no `<mdspan>` (`__cpp_lib_mdspan` undefined); `flat_map`/`flat_set` libc++ 20/21 (table: "not yet"); `add_sat` libc++ 18 ("not yet"); `std::jthread` libc++ 20 unguarded (18–19 need `-fexperimental-library`); `consteval` Clang 17 complete (table: 11); `constinit` Clang 10; P2128 `operator[]` Clang 15 (table: 17); coroutines need `-fcoroutines` on GCC 10; `__builtin_assume` does not exist in GCC (use `__attribute__((assume(x)))`); "see Appendix D.2 for `-std`/`-stdlib`" is dangling (D never documents them). ✅ `__builtin_assume`, `-flto` checked locally; versions are the reviewer's web checks
- **Appendix G** — Pikus "*C++ Optimization*" does not exist ✅ web; "Mark Newman" FIX/microstructure text does not exist; Cliff Click wrongly attributed to Ch. 34; multiply-cited works missing (Dean & Barroso *Tail at Scale*; Herlihy & Shavit; Boehm & Adve 2008; Michael 2004 hazard pointers; Fraser; Vyukov; Preshing); F.9 promises CUDA/MPI/FPGA/DOCA/PTP entries that G lacks.
- **Appendix F** — wrong pointers: Maker/Taker → Appendix J.1 (not Ch. 25); NBBO → J.2 (Ch. 25/53 never mention it); LFB/MLP → Ch. 10 (not Ch. 7); RAII → Ch. 23 never uses the term; `std::span`/`mdspan` → Ch. 72/App. H (not 49/53); Drop-copy → not Ch. 74. Missing entries for heavily used terms: TSC/rdtsc (182 uses), IRQ affinity (promised by CLAUDE.md), context switch, CAS, LLC, PMU, ILP, MPKI, STLB, SIMD/AVX, USL/Amdahl/shared-nothing, TTAS/MCS/convoy, lock-free/wait-free, epoll, IPC (the inter-process sense) and shared memory.

---

## 8. Duplication and placement

| Pair | What is duplicated | Suggestion |
|---|---|---|
| **Ch. 51 ↔ Ch. 52** | Nagle/delayed-ACK model, the 40 ms experiment and table, NODELAY/QUICKACK/warming techniques, "QUICKACK not sticky" pitfall, exercises | merge, or make 51 = socket knobs + multicast join, 52 = stack internals only (cwnd, CUBIC/BBR, GRO/GSO, `TCP_NOTSENT_LOWAT`, userspace TCP) |
| Ch. 53 §53.2.3/§53.4.3 ↔ Ch. 54 | A/B arbitration, gap detection, retransmission, snapshot rebuild | keep only hot-path sequence/dedup mechanics in 53 |
| Ch. 53 §53.2.4 ↔ §53.4.4 | the same four microburst bullets twice inside one chapter | one owner (§53.4.4) |
| Ch. 55 §55.4.1 ↔ Ch. 57 | RSS/ntuple/aRFS/IRQ-affinity steering | cut 55.4.1 to a pointer |
| Ch. 55 §55.4.3 ↔ Ch. 62 §62.4.4 (and §52.4.3) | Onload/ef_vi/libexanic mechanics and decision | 62.4.4 sole owner |
| Ch. 12 §12.4.4 ↔ Ch. 22 §22.4.3 | PGO/AutoFDO/BOLT paragraphs near-verbatim | keep the *why* in 12, mechanics in 22 |
| Ch. 23 §23.4.2 ↔ Ch. 26 §26.4.2 | the same five pre-faulting/`mlock`/first-touch bullets | 26.4.2 owner |
| Ch. 75 §75.4.5 ↔ Ch. 76 §76.4.2–4.3 | deterministic replay, simulation harness, naive-backtest caveat | one paragraph in 75 |
| Ch. 50 §50.3 ↔ Ch. 34 | a second SPSC ring implementation; "offsets not pointers" stated six times across 26/50 | instantiate the Ch. 34 ring into the `MAP_SHARED` segment |
| Ch. 44 before Ch. 45 | Ch. 44 lists Ch. 45 and Appendix C as prerequisites that come *later* | swap 44 ↔ 45 |
| Ch. 5 in Part I | 115 lines, no code, no measurement; built on Part X/XII machinery | either make it self-contained (gdb on `-O2`, `rr`, split DWARF, with measured costs) or move it |
| Ch. 21 and Ch. 22 | both claim to "close Part III" | delete the sentence in 21 |

---

## 9. What was *not* reviewed, and what remains

- **Ch. 18–76 prose and code were not given the per-chapter two-lens review** (the pass was stopped after Ch. 17 at the author's request). The density of problems found in Ch. 1–17 (≈20 findings per chapter, 1–4 high) and the book-wide checks' hits in later chapters (Ch. 29, 41, 43–47, 51–57, 60, 62, 75–76) suggest the same density there; the §3 stale-reference list already covers them mechanically.
- Appendices A, B, J, K, L were covered only by the cross-reference and chain checks.
- Benchmark numbers are "representative" by design (CLAUDE.md); this review did **not** flag the absence of real runs, only internal contradictions and physical impossibilities. Three of the flagged experiments (Ch. 10, 13 and the GCC flag claims) were reproduced on a GCC 13.3 box; the rest are reasoning or reviewer-compiled.

---

## 10. Gap analysis — missing topics

_(pending: the fourteen-lens gap-analysis workflow is still running; this section will be filled in from its merged, coverage-verified output)_

---

## Files in this review

- `review/2026-10-30-consistency-review-and-gap-analysis.md` — this report.
- `review/2026-10-30-findings-full.md` — all 541 findings grouped by file, with quotes, problem statements and suggested fixes.
- `review/2026-10-30-stale-crossrefs.md` — the mechanically detected stale cross-references (fenced: near-certain; prose: heuristic).
