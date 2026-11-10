# Appendix D — Compiler Flag Reference

> **Consolidates:** Ch. 6 & 92 (mitigations and hardening — D.8), Ch. 19 (`rdtsc`/serialization — D.4), Ch. 23 (exceptions/RTTI cost), Ch. 25 (aliasing — D.4), Ch. 26 (build toolchain — LTO/PGO/BOLT), Ch. 33 (floating-point/`-ffast-math`). This is the *build-side* companion to Appendix C's *system-side* checklist: a categorized GCC/Clang flag cheat sheet with the latency-relevant flags called out — what each does, what it **costs**, and when to drop or keep it on the hot path.

**How to read this:** flags are grouped by purpose (D.1-D.8). For each, the note says whether it's a **safe default**, a **measure-it** knob, or a **hazard** (correctness risk). The golden rule from Ch. 26 governs everything here: **a flag is a hypothesis, not a fact — measure its effect** (Ch. 3) on *your* code, *your* CPU. `-O3` is not always faster than `-O2`; `-march=native` can hurt portability; `-ffast-math` can silently change results. Flags shown are GCC; Clang equivalents are noted where they differ. A representative hot-path build line is given at the end.

---

## D.1 Optimization levels (`-O2`/`-O3`/`-Ofast` caveats)

- **`-O0`** — no optimization; debug only. Never benchmark or ship at `-O0` (Ch. 3 — it's not representative of anything).
- **`-O2`** — the **default for production**. Aggressive optimization that the compiler considers "safe" (no large code-size blowups, no risky transforms). The right baseline for the hot path; most of this book's codegen assumes `-O2`+ (Ch. 4, 26).
- **`-O3`** — `-O2` plus more aggressive vectorization (Ch. 35), inlining, loop transforms. **Sometimes faster, sometimes slower** (Ch. 26): the extra inlining/unrolling can bloat code and hurt the I-cache (Ch. 13) or cause front-end stalls — a *net loss* on a branchy hot path even as a tight loop speeds up. **Measure `-O2` vs `-O3` per binary** (Ch. 3); don't assume higher is faster. Often the right answer is `-O2` globally with `-O3`/targeted attributes on the hot kernels.
- **`-Ofast`** — `-O3` **plus `-ffast-math`** (D.3) **plus `-fallow-store-data-races`** (GCC: `[disabled]` at `-O3`, `[enabled]` at `-Ofast` per `-Q --help=optimizers` — it lets the optimizer introduce *speculative stores* to memory other threads may be reading, which breaks the C++ memory-model guarantees the lock-free code of Ch. 36–41 rests on; never on code with concurrent readers), and it turns **off** `-fsemantic-interposition`. (`-fprotect-parens` is a gfortran-only flag; Clang 19+ deprecates `-Ofast` altogether in favour of `-O3 -ffast-math`.) **Hazard** (Ch. 33): it silently relaxes IEEE FP semantics, which on price/quantity math can change results and break determinism. **Do not use `-Ofast` blindly** — if you want `-O3`, ask for `-O3`; reach for the specific `-ffast-math` sub-flags (D.3) deliberately, not the whole bundle.
- **`-Os`/`-Oz`** — optimize for size. Occasionally relevant where I-cache pressure (Ch. 13) dominates — a smaller hot path that fits the µop cache can beat a larger faster-in-isolation one. A *measure-it* option for front-end-bound code, not a default.

## D.2 Arch/tuning (`-march`/`-mtune`/`-mcpu`)

The single highest-value category after `-O2` (Ch. 26) — tell the compiler exactly what CPU it's targeting so it uses the available instructions (Ch. 34–35: BMI, AVX, etc.).

- **`-march=X`** — *generate* instructions for arch `X` (the binary won't run on older CPUs lacking them). `-march=native` = "this exact build machine." **Use `-march=` matching the deployment CPU** (e.g. `-march=icelake-server`, `-march=sapphirerapids`) — this is what unlocks AVX-512 (Ch. 35), BMI2 `pdep`/`pext` (Ch. 34), `lzcnt`/`tzcnt`, etc. (Ch. 26).
- **AMD targets:** `-march=znver3` (Zen 3 — no AVX-512), `-march=znver4` (Zen 4 — AVX-512 on a 256-bit datapath, so pair with `-mprefer-vector-width=256` unless measured otherwise), `-march=znver5` (Zen 5 — full-width AVX-512; GCC 14 / Clang 19+). Appendix Q.4 has the per-generation units; Appendix R.3 the `-march=` column.
- **`-mtune=X`** — *schedule/tune* for arch `X` without restricting the instruction set (binary still runs on the baseline `-march`). Use when you must support a *range* of CPUs but want to tune for the common one: `-march=x86-64-v3 -mtune=icelake-server`.
- **`-mcpu=X`** — **ARM's combined flag** (Appendix A.6): sets both arch and tuning for an ARM core (`-mcpu=neoverse-v1`). On ARM, prefer `-mcpu` over separate `-march`/`-mtune` — it pulls in LSE atomics and SVE (Appendix A.1-A.2).
- **Caveats** (Ch. 26): `-march=native` on the *build* box ≠ the deployment box is a portability/illegal-instruction trap (build on or for the target). The microarchitecture levels `-march=x86-64-v2/v3/v4` are a portable middle ground (v3 ≈ Haswell+/AVX2, v4 ≈ AVX-512). **Measure** — a wider `-march` enabling AVX-512 can *downclock* (Ch. 35) and net-lose on a mixed workload.
- **`-mprefer-vector-width=128|256|512`** — the knob for the AVX-512 frequency-license trade (Ch. 35): keeps the whole AVX-512 ISA available (masking, VNNI, the 256-bit EVEX encodings) while telling the auto-vectorizer to stop at 256-bit vectors. GCC's `-march=icelake-server` tuning already defaults to 256 (`g++ -march=icelake-server -Q --help=target` shows `-mprefer-vector-width= 256`, GCC 13.3), so you may already have it; measure 256 vs 512 on *your* kernel.
- **`-mno-...`** — selectively disable an extension (e.g. `-mno-avx512f`): the blunt alternative to `-mprefer-vector-width` for the downclocking concern — it throws the whole AVX-512 ISA away rather than just the 512-bit width. Measure both (Ch. 35).

## D.3 FP and math (`-ffast-math` hazards, `-fno-math-errno`, FTZ/DAZ)

The most **dangerous** category for a trading system (Ch. 33) — these change *numerical results*, and price/quantity math must be exact and deterministic.

- **`-ffast-math`** — **hazard, usually avoid on price math** (Ch. 33). A bundle that lets the compiler assume no NaN/Inf, reassociate FP (breaking determinism — `(a+b)+c ≠ a+(b+c)`), flush denormals, ignore signed zero, and drop `errno`. It can *meaningfully* speed FP code, but it **trades IEEE correctness for speed** — unacceptable where results must be exact/reproducible across builds (Ch. 33's determinism point). If you use scaled integers for prices (Ch. 33 — you should), this matters less for the *price* path; it matters for analytics/Greeks (Ch. 80). Reach for the *specific* sub-flags, not the bundle:
  - **`-fno-math-errno`** — don't set `errno` after `<cmath>` calls. **Safe and worthwhile** (Ch. 33): with `-fmath-errno` (the GCC default) `std::sqrt(x)` compiles to an inline `sqrtsd` *wrapped* in `ucomisd` + `ja` to a fallback `call sqrt` for negative inputs (verified, GCC 13.3 `-O2`); the flag deletes the check and the cold call, leaving the bare `sqrtsd`. Rarely does anyone check `errno` after a math call (the mechanism is `math_errhandling & MATH_ERRNO`); dropping it is a near-free win.
  - **`-fno-trapping-math`** — assume FP ops don't trap. Usually safe; enables reordering.
  - **`-fassociative-math`/`-freciprocal-math`/`-funsafe-math-optimizations`** — the **determinism-breaking** ones (reassociation, `x/y → x*(1/y)`). **Avoid** where cross-build determinism matters (Ch. 33).
  - **`-ffinite-math-only`** — assume no NaN/Inf. Hazard if your data can produce them (a NaN price check becomes dead code — Ch. 33).
- **FTZ/DAZ (denormals)** (Ch. 33): denormal (subnormal) FP operations can be **10-100× slower** — a nasty hidden tail (Ch. 1). Set **Flush-To-Zero / Denormals-Are-Zero** to treat denormals as zero: either via `-ffast-math` (which sets the MXCSR bits in CRT startup) or, better, **explicitly at runtime** with `_MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)` / `_MM_SET_DENORMALS_ZERO_MODE(...)` (so you control it without the rest of `-ffast-math`). For a hot path that touches FP, **set FTZ/DAZ explicitly** (Ch. 33) — it removes the denormal cliff without relaxing the rest of IEEE.
- **`-frounding-math`** — disables optimizations assuming default rounding; needed only if you change rounding modes dynamically. Usually leave default.

## D.4 Codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`, `-fno-omit-frame-pointer`)

Codegen-shaping flags from Ch. 23–26:

- **`-fno-exceptions`** (Ch. 23) — disable C++ exceptions entirely. Removes the landing pads and LSDA (`.gcc_except_table`) and the ability to throw (libstdc++ turns a would-be throw into `abort()`), and *forces* an error-handling style (`std::expected`, error codes — Ch. 23) that's hot-path-friendly. **Trade-off:** you can't use exceptions *or* libraries that throw (much of the STL throws — `.at()`, `vector` growth, `std::stoi`). Used in many HFT codebases that commit to an exception-free style; a *measure-and-decide* architectural choice, not a free flag (Ch. 23 covers the nuance — exceptions are near-zero-cost on the *happy path* with table-based unwinding, so `-fno-exceptions` is about banning the slow throw path and the abstraction, not shaving happy-path ns). Note that it does **not** remove the `.eh_frame` unwind tables: x86-64 Linux GCC defaults to `-fasynchronous-unwind-tables`, so `g++ -O2 -fno-exceptions -S` still emits the `.cfi_*` directives for every function (verified, GCC 13.3); they go away only with `-fno-asynchronous-unwind-tables -fno-unwind-tables` — which also breaks `perf`'s DWARF unwinding and `backtrace()`, so leave them in.
- **`-fno-rtti`** (Ch. 23) — disable RTTI (`dynamic_cast`, `typeid`). Saves the type-info tables; fine if you don't use them (CRTP/static polymorphism — Ch. 21 — instead of `dynamic_cast`). Small win, common in HFT builds.
- **`-fno-plt`** — call shared-library functions through the GOT directly (`call *sym@GOTPCREL(%rip)`) instead of through the PLT stub. A PLT call is `call sym@plt` → an indirect `jmp *GOT` inside the stub (lazily bound on first use) — two branches, the second of them indirect and exposed to the BTB-mispredict cost of Ch. 15 — so `-fno-plt` removes one branch per cross-DSO call and needs `-z now` (eager binding) at link time. Helps if you call into shared libs on the hot path (better: static-link the hot path — D.6 and Ch. 26's build-toolchain advice — and avoid the question).
- **`-fno-semantic-interposition`** (Ch. 26) — tell the compiler that symbols in *this* shared object won't be interposed (LD_PRELOAD-overridden) at runtime, so it can inline and devirtualize across TU boundaries within the `.so` (a big missed-optimization otherwise for `-fPIC` shared libraries). **Worthwhile** for hot-path shared libraries; default for static/executable. (Clang defaults to this behavior; GCC needs the flag with `-fPIC`.)
- **`-fno-omit-frame-pointer`** (Ch. 2, 26) — **keep** the frame pointer for **profiling** (Ch. 2 — `perf` call-graph/flame-graph stacks need it, or DWARF unwinding which is slower/less reliable). Costs one GPR (`rbp`, of 16 — the vector register file is irrelevant to it) plus a `push rbp; mov rbp, rsp … pop rbp` prologue/epilogue on every non-leaf call — typically ≤1–2% measured (Ch. 2), occasionally more on register-starved code. **Recommended on for profilable production builds** (Ch. 2, 109) — the observability is worth more than the register. `-fomit-frame-pointer` (the `-O2` default) saves the register but blinds the profiler. **Measured (Ch. 26 §26.4.4): 0 ± 1 % on the decode/book-update kernel** — keep it on *every* build, not just profiled ones; add `-mno-omit-leaf-frame-pointer` / `-fno-optimize-sibling-calls` where complete crash stacks matter (Ch. 98 §98.4.1), after measuring.
- **`-fvisibility=hidden`** (Ch. 26) — hide symbols by default (export deliberately), shrinking the dynamic symbol table and enabling more inlining/devirt within a `.so`. Good hygiene for hot-path libraries.
- **`-std=c++23` / `-std=c++26` and `-stdlib=libc++`** (Appendix H) — standard and library selection. GCC 11+ and Clang 17+ accept the final spelling `-std=c++23` (older releases: `-std=c++2b`); `-std=c++26` (`-std=c++2c` on slightly older releases) selects the draft on GCC 14+ / Clang 17+. `-stdlib=libc++` is Clang-only and swaps in LLVM's library (Clang's default on Linux is the system libstdc++). Neither flag guarantees a *library* feature exists — gate on the `<version>` macros as Appendix H.6 shows.
- **`-fstrict-aliasing` / `-fno-strict-aliasing`** (Ch. 25) — type-based alias analysis is **on at `-O2`** (and off at `-O0`; `g++ -O2 -Q --help=optimizers`, GCC 13.3). The common "fix" for a punning bug, `-fno-strict-aliasing`, is a global pessimization: it removes TBAA-based reordering and is one of the things that blocks vectorization (§25.5 shows the codegen with and without). Fix the punning instead — `std::bit_cast`/`memcpy` (§25.4.2) — and turn on `-Wstrict-aliasing=2` and `-Wcast-align` to find the offenders. Clang: same defaults and flags. Measured cost of the buy-back and of the other UB-licence flags (`-fwrapv`, `-fno-delete-null-pointer-checks`, `-ftrivial-auto-var-init`): **Appendix P.3**; the per-function attributes that sit inside these per-build flags (`[[likely]]`, `[[assume]]`, `pure`/`const`, `target_clones`, `musttail`, `trivial_abi`, contracts): **Appendix P.2**.
- **Timing-related (Ch. 19)** — nothing in `-O2` orders `rdtsc`/`rdtscp` for you: the intrinsics are `volatile` builtins the compiler will not reorder with each other, but the *measured* code can still move across them, so bracket a measurement with a compiler barrier (`asm volatile("" ::: "memory")`, or Google Benchmark's `DoNotOptimize`/`ClobberMemory` — Appendix L) and the hardware serialization Ch. 19 prescribes (`lfence`). `__rdtscp` needs no `-m` flag on x86-64, but `_rdpid_u32()` does (`-mrdpid`, or an `-march` that includes it — Ice Lake and later).

## D.5 Inlining/layout (`-finline-limit`, `-freorder-blocks-and-partition`, `-falign-*`)

The *per-function* forms of these — `[[likely]]`/`__builtin_expect_with_probability`, `[[gnu::hot]]`/`cold`/`noinline`/`always_inline`/`flatten`, `section`, `target_clones` — with their asm fingerprints are **Appendix P.2**.

Control inlining and code placement (Ch. 13 front-end, Ch. 26):

- **Inlining controls** — `-finline-limit=N` (GCC, raise the inlining threshold), `__attribute__((always_inline))`/`[[gnu::always_inline]]` and `noinline` for per-function control (Ch. 26). **Two-edged** (Ch. 13): more inlining removes call overhead and enables cross-call optimization, but **bloats code** and can thrash the I-cache/µop cache (Ch. 13) — a front-end-bound regression. Inline the *hot, small, hot-path* functions; *don't* force-inline large or cold ones. Measure I-cache/front-end counters (Ch. 2, 13), not just "did it inline."
- **`-freorder-blocks-and-partition`** (Ch. 13, 26) — split each function into **hot and cold parts** and place the cold parts (error paths, unlikely branches) in `.text.unlikely`, so the hot path is contiguous in the I-cache (Ch. 13). **GCC only, and already on at `-O2`/`-O3` on x86** — nothing to add, just don't pass `-fno-reorder-blocks-and-partition`; Clang does not accept the flag (its analogue is `-fsplit-machine-functions`, which needs a profile). Without PGO it splits only what you *mark* cold, so pair it with `[[likely]]`/`[[unlikely]]`, `[[gnu::cold]]` and `__builtin_expect` (Ch. 13–14). If you BOLT the binary (D.6), note that BOLT's own instructions ask GCC users to build with `-fno-reorder-blocks-and-partition` and let BOLT do the splitting.
- **`-falign-functions=N`/`-falign-loops=N`/`-falign-labels`** (Ch. 13) — align hot functions/loops to cache-line/16-byte boundaries so they don't straddle lines or fetch boundaries (Ch. 13 front-end). All three are **already on at `-O2` in GCC** (`-Q --help=optimizers`), so "tuning" means raising `N` (e.g. `-falign-functions=64`); the defaults are usually fine — raise `-falign-loops` for a measured hot loop that's sensitive to fetch alignment (a *measure-it* micro-knob, Ch. 13). Clang accepts `-falign-functions=N`/`-falign-loops=N` but warns that `-falign-labels` is not supported and ignores it (verified, Clang 18).
- **`-fno-reorder-blocks`/`-fno-reorder-functions`** — rarely wanted; the reordering *helps* (let PGO/BOLT do it better, D.6).
- **Section placement** — `__attribute__((section("...")))` / `[[gnu::hot]]`/`[[gnu::cold]]` to group hot code (Ch. 13). `[[gnu::hot]]` on hot functions and `[[gnu::cold]]` on error handlers guides placement and inlining.

## D.6 LTO/PGO/BOLT

The three highest-leverage *whole-program* optimizations (Ch. 26) — applied to the build as a whole, not per-flag-per-TU:

- **LTO (Link-Time Optimization)** — `-flto` (and `-flto=auto`/`=N` for parallelism); link with the same flag and a plugin-aware linker. **Optimizes across translation-unit boundaries** (Ch. 26): inlining, devirtualization (Ch. 15), and constant propagation that stop at the `.o` boundary in a normal build now happen whole-program. **Strongly recommended** for hot-path builds — often a real win, especially for devirtualization (Ch. 15) and cross-module inlining. Use **ThinLTO** (`-flto=thin`, **Clang/LLD only** — GCC rejects it) for much faster link times at near-full-LTO quality; GCC has no ThinLTO — its scalable equivalent is partitioned WHOPR LTO (`-flto=auto`/`-flto=N`, `-flto-partition=balanced`). Cost: longer link/build, occasionally exposes latent aliasing UB (Ch. 25 — LTO sees more, optimizes harder).
- **PGO (Profile-Guided Optimization)** — two-phase (Ch. 26): build instrumented (`-fprofile-generate`), run on **representative** workload (Ch. 3 — a captured/replayed session, Ch. 99, is ideal) to collect profiles, rebuild with `-fprofile-use`. Clang differs: its instrumented run writes `default_*.profraw` files that you must merge first — `llvm-profdata merge -o default.profdata default_*.profraw`, then `-fprofile-use=default.profdata`. For a *multithreaded* hot path add `-fprofile-update=atomic` to the GCC instrumented build (non-atomic counters racing across threads corrupt the profile); and if the instrumentation itself distorts the hot path's timing, switch to sample-based PGO — AutoFDO (`perf record -b` → `create_gcov`/`llvm-profgen`, then GCC `-fauto-profile=` / Clang `-fprofile-sample-use=`), which profiles the *production* binary. The compiler then *knows* which branches are taken (Ch. 14), which functions are hot (Ch. 13), what to inline and how to lay out code — driving the D.5 decisions with *data* instead of heuristics. **High-value** for branchy hot paths; the catch (Ch. 26) is the profile must *match production* (a backtest-trained profile that mis-predicts the open's branches can mis-optimize — feed it captured open data, Ch. 99).
- **The profile lifecycle and its gates** — `-fprofile-update=atomic` (GCC; threaded trainers; 3.5× slower instrumented build measured in Ch. 107 §107.3), `-fprofile-correction`, `-fprofile-partial-training`, `-Werror=coverage-mismatch` (GCC's default refusal of a stale-source profile — keep it) and `-Werror=missing-profile`; Clang `-Werror=profile-instr-out-of-date -Werror=profile-instr-unprofiled` (Clang 18 is silent by default), `LLVM_PROFILE_FILE="%m-%p.profraw"`, `llvm-profdata merge -weighted-input=W,file`. Stale profiles measured: −13 % on the trained mix, +3–6 % on another, +13–17 % after a source change (Ch. 26 §26.4.5, Ch. 107 §107.4.5).
- **BOLT** (Binary Optimization and Layout Tool) — a **post-link** optimizer (Ch. 26) that re-lays-out the *final binary* using a `perf` profile (Ch. 2) — reordering basic blocks and functions for I-cache/I-TLB locality (Ch. 13) better than the compiler can pre-link. Applied *after* LTO+PGO for an extra front-end win on large hot paths. Two prerequisites: link with **`-Wl,--emit-relocs`** (or `-q`) and keep the binary unstripped, or BOLT falls back to a mode that cannot reorder functions; and feed it **LBR** samples — `perf record -e cycles:u -j any,u -- ./app`, `perf2bolt -p perf.data -o perf.fdata ./app`, then `llvm-bolt ./app -o app.bolt -data=perf.fdata -reorder-blocks=ext-tsp -reorder-functions=hfsort` (Appendix I.9). Profile it on production-like load (Ch. 109). The LTO→PGO→BOLT stack is the Ch. 26 endgame for squeezing front-end/layout latency.

## D.7 Diagnostics (`-Wpadded`, `-Wfloat-equal`, `-fopt-info`) and sanitizers

Sanitizer *costs* on hot-path kernels (full UBSan 2.5–20×, the overflow/bounds/shift/null subset 1.0–1.5×, trapping vs runtime) are measured in **Appendix P.3**; this section lists the flags.

Flags that *find problems* rather than change codegen — build-time and test-time tools (Ch. 10, 33, 35, 48, 92):

- **`-Wpadded`** (Ch. 10) — warn when the compiler inserts struct padding. Invaluable for **object-layout** work (Ch. 10) — it shows exactly where padding bloats a hot struct so you can reorder members to shrink it to a cache line (Ch. 10). Noisy (don't leave it on globally); run it deliberately on hot structs. Pair with `pahole` (Ch. 10) for the full layout picture.
- **`-Wfloat-equal`** (Ch. 33) — warn on `==`/`!=` between floats (a correctness trap — Ch. 33). Useful in price/numeric code review.
- **`-fopt-info`** / `-fopt-info-vec` / `-fopt-info-vec-missed` (GCC), `-Rpass=loop-vectorize` / `-Rpass-missed` (Clang) (Ch. 35) — report which loops **vectorized** (and which *didn't*, and why). Essential for **SIMD** work (Ch. 35) — tells you whether auto-vectorization fired and what blocked it (aliasing — Ch. 25, alignment, trip count).
- **`-g` / `-gsplit-dwarf`** (Ch. 5) — debug info costs nothing at runtime (it lives in non-loaded sections), so production hot-path builds keep `-g`; `-gsplit-dwarf` writes it to `.dwo` files (or `objcopy --only-keep-debug` splits it after the fact) so the shipped binary is small and stripped while cores and `perf` profiles stay symbolizable offline — Ch. 5 measures the build-time and binary-size difference. Pair with `-fno-omit-frame-pointer` (D.4) for cheap stacks.
- **`-g1` (GCC) / `-gline-tables-only` (Clang), `-gz[=zstd]`** (Ch. 26 §26.4.4) — the minimum debug info that still resolves `perf annotate` lines and crash frames (~1/3 of full `-g`), and compressed `.debug_*` sections for the archived copy. Neither changes codegen; prefer full `-g` + archive by build ID.
- **`-fasynchronous-unwind-tables`** (Ch. 23, 26 §26.4.4) — on by default on x86-64 and independent of `-fno-exceptions`; emits `.eh_frame` for every function. **Never** turn it off to save space: `perf --call-graph dwarf`, gdb, libunwind and the terminate path all read it. `-Wa,--gsframe` (binutils ≥ 2.41; GCC 15: `-fsframe`) adds the compact `.sframe` table the kernel's deferred user unwinder consumes.
- **`-frecord-gcc-switches`** (Ch. 26 §26.4.4, Appendix N.2) — records the command line in a `.GCC.command.line` section (`readelf -p .GCC.command.line`), so an incident bundle knows exactly how the binary was built. Free.
- **`-D_GLIBCXX_ASSERTIONS` / `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST`** (Ch. 5 §5.4.5, Ch. 23 §23.4.4, Ch. 92 §92.3) — the *shippable* checks: library preconditions as predictable compares, **+3–8 % measured** on a decode loop; on unless measured otherwise.
- **`-Wall -Wextra`** plus targeted warnings — baseline hygiene; in a UB-as-vulnerability domain (Ch. 92.2.2), warnings are cheap bug-finders.
- **Sanitizers** (Ch. 48, 92) — **test/CI builds only, never production** (Ch. 92.3 / Ch. 48 §48.2 — ASan ~2×, UBSan typically well under 2×, MSan ~3×, TSan ~5–15× CPU *and* 5–10× memory, which is the number to budget for the lock-free stress tests of Ch. 40–42):
  - **`-fsanitize=address`** (ASan) — spatial/temporal memory errors (overflow, use-after-free — Ch. 30, 92). The arena/pool work (Ch. 30) especially needs it.
  - **`-fsanitize=undefined`** (UBSan) — the UB-as-vulnerability catcher (Ch. 92.2.2); run in CI, fix every finding.
  - **`-fsanitize=thread`** (TSan) — data races (Ch. 36–39, 48); essential for lock-free code, and the tool that catches the weak-memory bugs ARM would expose (Appendix A.1).
  - **`-fsanitize=memory`** (MSan, Clang) — uninitialized reads.
  - **`-fsanitize=cfi`** (**Clang only**; Ch. 92.4.4, needs `-flto -fvisibility=hidden`) — control-flow-integrity *hardening* (production-capable, measure its cost — Ch. 92.3); GCC has no CFI sanitizer — its hardware-assisted analogue is `-fcf-protection=branch|return|full` (CET, Ch. 92.4.4); distinct from the test-only sanitizers above.
  - Pair sanitizer builds with `-fno-omit-frame-pointer -g` (D.4) for usable reports, and with **fuzzing** (`-fsanitize=fuzzer`, **Clang only**/libFuzzer; with GCC use AFL++'s `afl-g++-fast` — Ch. 92.4.8) on the wire parsers.

## D.8 Hardening and speculative-execution mitigation flags (Ch. 6, 92.4.4)

The compiler side of the Ch. 6 / Ch. 92 trade: each flag closes an attack class and costs something on the hot path. Distro toolchains turn several of them on **by default** (Ubuntu's GCC 13.3 is built with `--enable-default-pie --enable-cet`, emits `endbr64` at `-O2` with no flag given, and sets `-fstack-protector-strong` and `_FORTIFY_SOURCE=2`), so the question on an isolated box is often which to *turn off*, after measuring (Ch. 92.3). All of these are accepted by both GCC and Clang unless noted.

- **`-fcf-protection=full|branch|return|none`** — CET: `branch` emits `endbr64` landing pads for IBT, `return` enables the shadow stack. The landing pads are near-free; the shadow stack is a per-call/return cost on CET-enabled hardware (Ch. 92.4.4). Incompatible with the GCC retpoline flags below (GCC errors out).
- **`-fstack-protector-strong`** (and `-fstack-protector-all`) — a canary check in the epilogue of every function with an address-taken local (`-strong`) or every function (`-all`). Cheap per call, but it is in *every* such function's epilogue, including the hot path's.
- **`-fstack-clash-protection`** — probes each page of a large stack frame; only matters for functions with big frames or `alloca`.
- **`-D_FORTIFY_SOURCE=2|3`** — bounds-checked `memcpy`/`strcpy`/… wrappers when the size is known (requires `-O1`+); level 3 checks more dynamic sizes at some cost on the copy-heavy decode path (Ch. 65).
- **`-fPIE -pie`** — ASLR for the executable. Costs little on x86-64 (RIP-relative addressing) but a few percent on register-starved code; `-fno-pie -no-pie` is a legitimate measured choice on an isolated box.
- **Retpolines / Spectre v2** — GCC `-mindirect-branch=thunk -mfunction-return=thunk`, Clang `-mretpoline`: every indirect call/return becomes a return-trampoline (Ch. 6's measured cost on virtual dispatch, Ch. 15). GCC refuses to combine them with `-fcf-protection` ("not compatible", GCC 13.3) — pick one scheme.
- **`-mharden-sls=all`** — straight-line-speculation hardening (`int3` after `ret`/indirect `jmp`); negligible cost.
- **`-fsanitize=cfi`** (Clang only, D.7) and **`-fvtable-verify`** (GCC) — software CFI for virtual calls; a measured per-call cost.

Measure each with the §92.3 harness and the §109.3 tick-to-trade distribution before dropping it; document the decision and the threat model it rests on (Appendix C's `mitigations=` item is the kernel-side half of the same choice).

## D.9 A representative hot-path build line

Pulling it together (GCC, single-socket Ice Lake target; adapt `-march`):

```
   # Production hot-path build (after measuring each choice — Ch. 3/26):
   g++ -std=c++23 -O2 -march=icelake-server -mtune=icelake-server \
       -fno-math-errno -fno-semantic-interposition -fvisibility=hidden \
       -fno-omit-frame-pointer \
       -flto=auto -Wl,--emit-relocs \
       -DNDEBUG ...
   #   (-freorder-blocks-and-partition is already on at -O2 -- D.5; --emit-relocs is for the BOLT step)
   #   + explicit FTZ/DAZ at runtime (Ch.33), not -ffast-math
   #   + PGO: -fprofile-generate → run on captured session (Ch.99) → -fprofile-use
   #   + BOLT post-link with a production LBR perf profile (perf record -j any,u -> perf2bolt -> llvm-bolt; Ch.26, D.6)
   #   + per-kernel: [[gnu::hot]], targeted -O3 / always_inline where MEASURED to help

   # Test/CI build (NOT production):
   g++ -std=c++23 -O1 -g -fno-omit-frame-pointer \
       -fsanitize=address,undefined ...        # + a separate TSan build, + fuzzing
   #   + diagnostics pass: -Wall -Wextra -Wfloat-equal (and -Wpadded on hot structs only -- D.7)
```

The discipline (Ch. 26): start from `-O2 -march=<target> -flto`, add the safe codegen flags, set FTZ/DAZ explicitly rather than via `-ffast-math`, layer PGO+BOLT for the front-end, and **measure every addition** — a flag that doesn't show up in the §109.3 tick-to-trade distribution (Ch. 109) isn't earning its place.

## D.10 References

- The **GCC** and **Clang/LLVM** command-line option references — the authoritative per-flag documentation (all sections).
- Ch. 26 (*Build Toolchain for Speed*) — the chapter this consolidates: `-march`, LTO, PGO, BOLT, inlining (D.2, D.5, D.6).
- The **BOLT** paper/documentation (Facebook/Meta) and the GCC/LLVM PGO guides — D.6.
- Ch. 33 (FP/`-ffast-math`/FTZ-DAZ — D.3), Ch. 23 (exceptions/RTTI — D.4), Ch. 13 (front-end/layout — D.5), Ch. 35 (vectorization reports — D.7), Ch. 48 & 92 (sanitizers/fuzzing — D.7), Ch. 6 & 92.4.4 (hardening/mitigations — D.8), Ch. 25 (aliasing — D.4), Ch. 19 (`rdtsc` serialization — D.4), Ch. 2 (frame pointer/profiling — D.4).
- Agner Fog's optimization manuals (Appendix G) — the microarchitectural *why* behind `-march`/alignment/inlining choices.

## D.11 Additional Reading

- The "what does `-ffast-math` actually do" and "is `-O3` worth it" analyses (compiler-vendor blogs, CppCon talks) — the measure-don't-assume message of D.1/D.3.
- LTO/ThinLTO and PGO/BOLT case studies on large C++ binaries — real front-end/layout wins (D.6).
- **Appendix C** (System Tuning Checklist) — the *system-side* counterpart to this *build-side* reference; **Appendix A.6** — the ARM `-mcpu`/extension flags; **Appendix F** (Glossary) — LTO/PGO/BOLT/FTZ/sanitizer terms; **Appendix G** — Agner Fog and the optimization-manual bibliography.

---

*Next: Appendix E — Latency Numbers Every Trading Developer Should Know, the Norvig/Jeff-Dean latency table refreshed for modern x86-64 server hardware and annotated for trading: L1/L2/L3 hit, local/NUMA-remote DRAM, branch mispredict, cross-core HITM, mutex, null syscall, context switch, kernel-stack vs bypass NIC RX, and a PCIe round-trip — each in ns and cycles, with how-measured notes and chapter pointers.*
