# Part III — Compile-Time & Language Mechanics

# Chapter 24 — Build Toolchain for Speed

> **Prerequisites:** Ch. 4 (reading asm — the only way to confirm what a flag actually did), Ch. 12 (code layout / I-cache — the front-end effects LTO/PGO/BOLT optimize), Ch. 32 & 23 (vectorization and aliasing — what `-O3`/`-march` unlock), Ch. 3 (benchmarking — every flag claim is a measurement), Ch. 14 & 21 (devirtualization, `-fno-exceptions`/`-fno-rtti`).
>
> **Leads into:** Part IV (Memory Management) and beyond — every later chapter's code is built with these flags. Directly ties to **Appendix D** (the categorized flag reference) and **Appendix C** (system tuning). PGO/BOLT here is the production-scale version of the layout work in Ch. 12. Closes **Part III**.

---

## 24.1 Why it matters: flags can swing latency by double digits

You can write perfect Part II/III code — cache-friendly layout, broken dependency chains, branchless hot paths, zero-cost abstractions, `restrict`-clean kernels — and then hand the compiler the wrong flags and watch half of it evaporate. The build toolchain is the last translation step between your source and the bytes the CPU runs, and its settings are not a footnote: the difference between `-O0` and `-O2` is routinely **10×**; between `-O2` and `-O2 -march=native` on vectorizable numeric code, another **2-5×** (§24.3); LTO and PGO each add high-single-digit to double-digit percentages on a large, front-end-bound binary, and BOLT recovers several more on top (the published numbers: Panchenko et al.'s BOLT paper, Ch. 12 §12.8; the mechanism is Ch. 12 §12.3's measurement, and exercise 5 has you reproduce the stack on your own binary). These are not micro-optimizations — they are some of the largest, cheapest latency levers in the entire book, applied uniformly across the *whole* program with zero source changes.

The reason flags matter so much is that the compiler's best transformations are *gated* on information and permission that only the build configuration supplies. `-march=native` tells it which instructions exist (AVX-512, BMI2, FMA — Ch. 31–32), unlocking SIMD that `-O3` alone can't emit for a generic target. **LTO** tears down the translation-unit walls so inlining and devirtualization (Ch. 14) work *across* files, not just within them. **PGO** replaces the compiler's hot/cold *guesses* (Ch. 12–13) with measured truth, so layout and inlining match reality. **BOLT** re-lays-out the final linked binary for I-cache/I-TLB locality (Ch. 12) using a production profile. Each is a different way of giving the optimizer more to work with, and for a tick-to-trade binary — large, branchy, front-end-sensitive — they compound.

But the toolchain is also where you can quietly *break* things: `-Ofast`/`-ffast-math` reorders floating-point and can change your prices' last bits or break determinism (Ch. 30); `-march=native` produces a binary that **SIGILLs** on an older CPU (a deployment landmine if you build and run on different hardware — colocation reality); aggressive inlining can bloat the I-cache (Ch. 12) and *regress* a front-end-bound path. So this chapter is the disciplined version of "turn the knobs": what `-O2` vs `-O3` actually change, how `-march`/`-mtune` target your colo hardware, what LTO/PGO/BOLT buy and cost, and — the through-line of the whole book — **verify every flag's effect by measuring (Ch. 3) and reading the asm (Ch. 4)**, because flags interact, platform-depend, and occasionally pessimize. The full categorized cheat-sheet lives in **Appendix D**; this chapter is the mental model and the workflow.

## 24.2 Mental model: optimization levels, inlining control, the linker

**Optimization levels — what each tier actually does.**

- **`-O0`** — no optimization, every variable to memory; for debugging only. Never benchmark or ship it.
- **`-O1`/`-O2`** — `-O2` is the sane production baseline: inlining, common-subexpression elimination, dead-code elimination, register allocation, loop optimizations — essentially all the "obviously correct" optimizations. (Exception handling is table-based at every `-O` level — Ch. 21 — it is the ABI, not an optimization.) Most of the win over `-O0` is here.
- **`-O3`** — `-O2` plus more *aggressive* (and sometimes code-size-increasing) transforms: more aggressive vectorization (Ch. 32), loop unrolling (Ch. 11), function cloning. Often faster on numeric/vectorizable code, occasionally *slower* if it bloats a front-end-bound path (Ch. 12) — so `-O3` is a *measure-it* choice, not a strict upgrade over `-O2`.
- **`-Os`/`-Oz`** — optimize for size; relevant when I-cache footprint dominates (Ch. 12), rarely the hot-path default but worth trying on a front-end-bound binary.
- **`-Ofast`** — `-O3` plus `-ffast-math` and other standard-relaxing flags. **Fast but dangerous** (§24.5): it changes FP semantics (Ch. 30). Opt into the specific relaxations you want, don't blanket-enable `-Ofast`.

