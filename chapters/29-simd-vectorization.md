# Part V — Numerics & Data Parallelism

# Chapter 29 — SIMD & Vectorization

> **Prerequisites:** Ch. 11 (accumulator splitting — SIMD is its vector conclusion; ILP), Ch. 21 (aliasing/`restrict` — the #1 vectorization blocker), Ch. 8–9 (SoA layout & alignment — what vectorization needs), Ch. 22 (`-march`/`-O3` — enabling SIMD), Ch. 27 (FP reassociation — vectorized reductions need it), Ch. 4 (reading asm — §29.5).
>
> **Leads into:** Ch. 53 (vectorized parsing/validation of market data), Ch. 25 (SIMD-probed hash maps), Ch. 28 (SIMD bit ops). Closes **Part V**. The downclocking caveat ties to Ch. 6 (frequency) and Ch. 43 (SMT). Appendix A covers NEON/SVE on ARM.

---

## 29.1 Why it matters: data parallelism for batch work

SIMD — Single Instruction, Multiple Data — does the *same* operation on a whole vector of values at once: one AVX2 instruction adds eight 32-bit integers (or four doubles) in the time of one scalar add; AVX-512 doubles that to sixteen. For **batch** work — operations applied uniformly across an array — that's a 4-16× throughput multiplier on the arithmetic itself, the largest per-instruction speedup available short of a different algorithm. It's the vector generalization of the accumulator-splitting ILP from Ch. 11: instead of keeping several independent scalar chains in flight, you pack the lanes into one register and the hardware runs them in genuine parallel.

In a trading system SIMD pays off in the **batch-shaped** parts: scanning and validating a buffer of market-data messages (Ch. 53 — comparing many bytes, finding delimiters, validating ranges in parallel), computing a vector of signals or P&L across many instruments, risk and pricing math over portfolios (Greeks, Monte Carlo — Ch. 67 contrasts GPU), parsing (SIMD `from_chars`, SWAR delimiter search), and the probing of SwissTable-style hash maps (Ch. 25). The common thread: a *loop over an array doing uniform work* is a vectorization candidate, and turning it from scalar into SIMD is often the single biggest win on that kernel.

But SIMD comes with caveats sharp enough that naive use can *lose*, and this chapter is as much about *when not to* as *how to*. It needs the right **data layout** (SoA, Ch. 8 — AoS defeats it) and **alignment** (Ch. 9); it's blocked by **aliasing** (Ch. 21 — the most common reason a loop won't vectorize) and **FP non-associativity** (Ch. 27 — a reduction won't vectorize without reassociation permission); and — the signature SIMD trap — heavy **AVX-512 downclocking**: wide vector instructions can be power-hungry enough that the CPU *lowers its clock frequency* while (and after) they run, so a kernel that's faster in isolation can slow down *everything else on the core*, including the latency-critical scalar hot path — a net loss for a tick-to-trade box that does a little SIMD amid lots of scalar work. The latency-vs-throughput tension of Ch. 1 is acute here: SIMD is a *throughput* win, and the tick-to-trade path is *latency*-bound, so SIMD belongs on the **batch/parallel** parts (feed scanning, risk) and is often *wrong* on the single-message critical path. This chapter measures the win and the downclocking (§29.3), shows the layout/intrinsics/width techniques (§29.4), and verifies the vector codegen (§29.5) — with the discipline to use SIMD where batch throughput matters and keep it off the core when it would tax the latency path.

## 29.2 Mental model: auto-vectorization; SSE/AVX/AVX-512; downclocking

**The SIMD register model.** SIMD packs multiple values into one wide register and operates on all lanes at once:

```
   scalar:  add  a0+b0 = c0                         (one op, one result)
   SIMD:    vaddps [a0 a1 a2 a3 a4 a5 a6 a7]
                 + [b0 b1 b2 b3 b4 b5 b6 b7]        (one op, EIGHT results — AVX, 8×float)
                 = [c0 c1 c2 c3 c4 c5 c6 c7]
```

The x86 vector ISAs, by width:

- **SSE** (128-bit, `xmm`): 4×float / 2×double / 16×int8 — the baseline, available everywhere.
- **AVX/AVX2** (256-bit, `ymm`): 8×float / 4×double / 32×int8 — the sweet spot for most servers; modest downclocking.
- **AVX-512** (512-bit, `zmm`): 16×float / 8×double / 64×int8, plus **mask registers** (`k0-k7`) for per-lane predication and many new ops — the widest, but the most downclocking (§29.2 below), and not on all CPUs.

**Two ways to get SIMD:**

- **Auto-vectorization (let the compiler do it).** At `-O3 -march=native` (Ch. 22) the compiler vectorizes loops it can *prove* safe — which requires no aliasing (Ch. 21 — `restrict`), a vectorizable access pattern (contiguous, SoA — Ch. 8), permission to reassociate FP reductions (Ch. 27 — `-ffast-math`), and known alignment (Ch. 9). When it works, it's free and portable. When it *doesn't* (and it often silently doesn't), you read the vectorization report (`-fopt-info-vec`/`-Rpass`) and the asm (§29.5) to find the blocker.
- **Intrinsics (do it yourself).** `<immintrin.h>` exposes the instructions directly (`_mm256_add_ps`, `_mm512_loadu_ps`, …) — full control, guaranteed vectorization, but verbose, ISA-specific, and unportable. Use when the compiler won't auto-vectorize a kernel that matters (§29.4.2). Wrapper libraries (`std::experimental::simd`, Highway, xsimd, Eve) give portable, readable SIMD across ISAs — usually the better choice than raw intrinsics.

**Downclocking — the signature caveat.** Wide SIMD draws a lot of power, so Intel CPUs (especially Skylake-SP through Ice Lake) run AVX2/AVX-512 at *reduced frequency* — there are "AVX2" and "AVX-512" frequency licenses, each lower than the base (non-AVX) turbo. Worse, the frequency reduction can apply to the *whole core* and **persist for ~milliseconds after** the SIMD code finishes, and can affect the *sibling SMT thread* (Ch. 43). So a heavy-AVX-512 kernel can lower the clock of the *scalar* code running before/after/alongside it — including your latency-critical hot path. The net effect depends on how *much* SIMD vs scalar work the core does: a SIMD-dominated batch core benefits; a latency core doing occasional SIMD amid scalar critical-path work can *lose overall* even though the kernel itself sped up (§29.3). Newer cores (Ice Lake, and especially the latest) have *much* milder AVX-512 downclocking, but it's a per-microarchitecture fact you must measure (§29.3), not assume away.

The model: **SIMD does N lanes per instruction (4-16× throughput) for uniform batch work, via auto-vectorization (needs no-alias + SoA + alignment + FP-reassoc) or intrinsics. It's a *throughput* tool — right for batch/parallel work, often wrong on the latency critical path — and wide AVX-512 can *downclock the core*, so the kernel's local speedup must be weighed against its effect on everything else on that core.**

## 29.3 Measure it: scalar vs vectorized throughput (and frequency effects)

Two measurements: the **vectorization throughput win** on batch work, and the **downclocking effect** on surrounding scalar work — because the second is what decides whether SIMD belongs on a given core.

```cpp
// simd.cpp — scalar vs auto-vectorized vs AVX-512 dot product; observe throughput.
// Build:  g++ -O3 -std=c++20 -march=native simd.cpp -o simd          (auto-vec)
//         g++ -O3 -std=c++20 -mavx2 simd.cpp -o simd_avx2            (cap at AVX2)
//         g++ -O3 -std=c++20 -mno-avx simd.cpp -o simd_sse           (SSE only)
// Run pinned, turbo policy fixed:  taskset -c 2 ./simd
#include <cstdio>
#include <cstdint>
#include <vector>
#include <chrono>

// restrict (Ch. 20) so the compiler may vectorize; SoA float arrays (Ch. 7).
float dot(const float* __restrict a, const float* __restrict b, int n) {
    float s = 0.f;
    for (int i = 0; i < n; ++i) s += a[i] * b[i];   // needs -ffast-math OR multiple accumulators to vectorize the reduction
    return s;
}

int main() {
    constexpr int N = 4096; constexpr long REPS = 4'000'000;
    std::vector<float> a(N, 1.0001f), b(N, 0.9999f);
    auto t0 = std::chrono::steady_clock::now();
    float s = 0;
    for (long r = 0; r < REPS; ++r) s += dot(a.data(), b.data(), N);
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("dot s=%g  %.4f ns/elem\n", (double)s, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, AVX-512), pinned, turbo off (illustrative; build with `-ffast-math` or split accumulators so the reduction vectorizes — Ch. 27):

```
   build / width            ns / elem     speedup    note
   SSE (4-wide)              ~0.50 ns       1.0x      baseline 128-bit
   AVX2 (8-wide)             ~0.26 ns       ~1.9x     256-bit; mild downclock
   AVX-512 (16-wide)         ~0.16 ns       ~3.1x     512-bit; more downclock (Ice Lake: mild)
   scalar (no vec)           ~1.6 ns        0.3x      reduction didn't vectorize (no -ffast-math)
```

And the **downclocking** measurement — the one that matters for a latency box. Run a heavy AVX-512 kernel in a loop on one core while timing a *scalar* latency-sensitive task; compare the scalar task's latency with the SIMD load present vs absent, and watch the core frequency (`perf stat -e cycles,ref-cycles`, or `turbostat`):

```
   scalar task, core idle otherwise           ~100 ns   @ ~3.5 GHz (full turbo)
   scalar task, while heavy AVX-512 on core    ~118 ns   @ ~3.0 GHz  <- downclock taxed the scalar path
   (effect lingers ~ms after the SIMD stops; can affect the SMT sibling — Ch. 41)
```

Read it two ways. (1) **The kernel win is real**: vectorization gives ~2-3× on the dot product (and the scalar row shows the reduction *didn't* vectorize without FP-reassociation permission — Ch. 27 — a common silent miss). (2) **The downclock is also real**: the *scalar* task got ~18% slower while AVX-512 ran on the core, because the core dropped frequency — so on a latency core, the AVX-512 kernel's local speedup can be *outweighed* by slowing the critical path around it. The decision rule: **SIMD wins where the core is SIMD-dominated (a batch/risk core); it can lose where a little SIMD shares a core with latency-critical scalar work.** Measure *both* the kernel speedup *and* the frequency effect on the surrounding work (`turbostat`, the scalar-task latency with/without SIMD) before putting wide SIMD on a tick-to-trade core. (Modern cores downclock far less; check yours.)

## 29.4 Techniques

### 29.4.1 Data layout for vectorization

SIMD's hard prerequisite is layout — the Ch. 8–9 work is what *enables* vectorization:

- **SoA, not AoS (Ch. 8).** SIMD loads N *contiguous, same-field* values into a lane vector. Struct-of-Arrays (`prices[]`, `qtys[]`) puts each field contiguous → one vector load grabs 8 prices. Array-of-Structs (`Order{price,qty,...}[]`) interleaves fields → a vector load grabs garbage (price, qty, price, qty…) and the compiler must *gather* (slow) or give up. **SoA is the single most important enabler of vectorization.** (Hybrid AoSoA — arrays of small SoA blocks — balances SoA's SIMD-friendliness with AoS's per-record locality.)
- **Alignment (Ch. 9).** Aligned vector loads/stores (`vmovaps`) are faster than unaligned (`vmovups`), and AVX-512 especially benefits; align hot arrays to the vector width (32B for AVX2, 64B for AVX-512 — also a cache line) with `alignas`/aligned allocation, and tell the compiler (`__builtin_assume_aligned`). Misalignment forces a scalar prologue/epilogue and slower loads (§29.6).
- **Contiguous, unit-stride access.** Vectorization wants `a[i]`, `a[i+1]`, … contiguous; strided/gather access (`a[idx[i]]`) is far slower (gather instructions exist but are weak). Restructure to unit stride where possible.
- **No aliasing (Ch. 21) and FP-reassoc permission (Ch. 27).** `restrict` the pointers (the #1 auto-vec blocker) and allow FP reassociation (`-ffast-math` or multiple accumulators) for reductions, or the compiler *can't* vectorize — the scalar-row miss in §29.3. These two are the most common "why won't it vectorize" answers.
- **Trip count and remainder.** Vectorized loops process N-at-a-time with a scalar remainder for `n % N`; very short loops may not be worth vectorizing (the setup/remainder dominates). Size and pad arrays to vector multiples where it helps.

### 29.4.2 Intrinsics when the compiler won't

When auto-vectorization fails or is suboptimal on a kernel that matters, drop to explicit SIMD:

- **When to reach for intrinsics.** The compiler won't vectorize (a blocker you can't remove, a pattern it doesn't recognize — horizontal ops, shuffles, saturating arithmetic, byte-wise compares for parsing), or it vectorizes *poorly*. Confirm the failure first (`-fopt-info-vec-missed`/asm — §29.5); often fixing layout/aliasing (§29.4.1) lets the compiler do it for free, which is preferable.
- **Prefer a portable wrapper over raw intrinsics.** `std::experimental::simd` (Parallelism TS / C++26 direction), Google **Highway**, **xsimd**, **Eve** give readable, portable SIMD that compiles to the best ISA available and falls back gracefully — far more maintainable than `_mm512_*` intrinsics hand-written per ISA. Use raw intrinsics only for the last few percent or an op the wrappers lack.
- **The common SIMD idioms.** Load (`loadu`/`load`), arithmetic (`add`/`mul`/`fmadd`), compare-to-mask + blend (predication — the SIMD form of branchless select, Ch. 13/28), horizontal reduce (sum/min/max across lanes — needed at the end of a reduction), shuffle/permute, gather/scatter (use sparingly). Market-data scanning uses byte compares + mask + `popcount`/`tzcnt` (Ch. 28) to find delimiters/matches across 32-64 bytes at once (Ch. 53).
- **Masking (AVX-512).** `k`-mask registers do per-lane predication natively — conditional vector ops without branches or blends, the clean way to vectorize data-dependent work. Powerful, but AVX-512-only (downclock/portability — §29.6).
- **Keep a scalar fallback and runtime-dispatch.** For portability across CPUs (and the AMD/ISA-availability reality — Ch. 22, 28), compile multiple ISA versions and dispatch at runtime on CPU features (`__builtin_cpu_supports`, function multiversioning, or the wrapper's dispatch). Never ship an AVX-512 binary that SIGILLs on a non-AVX-512 box (Ch. 22).

### 29.4.3 Choosing the ISA width

Wider is *not* automatically better — the width choice is a measured trade against downclocking and portability:

- **AVX2 (256-bit) is often the sweet spot** for a mixed-workload / latency-adjacent core: a solid 8-wide (float) / 4-wide (double) speedup with *mild* downclocking, and near-universal availability on modern servers. Many shops cap at AVX2 on latency cores precisely to avoid AVX-512's frequency hit.
- **AVX-512 (512-bit) for SIMD-dominated batch cores.** Where the core spends most of its time in vector work (risk, Monte Carlo, large scans), 16-wide + masking + the richer instruction set wins despite downclocking — and on newer cores the downclock is mild enough that AVX-512 is broadly good. But on a core that interleaves SIMD with latency-critical scalar work, AVX-512's frequency hit (and its lingering/sibling effects — §29.3, Ch. 43) can make AVX2 the better *overall* choice. **Decide per core based on the SIMD-vs-scalar mix, measured (§29.3).**
- **SSE (128-bit) as a portable floor.** Always available; the fallback in runtime dispatch and the choice when the data is small or downclocking must be avoided entirely.
- **Match the frequency-license reality to the box (Ch. 6).** Whether AVX-512 downclocks badly is a per-microarchitecture fact; check your CPU (and Ice Lake/newer are much better than Skylake-SP). On an isolated latency box you also control turbo/governor (Ch. 6), so measure the actual frequency behavior under your SIMD load.
- **ARM/Graviton (Appendix A).** NEON (128-bit fixed) and SVE/SVE2 (scalable, vector-length-agnostic) are the AArch64 equivalents; SVE's length-agnostic model changes how you write portable SIMD. Covered in Appendix A.

## 29.5 Verify the codegen: vector instruction selection

Whether a loop vectorized — and to what width — is visible in the asm, and it's the verification that the technique fired. Snippets are **verified** Clang output (`--target=x86_64-linux-gnu -O3`).

**Scalar (didn't vectorize) vs packed.** A reduction without FP-reassociation permission stays scalar (`...ss`/`...sd`, one lane); with `-ffast-math`/`-march` it goes packed (`...ps`/`...pd`, N lanes):

```asm
; dot product, -O3 (no fast-math):  SCALAR — reduction not reassociated (Ch. 26)
.loop:  vfmadd231ss xmm0, xmm1, [rsi+rax]    ; ss = scalar single, ONE lane

; dot product, -O3 -march=native -ffast-math:  PACKED AVX-512 (16 lanes) + multiple accumulators
.loop:  vfmadd231ps zmm0, zmm4, [rsi+rax]    ; ps + zmm = 16 floats/op
        vfmadd231ps zmm1, zmm4, [rsi+rax+64] ; multiple zmm accumulators (Ch. 10 ILP)
        ...
        ; + horizontal reduce of zmm0..zmm3 at the end
```

**Width is in the register name.** `xmm` = 128-bit (SSE, 4×f), `ymm` = 256-bit (AVX, 8×f), `zmm` = 512-bit (AVX-512, 16×f). Reading the register tells you the width the compiler chose:

```asm
        vaddps  ymm0, ymm0, [rdi]    ; 256-bit, 8 floats  (AVX2)
        vaddps  zmm0, zmm0, [rdi]    ; 512-bit, 16 floats (AVX-512)
```

**Aligned vs unaligned loads.** `vmovaps` (aligned) vs `vmovups` (unaligned) — and a misaligned loop emits a scalar prologue to reach alignment:

```asm
        vmovaps ymm0, [rdi]          ; aligned (fast) — array was 32B-aligned (Ch. 8)
        vmovups ymm0, [rdi]          ; unaligned (compiler couldn't prove alignment)
```

The verification habits: **for a kernel you expect to vectorize, disassemble and confirm packed ops (`...ps`/`...pd`) at the width you intended (`ymm`/`zmm`)** — `...ss`/`...sd` scalar ops mean it *didn't* vectorize (check aliasing — Ch. 21, FP-reassoc — Ch. 27, layout — §29.4.1, via `-fopt-info-vec-missed`); **confirm aligned loads** (`vmovaps`) where you aligned the data; and **confirm the width matches your ISA decision** (§29.4.3 — you didn't accidentally get `zmm`/AVX-512 on a core where you wanted AVX2 to avoid downclock). The vectorization report plus the asm is how you turn "I hope it vectorized" into "I confirmed it did, at the width I chose."

## 29.6 Pitfalls & anti-patterns: AVX-512 downclocking; misaligned loads

- **AVX-512 downclocking taxing the latency path.** The signature SIMD trap: heavy AVX-512 lowers the core frequency (and lingers ~ms, can hit the SMT sibling — Ch. 43), so a kernel that's faster *in isolation* slows the *scalar* critical-path work around it — a net loss on a latency core (§29.3). Measure the frequency effect on surrounding work; consider capping at AVX2 on latency cores. (Newer cores: much milder — check.)
- **Misaligned loads / no alignment.** Unaligned data forces `vmovups` and a scalar prologue/epilogue, losing much of the win (§29.5). Align hot arrays to the vector width (Ch. 9) and tell the compiler.
- **AoS layout defeating vectorization (Ch. 8).** Interleaved fields force gathers or block vectorization entirely. SoA (or AoSoA) is the prerequisite; "I added SIMD but it didn't help" is usually a layout problem.
- **Aliasing blocking auto-vec (Ch. 21).** No `restrict` → the compiler assumes overlap → scalar code (or a runtime guard). The #1 reason a loop won't vectorize. `restrict` the pointers and verify (§29.5).
- **Forgetting FP reassociation for reductions (Ch. 27).** A sum/dot reduction won't vectorize without `-ffast-math`/multiple accumulators (the scalar row in §29.3) — and enabling reassociation *changes the result bits* (determinism — Ch. 27). Decide deliberately; don't blanket `-ffast-math` on replay-critical math.
- **SIMD on the single-message latency path.** SIMD is a *throughput* tool; on the tick-to-trade critical path (one message, latency-bound) it often doesn't help (no batch to parallelize) and may downclock. Use it on *batch* work (feed scanning, risk over many instruments), not reflexively on the critical path (Ch. 1's latency-vs-throughput).
- **AVX-512 binary SIGILL / no fallback.** Shipping AVX-512 code that runs on a non-AVX-512 CPU crashes (Ch. 22). Runtime-dispatch on CPU features with a scalar/SSE fallback (§29.4.2).
- **Hand-written intrinsics where the compiler would do it.** Verbose, unportable intrinsics for a loop that auto-vectorizes once you fix layout/aliasing — wasted effort and a maintenance burden. Try fixing the blocker first; use a portable wrapper (Highway/xsimd) over raw intrinsics.
- **`vzeroupper` / AVX-SSE transition penalties.** Mixing legacy SSE and VEX-encoded AVX without `vzeroupper` causes transition stalls; compilers usually handle it, but hand-written intrinsics mixing encodings can hit it. Be aware when interfacing asm/intrinsics.
- **Short loops / no batch.** Vectorizing a loop too short to amortize setup + remainder is a loss. Vectorize where the trip count is large enough to pay (§29.4.1).

## 29.7 Exercises & checklist

**Exercises**

1. **Width sweep.** Build `simd.cpp` at SSE / AVX2 / AVX-512 (`-mno-avx` / `-mavx2` / `-march=native`), with `-ffast-math` so the reduction vectorizes. Measure ns/elem; confirm ~2× per width doubling (minus downclock). Disassemble (§29.5) — `xmm`/`ymm`/`zmm`?
2. **Why it didn't vectorize.** Build *without* `-ffast-math` and *without* `restrict`; use `-fopt-info-vec-missed`/`-Rpass-missed=loop-vectorize` to see the reasons. Add `restrict` (Ch. 21), then FP-reassoc (Ch. 27), and watch each unblock vectorization (§29.4.1).
3. **Measure downclocking.** Run a heavy AVX-512 loop on a core while timing a scalar latency task on the *same* core; record the scalar task's latency and the core frequency (`turbostat`) with/without the SIMD load. Quantify the downclock tax (§29.3). Repeat capped at AVX2 — is the *overall* result better?
4. **SoA vs AoS.** Vectorize a computation over `Order{price,qty}[]` (AoS) vs `prices[]`,`qtys[]` (SoA). Confirm (asm + `-fopt-info`) the AoS version gathers/doesn't vectorize and SoA does; measure (§29.4.1, Ch. 8).
5. **SIMD market-data scan (preview Ch. 53).** Use byte-compare + mask + `tzcnt` (Ch. 28) to find a delimiter across 32 bytes at once vs a scalar byte loop. Measure; confirm the vector compare in the asm.

**Checklist — SIMD & vectorization**

- [ ] SIMD is applied to **batch/throughput** work (feed scanning, risk/pricing over many instruments), **not** reflexively on the single-message latency critical path (Ch. 1).
- [ ] Data is **SoA (or AoSoA)** and **aligned** to the vector width (Ch. 8–9); pointers are **`restrict`** (Ch. 21) and FP reductions have **reassociation permission** (Ch. 27) — the auto-vec prerequisites.
- [ ] I **verified vectorization in the asm** (§29.5): packed `...ps`/`...pd` at the **intended width** (`ymm`/`zmm`), aligned loads — not scalar `...ss`/`...sd`; used `-fopt-info-vec`/`-Rpass` to diagnose misses.
- [ ] The **ISA width is chosen per core** by the SIMD-vs-scalar mix (AVX2 sweet spot for latency-adjacent cores; AVX-512 for SIMD-dominated cores), with the **downclocking effect measured** (§29.3, §29.4.3).
- [ ] **AVX-512 downclocking** is checked on the deployment CPU and weighed against the surrounding scalar latency path (Ch. 43) — capped at AVX2 where it taxes the hot path.
- [ ] Where auto-vec won't work, I use a **portable wrapper** (Highway/xsimd/`std::simd`) over raw intrinsics, with **runtime CPU-feature dispatch + scalar fallback** (no SIGILL — Ch. 22).
- [ ] Vectorized reductions' **determinism** impact (FP reassociation changes bits — Ch. 27) is accepted deliberately, not on replay-critical paths.
- [ ] Loops are **long enough** to amortize SIMD setup/remainder; short loops left scalar.

## 29.8 References

- Intel *Intrinsics Guide* and *Optimization Reference Manual*, and Agner Fog's *Instruction Tables* — SSE/AVX/AVX-512 instruction semantics, latency/throughput, and the **AVX frequency-license/downclocking** behavior central to §29.2-§29.3.
- D. Lemire et al., *simdjson* and related papers — production SIMD parsing/scanning (byte-compare + mask + bit ops), directly relevant to §29.4.2 and Ch. 53.
- The Google **Highway**, **xsimd**, **Eve**, and `std::experimental::simd` documentation — portable SIMD wrappers and runtime dispatch (§29.4.2).
- The GCC/Clang vectorization documentation — `-O3`/`-march`, `-fopt-info-vec`/`-Rpass=loop-vectorize`, alignment and `restrict` requirements (§29.4.1, §29.5; ties Ch. 21–22).
- T. Mattson / CppCon SIMD talks and the Intel "AVX-512 downclocking" analyses (Vlad Krasnov / Cloudflare blog) — the frequency-effect measurements behind §29.3.

## 29.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — vectorization analysis, downclocking, and reading vectorization reports.
- Matt Pharr / ISPC and the "SPMD on SIMD" model — an alternative way to write portable data-parallel kernels.
- Ch. 8–9 (*Layout/Alignment*) — the SoA/alignment prerequisites; Ch. 21 (*Aliasing*) — the #1 blocker; Ch. 27 (*FP*) — reassociation/determinism; Ch. 11 (*Pipelines*) — ILP/accumulators; Ch. 28 (*Bit Tricks*) — SIMD bit/byte ops; Ch. 43 (*SMT*) — downclock/sibling effects; Ch. 53 (*Wire Decoding*) — SIMD parsing; Ch. 67 (*GPU*) — when data-parallel work outgrows SIMD.
- **Appendix A** — NEON/SVE/SVE2 on ARM/Graviton; **Appendix E** — SIMD op throughput numbers; **Appendix D** — `-march`/SIMD flags.

---

*Next: Ch. 30 — The C++ Memory Model & Atomics, opening Part VI (Concurrency): `std::atomic`, `memory_order`, acquire/release/seq_cst, fences, and the compiler/hardware reordering that all the lock-free structures of this Part are built on.*
