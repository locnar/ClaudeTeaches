# Appendix D — Compiler Flag Reference

> **Consolidates:** Ch. 6 (mitigations), Ch. 17 (timing/FP), Ch. 20 (exceptions/RTTI cost), Ch. 21 (aliasing), Ch. 22 (build toolchain — LTO/PGO/BOLT), Ch. 27 (floating-point/`-ffast-math`). This is the *build-side* companion to Appendix C's *system-side* checklist: a categorized GCC/Clang flag cheat sheet with the latency-relevant flags called out — what each does, what it **costs**, and when to drop or keep it on the hot path.

**How to read this:** flags are grouped by purpose (D.1-D.7). For each, the note says whether it's a **safe default**, a **measure-it** knob, or a **hazard** (correctness risk). The golden rule from Ch. 22 governs everything here: **a flag is a hypothesis, not a fact — measure its effect** (Ch. 3) on *your* code, *your* CPU. `-O3` is not always faster than `-O2`; `-march=native` can hurt portability; `-ffast-math` can silently change results. Flags shown are GCC; Clang equivalents are noted where they differ. A representative hot-path build line is given at the end.

---

## D.1 Optimization levels (`-O2`/`-O3`/`-Ofast` caveats)

- **`-O0`** — no optimization; debug only. Never benchmark or ship at `-O0` (Ch. 3 — it's not representative of anything).
- **`-O2`** — the **default for production**. Aggressive optimization that the compiler considers "safe" (no large code-size blowups, no risky transforms). The right baseline for the hot path; most of this book's codegen assumes `-O2`+ (Ch. 4, 22).
- **`-O3`** — `-O2` plus more aggressive vectorization (Ch. 29), inlining, loop transforms. **Sometimes faster, sometimes slower** (Ch. 22): the extra inlining/unrolling can bloat code and hurt the I-cache (Ch. 12) or cause front-end stalls — a *net loss* on a branchy hot path even as a tight loop speeds up. **Measure `-O2` vs `-O3` per binary** (Ch. 3); don't assume higher is faster. Often the right answer is `-O2` globally with `-O3`/targeted attributes on the hot kernels.
- **`-Ofast`** — `-O3` **plus `-ffast-math`** (D.3) **plus other standard-violating flags** (`-fno-protect-parens`, etc.). **Hazard** (Ch. 27): it silently relaxes IEEE FP semantics, which on price/quantity math can change results and break determinism. **Do not use `-Ofast` blindly** — if you want `-O3`, ask for `-O3`; reach for the specific `-ffast-math` sub-flags (D.3) deliberately, not the whole bundle.
- **`-Os`/`-Oz`** — optimize for size. Occasionally relevant where I-cache pressure (Ch. 12) dominates — a smaller hot path that fits the µop cache can beat a larger faster-in-isolation one. A *measure-it* option for front-end-bound code, not a default.

## D.2 Arch/tuning (`-march`/`-mtune`/`-mcpu`)

The single highest-value category after `-O2` (Ch. 22) — tell the compiler exactly what CPU it's targeting so it uses the available instructions (Ch. 28–29: BMI, AVX, etc.).

- **`-march=X`** — *generate* instructions for arch `X` (the binary won't run on older CPUs lacking them). `-march=native` = "this exact build machine." **Use `-march=` matching the deployment CPU** (e.g. `-march=icelake-server`, `-march=sapphirerapids`) — this is what unlocks AVX-512 (Ch. 29), BMI2 `pdep`/`pext` (Ch. 28), `lzcnt`/`tzcnt`, etc. (Ch. 22).
- **`-mtune=X`** — *schedule/tune* for arch `X` without restricting the instruction set (binary still runs on the baseline `-march`). Use when you must support a *range* of CPUs but want to tune for the common one: `-march=x86-64-v3 -mtune=icelake-server`.
- **`-mcpu=X`** — **ARM's combined flag** (Appendix A.6): sets both arch and tuning for an ARM core (`-mcpu=neoverse-v1`). On ARM, prefer `-mcpu` over separate `-march`/`-mtune` — it pulls in LSE atomics and SVE (Appendix A.1-A.2).
- **Caveats** (Ch. 22): `-march=native` on the *build* box ≠ the deployment box is a portability/illegal-instruction trap (build on or for the target). The microarchitecture levels `-march=x86-64-v2/v3/v4` are a portable middle ground (v3 ≈ Haswell+/AVX2, v4 ≈ AVX-512). **Measure** — a wider `-march` enabling AVX-512 can *downclock* (Ch. 29) and net-lose on a mixed workload.
- **`-mno-...`** — selectively disable (e.g. `-mno-avx512f` to avoid downclocking while keeping AVX2, Ch. 29).

## D.3 FP and math (`-ffast-math` hazards, `-fno-math-errno`, FTZ/DAZ)

The most **dangerous** category for a trading system (Ch. 27) — these change *numerical results*, and price/quantity math must be exact and deterministic.

- **`-ffast-math`** — **hazard, usually avoid on price math** (Ch. 27). A bundle that lets the compiler assume no NaN/Inf, reassociate FP (breaking determinism — `(a+b)+c ≠ a+(b+c)`), flush denormals, ignore signed zero, and drop `errno`. It can *meaningfully* speed FP code, but it **trades IEEE correctness for speed** — unacceptable where results must be exact/reproducible across builds (Ch. 27's determinism point). If you use scaled integers for prices (Ch. 27 — you should), this matters less for the *price* path; it matters for analytics/Greeks (Ch. 67). Reach for the *specific* sub-flags, not the bundle:
  - **`-fno-math-errno`** — don't set `errno` after `math.h` calls. **Safe and worthwhile** (Ch. 27): lets `sqrt`/etc. inline to a single instruction instead of a libcall with errno handling. Rarely does anyone check `math_errno`; dropping it is a near-free win.
  - **`-fno-trapping-math`** — assume FP ops don't trap. Usually safe; enables reordering.
  - **`-fassociative-math`/`-freciprocal-math`/`-funsafe-math-optimizations`** — the **determinism-breaking** ones (reassociation, `x/y → x*(1/y)`). **Avoid** where cross-build determinism matters (Ch. 27).
  - **`-ffinite-math-only`** — assume no NaN/Inf. Hazard if your data can produce them (a NaN price check becomes dead code — Ch. 27).
- **FTZ/DAZ (denormals)** (Ch. 27): denormal (subnormal) FP operations can be **10-100× slower** — a nasty hidden tail (Ch. 1). Set **Flush-To-Zero / Denormals-Are-Zero** to treat denormals as zero: either via `-ffast-math` (which sets the MXCSR bits in CRT startup) or, better, **explicitly at runtime** with `_MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)` / `_MM_SET_DENORMALS_ZERO_MODE(...)` (so you control it without the rest of `-ffast-math`). For a hot path that touches FP, **set FTZ/DAZ explicitly** (Ch. 27) — it removes the denormal cliff without relaxing the rest of IEEE.
- **`-frounding-math`** — disables optimizations assuming default rounding; needed only if you change rounding modes dynamically. Usually leave default.

## D.4 Codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`, `-fno-omit-frame-pointer`)

Codegen-shaping flags from Ch. 20–22:

- **`-fno-exceptions`** (Ch. 20) — disable C++ exceptions entirely. Removes unwinding tables and the (small, cold-path) overhead, and *forces* an error-handling style (`std::expected`, error codes — Ch. 20) that's hot-path-friendly. **Trade-off:** you can't use exceptions *or* libraries that throw (much of the STL throws — `.at()`, `vector` growth, `std::stoi`). Used in many HFT codebases that commit to an exception-free style; a *measure-and-decide* architectural choice, not a free flag (Ch. 20 covers the nuance — exceptions are near-zero-cost on the *happy path* with table-based unwinding, so `-fno-exceptions` is about banning the slow throw path and the abstraction, not shaving happy-path ns).
- **`-fno-rtti`** (Ch. 20) — disable RTTI (`dynamic_cast`, `typeid`). Saves the type-info tables; fine if you don't use them (CRTP/static polymorphism — Ch. 19 — instead of `dynamic_cast`). Small win, common in HFT builds.
- **`-fno-plt`** — call shared-library functions via the GOT directly instead of through the PLT stub, removing one indirection per cross-DSO call (Ch. 14). Helps if you call into shared libs on the hot path (better: static-link the hot path — D.6/Ch. 22 — and avoid the question).
- **`-fno-semantic-interposition`** (Ch. 22) — tell the compiler that symbols in *this* shared object won't be interposed (LD_PRELOAD-overridden) at runtime, so it can inline and devirtualize across TU boundaries within the `.so` (a big missed-optimization otherwise for `-fPIC` shared libraries). **Worthwhile** for hot-path shared libraries; default for static/executable. (Clang defaults to this behavior; GCC needs the flag with `-fPIC`.)
- **`-fno-omit-frame-pointer`** (Ch. 2, 22) — **keep** the frame pointer for **profiling** (Ch. 2 — `perf` call-graph/flame-graph stacks need it, or DWARF unwinding which is slower/less reliable). Costs one register (negligible on x86-64's register-rich AVX world; can cost a bit on register-starved code). **Recommended on for profilable production builds** (Ch. 2, 76) — the observability is worth more than the register. `-fomit-frame-pointer` (the `-O2` default) saves the register but blinds the profiler.
- **`-fvisibility=hidden`** (Ch. 22) — hide symbols by default (export deliberately), shrinking the dynamic symbol table and enabling more inlining/devirt within a `.so`. Good hygiene for hot-path libraries.

## D.5 Inlining/layout (`-finline-limit`, `-freorder-blocks-and-partition`, `-falign-*`)

Control inlining and code placement (Ch. 12 front-end, Ch. 22):

- **Inlining controls** — `-finline-limit=N` (GCC, raise the inlining threshold), `__attribute__((always_inline))`/`[[gnu::always_inline]]` and `noinline` for per-function control (Ch. 22). **Two-edged** (Ch. 12): more inlining removes call overhead and enables cross-call optimization, but **bloats code** and can thrash the I-cache/µop cache (Ch. 12) — a front-end-bound regression. Inline the *hot, small, hot-path* functions; *don't* force-inline large or cold ones. Measure I-cache/front-end counters (Ch. 2, 12), not just "did it inline."
- **`-freorder-blocks-and-partition`** (Ch. 12, 22) — split each function into **hot and cold parts** and place the cold parts (error paths, unlikely branches) elsewhere, so the hot path is contiguous in the I-cache (Ch. 12). **Worthwhile** for hot-path code; pairs with `[[likely]]`/`[[unlikely]]` and `__builtin_expect` (Ch. 12–13) telling the compiler which paths are cold.
- **`-falign-functions=N`/`-falign-loops=N`/`-falign-labels`** (Ch. 12) — align hot functions/loops to cache-line/16-byte boundaries so they don't straddle lines or fetch boundaries (Ch. 12 front-end). The defaults are usually fine; tune `-falign-loops` for a measured hot loop that's sensitive to fetch alignment (a *measure-it* micro-knob, Ch. 12).
- **`-fno-reorder-blocks`/`-fno-reorder-functions`** — rarely wanted; the reordering *helps* (let PGO/BOLT do it better, D.6).
- **Section placement** — `__attribute__((section("...")))` / `[[gnu::hot]]`/`[[gnu::cold]]` to group hot code (Ch. 12). `[[gnu::hot]]` on hot functions and `[[gnu::cold]]` on error handlers guides placement and inlining.

## D.6 LTO/PGO/BOLT

The three highest-leverage *whole-program* optimizations (Ch. 22) — applied to the build as a whole, not per-flag-per-TU:

- **LTO (Link-Time Optimization)** — `-flto` (and `-flto=auto`/`=N` for parallelism); link with the same flag and a plugin-aware linker. **Optimizes across translation-unit boundaries** (Ch. 22): inlining, devirtualization (Ch. 14), and constant propagation that stop at the `.o` boundary in a normal build now happen whole-program. **Strongly recommended** for hot-path builds — often a real win, especially for devirtualization (Ch. 14) and cross-module inlining. Use **ThinLTO** (`-flto=thin`, Clang/modern GCC) for much faster link times at near-full-LTO quality. Cost: longer link/build, occasionally exposes latent UB (Ch. 21 — LTO sees more, optimizes harder).
- **PGO (Profile-Guided Optimization)** — two-phase (Ch. 22): build instrumented (`-fprofile-generate`), run on **representative** workload (Ch. 3 — a captured/replayed session, Ch. 75, is ideal) to collect profiles, rebuild with `-fprofile-use`. The compiler then *knows* which branches are taken (Ch. 13), which functions are hot (Ch. 12), what to inline and how to lay out code — driving the D.5 decisions with *data* instead of heuristics. **High-value** for branchy hot paths; the catch (Ch. 22) is the profile must *match production* (a backtest-trained profile that mis-predicts the open's branches can mis-optimize — feed it captured open data, Ch. 75).
- **BOLT** (Binary Optimization and Layout Tool) — a **post-link** optimizer (Ch. 22) that re-lays-out the *final binary* using a `perf` profile (Ch. 2) — reordering basic blocks and functions for I-cache/I-TLB locality (Ch. 12) better than the compiler can pre-link. Applied *after* LTO+PGO for an extra front-end win on large hot paths. Profile it on production-like load (Ch. 76). The LTO→PGO→BOLT stack is the Ch. 22 endgame for squeezing front-end/layout latency.

## D.7 Diagnostics (`-Wpadded`, `-Wfloat-equal`, `-fopt-info`) and sanitizers

Flags that *find problems* rather than change codegen — build-time and test-time tools (Ch. 9, 27, 29, 40, 72):

- **`-Wpadded`** (Ch. 9) — warn when the compiler inserts struct padding. Invaluable for **object-layout** work (Ch. 9) — it shows exactly where padding bloats a hot struct so you can reorder members to shrink it to a cache line (Ch. 9). Noisy (don't leave it on globally); run it deliberately on hot structs. Pair with `pahole` (Ch. 9) for the full layout picture.
- **`-Wfloat-equal`** (Ch. 27) — warn on `==`/`!=` between floats (a correctness trap — Ch. 27). Useful in price/numeric code review.
- **`-fopt-info`** / `-fopt-info-vec` / `-fopt-info-vec-missed` (GCC), `-Rpass=loop-vectorize` / `-Rpass-missed` (Clang) (Ch. 29) — report which loops **vectorized** (and which *didn't*, and why). Essential for **SIMD** work (Ch. 29) — tells you whether auto-vectorization fired and what blocked it (aliasing — Ch. 21, alignment, trip count).
- **`-Wall -Wextra`** plus targeted warnings — baseline hygiene; in a UB-as-vulnerability domain (Ch. 72.2.2), warnings are cheap bug-finders.
- **Sanitizers** (Ch. 40, 72) — **test/CI builds only, never production** (Ch. 72.3 — they cost 2-3×):
  - **`-fsanitize=address`** (ASan) — spatial/temporal memory errors (overflow, use-after-free — Ch. 24, 72). The arena/pool work (Ch. 24) especially needs it.
  - **`-fsanitize=undefined`** (UBSan) — the UB-as-vulnerability catcher (Ch. 72.2.2); run in CI, fix every finding.
  - **`-fsanitize=thread`** (TSan) — data races (Ch. 30–33, 40); essential for lock-free code, and the tool that catches the weak-memory bugs ARM would expose (Appendix A.1).
  - **`-fsanitize=memory`** (MSan, Clang) — uninitialized reads.
  - **`-fsanitize=cfi`** (Ch. 72.4.4, needs LTO) — control-flow-integrity *hardening* (production-capable, measure its cost — Ch. 72.3); distinct from the test-only sanitizers above.
  - Pair sanitizer builds with `-fno-omit-frame-pointer -g` (D.4) for usable reports, and with **fuzzing** (`-fsanitize=fuzzer`, libFuzzer — Ch. 72.4.8) on the wire parsers.

## D.8 A representative hot-path build line

Pulling it together (GCC, single-socket Ice Lake target; adapt `-march`):

```
   # Production hot-path build (after measuring each choice — Ch. 3/21):
   g++ -std=c++23 -O2 -march=icelake-server -mtune=icelake-server \
       -fno-math-errno -fno-semantic-interposition -fvisibility=hidden \
       -fno-omit-frame-pointer \
       -freorder-blocks-and-partition \
       -flto=auto \
       -DNDEBUG ...
   #   + explicit FTZ/DAZ at runtime (Ch.26), not -ffast-math
   #   + PGO: -fprofile-generate → run on captured session (Ch.63) → -fprofile-use
   #   + BOLT post-link with a production perf profile (Ch.21)
   #   + per-kernel: [[gnu::hot]], targeted -O3 / always_inline where MEASURED to help

   # Test/CI build (NOT production):
   g++ -std=c++23 -O1 -g -fno-omit-frame-pointer \
       -fsanitize=address,undefined ...        # + a separate TSan build, + fuzzing
   -Wall -Wextra -Wpadded -Wfloat-equal         # run diagnostics deliberately
```

The discipline (Ch. 22): start from `-O2 -march=<target> -flto`, add the safe codegen flags, set FTZ/DAZ explicitly rather than via `-ffast-math`, layer PGO+BOLT for the front-end, and **measure every addition** — a flag that doesn't show up in the §76.3 tick-to-trade distribution (Ch. 76) isn't earning its place.

## D.9 References

- The **GCC** and **Clang/LLVM** command-line option references — the authoritative per-flag documentation (all sections).
- Ch. 22 (*Build Toolchain for Speed*) — the chapter this consolidates: `-march`, LTO, PGO, BOLT, inlining (D.2, D.5, D.6).
- The **BOLT** paper/documentation (Facebook/Meta) and the GCC/LLVM PGO guides — D.6.
- Ch. 27 (FP/`-ffast-math`/FTZ-DAZ — D.3), Ch. 20 (exceptions/RTTI — D.4), Ch. 12 (front-end/layout — D.5), Ch. 29 (vectorization reports — D.7), Ch. 40 & 60 (sanitizers/fuzzing — D.7), Ch. 2 (frame pointer/profiling — D.4).
- Agner Fog's optimization manuals (Appendix G) — the microarchitectural *why* behind `-march`/alignment/inlining choices.

## D.10 Additional Reading

- The "what does `-ffast-math` actually do" and "is `-O3` worth it" analyses (compiler-vendor blogs, CppCon talks) — the measure-don't-assume message of D.1/D.3.
- LTO/ThinLTO and PGO/BOLT case studies on large C++ binaries — real front-end/layout wins (D.6).
- **Appendix C** (System Tuning Checklist) — the *system-side* counterpart to this *build-side* reference; **Appendix A.6** — the ARM `-mcpu`/extension flags; **Appendix F** (Glossary) — LTO/PGO/BOLT/FTZ/sanitizer terms; **Appendix G** — Agner Fog and the optimization-manual bibliography.

---

*Next: Appendix E — Latency Numbers Every Trading Developer Should Know, the Norvig/Jeff-Dean latency table refreshed for modern x86-64 server hardware and annotated for trading: L1/L2/L3 hit, local/NUMA-remote DRAM, branch mispredict, cross-core HITM, mutex, null syscall, context switch, kernel-stack vs bypass NIC RX, and a PCIe round-trip — each in ns and cycles, with how-measured notes and chapter pointers.*