The key mental correction: **`-O3` is not strictly "better" than `-O2`** — it's a different point on a speed/size/risk curve. The latency-bound, front-end-sensitive parts of a trading system sometimes prefer `-O2` (or `-O2` + targeted `-O3`/`#pragma` on hot numeric kernels). Mix them per *translation unit* from the build system (hot numeric TUs at `-O3`, the rest at `-O2`); `__attribute__((optimize))`/`#pragma GCC optimize` exist but GCC documents them as debugging aids "not suitable in production code" — they block inlining between functions with differing option sets — so for a single hot loop prefer `#pragma GCC unroll`/`#pragma clang loop` or targeted intrinsics.

**Inlining control.** Inlining is the optimization multiplier (it enables everything else — Ch. 11, 14, 19) but also the bloat risk (Ch. 12). The levers: `-finline-functions`, `-finline-limit=N`, `__attribute__((always_inline))`/`((noinline))`, `[[gnu::flatten]]`, and the hot/cold attributes of Ch. 12. The model from Ch. 12 holds: *inline the small and hot; outline the large and cold*; the flags tune the compiler's threshold, but the front-end counters (Ch. 12) are the arbiter.

**The linker, and where cross-module optimization happens.**

```
   per-file compile (-O2)            link
   ┌──────────┐  ┌──────────┐        ┌──────────────┐
   │  a.cpp   │─►│  a.o     │──┐     │              │
   │  (opt in │  │ (opt'd   │  ├────►│   linker     │──► executable
   │  isolation)  │  isolation)│     │ (resolves,   │
   └──────────┘  └──────────┘  │     │  places code)│
   ┌──────────┐  ┌──────────┐  │     └──────────────┘
   │  b.cpp   │─►│  b.o     │──┘            │
   └──────────┘  └──────────┘               └─ with LTO: real OPTIMIZATION happens HERE,
                                               across all .o, after the per-file walls fall
```

Without LTO, each `.cpp` is optimized in isolation — the compiler can't inline `a.cpp`'s function into `b.cpp` or devirtualize across the boundary (Ch. 14). **LTO (Link-Time Optimization)** defers real optimization to link time, when the whole program is visible — that's the §24.4.2 win. The linker *also* governs code placement (section ordering — Ch. 12), symbol resolution, and it merges the per-object ISA-level property notes — it does *not* check that every TU was built with the same `-march`, which is how the flag-drift `SIGILL` of §24.5 gets in; the choice of linker (`bfd`/`gold`/`lld`/`mold`) mostly affects link *speed*, but `lld`/`mold` are the modern fast defaults that make LTO/iterate loops bearable.

The unifying model: **`-O2` is the baseline of safe optimizations; `-O3`/`-march` add aggressive/ISA-specific ones (measure); LTO removes the per-file walls so inlining/devirtualization go whole-program; PGO/BOLT (next) replace guesses with measured layout. Each gives the optimizer more information or permission — and each must be verified, because they interact and platform-depend.**

## 24.3 Measure it: `-O2`/`-O3`/`-march` comparison

The point is to see the *tiers* and their interaction on representative code — a vectorizable numeric kernel (where `-O3`/`-march` shine) plus a branchy dispatch path (where they may not). Build the *same* source at several flag settings and compare; this is a meta-measurement, so the "benchmark" is a build matrix.

