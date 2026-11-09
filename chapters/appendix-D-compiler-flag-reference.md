# Appendix D — Compiler Flag Reference

> **Consolidates:** Ch. 6 & 77 (mitigations and hardening — D.8), Ch. 17 (`rdtsc`/serialization — D.4), Ch. 20 (exceptions/RTTI cost), Ch. 21 (aliasing — D.4), Ch. 22 (build toolchain — LTO/PGO/BOLT), Ch. 27 (floating-point/`-ffast-math`). This is the *build-side* companion to Appendix C's *system-side* checklist: a categorized GCC/Clang flag cheat sheet with the latency-relevant flags called out — what each does, what it **costs**, and when to drop or keep it on the hot path.

**How to read this:** flags are grouped by purpose (D.1-D.8). For each, the note says whether it's a **safe default**, a **measure-it** knob, or a **hazard** (correctness risk). The golden rule from Ch. 22 governs everything here: **a flag is a hypothesis, not a fact — measure its effect** (Ch. 3) on *your* code, *your* CPU. `-O3` is not always faster than `-O2`; `-march=native` can hurt portability; `-ffast-math` can silently change results. Flags shown are GCC; Clang equivalents are noted where they differ. A representative hot-path build line is given at the end.

---

## D.1 Optimization levels (`-O2`/`-O3`/`-Ofast` caveats)

