# Part V — Numerics & Data Parallelism

# Chapter 27 — Fixed-Point & Floating-Point Arithmetic

> **Prerequisites:** Ch. 11 (FP op latency & dependency chains — `div`/`sqrt` on the critical path), Ch. 22 (`-ffast-math`/`-Ofast` — the flags that change FP semantics), Ch. 6 (MXCSR/CPU state at startup), Ch. 4 (reading asm — §27.5), Ch. 1 (tail latency — denormals are a tail event).
>
> **Leads into:** Ch. 28 (bit/integer tricks — the integer math scaled prices use), Ch. 29 (SIMD — FP vectorization and downclocking), Ch. 53 (decoding prices off the wire), Ch. 72 (integer overflow in price/qty math as a security concern), Ch. 75 (deterministic replay needs bit-exact FP). Opens **Part V**.

---

## 27.1 Why it matters: representing prices without losing money

A trading system's single most important number is a **price**, and the way you represent it is both a *correctness* decision (get it wrong and you misprice, mis-round, or violate tick rules — and lose money or fail compliance) and a *performance* decision (the arithmetic runs on the hot path, on every tick and every order). The naive choice — `double` — is wrong on *both* axes for prices: binary floating-point **cannot represent most decimal fractions exactly** (0.1 is not 0.1 in binary), so `0.1 + 0.2 != 0.3`, accumulated prices drift, and equality comparisons are landmines; *and* `double` arithmetic carries hazards (denormals, NaN propagation, op latency) that can spike latency or silently corrupt results. The professional answer for prices is almost always **scaled integers** (fixed-point): represent a price as an integer number of ticks or of some minimal unit (e.g. price × 10⁸ as an `int64`), so arithmetic is exact, fast integer math (Ch. 28), and tick handling is natural.

This doesn't mean floating-point has no place — P&L aggregation, risk, statistics, signal math, and pricing models legitimately use `double` — but it means you must *understand* floating-point's hazards rather than assume it "just works," because on the hot path it can bite in ways invisible until production. The headline performance hazard is **denormals** (subnormal numbers near zero): when a computation produces values tiny enough to become denormal, many CPUs handle them via a **microcode-assisted slow path** that is *100×+ slower* than normal FP ops — a sudden, data-dependent latency cliff (Ch. 1) that strikes exactly when a signal decays toward zero or a calculation underflows. The cure (FTZ/DAZ flags — flush denormals to zero) is one MXCSR setting, but you have to *know* to set it (§27.4.2). Other hazards — NaN/Inf propagation poisoning a calculation, the latency of `div`/`sqrt` on a dependency chain (Ch. 11), and the **non-determinism** of FP across compilers/flags/`-ffast-math` (Ch. 22) that breaks bit-exact replay (Ch. 75) — round out the chapter.

So the discipline is: **prices and money are scaled integers (exact, fast, tick-correct); floating-point is for the math that genuinely needs it, used with denormals flushed, NaN/Inf guarded, high-latency ops kept off the critical path, and determinism controlled.** This chapter covers the representations (§27.2.1), tick-size handling (§27.2.2), the FP hazards (§27.2.3-4), measures the denormal cliff and op latencies (§27.3), gives the techniques (§27.4), and verifies in the asm how FP flags change instruction selection (§27.5) — because, as always, what the compiler actually emitted for your "fast math" is a thing you confirm, not assume.

## 27.2 Mental model

### 27.2.1 Scaled integers vs decimal vs binary float

Three ways to represent a price, with very different properties:

- **Binary floating-point (`float`/`double`, IEEE 754).** Fast hardware arithmetic, huge range — but **base-2**, so most decimal fractions (0.1, 0.01, a $0.05 tick) are *not* exactly representable; they round to the nearest binary fraction. Consequences: `0.1 + 0.2 == 0.30000000000000004`, accumulation drifts, exact equality is unreliable, and a price stored as `double` and back may not round-trip. `double` has 52 bits of mantissa (~15-16 decimal digits) — plenty of *precision*, but the *representability* problem is fundamental and unfixable. **Wrong for prices/money** (right for many other computations).
- **Scaled integers (fixed-point) — the price/money default.** Represent the value as an integer in units of a fixed scale: price × 10⁸ (or × tick) stored in `int64`. `$123.45` at scale 10⁴ is the integer `1234500`. Arithmetic is **exact** integer math (add/subtract/compare are trivial and fast — Ch. 28), tick alignment is natural (a price is an integer number of minimum units), and there's no representability error. You manage the scale explicitly (multiplication needs rescaling; division needs care — §27.4.1), and you must size the integer to avoid **overflow** (an `int64` at scale 10⁸ holds ~$92 billion — usually ample, but *check* — Ch. 72). This is what exchanges and serious systems use for prices, quantities, and notional.
- **Decimal floating-point / decimal libraries.** Base-10 representations (IEEE 754 decimal, or libraries like `decNumber`, Boost.Multiprecision, intel DFP) represent decimal fractions *exactly* and round per decimal rules — *correct* for money, but **slow** (often software-emulated, no hardware decimal on mainstream x86) — fine for back-office/accounting, **too slow for the hot path**. Scaled integers give you decimal-exact behavior at integer speed, which is why they win on the tick-to-trade path.

The decision: **scaled `int64` for prices/quantities/notional on the hot path** (exact, fast, tick-natural); `double` for genuinely floating-range math (risk, stats, models) where small relative error is acceptable; decimal libraries only off the hot path where exact decimal *and* large range are both needed.

### 27.2.2 Rounding and tick-size handling

Prices live on a **tick grid** — the minimum price increment, which may *vary by price band* (e.g. finer ticks for low-priced instruments). Scaled integers make this clean:

- **Tick as the unit (or a divisor).** If the tick is the scale unit, a valid price is simply an integer; rounding to a tick is integer division/rounding. With a finer scale, round to the nearest tick by integer arithmetic: `rounded = ((price + tick/2) / tick) * tick` (round-half-up), or floor/ceil per the rule — all exact integer ops, no FP.
- **Band-dependent tick size (Ch. 25).** A **tick-size table** indexed by price band (a `constexpr` table — Ch. 18, or a `flat_map` — Ch. 25) gives the tick for a given price; round using that tick. Precompute the table at compile time; the hot-path lookup is one indexed load.
- **Rounding direction matters and is a *rule*, not a default.** Rounding a buy vs sell, a bid vs offer, aggressive vs passive — the *direction* (floor/ceil/nearest/half-even) is a business rule with money implications; implement it explicitly and test it. FP rounding modes (round-to-nearest-even default) are a separate, subtler thing (§27.2.3) you mostly avoid by using integers.
- **Why integers win here.** Tick rounding in `double` reintroduces representability error (the "rounded" price isn't exactly on the grid); in scaled integers it's exact by construction. This is a concrete reason prices are integers.

### 27.2.3 Denormals and the FTZ/DAZ flags

**Denormals (subnormals)** are IEEE 754's representation of numbers too small for the normal exponent range — values very close to zero. They preserve gradual underflow (a nice numerical property) but at a **brutal performance cost**: many x86 microarchitectures handle operations *producing or consuming* denormals via a **microcode assist** that is **tens to 100×+ slower** than the same op on normal numbers. So a computation that drifts toward zero — a decaying signal, an EWMA settling, an underflowing intermediate — can suddenly hit a latency cliff that's entirely data-dependent and invisible until it happens (Ch. 1's tail).

The fix is two CPU flags in the **MXCSR** register (SSE/AVX control):

- **FTZ (Flush To Zero):** results that would be denormal are flushed to zero instead.
- **DAZ (Denormals Are Zero):** denormal *inputs* are treated as zero.

With both set, denormals never occur on the hot path and the cliff disappears — at the cost of a tiny loss of near-zero precision (almost always irrelevant for trading math; you're not doing precision physics near 10⁻³⁰⁸). They're set per-thread via `_mm_setcsr`/`_MM_SET_FLUSH_ZERO_MODE` (or implied by `-ffast-math`), and you set them **once at thread startup** (§27.4.2). Crucially: **they're a thread-local CPU state you must explicitly establish** — a fresh thread defaults to denormals-on (slow), so every hot thread must set FTZ/DAZ in its setup, or it's one underflow away from a latency spike.

### 27.2.4 FP op latency/throughput; NaN/Inf traps

The rest of the FP cost/correctness model (ties Ch. 11):

- **Op latency/throughput varies enormously.** Add/multiply (and FMA) are cheap (~4-5 cycle latency, high throughput); **division and square root are expensive** (~10-40+ cycles, low throughput, often not pipelined). On a **dependency chain** (Ch. 11) a `div`/`sqrt` dominates — hoist it, replace with reciprocal-multiply (`a/b` → `a * (1/b)` computed once), or restructure so it's off the critical path. `fma` (fused multiply-add) does `a*b+c` in one op with one rounding — faster *and* more accurate (needs `-mfma`/`-march`, Ch. 22).
- **NaN/Inf propagation.** Any op with a NaN input produces NaN; `0.0/0.0`, `inf-inf`, `sqrt(-1)` make NaN; overflow makes Inf. NaN **poisons** a computation silently (it propagates through everything and `NaN != NaN`, so comparisons mislead). On the hot path, *guard inputs* (validate, clamp) rather than let a bad market value (a zero price, a garbage decode — Ch. 53) inject a NaN that corrupts a signal. FP exception *traps* (SIGFPE on NaN/overflow) are usually *disabled* (masked) on the hot path — you don't want a trap mid-tick — so bad values flow silently unless you check.
- **Rounding mode and reassociation.** IEEE addition/multiplication aren't associative, so `(a+b)+c != a+(b+c)` in general; the compiler may not reorder FP without `-ffast-math` (Ch. 11, 22), and *enabling* reassociation changes results. The default rounding mode (round-to-nearest-even) is fine; changing it is rare and slow.
- **Determinism.** The *same* FP source can produce *different* bits across compilers, optimization levels, `-ffast-math`, FMA-vs-separate-ops, and x87-vs-SSE — a problem for **bit-exact replay** (Ch. 75) and cross-machine agreement. Controlling determinism (§27.4.3) means pinning the FP environment.

The model: **FP ops differ wildly in cost (add cheap, div/sqrt expensive — keep the latter off dependency chains); NaN/Inf propagate silently (guard inputs); FP is non-associative and non-deterministic across builds (control it where replay/agreement matters); and denormals are a hidden latency cliff (flush them).**

## 27.3 Measure it: denormal slowdown; FP op latencies

Two measurements: the **denormal cliff** (the headline hazard) and the **latency of FP ops** (to know what to keep off dependency chains, Ch. 11). For denormals, run the same loop on normal vs denormal data, with FTZ/DAZ off vs on.

```cpp
// fp.cpp — denormal slowdown, and FTZ/DAZ fix.  Also illustrates div latency.
// Build: g++ -O2 -std=c++20 -march=native fp.cpp -o fp   (NO -ffast-math, so FTZ isn't auto-set)
// Run pinned:  taskset -c 2 ./fp normal | ./fp denormal | ./fp denormal-ftz
#include <cstdio>
#include <cstring>
#include <vector>
#include <chrono>
#include <pmmintrin.h>   // _MM_SET_FLUSH_ZERO_MODE / _MM_SET_DENORMALS_ZERO_MODE

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "normal";
    bool denormal = std::strncmp(mode, "denormal", 8) == 0;
    bool ftz      = std::strcmp(mode, "denormal-ftz") == 0;

    if (ftz) {                                            // set FTZ + DAZ for THIS thread
        _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);
        _MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON);
    }
    constexpr int N = 4096, REPS = 200'000;
    std::vector<float> a(N);
    float seed = denormal ? 1e-39f : 1.0f;               // 1e-39f is denormal (< ~1.2e-38)
    for (auto& x : a) x = seed;

    auto t0 = std::chrono::steady_clock::now();
    float s = 0;
    for (int r = 0; r < REPS; ++r)
        for (int i = 0; i < N; ++i) s += a[i] * 0.5f;     // produces/uses denormals if seed tiny
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-13s s=%g  %.3f ns/op\n", mode, (double)s, ns/((double)REPS*N));
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2 -march=native` (no fast-math), pinned, turbo off (illustrative; the *ratio* is the point):

```
                       ns / op        notes
normal                 ~0.4 ns        normal floats: fast hardware path
denormal               ~45 ns         denormal values: microcode assist  <- ~100x CLIFF
denormal-ftz           ~0.4 ns        FTZ/DAZ flush denormals → back to fast path
```

And FP op latencies (from Agner Fog / `llvm-mca`, Ch. 11), the numbers that drive "keep off the dependency chain":

```
   op (double, scalar)   latency (cyc)   throughput      note
   add / sub / mul / fma   ~4              ~2/cyc          cheap, pipelined
   div                     ~14-22          ~1 per 5-8 cyc  expensive, poorly pipelined
   sqrt                    ~15-22          low             expensive
```

Read it the Ch. 1 / Ch. 11 way: the denormal run is **~100× slower** with no code change — purely because the *values* drifted into the denormal range and triggered the microcode assist; FTZ/DAZ restores full speed. This is the cliff: invisible in the source, data-dependent, and a tail-latency disaster when a signal decays toward zero. **Set FTZ/DAZ on every hot thread** (§27.4.2). The op-latency table is the Ch. 11 lesson made concrete: a `div`/`sqrt` on a dependency chain costs ~4-5× an add and doesn't pipeline — hoist it or reciprocal-multiply. (And the headline reason prices are integers: none of this — denormals, representability, non-associativity — applies to `int64` price math.)

## 27.4 Techniques

### 27.4.1 Scaled-integer price math

Implement prices/quantities/money as scaled integers (§27.2.1) — the hot-path default:

- **Pick a scale and stick to it.** A fixed scale (e.g. 10⁸, or "in ticks") in `int64`; document it, and ideally wrap it in a strong type (`struct Price { std::int64_t ticks; };` — a zero-cost abstraction, Ch. 19) so you can't accidentally mix a scaled and unscaled value or add a price to a quantity. The strong type also lets you *forbid* nonsensical ops (price × price) at compile time.
- **Exact add/subtract/compare.** Same-scale add/subtract/compare are just integer ops — exact, fast (Ch. 28), branchless-friendly (Ch. 13). This covers the overwhelming majority of price math (book updates, spread = ask − bid, P&L = Σ(price × qty)).
- **Multiply/divide need scale management.** `price × qty` (a price times a share count) is fine into a wider notional integer; multiplying two *scaled* values double-scales (divide back by the scale, with rounding); division needs explicit rounding (§27.2.2). Use `__int128` (or `_mul128`) for intermediate products that might overflow `int64` (notional = price × large qty), then rescale — and **check for overflow** (Ch. 72): a silent `int64` overflow in notional is a correctness/security bug.
- **Tick handling is integer rounding** (§27.2.2) — round to the tick grid with integer arithmetic and a compile-time tick table (Ch. 18, 25). Exact, no FP.
- **Convert at the boundaries only.** Parse the wire price directly into scaled integer (Ch. 53's integer parsing — `from_chars`/branch-free), keep it integer through the hot path, and convert to `double`/string only for display/logging (off the hot path, Ch. 71). Don't round-trip through `double`.

### 27.4.2 Enabling FTZ/DAZ

Eliminate the denormal cliff (§27.2.3, §27.3):

- **Set FTZ and DAZ once per thread at startup.** `_MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)` and `_MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON)` (from `<pmmintrin.h>`), or set the MXCSR bits directly, in each hot thread's setup — **not** `main` only, because MXCSR is **thread-local** and a new thread defaults to denormals-on. Make it part of the standard thread-init (alongside affinity — Ch. 42, and FP env).
- **Know that `-ffast-math` sets it for you — with caveats.** `-ffast-math`/`-Ofast` (Ch. 22) enables FTZ/DAZ (via crt startup) *and* a host of other FP relaxations (reassociation, no-NaN assumptions) you may not want (§27.5, §27.6). If you want *only* FTZ/DAZ, set the flags explicitly rather than enabling all of fast-math.
- **Accept the precision trade.** FTZ/DAZ lose gradual underflow (tiny near-zero values snap to zero). For trading math (prices are integers anyway; signals/risk don't need 10⁻³⁰⁸ precision) this is a non-issue; for the rare numerically-delicate computation, scope FTZ to the hot loop and restore for that calculation. Verify your specific math tolerates it.
- **Document and test it.** A thread missing the FTZ/DAZ setup is a latent latency bug. Assert/log the MXCSR state in hot threads; include "FTZ/DAZ set" in the warm-up checklist (Ch. 46, Appendix C).

### 27.4.3 Cross-build determinism on the hot path

When bit-exact reproducibility matters — **deterministic replay** (Ch. 75, 76), cross-machine agreement, regression testing — pin the FP environment:

- **Prefer integers where determinism is required.** Integer math is *always* bit-exact and deterministic across compilers/flags/machines — another reason prices and any decision-critical math use scaled integers. The deterministic core (Ch. 74) ideally avoids FP in the decision path entirely.
- **Avoid `-ffast-math` where you need determinism.** `-ffast-math` permits reassociation and other reorderings that make results depend on optimization level/compiler/FMA-contraction — *non-deterministic across builds* (Ch. 22). Determinism-critical FP code is built at *strict* FP (and may need `-ffp-contract=off` to stop FMA-vs-separate-ops differences).
- **Pin the FP details that vary.** Use SSE/AVX (not x87 — its 80-bit intermediates vary), control FMA contraction (`-ffp-contract`), fix the rounding mode (default round-to-nearest-even), and keep the *same* compiler/flags across the systems that must agree. Document the FP build contract.
- **Same code, same data, same bits — verify it.** Run the replay and a live computation and diff the outputs bit-for-bit (Ch. 75). A mismatch points at an FP-environment difference (flags, FMA, thread FTZ state) — track it down rather than tolerating "close enough," because replay/backtest validity depends on exactness.

## 27.5 Verify the codegen: FP flags and instruction selection

FP flags change *which instructions* the compiler emits, and that's verifiable. Snippets are **verified** Clang output (`--target=x86_64-linux-gnu -march=x86-64-v3`).

**FMA contraction (`a*b+c`).** With `-ffp-contract=fast` (or `-ffast-math`), the compiler fuses to one `vfmadd`; with `-ffp-contract=off`, it emits separate `mul` then `add` (one extra rounding — a *different result*):

```asm
; a*b + c   with -ffp-contract=fast:        ONE op, one rounding
        vfmadd213sd xmm0, xmm1, xmm2        ; xmm0 = xmm0*xmm1 + xmm2
; a*b + c   with -ffp-contract=off:         TWO ops, two roundings (deterministic-strict)
        vmulsd  xmm0, xmm0, xmm1
        vaddsd  xmm0, xmm0, xmm2
```

The two produce **different bits** — which is exactly the determinism knob of §27.4.3: replay-critical code uses `-ffp-contract=off` for reproducibility, performance-critical-but-not-replayed code uses the FMA.

**Reassociation / vectorized reduction (`-ffast-math`).** Without fast-math the compiler keeps a serial `vaddsd` chain (Ch. 11.5); with `-ffast-math` it reassociates into a vectorized multi-accumulator reduction — *faster, different result*:

```asm
; sum reduction, -O2 (strict):   serial scalar chain (Ch. 10) — exact order preserved
        vaddsd  xmm0, xmm0, [rdi + 8*rcx]
; sum reduction, -O2 -ffast-math: vectorized packed accumulators — reassociated
        vaddpd  ymm0, ymm0, [rdi + 8*rcx]
        vaddpd  ymm1, ymm1, [rdi + 8*rcx + 32]
```

**Division → reciprocal (`-ffast-math` / `-freciprocal-math`).** Fast-math may replace a `divpd` with a (lower-latency, lower-precision) reciprocal-estimate sequence (`vrcpps` + Newton step) — relevant to the Ch. 11 "keep div off the chain" point:

```asm
; a / b  strict:   vdivsd (expensive, ~14-22 cyc)
; a / b  fast:     vrcpps + refine (faster, approximate)   <- precision traded for latency
```

The verification habits: **for FP hot code, disassemble and confirm (1) the instruction selection matches your flag intent** — FMA where you want it / separate ops where you need determinism; vectorized reduction only with reassociation enabled; **(2) FTZ/DAZ is actually in effect** (it's runtime MXCSR state, not an instruction — assert it at thread start, §27.4.2); **(3) `div`/`sqrt` aren't sitting on a dependency chain** (Ch. 11) where a reciprocal-multiply or hoist would help. When you enable `-ffast-math` for speed, *read the asm and re-check the numbers* — it changed both.

## 27.6 Pitfalls & anti-patterns: float equality, accumulated error

- **Representing prices/money as `double`.** The cardinal sin: binary float can't represent decimal fractions exactly (§27.2.1), so prices drift, don't round-trip, and don't sit exactly on the tick grid — money bugs and compliance failures. Use **scaled integers** for prices/quantities/notional.
- **Float equality (`==`) on computed values.** `0.1 + 0.2 == 0.3` is *false*; comparing computed `double`s for exact equality is almost always a bug (`-Wfloat-equal` flags it). Use a tolerance (epsilon) where FP comparison is unavoidable — and use integers where exactness is required (then `==` is correct).
- **Forgetting FTZ/DAZ → the denormal cliff.** A hot thread without FTZ/DAZ set is one underflow away from a ~100× slowdown (§27.3) — a data-dependent tail spike. Set FTZ/DAZ in *every* hot thread's startup; it's thread-local.
- **`div`/`sqrt` on the dependency chain.** A ~14-22-cycle division on the critical path (Ch. 11) dominates; hoist it, reciprocal-multiply (compute `1/b` once, multiply), or restructure. Don't leave a per-iteration `div` in a latency-bound loop.
- **NaN/Inf poisoning silently.** A bad market value (zero price, garbage decode — Ch. 53) producing a NaN propagates through the whole computation undetected (`NaN != NaN` defeats comparisons). Guard/validate inputs; consider checking for NaN/Inf at boundaries (FP traps are usually masked on the hot path).
- **`-ffast-math` breaking determinism (or correctness).** Fast-math reassociates and assumes no NaN/Inf — changing results (non-deterministic across builds — §27.4.3, breaks replay Ch. 75) and miscompiling NaN-dependent code. Don't blanket-enable it on financial/replay-critical math; opt into specific relaxations and verify (§27.5).
- **Accumulated error in long sums.** Summing many `double`s loses precision (small terms lost against a large running sum); use Kahan/compensated summation, or (better for money) integers. Order-dependent results also hurt determinism.
- **Mixing scales / unit confusion.** Adding a scaled price to an unscaled one, or a price to a quantity, silently — a classic fixed-point bug. Wrap in **strong types** (Ch. 19) so the compiler catches it.
- **Integer overflow in notional/price math (Ch. 72).** `price × qty` overflowing `int64` is a silent wrap — a correctness *and* security bug. Use `__int128` intermediates and check ranges; treat price/qty arithmetic as needing overflow safety (Ch. 72).
- **x87 / 80-bit intermediates.** Legacy x87 FP uses 80-bit intermediates that vary by spill timing — non-deterministic and slow. Use SSE/AVX (default on x86-64); don't resurrect x87.

## 27.7 Exercises & checklist

**Exercises**

1. **Reproduce the denormal cliff.** Build `fp.cpp`; run `normal`/`denormal`/`denormal-ftz` pinned. Confirm the ~100× slowdown on denormals and that FTZ/DAZ removes it. Then *forget* to set FTZ in a spawned `std::thread` and show the cliff returns (MXCSR is thread-local).
2. **Scaled-integer price type.** Implement a `Price` strong type (scaled `int64`) with exact add/sub/compare, `price × qty` into `__int128` notional with overflow check, and tick rounding via a `constexpr` tick table (Ch. 18). Show `0.1+0.2`-style errors that `double` has and integers don't.
3. **FMA/contraction determinism.** Compile `a*b+c` with `-ffp-contract=fast` vs `off`; diff the asm (§27.5) and the *result bits*. Build a tiny "replay" that recomputes a value and show it matches only with contraction off and matching flags (§27.4.3).
4. **Div on the chain.** Write a latency-bound loop with a `div` in the carried dependency (Ch. 11); measure. Replace with reciprocal-multiply (hoist `1/b`); re-measure. Quantify using the op-latency table (§27.3).
5. **NaN poisoning.** Feed a signal computation a single NaN (simulate a bad decode) and watch it corrupt all downstream output. Add input validation/NaN guard at the boundary and show it's contained (ties Ch. 53, 72).

**Checklist — fixed/floating-point arithmetic**

- [ ] Prices, quantities, and notional are **scaled integers** (`int64`/`__int128`), ideally **strong-typed** (Ch. 19) — never `double`; conversion to FP/string is at the **boundaries** only.
- [ ] **FTZ + DAZ are set in every hot thread's startup** (thread-local MXCSR) — the denormal cliff (§27.3) can't occur; the state is asserted/logged (Ch. 46).
- [ ] **No `==` on computed floats** (`-Wfloat-equal`); FP comparison uses tolerance, exactness uses integers.
- [ ] **`div`/`sqrt` are off dependency chains** (Ch. 11) — hoisted/reciprocal-multiplied; FMA used where precision/perf allow.
- [ ] **NaN/Inf are guarded at input boundaries** (Ch. 53); bad market values can't silently poison computations.
- [ ] **Tick rounding is exact integer arithmetic** with a compile-time tick(-band) table (Ch. 18, 25), with the rounding *direction* an explicit, tested rule.
- [ ] Determinism-critical/replayed FP (Ch. 75) is built **strict** (no `-ffast-math`, `-ffp-contract=off`, SSE not x87), with a documented FP build contract; `-ffast-math` is **opt-in and verified** (§27.5).
- [ ] **Integer overflow is handled** in price/qty/notional math (`__int128`, range checks — Ch. 72); scales aren't mixed (strong types catch it).

## 27.8 References

- D. Goldberg, *"What Every Computer Scientist Should Know About Floating-Point Arithmetic"* — the canonical treatment of IEEE 754, representability, rounding, and accumulated error (§27.2).
- IEEE 754-2019 and the Intel *SDM* (MXCSR, FTZ/DAZ, SSE/AVX FP) and *Optimization Reference Manual* — the FP control state and denormal-handling behavior of §27.2.3/§27.4.2.
- A. Fog, *Instruction Tables* and *Optimizing software in C++* — FP op latency/throughput (the §27.3 table) and the cost of denormals, div/sqrt, and reassociation.
- The GCC/Clang documentation — `-ffast-math` and its components (`-ffinite-math-only`, `-fassociative-math`, `-freciprocal-math`), `-ffp-contract`, and `-Wfloat-equal` (§27.4.3, §27.5; ties Ch. 22, Appendix D).
- Boost.Multiprecision / Intel Decimal Floating-Point and the C `_Decimal` proposals — decimal representations for off-hot-path exact money math (§27.2.1).

## 27.9 Additional Reading

- The "fixed-point arithmetic" and "representing money" writeups (e.g. Martin Fowler's *Money* pattern, exchange protocol price encodings) — practical scaled-integer price design.
- N. Maclaren / numerical-analysis notes on FP determinism and reproducibility — background for §27.4.3 and replay (Ch. 75).
- Ch. 11 (*Pipelines*) — FP latency/dependency chains and reassociation; Ch. 28 (*Bit/Integer Tricks*) — the integer math scaled prices run on; Ch. 29 (*SIMD*) — FP vectorization and downclocking; Ch. 53 (*Wire Decoding*) — parsing prices to scaled integers; Ch. 75 (*Capture & Replay*) — deterministic FP; Ch. 72 (*Secure Programming*) — integer overflow in price math.
- **Appendix D** (Compiler Flag Reference) — the FP/math flags consolidated; **Appendix E** — FP op latency numbers.

---

*Next: Ch. 28 — Bit Manipulation & Integer Tricks, the integer-arithmetic companion to this chapter: `popcnt`/`lzcnt`/`tzcnt`/`pdep`/`pext`, bitmaps for order-book occupancy and free-lists, branchless min/max/abs/sign, and power-of-two rounding — one instruction where you'd have written a loop.*