```bash
# build_matrix.sh — same source, escalating flags. Time + size each.
SRC="hotpath.cpp handlers.cpp"   # hotpath.cpp: a SAXPY/reduction kernel + the message-dispatch loop;
                                 # handlers.cpp: the dispatch targets, in a SEPARATE TU so LTO has a
                                 # boundary to remove (single-TU, the -flto row would show nothing)
for FLAGS in \
  "-O0" \
  "-O2" \
  "-O3" \
  "-O3 -march=native" \
  "-O3 -march=native -flto" ; do
    g++ -std=c++20 $FLAGS $SRC -o hp
    SIZE=$(size hp | awk 'NR==2{print $1}')        # .text size
    NS=$(taskset -c 2 ./hp | tr '\n' ' ')          # the program prints two lines: numeric=X ns/op, dispatch=Y ns/op
    printf '%-32s text=%-8s %s\n' "$FLAGS" "$SIZE" "$NS"
done
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP, AVX-512), GCC 13, Linux 6.18, two TUs (kernel + dispatch loop in `hotpath.cpp`, the handlers in `handlers.cpp`), pinned, turbo off (illustrative; the *shape* is the point):

```
flags                              .text     numeric kernel   dispatch loop
-O0                                 small      ~6.0 ns/op       ~9.0 ns/op    <- never ship this
-O2                                 base       ~0.60 ns/op      ~1.2 ns/op    <- sane baseline
-O3                                 +bloat     ~0.55 ns/op      ~1.2 ns/op    <- unroll/vec, ~flat on branchy
-O3 -march=native                   +isa       ~0.12 ns/op      ~1.1 ns/op    <- AVX-512 unlocked: ~5x on numeric
-O3 -march=native -flto             +xtu       ~0.12 ns/op      ~0.7 ns/op    <- LTO inlines/devirt across TUs
```

The lessons the matrix teaches:

- **`-O0 → -O2` is the giant step** (~10×) — the price of debugging-friendly codegen is enormous; never benchmark or deploy `-O0`.
- **`-O3` over `-O2` is modest and conditional** — it helps the *numeric* kernel (more vectorization/unrolling) but is roughly flat on the *branchy* dispatch loop, and it grew `.text` (Ch. 12). On a front-end-bound path `-O3` can even regress; this is why it's a measure-it choice.
- **`-march=native` is the big numeric unlock** (~5× here) — it lets the compiler emit AVX-512/FMA the generic baseline can't (Ch. 31–32). But the resulting binary runs *only* on CPUs with those features (§24.5).
- **LTO helps the *cross-TU* path** — the dispatch loop improves because LTO inlined/devirtualized (Ch. 14) across translation units that `-O3` alone couldn't see through; the single-TU numeric kernel was already fully optimized, so LTO barely moves it.

The discipline: **don't assume a flag helped — measure each tier on *your* code, watch `.text` size (Ch. 12) alongside latency, and read the asm (Ch. 4) to confirm the intended transform happened** (e.g. `-march=native` actually emitted AVX). Flags are powerful and interacting; the build matrix is how you find the right point on the speed/size/risk curve for each part of the system.

## 24.4 Techniques

### 24.4.1 `-march`/`-mtune` targeting

`-march`/`-mtune` decide *which* instructions the compiler may emit and *which* microarchitecture it schedules for — the single highest-leverage flag for numeric hot paths, and the easiest to misdeploy.

- **`-march=X` sets the instruction-set baseline** the binary *requires*. `-march=native` = "everything this build machine supports" (AVX-512, BMI2, FMA, …) — maximal performance, but the binary **SIGILLs on any CPU lacking those features**. For deployment you must match the build target to the *run* target.
- **`-mtune=X` only schedules/tunes** for a microarchitecture without restricting the instruction set — safe to tune for your colo CPU while keeping a portable `-march` baseline.
- **The colocation recipe.** Trading boxes are *known* hardware. Build with `-march=<exact-colo-uarch>` (e.g. `-march=icelake-server` / `-march=znver3`) rather than `native` so builds are reproducible and target the deployment CPU explicitly — you get the AVX-512/BMI2 win *and* a binary that's guaranteed to run on the colo fleet (and won't accidentally use instructions your colo CPU lacks because the build host differed). Use `-mtune=` to match if `-march` is set to a conservative baseline for a mixed fleet.
- **Verify the ISA actually got used (Ch. 4).** `-march=native` *enables* AVX-512 but the loop only benefits if it vectorized (aliasing/alignment — Ch. 23, 9 — can still block it). Disassemble and confirm `zmm`/`ymm`/FMA/BMI2 ops appear; `-fopt-info-vec`/`-Rpass=loop-vectorize` reports help.
- **Mind SIMD downclocking (Ch. 32).** Heavy AVX-512 can lower the core's frequency; on a mixed workload the `-march` win on one kernel can tax everything else. Measure end-to-end, not just the kernel (Ch. 32's caveat).

### 24.4.2 LTO

**Link-Time Optimization** defers optimization to link time so the compiler sees the *whole program*, removing the per-translation-unit walls (§24.2).

- **What it unlocks:** cross-TU **inlining** (a hot accessor defined in `a.cpp` inlined into `b.cpp`), cross-TU **devirtualization** (Ch. 14 — proving a call monomorphic across the whole program), cross-TU constant propagation and dead-code elimination, and better whole-program code layout. For a codebase split across many files (every real system), LTO recovers the optimizations that file boundaries otherwise block — the §24.3 dispatch-loop win.
- **Flavors:** **full LTO** (`-flto`) optimizes the entire program together — best results, slowest/most-memory-hungry link. **ThinLTO** (`-flto=thin`, **Clang/LLD only** — GCC rejects the flag) does scalable, parallel, mostly-as-good LTO with far faster links — the practical default for large codebases (keeps the iterate loop, Ch. 3, tolerable). GCC's scalable equivalent is its partitioned WHOPR mode, `-flto=auto` (or `-flto=N`), tuned with `-flto-partition=`. Use ThinLTO/WHOPR unless you've measured full LTO meaningfully better.
- **Costs:** longer link times and more memory; build all objects *and* the final link with the matching `-flto` flags (and a compatible linker plugin); debugging is a little harder. Worth it for the production binary; you may keep a non-LTO config for fast dev builds.
- **Pairs with `-fno-semantic-interposition` and visibility.** Hidden visibility (`-fvisibility=hidden`) and `-fno-semantic-interposition` let the compiler assume functions aren't replaced at load time, enabling more inlining/devirtualization (especially in shared libraries) — natural companions to LTO for a self-contained trading binary.

### 24.4.3 PGO and BOLT

The highest tier: replace the compiler's *guesses* about hot/cold and branch direction (Ch. 12–13) with *measured* truth from a representative run.

- **PGO (Profile-Guided Optimization) — the three-step build.** (1) Build instrumented (`-fprofile-generate`); (2) run it on a **representative** workload — for trading, a captured market-data replay (Ch. 94) of a realistic session; (3) rebuild with the profile (`-fprofile-use`). Now the compiler *knows* which branches are taken, which calls are hot, which paths are cold — and does accurate hot/cold splitting (Ch. 12), speculative **devirtualization** (Ch. 14), profile-driven inlining, and block layout. PGO is the highest-leverage front-end/layout win on a large branchy binary precisely because it gets the layout right *everywhere*, not just where you annotated.
- **AutoFDO** is the lower-friction variant: instead of an instrumented build, collect an **LBR** profile in production (`perf record -b -e cycles:u`), convert it (`create_gcov` from the AutoFDO toolkit → `-fauto-profile=` for GCC; `create_llvm_prof`/`llvm-profgen` → `-fprofile-sample-use=` for Clang — the better-maintained path) and rebuild — less accurate than instrumented PGO but no special build to deploy, so it's continuously refreshable from live traffic.
- **BOLT (Binary Optimization and Layout Tool)** goes *after* the linker: it takes the final linked binary plus a `perf` profile and **re-lays-out basic blocks and functions** for I-cache/I-TLB locality and straight hot paths (Ch. 12) — recovering several percent *on top of* an already-PGO'd, LTO'd binary, because it optimizes the actual layout the front-end sees. Two prerequisites it will not forgive: link with relocations preserved (`-Wl,--emit-relocs`, binary unstripped) or `llvm-bolt` cannot reorder functions, and feed it LBR samples (`perf record -e cycles:u -j any,u -- ./app`, then `perf2bolt -p perf.data -o perf.fdata ./app`, then `llvm-bolt ./app -o app.bolt -data=perf.fdata -reorder-blocks=ext-tsp -reorder-functions=hfsort -split-functions`). Order matters and they stack: **`-O3 -march` → LTO → PGO → BOLT**, each adding on top.
- **The catch — representative profiles.** PGO/AutoFDO/BOLT are only as good as the profile. A profile from idle/synthetic traffic mis-marks hot/cold and can *pessimize* the real hot path (Ch. 12 pitfall). Profile with realistic captured sessions (Ch. 94), including market-open bursts; refresh profiles as the strategy/code changes. This is the strongest practical reason to capture production traffic.

### 24.4.4 Debug info, frame pointers and unwind tables: what the production binary carries

Ch. 2 and Ch. 5 told you to keep frame pointers and `-g` on the production build and called the cost "small". This section measures it and decides, flag by flag, what the shipped binary carries for the profiler (Ch. 2, 104), the crash handler (Ch. 93) and the post-mortem (Ch. 5). *Toolchain and machine: Appendix L.3; the numbers below are GCC 13.3, `-O2 -std=c++20 -march=native`, on a 4-vCPU Xeon VM (TSC 2.7 GHz) under Linux 6.18 — CPU-bound, and the same shape on the reference machine.*

**1. `-fno-omit-frame-pointer` on the hot kernels.** A decode (`memcpy` + two `bswap`s) feeding a book update (one level, two fields), both `noinline` so the frame-pointer prologue has somewhere to land, one million messages, best of twenty:

```
   build                                   decode+apply (cycles/msg)     .text of the two functions
   ─────────────────────────────────────── ───────────────────────────── ──────────────────────────────────
   -O2 -march=native                       6.00 / 5.94                   reference
   -O2 -march=native -fno-omit-frame-pointer   5.93 / 5.96               +4 bytes per non-leaf (push/mov/pop)