- **`-O0`** — no optimization; debug only. Never benchmark or ship at `-O0` (Ch. 3 — it's not representative of anything).
- **`-O2`** — the **default for production**. Aggressive optimization that the compiler considers "safe" (no large code-size blowups, no risky transforms). The right baseline for the hot path; most of this book's codegen assumes `-O2`+ (Ch. 4, 22).
- **`-O3`** — `-O2` plus more aggressive vectorization (Ch. 29), inlining, loop transforms. **Sometimes faster, sometimes slower** (Ch. 22): the extra inlining/unrolling can bloat code and hurt the I-cache (Ch. 12) or cause front-end stalls — a *net loss* on a branchy hot path even as a tight loop speeds up. **Measure `-O2` vs `-O3` per binary** (Ch. 3); don't assume higher is faster. Often the right answer is `-O2` globally with `-O3`/targeted attributes on the hot kernels.
- **`-Ofast`** — `-O3` **plus `-ffast-math`** (D.3) **plus `-fallow-store-data-races`** (GCC: `[disabled]` at `-O3`, `[enabled]` at `-Ofast` per `-Q --help=optimizers` — it lets the optimizer introduce *speculative stores* to memory other threads may be reading, which breaks the C++ memory-model guarantees the lock-free code of Ch. 30–35 rests on; never on code with concurrent readers), and it turns **off** `-fsemantic-interposition`. (`-fprotect-parens` is a gfortran-only flag; Clang 19+ deprecates `-Ofast` altogether in favour of `-O3 -ffast-math`.) **Hazard** (Ch. 27): it silently relaxes IEEE FP semantics, which on price/quantity math can change results and break determinism. **Do not use `-Ofast` blindly** — if you want `-O3`, ask for `-O3`; reach for the specific `-ffast-math` sub-flags (D.3) deliberately, not the whole bundle.
- **`-Os`/`-Oz`** — optimize for size. Occasionally relevant where I-cache pressure (Ch. 12) dominates — a smaller hot path that fits the µop cache can beat a larger faster-in-isolation one. A *measure-it* option for front-end-bound code, not a default.

## D.2 Arch/tuning (`-march`/`-mtune`/`-mcpu`)

The single highest-value category after `-O2` (Ch. 22) — tell the compiler exactly what CPU it's targeting so it uses the available instructions (Ch. 28–29: BMI, AVX, etc.).

- **`-march=X`** — *generate* instructions for arch `X` (the binary won't run on older CPUs lacking them). `-march=native` = "this exact build machine." **Use `-march=` matching the deployment CPU** (e.g. `-march=icelake-server`, `-march=sapphirerapids`) — this is what unlocks AVX-512 (Ch. 29), BMI2 `pdep`/`pext` (Ch. 28), `lzcnt`/`tzcnt`, etc. (Ch. 22).
- **`-mtune=X`** — *schedule/tune* for arch `X` without restricting the instruction set (binary still runs on the baseline `-march`). Use when you must support a *range* of CPUs but want to tune for the common one: `-march=x86-64-v3 -mtune=icelake-server`.
- **`-mcpu=X`** — **ARM's combined flag** (Appendix A.6): sets both arch and tuning for an ARM core (`-mcpu=neoverse-v1`). On ARM, prefer `-mcpu` over separate `-march`/`-mtune` — it pulls in LSE atomics and SVE (Appendix A.1-A.2).
- **Caveats** (Ch. 22): `-march=native` on the *build* box ≠ the deployment box is a portability/illegal-instruction trap (build on or for the target). The microarchitecture levels `-march=x86-64-v2/v3/v4` are a portable middle ground (v3 ≈ Haswell+/AVX2, v4 ≈ AVX-512). **Measure** — a wider `-march` enabling AVX-512 can *downclock* (Ch. 29) and net-lose on a mixed workload.
- **`-mprefer-vector-width=128|256|512`** — the knob for the AVX-512 frequency-license trade (Ch. 29): keeps the whole AVX-512 ISA available (masking, VNNI, the 256-bit EVEX encodings) while telling the auto-vectorizer to stop at 256-bit vectors. GCC's `-march=icelake-server` tuning already defaults to 256 (`g++ -march=icelake-server -Q --help=target` shows `-mprefer-vector-width= 256`, GCC 13.3), so you may already have it; measure 256 vs 512 on *your* kernel.
- **`-mno-...`** — selectively disable an extension (e.g. `-mno-avx512f`): the blunt alternative to `-mprefer-vector-width` for the downclocking concern — it throws the whole AVX-512 ISA away rather than just the 512-bit width. Measure both (Ch. 29).

## D.3 FP and math (`-ffast-math` hazards, `-fno-math-errno`, FTZ/DAZ)

The most **dangerous** category for a trading system (Ch. 27) — these change *numerical results*, and price/quantity math must be exact and deterministic.

- **`-ffast-math`** — **hazard, usually avoid on price math** (Ch. 27). A bundle that lets the compiler assume no NaN/Inf, reassociate FP (breaking determinism — `(a+b)+c ≠ a+(b+c)`), flush denormals, ignore signed zero, and drop `errno`. It can *meaningfully* speed FP code, but it **trades IEEE correctness for speed** — unacceptable where results must be exact/reproducible across builds (Ch. 27's determinism point). If you use scaled integers for prices (Ch. 27 — you should), this matters less for the *price* path; it matters for analytics/Greeks (Ch. 69). Reach for the *specific* sub-flags, not the bundle:
  - **`-fno-math-errno`** — don't set `errno` after `<cmath>` calls. **Safe and worthwhile** (Ch. 27): with `-fmath-errno` (the GCC default) `std::sqrt(x)` compiles to an inline `sqrtsd` *wrapped* in `ucomisd` + `ja` to a fallback `call sqrt` for negative inputs (verified, GCC 13.3 `-O2`); the flag deletes the check and the cold call, leaving the bare `sqrtsd`. Rarely does anyone check `errno` after a math call (the mechanism is `math_errhandling & MATH_ERRNO`); dropping it is a near-free win.
  - **`-fno-trapping-math`** — assume FP ops don't trap. Usually safe; enables reordering.
  - **`-fassociative-math`/`-freciprocal-math`/`-funsafe-math-optimizations`** — the **determinism-breaking** ones (reassociation, `x/y → x*(1/y)`). **Avoid** where cross-build determinism matters (Ch. 27).
  - **`-ffinite-math-only`** — assume no NaN/Inf. Hazard if your data can produce them (a NaN price check becomes dead code — Ch. 27).
- **FTZ/DAZ (denormals)** (Ch. 27): denormal (subnormal) FP operations can be **10-100× slower** — a nasty hidden tail (Ch. 1). Set **Flush-To-Zero / Denormals-Are-Zero** to treat denormals as zero: either via `-ffast-math` (which sets the MXCSR bits in CRT startup) or, better, **explicitly at runtime** with `_MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)` / `_MM_SET_DENORMALS_ZERO_MODE(...)` (so you control it without the rest of `-ffast-math`). For a hot path that touches FP, **set FTZ/DAZ explicitly** (Ch. 27) — it removes the denormal cliff without relaxing the rest of IEEE.
- **`-frounding-math`** — disables optimizations assuming default rounding; needed only if you change rounding modes dynamically. Usually leave default.

## D.4 Codegen (`-fno-exceptions`/`-fno-rtti`, `-fno-plt`, `-fno-semantic-interposition`, `-fno-omit-frame-pointer`)

Codegen-shaping flags from Ch. 20–22:

- **`-fno-exceptions`** (Ch. 20) — disable C++ exceptions entirely. Removes the landing pads and LSDA (`.gcc_except_table`) and the ability to throw (libstdc++ turns a would-be throw into `abort()`), and *forces* an error-handling style (`std::expected`, error codes — Ch. 20) that's hot-path-friendly. **Trade-off:** you can't use exceptions *or* libraries that throw (much of the STL throws — `.at()`, `vector` growth, `std::stoi`). Used in many HFT codebases that commit to an exception-free style; a *measure-and-decide* architectural choice, not a free flag (Ch. 20 covers the nuance — exceptions are near-zero-cost on the *happy path* with table-based unwinding, so `-fno-exceptions` is about banning the slow throw path and the abstraction, not shaving happy-path ns). Note that it does **not** remove the `.eh_frame` unwind tables: x86-64 Linux GCC defaults to `-fasynchronous-unwind-tables`, so `g++ -O2 -fno-exceptions -S` still emits the `.cfi_*` directives for every function (verified, GCC 13.3); they go away only with `-fno-asynchronous-unwind-tables -fno-unwind-tables` — which also breaks `perf`'s DWARF unwinding and `backtrace()`, so leave them in.
- **`-fno-rtti`** (Ch. 20) — disable RTTI (`dynamic_cast`, `typeid`). Saves the type-info tables; fine if you don't use them (CRTP/static polymorphism — Ch. 19 — instead of `dynamic_cast`). Small win, common in HFT builds.
- **`-fno-plt`** — call shared-library functions through the GOT directly (`call *sym@GOTPCREL(%rip)`) instead of through the PLT stub. A PLT call is `call sym@plt` → an indirect `jmp *GOT` inside the stub (lazily bound on first use) — two branches, the second of them indirect and exposed to the BTB-mispredict cost of Ch. 14 — so `-fno-plt` removes one branch per cross-DSO call and needs `-z now` (eager binding) at link time. Helps if you call into shared libs on the hot path (better: static-link the hot path — D.6 and Ch. 22's build-toolchain advice — and avoid the question).
- **`-fno-semantic-interposition`** (Ch. 22) — tell the compiler that symbols in *this* shared object won't be interposed (LD_PRELOAD-overridden) at runtime, so it can inline and devirtualize across TU boundaries within the `.so` (a big missed-optimization otherwise for `-fPIC` shared libraries). **Worthwhile** for hot-path shared libraries; default for static/executable. (Clang defaults to this behavior; GCC needs the flag with `-fPIC`.)
- **`-fno-omit-frame-pointer`** (Ch. 2, 22) — **keep** the frame pointer for **profiling** (Ch. 2 — `perf` call-graph/flame-graph stacks need it, or DWARF unwinding which is slower/less reliable). Costs one GPR (`rbp`, of 16 — the vector register file is irrelevant to it) plus a `push rbp; mov rbp, rsp … pop rbp` prologue/epilogue on every non-leaf call — typically ≤1–2% measured (Ch. 2), occasionally more on register-starved code. **Recommended on for profilable production builds** (Ch. 2, 85) — the observability is worth more than the register. `-fomit-frame-pointer` (the `-O2` default) saves the register but blinds the profiler. **Measured (Ch. 22 §22.4.4): 0 ± 1 % on the decode/book-update kernel** — keep it on *every* build, not just profiled ones; add `-mno-omit-leaf-frame-pointer` / `-fno-optimize-sibling-calls` where complete crash stacks matter (Ch. 80 §80.4.1), after measuring.
- **`-fvisibility=hidden`** (Ch. 22) — hide symbols by default (export deliberately), shrinking the dynamic symbol table and enabling more inlining/devirt within a `.so`. Good hygiene for hot-path libraries.
- **`-std=c++23` / `-std=c++26` and `-stdlib=libc++`** (Appendix H) — standard and library selection. GCC 11+ and Clang 17+ accept the final spelling `-std=c++23` (older releases: `-std=c++2b`); `-std=c++26` (`-std=c++2c` on slightly older releases) selects the draft on GCC 14+ / Clang 17+. `-stdlib=libc++` is Clang-only and swaps in LLVM's library (Clang's default on Linux is the system libstdc++). Neither flag guarantees a *library* feature exists — gate on the `<version>` macros as Appendix H.6 shows.
- **`-fstrict-aliasing` / `-fno-strict-aliasing`** (Ch. 21) — type-based alias analysis is **on at `-O2`** (and off at `-O0`; `g++ -O2 -Q --help=optimizers`, GCC 13.3). The common "fix" for a punning bug, `-fno-strict-aliasing`, is a global pessimization: it removes TBAA-based reordering and is one of the things that blocks vectorization (§21.5 shows the codegen with and without). Fix the punning instead — `std::bit_cast`/`memcpy` (§21.4.2) — and turn on `-Wstrict-aliasing=2` and `-Wcast-align` to find the offenders. Clang: same defaults and flags.
- **Timing-related (Ch. 17)** — nothing in `-O2` orders `rdtsc`/`rdtscp` for you: the intrinsics are `volatile` builtins the compiler will not reorder with each other, but the *measured* code can still move across them, so bracket a measurement with a compiler barrier (`asm volatile("" ::: "memory")`, or Google Benchmark's `DoNotOptimize`/`ClobberMemory` — Appendix L) and the hardware serialization Ch. 17 prescribes (`lfence`). `__rdtscp` needs no `-m` flag on x86-64, but `_rdpid_u32()` does (`-mrdpid`, or an `-march` that includes it — Ice Lake and later).

## D.5 Inlining/layout (`-finline-limit`, `-freorder-blocks-and-partition`, `-falign-*`)

Control inlining and code placement (Ch. 12 front-end, Ch. 22):

- **Inlining controls** — `-finline-limit=N` (GCC, raise the inlining threshold), `__attribute__((always_inline))`/`[[gnu::always_inline]]` and `noinline` for per-function control (Ch. 22). **Two-edged** (Ch. 12): more inlining removes call overhead and enables cross-call optimization, but **bloats code** and can thrash the I-cache/µop cache (Ch. 12) — a front-end-bound regression. Inline the *hot, small, hot-path* functions; *don't* force-inline large or cold ones. Measure I-cache/front-end counters (Ch. 2, 12), not just "did it inline."
- **`-freorder-blocks-and-partition`** (Ch. 12, 22) — split each function into **hot and cold parts** and place the cold parts (error paths, unlikely branches) in `.text.unlikely`, so the hot path is contiguous in the I-cache (Ch. 12). **GCC only, and already on at `-O2`/`-O3` on x86** — nothing to add, just don't pass `-fno-reorder-blocks-and-partition`; Clang does not accept the flag (its analogue is `-fsplit-machine-functions`, which needs a profile). Without PGO it splits only what you *mark* cold, so pair it with `[[likely]]`/`[[unlikely]]`, `[[gnu::cold]]` and `__builtin_expect` (Ch. 12–13). If you BOLT the binary (D.6), note that BOLT's own instructions ask GCC users to build with `-fno-reorder-blocks-and-partition` and let BOLT do the splitting.
- **`-falign-functions=N`/`-falign-loops=N`/`-falign-labels`** (Ch. 12) — align hot functions/loops to cache-line/16-byte boundaries so they don't straddle lines or fetch boundaries (Ch. 12 front-end). All three are **already on at `-O2` in GCC** (`-Q --help=optimizers`), so "tuning" means raising `N` (e.g. `-falign-functions=64`); the defaults are usually fine — raise `-falign-loops` for a measured hot loop that's sensitive to fetch alignment (a *measure-it* micro-knob, Ch. 12). Clang accepts `-falign-functions=N`/`-falign-loops=N` but warns that `-falign-labels` is not supported and ignores it (verified, Clang 18).
- **`-fno-reorder-blocks`/`-fno-reorder-functions`** — rarely wanted; the reordering *helps* (let PGO/BOLT do it better, D.6).
- **Section placement** — `__attribute__((section("...")))` / `[[gnu::hot]]`/`[[gnu::cold]]` to group hot code (Ch. 12). `[[gnu::hot]]` on hot functions and `[[gnu::cold]]` on error handlers guides placement and inlining.

## D.6 LTO/PGO/BOLT

The three highest-leverage *whole-program* optimizations (Ch. 22) — applied to the build as a whole, not per-flag-per-TU:

- **LTO (Link-Time Optimization)** — `-flto` (and `-flto=auto`/`=N` for parallelism); link with the same flag and a plugin-aware linker. **Optimizes across translation-unit boundaries** (Ch. 22): inlining, devirtualization (Ch. 14), and constant propagation that stop at the `.o` boundary in a normal build now happen whole-program. **Strongly recommended** for hot-path builds — often a real win, especially for devirtualization (Ch. 14) and cross-module inlining. Use **ThinLTO** (`-flto=thin`, **Clang/LLD only** — GCC rejects it) for much faster link times at near-full-LTO quality; GCC has no ThinLTO — its scalable equivalent is partitioned WHOPR LTO (`-flto=auto`/`-flto=N`, `-flto-partition=balanced`). Cost: longer link/build, occasionally exposes latent aliasing UB (Ch. 21 — LTO sees more, optimizes harder).
- **PGO (Profile-Guided Optimization)** — two-phase (Ch. 22): build instrumented (`-fprofile-generate`), run on **representative** workload (Ch. 3 — a captured/replayed session, Ch. 81, is ideal) to collect profiles, rebuild with `-fprofile-use`. Clang differs: its instrumented run writes `default_*.profraw` files that you must merge first — `llvm-profdata merge -o default.profdata default_*.profraw`, then `-fprofile-use=default.profdata`. For a *multithreaded* hot path add `-fprofile-update=atomic` to the GCC instrumented build (non-atomic counters racing across threads corrupt the profile); and if the instrumentation itself distorts the hot path's timing, switch to sample-based PGO — AutoFDO (`perf record -b` → `create_gcov`/`llvm-profgen`, then GCC `-fauto-profile=` / Clang `-fprofile-sample-use=`), which profiles the *production* binary. The compiler then *knows* which branches are taken (Ch. 13), which functions are hot (Ch. 12), what to inline and how to lay out code — driving the D.5 decisions with *data* instead of heuristics. **High-value** for branchy hot paths; the catch (Ch. 22) is the profile must *match production* (a backtest-trained profile that mis-predicts the open's branches can mis-optimize — feed it captured open data, Ch. 81).
- **BOLT** (Binary Optimization and Layout Tool) — a **post-link** optimizer (Ch. 22) that re-lays-out the *final binary* using a `perf` profile (Ch. 2) — reordering basic blocks and functions for I-cache/I-TLB locality (Ch. 12) better than the compiler can pre-link. Applied *after* LTO+PGO for an extra front-end win on large hot paths. Two prerequisites: link with **`-Wl,--emit-relocs`** (or `-q`) and keep the binary unstripped, or BOLT falls back to a mode that cannot reorder functions; and feed it **LBR** samples — `perf record -e cycles:u -j any,u -- ./app`, `perf2bolt -p perf.data -o perf.fdata ./app`, then `llvm-bolt ./app -o app.bolt -data=perf.fdata -reorder-blocks=ext-tsp -reorder-functions=hfsort` (Appendix I.9). Profile it on production-like load (Ch. 85). The LTO→PGO→BOLT stack is the Ch. 22 endgame for squeezing front-end/layout latency.

## D.7 Diagnostics (`-Wpadded`, `-Wfloat-equal`, `-fopt-info`) and sanitizers

Flags that *find problems* rather than change codegen — build-time and test-time tools (Ch. 9, 27, 29, 40, 77):

- **`-Wpadded`** (Ch. 9) — warn when the compiler inserts struct padding. Invaluable for **object-layout** work (Ch. 9) — it shows exactly where padding bloats a hot struct so you can reorder members to shrink it to a cache line (Ch. 9). Noisy (don't leave it on globally); run it deliberately on hot structs. Pair with `pahole` (Ch. 9) for the full layout picture.
- **`-Wfloat-equal`** (Ch. 27) — warn on `==`/`!=` between floats (a correctness trap — Ch. 27). Useful in price/numeric code review.
- **`-fopt-info`** / `-fopt-info-vec` / `-fopt-info-vec-missed` (GCC), `-Rpass=loop-vectorize` / `-Rpass-missed` (Clang) (Ch. 29) — report which loops **vectorized** (and which *didn't*, and why). Essential for **SIMD** work (Ch. 29) — tells you whether auto-vectorization fired and what blocked it (aliasing — Ch. 21, alignment, trip count).
- **`-g` / `-gsplit-dwarf`** (Ch. 5) — debug info costs nothing at runtime (it lives in non-loaded sections), so production hot-path builds keep `-g`; `-gsplit-dwarf` writes it to `.dwo` files (or `objcopy --only-keep-debug` splits it after the fact) so the shipped binary is small and stripped while cores and `perf` profiles stay symbolizable offline — Ch. 5 measures the build-time and binary-size difference. Pair with `-fno-omit-frame-pointer` (D.4) for cheap stacks.
- **`-g1` (GCC) / `-gline-tables-only` (Clang), `-gz[=zstd]`** (Ch. 22 §22.4.4) — the minimum debug info that still resolves `perf annotate` lines and crash frames (~1/3 of full `-g`), and compressed `.debug_*` sections for the archived copy. Neither changes codegen; prefer full `-g` + archive by build ID.
- **`-fasynchronous-unwind-tables`** (Ch. 20, 22 §22.4.4) — on by default on x86-64 and independent of `-fno-exceptions`; emits `.eh_frame` for every function. **Never** turn it off to save space: `perf --call-graph dwarf`, gdb, libunwind and the terminate path all read it. `-Wa,--gsframe` (binutils ≥ 2.41; GCC 15: `-fsframe`) adds the compact `.sframe` table the kernel's deferred user unwinder consumes.
- **`-frecord-gcc-switches`** (Ch. 22 §22.4.4, Appendix N.2) — records the command line in a `.GCC.command.line` section (`readelf -p .GCC.command.line`), so an incident bundle knows exactly how the binary was built. Free.
- **`-D_GLIBCXX_ASSERTIONS` / `-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST`** (Ch. 5 §5.4.5, Ch. 20 §20.4.4, Ch. 77 §77.3) — the *shippable* checks: library preconditions as predictable compares, **+3–8 % measured** on a decode loop; on unless measured otherwise.
- **`-Wall -Wextra`** plus targeted warnings — baseline hygiene; in a UB-as-vulnerability domain (Ch. 77.2.2), warnings are cheap bug-finders.
- **Sanitizers** (Ch. 40, 77) — **test/CI builds only, never production** (Ch. 77.3 / Ch. 40 §40.2 — ASan ~2×, UBSan typically well under 2×, MSan ~3×, TSan ~5–15× CPU *and* 5–10× memory, which is the number to budget for the lock-free stress tests of Ch. 34–36):
  - **`-fsanitize=address`** (ASan) — spatial/temporal memory errors (overflow, use-after-free — Ch. 24, 77). The arena/pool work (Ch. 24) especially needs it.
  - **`-fsanitize=undefined`** (UBSan) — the UB-as-vulnerability catcher (Ch. 77.2.2); run in CI, fix every finding.
  - **`-fsanitize=thread`** (TSan) — data races (Ch. 30–33, 40); essential for lock-free code, and the tool that catches the weak-memory bugs ARM would expose (Appendix A.1).
  - **`-fsanitize=memory`** (MSan, Clang) — uninitialized reads.
  - **`-fsanitize=cfi`** (**Clang only**; Ch. 77.4.4, needs `-flto -fvisibility=hidden`) — control-flow-integrity *hardening* (production-capable, measure its cost — Ch. 77.3); GCC has no CFI sanitizer — its hardware-assisted analogue is `-fcf-protection=branch|return|full` (CET, Ch. 77.4.4); distinct from the test-only sanitizers above.
  - Pair sanitizer builds with `-fno-omit-frame-pointer -g` (D.4) for usable reports, and with **fuzzing** (`-fsanitize=fuzzer`, **Clang only**/libFuzzer; with GCC use AFL++'s `afl-g++-fast` — Ch. 77.4.8) on the wire parsers.

## D.8 Hardening and speculative-execution mitigation flags (Ch. 6, 77.4.4)

The compiler side of the Ch. 6 / Ch. 77 trade: each flag closes an attack class and costs something on the hot path. Distro toolchains turn several of them on **by default** (Ubuntu's GCC 13.3 is built with `--enable-default-pie --enable-cet`, emits `endbr64` at `-O2` with no flag given, and sets `-fstack-protector-strong` and `_FORTIFY_SOURCE=2`), so the question on an isolated box is often which to *turn off*, after measuring (Ch. 77.3). All of these are accepted by both GCC and Clang unless noted.

- **`-fcf-protection=full|branch|return|none`** — CET: `branch` emits `endbr64` landing pads for IBT, `return` enables the shadow stack. The landing pads are near-free; the shadow stack is a per-call/return cost on CET-enabled hardware (Ch. 77.4.4). Incompatible with the GCC retpoline flags below (GCC errors out).
- **`-fstack-protector-strong`** (and `-fstack-protector-all`) — a canary check in the epilogue of every function with an address-taken local (`-strong`) or every function (`-all`). Cheap per call, but it is in *every* such function's epilogue, including the hot path's.
- **`-fstack-clash-protection`** — probes each page of a large stack frame; only matters for functions with big frames or `alloca`.
- **`-D_FORTIFY_SOURCE=2|3`** — bounds-checked `memcpy`/`strcpy`/… wrappers when the size is known (requires `-O1`+); level 3 checks more dynamic sizes at some cost on the copy-heavy decode path (Ch. 54).
- **`-fPIE -pie`** — ASLR for the executable. Costs little on x86-64 (RIP-relative addressing) but a few percent on register-starved code; `-fno-pie -no-pie` is a legitimate measured choice on an isolated box.
- **Retpolines / Spectre v2** — GCC `-mindirect-branch=thunk -mfunction-return=thunk`, Clang `-mretpoline`: every indirect call/return becomes a return-trampoline (Ch. 6's measured cost on virtual dispatch, Ch. 14). GCC refuses to combine them with `-fcf-protection` ("not compatible", GCC 13.3) — pick one scheme.
- **`-mharden-sls=all`** — straight-line-speculation hardening (`int3` after `ret`/indirect `jmp`); negligible cost.
- **`-fsanitize=cfi`** (Clang only, D.7) and **`-fvtable-verify`** (GCC) — software CFI for virtual calls; a measured per-call cost.

Measure each with the §77.3 harness and the §85.3 tick-to-trade distribution before dropping it; document the decision and the threat model it rests on (Appendix C's `mitigations=` item is the kernel-side half of the same choice).

## D.9 A representative hot-path build line

Pulling it together (GCC, single-socket Ice Lake target; adapt `-march`):

```
   # Production hot-path build (after measuring each choice — Ch. 3/22):
   g++ -std=c++23 -O2 -march=icelake-server -mtune=icelake-server \
       -fno-math-errno -fno-semantic-interposition -fvisibility=hidden \
       -fno-omit-frame-pointer \
       -flto=auto -Wl,--emit-relocs \
       -DNDEBUG ...
   #   (-freorder-blocks-and-partition is already on at -O2 -- D.5; --emit-relocs is for the BOLT step)
   #   + explicit FTZ/DAZ at runtime (Ch.27), not -ffast-math
   #   + PGO: -fprofile-generate → run on captured session (Ch.81) → -fprofile-use
   #   + BOLT post-link with a production LBR perf profile (perf record -j any,u -> perf2bolt -> llvm-bolt; Ch.22, D.6)
   #   + per-kernel: [[gnu::hot]], targeted -O3 / always_inline where MEASURED to help

   # Test/CI build (NOT production):
   g++ -std=c++23 -O1 -g -fno-omit-frame-pointer \
       -fsanitize=address,undefined ...        # + a separate TSan build, + fuzzing
   #   + diagnostics pass: -Wall -Wextra -Wfloat-equal (and -Wpadded on hot structs only -- D.7)
```

The discipline (Ch. 22): start from `-O2 -march=<target> -flto`, add the safe codegen flags, set FTZ/DAZ explicitly rather than via `-ffast-math`, layer PGO+BOLT for the front-end, and **measure every addition** — a flag that doesn't show up in the §85.3 tick-to-trade distribution (Ch. 85) isn't earning its place.

## D.10 References

- The **GCC** and **Clang/LLVM** command-line option references — the authoritative per-flag documentation (all sections).
- Ch. 22 (*Build Toolchain for Speed*) — the chapter this consolidates: `-march`, LTO, PGO, BOLT, inlining (D.2, D.5, D.6).
- The **BOLT** paper/documentation (Facebook/Meta) and the GCC/LLVM PGO guides — D.6.
- Ch. 27 (FP/`-ffast-math`/FTZ-DAZ — D.3), Ch. 20 (exceptions/RTTI — D.4), Ch. 12 (front-end/layout — D.5), Ch. 29 (vectorization reports — D.7), Ch. 40 & 77 (sanitizers/fuzzing — D.7), Ch. 6 & 77.4.4 (hardening/mitigations — D.8), Ch. 21 (aliasing — D.4), Ch. 17 (`rdtsc` serialization — D.4), Ch. 2 (frame pointer/profiling — D.4).
- Agner Fog's optimization manuals (Appendix G) — the microarchitectural *why* behind `-march`/alignment/inlining choices.

## D.11 Additional Reading

- The "what does `-ffast-math` actually do" and "is `-O3` worth it" analyses (compiler-vendor blogs, CppCon talks) — the measure-don't-assume message of D.1/D.3.
- LTO/ThinLTO and PGO/BOLT case studies on large C++ binaries — real front-end/layout wins (D.6).
- **Appendix C** (System Tuning Checklist) — the *system-side* counterpart to this *build-side* reference; **Appendix A.6** — the ARM `-mcpu`/extension flags; **Appendix F** (Glossary) — LTO/PGO/BOLT/FTZ/sanitizer terms; **Appendix G** — Agner Fog and the optimization-manual bibliography.

---

*Next: Appendix E — Latency Numbers Every Trading Developer Should Know, the Norvig/Jeff-Dean latency table refreshed for modern x86-64 server hardware and annotated for trading: L1/L2/L3 hit, local/NUMA-remote DRAM, branch mispredict, cross-core HITM, mutex, null syscall, context switch, kernel-stack vs bypass NIC RX, and a PCIe round-trip — each in ns and cycles, with how-measured notes and chapter pointers.*
