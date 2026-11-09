# Part V — Numerics & Data Parallelism

# Chapter 32 — SIMD & Vectorization

> **Prerequisites:** Ch. 11 (accumulator splitting — SIMD is its vector conclusion; ILP), Ch. 23 (aliasing/`restrict` — the #1 vectorization blocker), Ch. 8–9 (SoA layout & alignment — what vectorization needs), Ch. 24 (`-march`/`-O3` — enabling SIMD), Ch. 30 (FP reassociation — vectorized reductions need it), Ch. 4 (reading asm — §32.5).
>
> **Leads into:** Ch. 60 (vectorized parsing/validation of market data), Ch. 28 (SIMD-probed hash maps), Ch. 31 (SIMD bit ops). Closes **Part V**. The downclocking caveat ties to Ch. 6 (frequency) and Ch. 49 (SMT). Appendix A covers NEON/SVE on ARM.

---

## 32.1 Why it matters: data parallelism for batch work

SIMD — Single Instruction, Multiple Data — does the *same* operation on a whole vector of values at once: one AVX2 instruction adds eight 32-bit integers (or four doubles) in the time of one scalar add; AVX-512 doubles that to sixteen. For **batch** work — operations applied uniformly across an array — that's a 4-16× throughput multiplier on the arithmetic itself, the largest per-instruction speedup available short of a different algorithm. It's the vector generalization of the accumulator-splitting ILP from Ch. 11: instead of keeping several independent scalar chains in flight, you pack the lanes into one register and the hardware runs them in genuine parallel.

In a trading system SIMD pays off in the **batch-shaped** parts: scanning and validating a buffer of market-data messages (Ch. 60 — comparing many bytes, finding delimiters, validating ranges in parallel), computing a vector of signals or P&L across many instruments, risk and pricing math over portfolios (Greeks, Monte Carlo — Ch. 75 contrasts GPU), parsing (SIMD `from_chars`, SWAR delimiter search), and the probing of SwissTable-style hash maps (Ch. 28). The common thread: a *loop over an array doing uniform work* is a vectorization candidate, and turning it from scalar into SIMD is often the single biggest win on that kernel.

But SIMD comes with caveats sharp enough that naive use can *lose*, and this chapter is as much about *when not to* as *how to*. It needs the right **data layout** (SoA, Ch. 8 — AoS defeats it) and **alignment** (Ch. 9); it's blocked by **aliasing** (Ch. 23 — the most common reason a loop won't vectorize) and **FP non-associativity** (Ch. 30 — a reduction won't vectorize without reassociation permission); and — the signature SIMD trap — heavy **AVX-512 downclocking**: wide vector instructions can be power-hungry enough that the CPU *lowers its clock frequency* while (and after) they run, so a kernel that's faster in isolation can slow down *everything else on the core*, including the latency-critical scalar hot path — a net loss for a tick-to-trade box that does a little SIMD amid lots of scalar work. The latency-vs-throughput tension of Ch. 1 is acute here: SIMD is a *throughput* win, and the tick-to-trade path is *latency*-bound, so SIMD belongs on the **batch/parallel** parts (feed scanning, risk) and is often *wrong* on the single-message critical path. This chapter measures the win and the downclocking (§32.3), shows the layout/intrinsics/width techniques (§32.4), and verifies the vector codegen (§32.5) — with the discipline to use SIMD where batch throughput matters and keep it off the core when it would tax the latency path.

## 32.2 Mental model: auto-vectorization; SSE/AVX/AVX-512; downclocking

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
- **AVX-512** (512-bit, `zmm`): 16×float / 8×double / 64×int8, plus **mask registers** (`k0-k7`) for per-lane predication and many new ops — the widest, but the most downclocking (the downclocking paragraph at the end of this section, and the measurement in §32.3), and not on all CPUs.

**Two ways to get SIMD:**

- **Auto-vectorization (let the compiler do it).** At `-O3 -march=native` (Ch. 24) the compiler vectorizes loops it can *prove* safe — which requires no aliasing (Ch. 23 — `restrict`), a vectorizable access pattern (contiguous, SoA — Ch. 8), permission to reassociate FP reductions (Ch. 30 — `-ffast-math`), and known alignment (Ch. 9). When it works, it's free and portable. When it *doesn't* (and it often silently doesn't), you read the vectorization report (`-fopt-info-vec`/`-Rpass`) and the asm (§32.5) to find the blocker.
- **Intrinsics (do it yourself).** `<immintrin.h>` exposes the instructions directly (`_mm256_add_ps`, `_mm512_loadu_ps`, …) — full control, guaranteed vectorization, but verbose, ISA-specific, and unportable. Use when the compiler won't auto-vectorize a kernel that matters (§32.4.2). Wrapper libraries (`std::experimental::simd`, Highway, xsimd, Eve) give portable, readable SIMD across ISAs — usually the better choice than raw intrinsics.

**Downclocking — the signature caveat.** Wide SIMD draws a lot of power, so Intel CPUs (especially Skylake-SP / Cascade Lake; Ice Lake-SP and later are much milder) run AVX2/AVX-512 at *reduced frequency* — there are "AVX2" and "AVX-512" frequency licenses, each lower than the base (non-AVX) turbo. Worse, the frequency reduction can apply to the *whole core* and **persist for ~milliseconds after** the SIMD code finishes, and can affect the *sibling SMT thread* (Ch. 49). So a heavy-AVX-512 kernel can lower the clock of the *scalar* code running before/after/alongside it — including your latency-critical hot path. The net effect depends on how *much* SIMD vs scalar work the core does: a SIMD-dominated batch core benefits; a latency core doing occasional SIMD amid scalar critical-path work can *lose overall* even though the kernel itself sped up (§32.3). Newer cores (Ice Lake, and especially the latest) have *much* milder AVX-512 downclocking, but it's a per-microarchitecture fact you must measure (§32.3), not assume away.

The model: **SIMD does N lanes per instruction (4-16× throughput) for uniform batch work, via auto-vectorization (needs no-alias + SoA + alignment + FP-reassoc) or intrinsics. It's a *throughput* tool — right for batch/parallel work, often wrong on the latency critical path — and wide AVX-512 can *downclock the core*, so the kernel's local speedup must be weighed against its effect on everything else on that core.**

## 32.3 Measure it: scalar vs vectorized throughput (and frequency effects)

Two measurements: the **vectorization throughput win** on batch work, and the **downclocking effect** on surrounding scalar work — because the second is what decides whether SIMD belongs on a given core.

```cpp
// simd.cpp — scalar vs auto-vectorized vs AVX-512 dot product; observe throughput.
// Build:  g++ -O3 -std=c++20 -march=native -ffast-math -mprefer-vector-width=512 simd.cpp -o simd_avx512
//         g++ -O3 -std=c++20 -march=native -ffast-math simd.cpp -o simd_avx2      (GCC/Clang default to 256-bit on AVX-512 parts)
//         g++ -O3 -std=c++20 -mno-avx -ffast-math simd.cpp -o simd_sse            (SSE only)
//         g++ -O3 -std=c++20 -march=native simd.cpp -o simd_strict                (NO fast-math: the reduction stays in order)
// Run pinned, turbo off:  taskset -c 2 ./simd_avx512   (etc.)
#include <cstdio>
#include <cstdint>
#include <vector>
#include <chrono>

// restrict (Ch. 23) so the compiler may vectorize; SoA float arrays (Ch. 8).
// noinline: an inlined pure kernel with loop-invariant inputs is exactly what the optimizer hoists
// or outer-loop-vectorizes across REPS (GCC 13 did: 8 reps at a time, so the strict build measured
// the same as the vectorized ones). The call boundary + the barrier below make every rep a real dot.
[[gnu::noinline]] float dot(const float* __restrict a, const float* __restrict b, int n) {
    float s = 0.f;
    for (int i = 0; i < n; ++i) s += a[i] * b[i];   // needs -ffast-math OR multiple accumulators to vectorize the reduction
    return s;
}

int main() {
    constexpr int N = 4096; constexpr long REPS = 4'000'000;
    std::vector<float> a(N, 1.0001f), b(N, 0.9999f);
    auto t0 = std::chrono::steady_clock::now();
    float s = 0;
    for (long r = 0; r < REPS; ++r) {
        s += dot(a.data(), b.data(), N);
        asm volatile("" : "+x"(s) :: "memory");        // Appendix L: s is live, memory may have changed
    }
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("dot s=%g  %.4f ns/elem\n", (double)s, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz, AVX-512), GCC 13 with the per-row flags above, Linux 6.18, pinned, turbo off (illustrative; the strict row is the same source without `-ffast-math`, so the reduction must stay in order — Ch. 30). The kernel is `noinline` and each rep passes an asm barrier: an earlier draft let GCC 13 outer-loop-vectorize the *REPS* loop of an inlined, loop-invariant `dot`, so the strict build timed the same as the vector ones:

```
   build / width                                   ns / elem     speedup    note
   SSE (4-wide, -mno-avx -ffast-math)               ~0.34 ns       1.0x      mulps + addps, one vector accumulator: 4 cycles per 4 elements
   AVX2 (8-wide, -march=native -ffast-math)         ~0.17 ns       ~2x       vfmadd ymm — GCC/Clang's DEFAULT width on AVX-512 parts
   AVX-512 (16-wide, + -mprefer-vector-width=512)   ~0.09 ns       ~4x       vfmadd zmm; opt-in only
   strict (no -ffast-math)                          ~1.4 ns        0.25x     the in-order reduction: one vaddss per element, 4-cycle chain
```

(All three vector rows are latency-bound on GCC's single vector accumulator — one FMA per 4 cycles — not throughput-bound; `-funroll-loops`, or Clang's 4-way interleaving, roughly doubles them again. The AVX-512 row needs `-mprefer-vector-width=512`: both compilers default to 256-bit vectors on Intel AVX-512 targets precisely because of the frequency effect below. And GCC's strict build *reports* "loop vectorized" — it is an in-order reduction, `vmulps` feeding a serial `vaddss` chain; Clang leaves it scalar and says so. Look for packed `vaddps`/`vfmadd` in the asm, §32.5, not at the optimizer's remark.)

And the **downclocking** measurement — the one that matters for a latency box. Turbo is **on** for this experiment (the AVX-512 license drop is invisible at a fixed 2.9 GHz). Interleave, in *one* pinned thread, bursts of heavy `_mm512_fmadd_ps` work with a timed scalar section — two processes cannot share one core without time-slicing noise that swamps a 100 ns measurement — and watch the core frequency (`perf stat -e cycles,ref-cycles`, or `turbostat`):

```
   scalar task, no SIMD in the preceding ms        ~100 ns   @ ~3.5 GHz (full turbo)
   scalar task, right after a heavy AVX-512 burst   ~118 ns   @ ~3.0 GHz  <- downclock taxed the scalar path
   (the lower license lingers ~ms after the SIMD stops; it is per core, so it also hits the SMT sibling — Ch. 49)
```

Read it two ways. (1) **The kernel win is real**: vectorization gives ~2-4× on the dot product over SSE and ~4-16× over the strict scalar chain (and the strict row shows the reduction *didn't* really vectorize without FP-reassociation permission — Ch. 30 — a common silent miss). (2) **The downclock is also real**: the *scalar* task got ~18% slower while AVX-512 ran on the core, because the core dropped frequency — so on a latency core, the AVX-512 kernel's local speedup can be *outweighed* by slowing the critical path around it. The decision rule: **SIMD wins where the core is SIMD-dominated (a batch/risk core); it can lose where a little SIMD shares a core with latency-critical scalar work.** Measure *both* the kernel speedup *and* the frequency effect on the surrounding work (`turbostat`, the scalar-task latency with/without SIMD) before putting wide SIMD on a tick-to-trade core. (Modern cores downclock far less; check yours.)

## 32.4 Techniques

### 32.4.1 Data layout for vectorization

SIMD's hard prerequisite is layout — the Ch. 8–9 work is what *enables* vectorization:

- **SoA, not AoS (Ch. 8).** SIMD loads N *contiguous, same-field* values into a lane vector. Struct-of-Arrays (`prices[]`, `qtys[]`) puts each field contiguous → one vector load grabs 8 prices. Array-of-Structs (`Order{price,qty,...}[]`) interleaves fields → a vector load grabs garbage (price, qty, price, qty…) and the compiler must *gather* (slow) or give up. **SoA is the single most important enabler of vectorization.** (Hybrid AoSoA — arrays of small SoA blocks — balances SoA's SIMD-friendliness with AoS's per-record locality.)
- **Alignment (Ch. 9).** The cost is in the *data*, not the mnemonic: on every AVX-capable core `vmovups` on an aligned address runs exactly like `vmovaps`, but a vector access that *straddles a cache line* (or a 4 KiB page) costs an extra line access — and a 64-byte `zmm` load that isn't 64-byte-aligned straddles one *every time*. So align hot arrays to the vector width (32B for AVX2, 64B for AVX-512 — also a cache line) with `alignas`/aligned allocation, and tell the compiler (`__builtin_assume_aligned`) so it can skip the alignment prologue/epilogue it otherwise emits to reach a safe boundary (§32.6).
- **Contiguous, unit-stride access.** Vectorization wants `a[i]`, `a[i+1]`, … contiguous; strided/gather access (`a[idx[i]]`) is far slower (gather instructions exist but are weak). Restructure to unit stride where possible.
- **No aliasing (Ch. 23) and FP-reassoc permission (Ch. 30).** `restrict` the pointers (the #1 auto-vec blocker) and allow FP reassociation (`-ffast-math` or multiple accumulators) for reductions, or the compiler *can't* vectorize — the scalar-row miss in §32.3. These two are the most common "why won't it vectorize" answers.
- **Trip count and remainder.** Vectorized loops process N-at-a-time with a scalar remainder for `n % N`; very short loops may not be worth vectorizing (the setup/remainder dominates). Size and pad arrays to vector multiples where it helps.

### 32.4.2 Intrinsics when the compiler won't

When auto-vectorization fails or is suboptimal on a kernel that matters, drop to explicit SIMD:

- **When to reach for intrinsics.** The compiler won't vectorize (a blocker you can't remove, a pattern it doesn't recognize — horizontal ops, shuffles, saturating arithmetic, byte-wise compares for parsing), or it vectorizes *poorly*. Confirm the failure first (`-fopt-info-vec-missed`/asm — §32.5); often fixing layout/aliasing (§32.4.1) lets the compiler do it for free, which is preferable.
- **Prefer a portable wrapper over raw intrinsics.** `std::simd` (C++26 `<simd>`; today `std::experimental::simd` — §32.4.4 measures it), Google **Highway**, **xsimd**, **Eve** give readable, portable SIMD that compiles to the best ISA available and falls back gracefully — far more maintainable than `_mm512_*` intrinsics hand-written per ISA. Use raw intrinsics only for the last few percent or an op the wrappers lack.
- **The common SIMD idioms.** Load (`loadu`/`load`), arithmetic (`add`/`mul`/`fmadd`), compare-to-mask + blend (predication — the SIMD form of branchless select, Ch. 13/31), horizontal reduce (sum/min/max across lanes — needed at the end of a reduction), shuffle/permute, gather/scatter (use sparingly). Market-data scanning uses byte compares + mask + `popcount`/`tzcnt` (Ch. 31) to find delimiters/matches across 32-64 bytes at once (Ch. 60). The canonical kernel, in raw intrinsics:

```cpp
// scan.cpp — first '|' in a 32-byte block: AVX2 compare + movemask + tzcnt vs a scalar loop.
// Build: g++ -O2 -std=c++20 -mavx2 scan.cpp -c      (clang++ 18 gives the same four instructions)
#include <immintrin.h>
#include <bit>
#include <cstdint>

// Index of the first `delim` in buf[0..32), or 32 if none.  Scalar: a data-dependent loop + exit mispredict.
int find_delim_scalar(const char* buf, char delim) {
    for (int i = 0; i < 32; ++i) if (buf[i] == delim) return i;
    return 32;
}
// SIMD: 32 byte-compares in one instruction; the mask's lowest set bit is the answer (Ch. 31).
int find_delim_avx2(const char* buf, char delim) {
    __m256i chunk = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(buf));   // vmovdqu (no alignment needed)
    __m256i eq    = _mm256_cmpeq_epi8(chunk, _mm256_set1_epi8(delim));           // vpcmpeqb: 0xFF per matching lane
    std::uint32_t mask = static_cast<std::uint32_t>(_mm256_movemask_epi8(eq));   // vpmovmskb: one bit per lane
    return mask ? std::countr_zero(mask) : 32;                                    // tzcnt + cmov — no loop, no mispredict
}
```

  GCC 13.3 compiles `find_delim_avx2` to `vpbroadcastb` / `vpcmpeqb` / `vpmovmskb` / `tzcnt` / `cmove` (verified) — a flat ~6-cycle answer regardless of where the delimiter sits, where the scalar loop costs 1 iteration per byte plus a mispredict at exit. Ch. 60 builds the full field tokenizer on this; the same kernel in Highway or `std::simd` is portable to NEON/SVE (Appendix A).
- **Masking (AVX-512).** `k`-mask registers do per-lane predication natively — conditional vector ops without branches or blends, the clean way to vectorize data-dependent work. Powerful, but AVX-512-only (downclock/portability — §32.6).
- **Keep a scalar fallback and runtime-dispatch.** For portability across CPUs (and the AMD/ISA-availability reality — Ch. 24, 31), compile multiple ISA versions and dispatch at runtime on CPU features (`__builtin_cpu_supports`, function multiversioning, or the wrapper's dispatch). Never ship an AVX-512 binary that SIGILLs on a non-AVX-512 box (Ch. 24).

### 32.4.3 Choosing the ISA width

Wider is *not* automatically better — the width choice is a measured trade against downclocking and portability:

- **AVX2 (256-bit) is often the sweet spot** for a mixed-workload / latency-adjacent core: a solid 8-wide (float) / 4-wide (double) speedup with *mild* downclocking, and near-universal availability on modern servers. Many shops cap at AVX2 on latency cores precisely to avoid AVX-512's frequency hit.
- **AVX-512 (512-bit) for SIMD-dominated batch cores.** Where the core spends most of its time in vector work (risk, Monte Carlo, large scans), 16-wide + masking + the richer instruction set wins despite downclocking — and on newer cores the downclock is mild enough that AVX-512 is broadly good. But on a core that interleaves SIMD with latency-critical scalar work, AVX-512's frequency hit (and its lingering/sibling effects — §32.3, Ch. 49) can make AVX2 the better *overall* choice. **Decide per core based on the SIMD-vs-scalar mix, measured (§32.3).**
- **SSE (128-bit) as a portable floor.** Always available; the fallback in runtime dispatch and the choice when the data is small or downclocking must be avoided entirely.
- **Match the frequency-license reality to the box (Ch. 6).** Whether AVX-512 downclocks badly is a per-microarchitecture fact; check your CPU (and Ice Lake/newer are much better than Skylake-SP). On an isolated latency box you also control turbo/governor (Ch. 6), so measure the actual frequency behavior under your SIMD load.
- **ARM/Graviton (Appendix A).** NEON (128-bit fixed) and SVE/SVE2 (scalable, vector-length-agnostic) are the AArch64 equivalents; SVE's length-agnostic model changes how you write portable SIMD. Covered in Appendix A.

### 32.4.4 `std::simd` and portable wrappers

`std::simd` (C++26 `<simd>`, P1928 — the Parallelism TS v2 `std::experimental::simd` with the rough edges filed off) is a *data-parallel type*: `simd<float, Abi>` holds `size()` lanes, supports the arithmetic operators element-wise, loads/stores from pointers with an alignment tag, reduces with `reduce()`, and masks with `simd_mask`/`where()`. Its value is that the kernel is written once and the *width* is a property of the target: `native_simd<float>::size()` is **4** under `-march=x86-64-v2` (SSE), **8** under `x86-64-v3` (AVX2) and **16** under `icelake-server` (AVX-512). Availability (Appendix H): the TS form `<experimental/simd>` ships in libstdc++ 11+ (GCC 11–13 as used here), the C++26 `<simd>` is experimental in libstdc++ 16 and absent from libc++ 18; Highway (Google), xsimd and Eve are the production-grade portable wrappers today, and Highway adds *runtime dispatch* across ISAs from one source. *VM-measured, GCC 13.3, `-O2 -march=x86-64-v3`, 4 096 floats × 100 000:*

```cpp
#include <experimental/simd>
namespace stdx = std::experimental;
// weighted mid-price accumulate over N instruments — the same kernel three ways
float scalar(const float* b, const float* a, const float* w, int n) { float s = 0; for (int i = 0; i < n; ++i) s += (b[i] + a[i]) * 0.5f * w[i]; return s; }
float with_simd(const float* b, const float* a, const float* w, int n) {
    using V = stdx::native_simd<float>; V acc = 0; const int L = int(V::size()); int i = 0;
    for (; i + L <= n; i += L) {                                   // the width is the target's, not the code's
        V vb(b + i, stdx::element_aligned), va(a + i, stdx::element_aligned), vw(w + i, stdx::element_aligned);
        acc += (vb + va) * 0.5f * vw;
    }
    float s = stdx::reduce(acc); for (; i < n; ++i) s += (b[i] + a[i]) * 0.5f * w[i]; return s;   // tail
}
float intrinsics(const float* b, const float* a, const float* w, int n) { /* _mm256_fmadd_ps over one __m256 accumulator — §32.4.2 */ }
```

```
                      -O2 -march=x86-64-v3     -O2 … -ffast-math    -O3 … -ffast-math
scalar                0.523 ns/elem            0.524                0.083   ← the FP reduction is reassociable only under fast-math
std::simd (8 lanes)   0.080                    0.083                —       6.5× over scalar, with no fast-math and the same rounding order every run
AVX2 intrinsics       0.127                    0.128                —       one accumulator = one 4-cycle FMA chain; the simd version let GCC split it
```

Two lessons. The scalar reduction *cannot* auto-vectorize without `-ffast-math` because reordering the adds changes the result (Ch. 30 §30.2 on determinism), and the §32.3 chapter discipline says you do not ship `-ffast-math` on price math; `std::simd` states the vector order *explicitly*, so the compiler vectorizes it at `-O2` with no fast-math and the result is bit-reproducible across runs and builds. And the wrapper beat the hand-written intrinsics by 1.6× — the intrinsic version serialises on a single `acc` chain, which GCC was free to split for the `simd` version. The trap is Ch. 25 §25.2.2's: `native_simd<float>` has a *different size* in a TU built `-march=icelake-server` than in one built `x86-64-v3`, so a struct holding one in a shared header is an ODR violation across those TUs — fix the width (`fixed_size_simd<float, 8>`) in any type that crosses a TU boundary, and keep `native_simd` inside kernel bodies. For AVX-512 widths, §32.6's downclocking caveat applies to the wrapper exactly as to the intrinsics.

## 32.5 Verify the codegen: vector instruction selection

Whether a loop vectorized — and to what width — is visible in the asm, and it's the verification that the technique fired. Snippets are **verified** Clang 18.1 output (Appendix L.3; `--target=x86_64-linux-gnu -O3 -march=icelake-server` for the scalar-FMA form, plus `-ffast-math -mprefer-vector-width=512` for the `zmm` form — with plain `-O3` and no `-march` you get SSE `mulss`/`addss`, and without the width flag `ymm`).

**Scalar (didn't vectorize) vs packed.** A reduction without FP-reassociation permission stays scalar (`...ss`/`...sd`, one lane); with `-ffast-math`/`-march` it goes packed (`...ps`/`...pd`, N lanes):

```asm
; dot product, -O3 (no fast-math):  SCALAR — reduction not reassociated (Ch. 30)
.loop:  vfmadd231ss xmm0, xmm1, [rsi+rax]    ; ss = scalar single, ONE lane

; dot product, -O3 -march=icelake-server -ffast-math -mprefer-vector-width=512:  PACKED AVX-512 (16 lanes) + multiple accumulators
.loop:  vfmadd231ps zmm0, zmm4, [rsi+rax]    ; ps + zmm = 16 floats/op
        vfmadd231ps zmm1, zmm4, [rsi+rax+64] ; multiple zmm accumulators (Ch. 11 ILP)
        ...
        ; + horizontal reduce of zmm0..zmm3 at the end
```

**Width is in the register name.** `xmm` = 128-bit (SSE, 4×f), `ymm` = 256-bit (AVX, 8×f), `zmm` = 512-bit (AVX-512, 16×f). Reading the register tells you the width the compiler chose:

```asm
        vaddps  ymm0, ymm0, [rdi]    ; 256-bit, 8 floats  (AVX2)
        vaddps  zmm0, zmm0, [rdi]    ; 512-bit, 16 floats (AVX-512)
```

**Aligned vs unaligned loads.** `vmovaps` (asserts alignment — faults otherwise) vs `vmovups` (no assertion) — the mnemonic tells you what the compiler could *prove*, not how fast the load is; a loop over data of unknown alignment also gets a scalar peel/prologue to reach a safe boundary:

```asm
        vmovaps ymm0, [rdi]          ; compiler PROVED 32B alignment (Ch. 9) — no peel loop needed
        vmovups ymm0, [rdi]          ; alignment unknown — same speed on aligned data, a split-line penalty on misaligned data
```

The verification habits: **for a kernel you expect to vectorize, disassemble and confirm packed ops (`...ps`/`...pd`) at the width you intended (`ymm`/`zmm`)** — `...ss`/`...sd` scalar ops mean it *didn't* vectorize (check aliasing — Ch. 23, FP-reassoc — Ch. 30, layout — §32.4.1, via `-fopt-info-vec-missed`); **confirm the alignment you established reached the compiler** (`vmovaps`, or `vmovups` with no peel loop after `__builtin_assume_aligned`) where you aligned the data; and **confirm the width matches your ISA decision** (§32.4.3 — you didn't accidentally get `zmm`/AVX-512 on a core where you wanted AVX2 to avoid downclock). The vectorization report plus the asm is how you turn "I hope it vectorized" into "I confirmed it did, at the width I chose."

**`std::simd` compiles to the packed instructions (§32.4.4).** GCC 13.3, `-O2 -march=x86-64-v3`, the `with_simd` inner loop:

```asm
.L8:    vmovups ymm3, YMMWORD PTR [rsi+rdx]      ; a[i..i+8)
        vaddps  ymm0, ymm3, YMMWORD PTR [rdi+rdx] ; + b
        vmulps  ymm0, ymm0, ymm2                  ; * 0.5
        vmulps  ymm0, ymm0, YMMWORD PTR [r8+rdx]  ; * w
        vaddps  ymm1, ymm1, ymm0                  ; acc +=      (8 lanes per iteration; no scalar fallback inside the loop)
        …
        vextractf128 xmm0, ymm1, 0x1              ; reduce(acc): 8 → 4 → 2 → 1
        vaddps  xmm0, xmm0, xmm1
```

Eight-lane `ymm` arithmetic and a one-time horizontal reduction after the loop — the shape §32.3 wanted and the scalar version could not reach without `-ffast-math`. Under `-march=icelake-server` the same source emits `zmm` (16 lanes) with no code change; under `x86-64-v2`, `xmm`.

## 32.6 Pitfalls & anti-patterns: AVX-512 downclocking; misaligned loads

- **AVX-512 downclocking taxing the latency path.** The signature SIMD trap: heavy AVX-512 lowers the core frequency (and lingers ~ms, can hit the SMT sibling — Ch. 49), so a kernel that's faster *in isolation* slows the *scalar* critical-path work around it — a net loss on a latency core (§32.3). Measure the frequency effect on surrounding work; consider capping at AVX2 on latency cores. (Newer cores: much milder — check.)
- **Misaligned data / unknown alignment.** Data that straddles cache lines pays a split-line penalty on every vector access, and alignment the compiler can't *prove* costs a scalar prologue/epilogue (§32.5) — `vmovups` itself is free on aligned addresses. Align hot arrays to the vector width (Ch. 9) and tell the compiler.
- **AoS layout defeating vectorization (Ch. 8).** Interleaved fields force gathers or block vectorization entirely. SoA (or AoSoA) is the prerequisite; "I added SIMD but it didn't help" is usually a layout problem.
- **Aliasing blocking auto-vec (Ch. 23).** No `restrict` → the compiler assumes overlap → scalar code (or a runtime guard). The #1 reason a loop won't vectorize. `restrict` the pointers and verify (§32.5).
- **Forgetting FP reassociation for reductions (Ch. 30).** A sum/dot reduction won't vectorize without `-ffast-math`/multiple accumulators (the scalar row in §32.3) — and enabling reassociation *changes the result bits* (determinism — Ch. 30). Decide deliberately; don't blanket `-ffast-math` on replay-critical math.
- **SIMD on the single-message latency path.** SIMD is a *throughput* tool; on the tick-to-trade critical path (one message, latency-bound) it often doesn't help (no batch to parallelize) and may downclock. Use it on *batch* work (feed scanning, risk over many instruments), not reflexively on the critical path (Ch. 1's latency-vs-throughput).
- **AVX-512 binary SIGILL / no fallback.** Shipping AVX-512 code that runs on a non-AVX-512 CPU crashes (Ch. 24). Runtime-dispatch on CPU features with a scalar/SSE fallback (§32.4.2).
- **Hand-written intrinsics where the compiler would do it.** Verbose, unportable intrinsics for a loop that auto-vectorizes once you fix layout/aliasing — wasted effort and a maintenance burden. Try fixing the blocker first; use a portable wrapper (Highway/xsimd) over raw intrinsics.
- **`vzeroupper` / AVX-SSE transition penalties.** Mixing legacy SSE and VEX-encoded AVX without `vzeroupper` causes transition stalls; compilers usually handle it, but hand-written intrinsics mixing encodings can hit it. Be aware when interfacing asm/intrinsics.
- **Short loops / no batch.** Vectorizing a loop too short to amortize setup + remainder is a loss. Vectorize where the trip count is large enough to pay (§32.4.1).

## 32.7 Exercises & checklist

**Exercises**

1. **Width sweep.** Build `simd.cpp` at SSE / AVX2 / AVX-512 (`-mno-avx` / `-march=native` / `-march=native -mprefer-vector-width=512`), with `-ffast-math` so the reduction vectorizes. Measure ns/elem; confirm ~2× per width doubling (minus downclock). Disassemble (§32.5) — `xmm`/`ymm`/`zmm`?
2. **Why it didn't vectorize.** Build *without* `-ffast-math` and *without* `restrict`; use `-fopt-info-vec-missed`/`-Rpass-missed=loop-vectorize` to see the reasons. Add `restrict` (Ch. 23), then FP-reassoc (Ch. 30), and watch each unblock vectorization (§32.4.1).
3. **Measure downclocking.** Run a heavy AVX-512 loop on a core while timing a scalar latency task on the *same* core; record the scalar task's latency and the core frequency (`turbostat`) with/without the SIMD load. Quantify the downclock tax (§32.3). Repeat capped at AVX2 — is the *overall* result better?
4. **SoA vs AoS.** Vectorize a computation over `Order{price,qty}[]` (AoS) vs `prices[]`,`qtys[]` (SoA). Confirm (asm + `-fopt-info`) the AoS version gathers/doesn't vectorize and SoA does; measure (§32.4.1, Ch. 8).
5. **SIMD market-data scan (preview Ch. 60).** Use byte-compare + mask + `tzcnt` (Ch. 31) to find a delimiter across 32 bytes at once vs a scalar byte loop. Measure; confirm the vector compare in the asm.

**Checklist — SIMD & vectorization**

- [ ] SIMD is applied to **batch/throughput** work (feed scanning, risk/pricing over many instruments), **not** reflexively on the single-message latency critical path (Ch. 1).
- [ ] Data is **SoA (or AoSoA)** and **aligned** to the vector width (Ch. 8–9); pointers are **`restrict`** (Ch. 23) and FP reductions have **reassociation permission** (Ch. 30) — the auto-vec prerequisites.
- [ ] I **verified vectorization in the asm** (§32.5): packed `...ps`/`...pd` at the **intended width** (`ymm`/`zmm`), no alignment peel loop — not scalar `...ss`/`...sd`; used `-fopt-info-vec`/`-Rpass` to diagnose misses.
- [ ] The **ISA width is chosen per core** by the SIMD-vs-scalar mix (AVX2 sweet spot for latency-adjacent cores; AVX-512 for SIMD-dominated cores), with the **downclocking effect measured** (§32.3, §32.4.3).
- [ ] **AVX-512 downclocking** is checked on the deployment CPU and weighed against the surrounding scalar latency path (Ch. 49) — capped at AVX2 where it taxes the hot path.
- [ ] Where auto-vec won't work, I use a **portable wrapper** (Highway/xsimd/`std::simd`) over raw intrinsics, with **runtime CPU-feature dispatch + scalar fallback** (no SIGILL — Ch. 24).
- [ ] Vectorized reductions' **determinism** impact (FP reassociation changes bits — Ch. 30) is accepted deliberately, not on replay-critical paths.
- [ ] Loops are **long enough** to amortize SIMD setup/remainder; short loops left scalar.

## 32.8 References

- Intel *Intrinsics Guide* and *Optimization Reference Manual*, and Agner Fog's *Instruction Tables* — SSE/AVX/AVX-512 instruction semantics, latency/throughput, and the **AVX frequency-license/downclocking** behavior central to §32.2-§32.3.
- D. Lemire et al., *simdjson* and related papers — production SIMD parsing/scanning (byte-compare + mask + bit ops), directly relevant to §32.4.2 and Ch. 60.
- The Google **Highway**, **xsimd**, **Eve**, and `std::experimental::simd` documentation — portable SIMD wrappers and runtime dispatch (§32.4.2).
- The GCC/Clang vectorization documentation — `-O3`/`-march`, `-fopt-info-vec`/`-Rpass=loop-vectorize`, alignment and `restrict` requirements (§32.4.1, §32.5; ties Ch. 23–24).
- T. Mattson / CppCon SIMD talks and the Intel "AVX-512 downclocking" analyses (Vlad Krasnov / Cloudflare blog) — the frequency-effect measurements behind §32.3.

## 32.9 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — vectorization analysis, downclocking, and reading vectorization reports.
- Matt Pharr / ISPC and the "SPMD on SIMD" model — an alternative way to write portable data-parallel kernels.
- Ch. 8–9 (*Layout/Alignment*) — the SoA/alignment prerequisites; Ch. 23 (*Aliasing*) — the #1 blocker; Ch. 30 (*FP*) — reassociation/determinism; Ch. 11 (*Pipelines*) — ILP/accumulators; Ch. 31 (*Bit Tricks*) — SIMD bit/byte ops; Ch. 49 (*SMT*) — downclock/sibling effects; Ch. 60 (*Wire Decoding*) — SIMD parsing; Ch. 75 (*GPU*) — when data-parallel work outgrows SIMD.
- **Appendix A** — NEON/SVE/SVE2 on ARM/Graviton; Agner Fog's *Instruction Tables* / uops.info and the Intel *Optimization Reference Manual* (§32.8) — SIMD op throughput and AVX frequency-license numbers (Appendix E's table has no SIMD rows); **Appendix D** — `-march`/SIMD flags.

---

*Next: Ch. 33 — The C++ Memory Model & Atomics, opening Part VI (Concurrency): `std::atomic`, `memory_order`, acquire/release/seq_cst, fences, and the compiler/hardware reordering that all the lock-free structures of this Part are built on.*