```

**Zero, within noise.** `rbp` is one of fifteen general registers; losing it costs a spill only in a function that was already using all fifteen, and on a modern core the `push %rbp; mov %rsp,%rbp … pop %rbp` sequence is handled by the stack engine and renamer at essentially no cost (Ch. 11). Ch. 5 §5.3 measured the size side (+15 bytes of `.text` for the same program); this is the time side. The "0–2 %" folklore comes from register-starved 32-bit code and from benchmarks dominated by tiny leaf calls that `-fno-omit-frame-pointer` *also* leaves frameless (GCC's `-momit-leaf-frame-pointer` is implied). Keep frame pointers on every production build.

**2. What each stack-capture mechanism costs per frame.** The same program captures its own stack at two depths (Ch. 93 §93.3 has the crash-handler reading of this table):

```
   mechanism                      needs in the binary          per frame        13 frames    37 frames    first call        who uses it
   ────────────────────────────── ──────────────────────────── ──────────────── ──────────── ──────────── ───────────────── ────────────────────────────────
   frame-pointer walk             -fno-omit-frame-pointer       2–4 cycles         23 cyc      152 cyc     —                 perf --call-graph fp, Ch. 93 handler
   _Unwind_Backtrace (.eh_frame)  -fasynchronous-unwind-tables  ~200–230 cycles  2,957 cyc   7,338 cyc    4.8k cyc          C++ exceptions (Ch. 21), libunwind
                                  (default on x86-64)
   glibc backtrace()              same + libgcc_s at runtime    ~200–235 cycles  3,056 cyc   7,308 cyc    984k cyc = 365 µs backtrace_symbols_fd, lazy dlopen
                                                                                                           (dlopen!)
   LBR (hardware)                 nothing                       0 (the CPU records 32 taken branches)      —                 perf --call-graph lbr: depth ≤ 32
                                                                                                                             branches, no program cost (Ch. 104 §104.4.4)
   DWARF copy (perf dwarf)        .eh_frame + -g                copies 8 KiB of stack per SAMPLE into the ring (Ch. 2 §2.5),  perf --call-graph dwarf: unwinds offline,
                                                                unwinds offline                                              costs the NMI path the copy
   SFrame (.sframe)               -Wa,--gsframe (binutils ≥     ~4–8 cycles/frame: a sorted table lookup per frame, no        the kernel's deferred user unwinder
                                  2.41; GCC 15 driver option)   expression interpreter; section ~1/4 of .eh_frame             (landing in 6.18+ mainline), libsframe
```

Read it as a design decision: **frame pointers are the hot-path-safe capture** — two loads per frame, no library, no lock, usable from a signal handler (Ch. 93 §93.2.2) and by `perf` at one NMI's cost — and their only weakness is completeness (leaf functions and tail calls leave no frame unless you also pay `-mno-omit-leaf-frame-pointer` and `-fno-optimize-sibling-calls`, a few bytes per leaf and the loss of tail-call optimisation on the paths you compile them into). DWARF CFI (`_Unwind_Backtrace`, libunwind) is fifty times dearer per frame and must be pre-warmed; `backtrace()`'s first call is a third of a millisecond and a `dlopen`. LBR costs the program nothing but gives the profiler at most 32 branches of history. SFrame is the new middle: a compact, table-driven format (`.sframe`, from the assembler) that the kernel can walk without frame pointers and without DWARF — the user-space deferred unwinding that makes it usable for `perf` was still landing in mainline around 6.18, so treat it as the direction of travel and keep frame pointers until your kernel and `perf` consume it.

**3. What the production binary carries.** The flags, and the policy this book uses:

```
   flag                              adds                                                    run-time cost   ship it?
   ───────────────────────────────── ─────────────────────────────────────────────────────── ─────────────── ──────────────────────────────────────────────
   -g (DWARF 5, GCC 11+/Clang 14+)   .debug_* sections: types, locations, lines                none (Ch. 5    yes at build; strip at package time and ARCHIVE
                                                                                                §5.3: .text     the .debug by build ID (objcopy --only-keep-debug,
                                                                                                identical)      --add-gnu-debuglink; debuginfod serves it)
   -g1 (GCC) / -gline-tables-only    line tables + function names only (~1/3 the size)         none            the minimum that makes perf annotate and crash
   (Clang)                                                                                                     frames resolve; use -g and archive instead
   -gsplit-dwarf (+ -gdwarf-5)       bulk DWARF in per-object .dwo; .dwp packs them             none            yes for large binaries (link time, Ch. 5 §5.3)
   -gz=zstd / =zlib                  compressed .debug_* (binutils 2.40+ for zstd)              none            yes on the archived .debug
   -fasynchronous-unwind-tables      .eh_frame for every function, even with -fno-exceptions    none (data)     ALWAYS — perf dwarf, gdb, libunwind and the
                                     (x86-64 default; -fno-asynchronous-unwind-tables drops it)                 terminate path all need it (Ch. 21 §21.4.3)
   -fno-omit-frame-pointer           push/mov/pop per non-leaf; rbp reserved                    0 ± 1 % (above) ALWAYS
   -mno-omit-leaf-frame-pointer      frames in leaves too                                       a few bytes/leaf for complete crash stacks (Ch. 93 §93.4.1); measure
   -fno-optimize-sibling-calls       no tail-call elimination                                   measure         same reason; usually only on the strategy TU
   -frecord-gcc-switches             .GCC.command.line section with the flags                   none            yes — Appendix N.2 reads it back
   -Wa,--gsframe (GCC 13/14) /       .sframe unwind table                                       none (data)     when the kernel/perf you run consume it
   -fsframe (GCC 15)
   -Wl,--build-id=sha1               NT_GNU_BUILD_ID note (distro default)                      none            ALWAYS — the key to everything archived
```

The convention, in one build line: `-O2 -g -march=<colo uarch> -fno-omit-frame-pointer -frecord-gcc-switches -Wl,--build-id=sha1`, strip at package time, archive `.debug` by build ID, keep `.eh_frame` (never `-fno-asynchronous-unwind-tables`), and add `-mno-omit-leaf-frame-pointer -fno-optimize-sibling-calls` to the TUs whose crash stacks you want complete after measuring them with the Ch. 3 harness. Appendix D.4 and D.7 carry the rows.

### 24.4.5 Operating PGO/BOLT: the profile lifecycle

§24.4.3 ends with "refresh profiles as the strategy/code changes"; this is the procedure, with the costs measured in Ch. 102 §102.3 on a decoder-style dispatch kernel (GCC 13.3 / Clang 18.1, the reference VM): PGO trained on the open's message mix bought −13 % (GCC) / −10 % (Clang) on the open and cost **+3 % / +6 %** on the midday mix — a *stale-workload* profile; a profile from the previous source revision, forced through after a one-function change, cost **+13–17 %** against a fresh one (GCC refuses by default with `-Werror=coverage-mismatch`; Clang 18 built silently). The instrumented GCC build (`-fprofile-update=atomic`) ran 3.5× slower, Clang's front-end instrumentation 3 % slower, which decides where training can run.

```
   TRAIN    on a named replayed capture (Ch. 94–95) containing the open and the day — never synthetic, never idle; at 0.3× speed into the instrumented build
            GCC:   g++ -O2 -march=… -fprofile-generate [-fprofile-update=atomic] -c x.cpp -o build/x.o   (the .gcda is keyed by the OBJECT name: keep -o identical in the -fprofile-use compile)
            Clang: clang++ -O2 -fprofile-instr-generate …;  LLVM_PROFILE_FILE="prof/%m-%p.profraw" ./app_instr
   MERGE    llvm-profdata merge -output=rev.profdata prof/*.profraw   [-weighted-input=3,open.profdata -weighted-input=1,midday.profdata]
            gcov-tool merge -w 3,1 dirA dirB   (GCC; or let the runtime accumulate .gcda across runs)
   STORE    the profile is a build input keyed by (source revision, capture id, date) and named in the release manifest (Ch. 103 §103.4.1); the hermetic build (Ch. 25) pins it
   USE      -fprofile-use -fprofile-correction [-fprofile-partial-training]  /  -fprofile-instr-use=rev.profdata;  then LTO;  then BOLT from an LBR profile of the SAME replay, as the last build step, every time
   GATE     mismatch diagnostics fatal — GCC: -Werror=coverage-mismatch (default) -Werror=missing-profile;  Clang: -Werror=profile-instr-out-of-date -Werror=profile-instr-unprofiled;
            capture age < D days;  the replay gate (Ch. 102 §102.4.4) runs the PGO'd binary on the open AND a midday window and fails if either is slower than plain -O2
   REFRESH  on every change to a hot-path TU, weekly, after any venue/feed change;  AutoFDO (perf record -b on production → create_llvm_prof / create_gcov → -fprofile-sample-use / -fauto-profile) refreshes between instrumented trainings at ~zero runtime cost
```

The lifecycle's shape is Ch. 102's for a benchmark baseline — a measured artefact with provenance, a validity window and a gate — because a profile *is* a performance claim about the workload, and it decays like one. Appendix D.6 lists the flags; Appendix I.9 the one-liners.

## 24.5 Pitfalls & anti-patterns: `-Ofast` hazards, portability loss

- **`-Ofast`/`-ffast-math` changing your numbers.** `-Ofast` silently enables `-ffast-math`: FP reassociation, no NaN/Inf handling, flush-to-zero — which can change computed prices/risk in the last bits, break cross-build **determinism** (Ch. 30, 94 — replay must be bit-exact), and miscompile code that relies on NaN checks. Never blanket-enable it on financial math; opt into specific, understood relaxations (e.g. `-fno-math-errno`) and keep determinism-critical code at strict FP (Ch. 30). 
- **`-march=native` deployment SIGILL.** A binary built `-march=native` on a newer build host **crashes with SIGILL** on an older run host lacking those instructions. Build for the *exact deployment uarch* (`-march=icelake-server`, not `native`) or a fleet-safe baseline; verify the run hardware (Ch. 6). This is a classic colo outage cause.
- **Assuming `-O3` ≥ `-O2`.** `-O3` can *regress* a front-end-bound path by bloating the I-cache (Ch. 12) or making unhelpful inlining/unrolling choices. It's a measure-it choice per component, not a free upgrade — benchmark both (§24.3).
- **Over-aggressive inlining bloat.** Cranking `-finline-limit`/`flatten` pulls large cold callees into hot functions, blowing the I-cache (Ch. 12) and slowing the very path you meant to speed up. Tune inlining by the front-end counters, not by maximizing it.
- **LTO/PGO with mismatched or stale inputs.** Mixing LTO and non-LTO objects, or using a stale/unrepresentative PGO profile, yields broken builds or *pessimized* layout. Build consistently; refresh profiles from representative captures (Ch. 94).
- **Benchmarking the wrong flags.** Profiling a `-O0`/`-Og` debug build, or a different `-march` than you deploy, produces numbers that don't reflect production. Always benchmark (Ch. 3) the *shipping* flag set on the *deployment* hardware.
- **Forgetting `-fno-omit-frame-pointer`, or dropping `.eh_frame`.** Stripped frame pointers make `perf` call stacks unreliable (Ch. 2) and leave the crash handler nothing to walk (Ch. 93); `-fno-asynchronous-unwind-tables` "to save space" with `-fno-exceptions` removes the tables `perf`, gdb and libunwind need. Keep frame pointers on *every* build — the cost is 0 ± 1 % on the hot kernels (§24.4.4), not the folklore 2 % — and keep `.eh_frame`; a toolchain choice that affects your ability to *measure* and *diagnose* everything else.
- **Letting flags drift across the codebase.** Different TUs built with different `-march`/FP/`-fno-exceptions` settings can violate ODR or interact badly (e.g. a `noexcept`/exception mismatch, ABI differences). Centralize the optimization flag policy; document per-TU exceptions deliberately.
- **Ignoring the asm.** The recurring sin of Part III: trusting a flag did what its name suggests. `-march=native` doesn't guarantee vectorization (aliasing — Ch. 23); LTO doesn't guarantee the inline you wanted. Confirm in the disassembly (Ch. 4).

## 24.6 Exercises & checklist

*(The full categorized flag reference is Appendix D.)*

**Exercises**

1. **Build the matrix.** Take a hot TU with a numeric kernel and a dispatch loop; run `build_matrix.sh` across `-O0/-O2/-O3/-O3 -march=native/-O3 -march=native -flto`. Tabulate ns/op *and* `.text` size. Where is the giant step? Where does `-O3` help vs not? Where does `-march`/LTO pay (§24.3)?
2. **Confirm the ISA.** Disassemble (Ch. 4) the numeric kernel at `-O3` vs `-O3 -march=native`. Does AVX-512 (`zmm`)/FMA actually appear with `native`? Use `-Rpass=loop-vectorize`/`-fopt-info-vec`. If it *didn't* vectorize, why (aliasing — Ch. 23)?
3. **`-march=native` portability.** Build `-march=native` on the newest CPU you have and run on an older one (or use `-march=icelake-server` then run on Haswell). Reproduce the SIGILL; then fix it with an explicit deployment `-march`. 
4. **LTO across TUs.** Split a hot accessor and its caller into two `.cpp` files. Confirm (asm) the call is *not* inlined without LTO and *is* with `-flto` (Clang: `-flto=thin`). Measure the difference and the link-time cost (§24.4.2).
5. **PGO end-to-end.** Instrument (`-fprofile-generate`) a dispatch-heavy program, run a representative message replay, rebuild (`-fprofile-use`). Compare ns/op, `.text` layout (`readelf -S`/hot-section placement — Ch. 12), and `br_misp_retired.indirect` (Ch. 14) vs the non-PGO build. Then try a *bad* (idle-traffic) profile — does it regress?

**Checklist — build toolchain**

- [ ] Production builds use at least **`-O2`** (never `-O0`); `-O3` is applied where **measured** to help (often numeric kernels) and *not* where it bloats a front-end-bound path (Ch. 12).
- [ ] `-march`/`-mtune` target the **exact deployment (colo) uarch** for reproducible builds — **not `-march=native`** unless build and run hardware are identical (else SIGILL — §24.5).
- [ ] I **verified the intended transform in the asm** (Ch. 4): vectorization actually emitted (`zmm`/FMA), the inline/devirt actually happened — flags aren't trusted by name.
- [ ] **LTO** (ThinLTO on Clang, `-flto=auto` on GCC, by default) is on for the production binary so inlining/devirtualization (Ch. 14) go cross-TU; objects and link use matching flags.
- [ ] **PGO** (or AutoFDO) and **BOLT** are applied to the large branchy binary with a **representative captured profile** (Ch. 94), in the order `-O3 -march → LTO → PGO → BOLT`.
- [ ] **No `-Ofast`/`-ffast-math`** on determinism- or precision-critical financial math (Ch. 30); FP relaxations are opted into specifically and documented.
- [ ] **Every** build keeps **frame pointers** (0 ± 1 %, §24.4.4), `.eh_frame`, `-g` and a build ID; debug info is archived by build ID at package time; `-frecord-gcc-switches` is on; leaf-frame/sibling-call flags are decided per TU by measurement.
- [ ] The optimization-flag **policy is centralized** (consistent `-march`/FP/exception settings across TUs), with per-TU exceptions deliberate (ties Ch. 21); the full reference is **Appendix D**.

## 24.7 References

- GCC and Clang documentation — the optimization-level (`-O*`), `-march`/`-mtune`, inlining, LTO (`-flto`/`-flto=auto`; Clang `-flto=thin`), PGO (`-fprofile-generate`/`-fprofile-use`, `-fauto-profile`), and FP (`-ffast-math` and friends) flag references; the authoritative basis for this chapter and **Appendix D**.
- M. Panchenko et al., *"BOLT: A Practical Binary Optimizer for Data Centers and Beyond"* — post-link layout optimization and how it stacks on PGO/LTO (§24.4.3).
- D. Chen et al., *"AutoFDO: Automatic Feedback-Directed Optimization for Warehouse-Scale Applications"* — sample-based PGO from production profiles (§24.4.3).
- A. Fog, *Optimizing software in C++* and *The microarchitecture of Intel/AMD CPUs* — the effect of `-march`/`-mtune` and instruction selection on real microarchitectures (§24.4.1).
- The LLD/mold and gold linker documentation — link speed and section ordering relevant to LTO and code layout (§24.2, ties Ch. 12).

## 24.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical chapters on PGO, LTO, BOLT, and reading their effect with `perf`.
- The Meta/Google engineering writeups on BOLT, Propeller, and AutoFDO at scale — real-world profile-guided layout on large latency-sensitive services.
- Ch. 12 (*Code Layout*) — the I-cache/front-end locality PGO/BOLT optimize; Ch. 14 (*Devirtualization*) — what LTO/PGO devirtualize; Ch. 23 (*Aliasing*) & Ch. 32 (*SIMD*) — what `-march`/`-O3` vectorize; Ch. 30 (*Floating-Point*) — the `-ffast-math` hazards; Ch. 94 (*Capture & Replay*) — the representative profiles PGO/BOLT need.
- **Appendix D** (Compiler Flag Reference) — the full categorized, latency-annotated flag cheat-sheet this chapter summarizes; **Appendix C** (System Tuning Checklist) — the runtime side of the deployment story.

---

*Next: Ch. 25 — Build Systems, Build-Time Hygiene & Hermetic Toolchains, closing Part III: the build as a latency path (`-ftime-trace`, PCH, unity builds, `extern template` where it helps and where it hurts, C++20 modules where the toolchain can), and the build as a correctness boundary — one flag policy that cannot drift into an ODR violation, a pinned toolchain, and bit-for-bit reproducible binaries keyed by build ID.*
