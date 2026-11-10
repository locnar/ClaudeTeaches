# Part V — Numerics & Data Parallelism

# Chapter 36 — Fast Transcendentals & Approximate Math for Pricing and Signals

> **Prerequisites:** Ch. 35 (floating-point hazards — denormals, FMA contraction, determinism; this chapter is the *other* half of its "FP is for the math that needs it" rule), Ch. 13 (dependency chains and FP op latency — a polynomial is a chain), Ch. 27 and Appendix D.3 (`-fno-math-errno`, `-ffast-math`'s sub-flags), Ch. 4 (reading asm — §36.5), Ch. 21 (`constexpr` tables), Ch. 8 (cache footprint of a table).
>
> **Leads into:** Ch. 37 (bit tricks — the exponent-bit manipulation §36.4.2 uses), Ch. 38 (fast hashing — the other place a multiply-and-shift replaces a library call), Ch. 39 (SIMD — vectorizing the polynomial across an options chain), Ch. 40 (streaming statistics — EWMA decay factors and log-returns are `exp`/`log` calls on every tick), Ch. 89 (GPU pricing — the batch tier that publishes the vol surface and Greeks this chapter's hot path consumes), Ch. @QUOTING@ (the quoting engine — the consumer of a 50 ns theoretical price), Ch. 108 (replay — a hand-written approximation is bit-exact across builds in a way libm is not).

---

## 36.1 Why it matters: `exp` is on the tick-to-trade path, and libm was not written for it

The book's price math is integer (Ch. 35 §35.4.1), and for a taker strategy that is the end of the numerics story. A market maker's is not. Every underlying tick re-prices a *chain*: hundreds to thousands of options series whose theoretical value is a function of the underlying, time, rates and an implied volatility — `exp`, `log`, `sqrt` and the normal CDF `N(x)` (an `erf`) several times per series — and the quoting engine (Ch. @QUOTING@) then decides which quotes to pull and re-post. A signal engine does the same in miniature: an EWMA is `exp(-dt/τ)` per update, a log-return is a `log` per tick, a z-score is a `sqrt`. These are the calls that stand between "the underlying moved" and "our quotes moved", and the budget for the whole decision is a few hundred nanoseconds (Ch. 1, Ch. 118 §118.3).

glibc's `libm` is excellent at what it was written for — correctly rounded (or nearly) results for *every* double, with `errno`, IEEE exceptions and the full special-case ladder — and that is exactly what costs on the hot path. Measured below: a scalar `exp` is ~8 cycles of throughput and ~28 of latency on the reference toolchain — fine — but `erf` is 37/78, a Black-Scholes call with its vega through libm is ~87 cycles and an implied-vol solve 300–500, and the **tail** is worse than the mean: `exp(-745)` (an underflowing input a decaying signal *will* produce) costs **126 cycles** against 8, and `log` of a denormal 118, because libm takes a slow path to get the subnormal result right. A hand-written approximation with a *stated* error bound — a range reduction, a degree-6 minimax polynomial and seven FMAs — costs 5 cycles, has no slow path, vectorizes 4–8 wide across the chain where libm cannot, and is bit-identical on every build, which libm across glibc versions is not (Ch. 108).

This chapter is about building those approximations *with the error bound in hand* rather than copying one off the internet: what libm does and what it charges (§36.2.1), the anatomy of `exp` and `log` — reduction, polynomial, reconstruction (§36.2.2), minimax against Taylor and Horner against Estrin with the dependency chain in view (§36.2.3), tables and vector libraries and where each belongs (§36.2.4); the measurements (§36.3); the techniques — build flags first, a polynomial `exp`, a table `log`, `N(x)` and the Black-Scholes quote with an implied-vol solve at a fixed iteration budget, vectorization across the chain, and determinism (§36.4); the codegen that proves there is no `call exp` left (§36.5); and the pitfalls — accuracy you did not measure, a Newton that does not converge deep out of the money, `-ffast-math` as a substitute for thought (§36.6).

## 36.2 Mental model

### 36.2.1 What a libm call costs, and where its tail comes from

A call to `std::exp(x)` with default flags is: a `call exp@PLT` (an indirect jump through the PLT unless `-fno-plt`, Ch. 27), an argument check for NaN/±Inf/overflow/underflow, the fast path (glibc ≥ 2.28 uses a 128-entry table + a short polynomial for `exp`, ~0.5 ULP), and — depending on the result — an `errno` store and a slow path that produces a correctly rounded *subnormal* result or raises the IEEE underflow/overflow flags. Three consequences:

- **`-fno-math-errno` is table stakes.** With `errno` semantics on, `sqrt` cannot be the single `vsqrtsd` instruction: the compiler emits a compare-and-branch to `sqrt@PLT` for negative inputs so `errno` can be set (verified in §36.5). The same flag lets the compiler treat `exp`/`log`/`pow` as pure and hoist/CSE them. `-fno-trapping-math` additionally lets it assume no FP exception will be observed, which unlocks contraction and reordering of the checks. Neither flag changes a *result*; both are safe on the hot path and are the only two `-ffast-math` sub-flags this chapter recommends unconditionally (§36.4.1).
- **The special cases are branches, and the slow paths are the tail.** The ladder of checks is predicted cheaply in the common case; the cost appears when the *data* lands on a slow path: inputs near the overflow/underflow boundary, denormal inputs or results. A signal that decays geometrically toward zero (an EWMA with a long tail, a Gaussian kernel evaluated far from its centre) will hand `exp` a −745 one day, and that day the call costs 15× — §36.3 measures 126 cycles against 8. This is Ch. 35's denormal cliff in a different costume, and FTZ/DAZ does not help because the slow path is *software*.
- **It does not vectorize.** A loop calling `std::exp` per element stays scalar: the compiler will not call `libmvec`'s `_ZGVdN4v_exp` unless you say `-ffast-math` (or `-fopenmp-simd` with `#pragma omp simd` plus glibc's `math-vector` declarations), and the vector variants have their own, laxer accuracy. A hand-written polynomial is plain arithmetic, and the auto-vectorizer treats it as such (§36.5: Clang emits an 8-wide AVX-512 loop at 1.25 cycles/element).

### 36.2.2 The anatomy of `exp` and `log`: reduce, approximate, reconstruct

Every fast transcendental has the same three-part shape, and the parts are where the engineering decisions live.

**Range reduction** maps the argument into a small interval where a low-degree polynomial is accurate. For `exp`: `x = k·ln2 + r` with `k = round(x·log2e)` and `|r| ≤ ln2/2 ≈ 0.347`, so `exp(x) = 2^k · exp(r)`. The `k·ln2` subtraction must be done with an FMA (or a split hi/lo constant) so the reduced argument is accurate to the last bit — a cancellation error here is the most common bug in a home-made `exp`. For `log`: split `x = 2^e · m` with the mantissa `m ∈ [1,2)` via the exponent bits (a `bit_cast`, a shift and a mask — Ch. 37), so `log(x) = e·ln2 + log(m)`; a further table split of `m` into 64–128 sub-intervals shrinks the polynomial's domain to ±1/128 (§36.4.3).

**Approximation** evaluates `exp(r)` or `log1p(u)` on the small interval with a polynomial of degree *d*. The degree sets both the error and the cost: one FMA per coefficient. The coefficients are the decision — §36.2.3.

**Reconstruction** puts the scale back: `2^k` is not a multiplication but an *integer add into the exponent field* — `bit_cast<double>((k + 1023) << 52)` — one `shl`, one `add`, one `vmovq` (§36.5). For `log` it is one FMA: `e·ln2 + poly`. Reconstruction is also where the *domain* of the approximation is decided: the exponent trick is only valid for `k ∈ [−1022, 1023]`; outside it the result must be clamped (`exp(−745) → 0`, `exp(710) → +Inf`) or the function must declare it never receives such inputs. A hot-path approximation *should* declare its domain — that is what makes it fast — and the caller's input validation (Ch. 70 §70.6, Ch. 101) is what makes the declaration true.

### 36.2.3 Minimax vs Taylor, Horner vs Estrin, and the dependency chain

Two polynomials of the same degree can differ by a hundred-fold in accuracy, and two evaluations of the same polynomial by a factor of two in latency.

- **Taylor coefficients are the wrong coefficients.** A Taylor series is optimal at *one point* (the centre) and its error grows as `r^(d+1)/(d+1)!` toward the interval's edges. A **minimax** polynomial (Remez's algorithm; `sollya` generates them, and a Chebyshev-node interpolation is within a few percent of it) spreads the error evenly across the interval and is far better at the edges: measured in §36.3, degree 6 on `[−ln2/2, ln2/2]` has a maximum relative error of **1.6 × 10⁻⁷ with Taylor coefficients and 2.5 × 10⁻⁹ with near-minimax ones** — the same seven FMAs, 60× the accuracy. Degree 8 minimax reaches 10⁻¹² (one more FMA pair). The coefficients are *data*: a `constexpr` array, fitted once, checked into the repository with the script that produced them and the measured bound.
- **State the error bound in the units that matter.** Relative error (for `exp`, whose output spans decades), absolute error (for `erf`/`N(x)`, bounded in [−1, 1], where 10⁻⁷ absolute is what a pricing model needs), or ULPs (when the result feeds a comparison that must agree with a reference). Measure the bound by *exhaustive or dense sampling against libm* (§36.3's harness does 16 Ki points per function; a release-gate version does 10⁸) and record it next to the function. A bound you did not measure is not a bound.
- **Horner is a chain; Estrin is a tree.** Horner's `p = fma(p, r, c_i)` is *d* dependent FMAs — 4 cycles each on Ice Lake (Ch. 13 §13.3), so a degree-6 polynomial has a 24-cycle critical path and the latency benchmark in §36.3 shows it (32 cycles for the whole `exp`). Estrin's scheme evaluates even and odd halves in parallel (`r²`, `r⁴` as shared powers) and halves the depth at the price of two extra multiplies: **latency 25.6 vs 32 cycles, throughput 5.6 vs 4.9** in §36.3. Which matters depends on the caller: a quote loop over a thousand independent series is throughput-bound and Horner wins; one `exp` on the decision path of a single order is latency-bound and Estrin wins. Both are five lines; keep both and pick per call site.
- **FMA is not optional.** `fma(p, r, c)` is one instruction, one rounding, 4 cycles; `p * r + c` without contraction is two instructions, two roundings and 8 cycles. Build with `-mfma` (implied by `-march=x86-64-v3`/`native`) and write `std::fma` explicitly rather than relying on `-ffp-contract=fast`, because the explicit form is what makes the result identical across GCC and Clang and across `-O` levels (§36.4.6).

### 36.2.4 Tables, vector libraries, and where each belongs

A **table** trades polynomial degree for a memory load: a 256-entry table of `2^(j/256)` lets `exp` use a degree-3 polynomial on an interval 256× smaller, which is how glibc itself does it. The cost is a cache line per lookup — 2 KiB for the table, L1-resident if the function is hot and *not* if it runs once per tick among 100 KiB of other state (Ch. 8 §8.2.3). The measured table `exp` in §36.3 is 4.8 cycles at 1.4 × 10⁻¹³ relative error — faster *and* more accurate than the degree-6 polynomial — because the table is L1-hot in that benchmark; in a cold call site the load is an L2 hit (~14 cycles) and the polynomial wins. Rule: tables for throughput loops with a warm cache, pure polynomials for isolated calls on the latency path. Make the table `constexpr` (Ch. 21) so it is in `.rodata` and warmed with the code (Ch. 62).

**Vector math libraries** — glibc's `libmvec`, SLEEF, Intel's SVML (via `-fveclib=SVML` in Clang/ICX, or oneMKL VM) — provide 4- and 8-wide `exp`/`log`/`erf` with 1–4 ULP accuracy and are the right answer when the work is a *batch*: re-pricing a chain, computing Greeks for a book, a Monte-Carlo path (Ch. 89 does it on the GPU, Ch. 39 on the CPU). They are the wrong answer for a single value on the decision path: the vector call has a fixed overhead and a horizontal reduction you do not need. The hand-written polynomial covers both cases — the auto-vectorizer widens it in the batch loop and leaves it scalar in the single-value call — which is why this chapter builds one rather than linking one.

The model: **a fast transcendental is a range reduction that is exact, a polynomial whose coefficients were fitted (not expanded) to a measured error bound, and a reconstruction that is an integer operation on the exponent — seven FMAs with no branches, no `errno`, no slow path and a declared input domain. Its accuracy is a number you measured and wrote down; its speed comes from being plain arithmetic the compiler can inline, contract and vectorize.**

## 36.3 Measure it: libm vs polynomial vs table, the tail inputs, and a Black-Scholes quote

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native -fno-math-errno -fno-trapping-math`, glibc 2.39, TSC at 2.7 GHz — divide cycles by 2.7 for ns); the reference box runs the same source with GCC 14.2.*

One harness, three functions, four implementations each, two loops per implementation — **throughput** (independent evaluations over a 16 Ki L1-resident input array, best of 200) and **latency** (a dependent chain, each input perturbed by the previous output) — and a dense error measurement against libm as the reference:

```cpp
// fastmath.cpp (excerpt) — the polynomial exp of §36.4.2 and the harness shape; full file in the repo.
// Build: g++ -O2 -std=c++20 -march=native -fno-math-errno -fno-trapping-math fastmath.cpp -o fastmath -lm
static inline double exp_poly6(double x) {
    const double LOG2E = 1.4426950408889634, LN2 = 0.6931471805599453;
    double k = std::nearbyint(x * LOG2E);            // vroundsd
    double r = std::fma(-k, LN2, x);                  // exact-enough reduction: |r| <= ln2/2
    double p = 1.3941108418388697e-3;                 // near-minimax degree-6, rel err 2.5e-9 on the interval
    p = std::fma(p, r, 8.3751263976791154e-3);
    p = std::fma(p, r, 4.1666352896823876e-2);
    p = std::fma(p, r, 1.6666415514660393e-1);
    p = std::fma(p, r, 5.0000000471177675e-1);
    p = std::fma(p, r, 1.0000000377162124);
    p = std::fma(p, r, 1.0);
    return p * std::bit_cast<double>((std::uint64_t)((std::int64_t)k + 1023) << 52);   // * 2^k, no multiply by a power
}
template <class F> void bench(const char* name, const std::vector<double>& in, F f, const std::vector<double>* ref) {
    std::vector<double> out(in.size());
    std::uint64_t best = ~0ull;
    for (int rep = 0; rep < 200; ++rep) {                               // throughput: independent
        auto t0 = rdtsc(); for (size_t i = 0; i < in.size(); ++i) out[i] = f(in[i]); auto t1 = rdtsc();
        asm volatile("" : : "r"(out.data()) : "memory"); best = std::min(best, t1 - t0);
    }
    double y = 0.1; auto l0 = rdtsc();                                  // latency: dependent chain
    for (size_t i = 0; i < in.size(); ++i) y = f(in[i] + y * 1e-9);
    auto l1 = rdtsc();
    double maxrel = 0; if (ref) for (size_t i = 0; i < in.size(); ++i) maxrel = std::max(maxrel, std::fabs(out[i] / (*ref)[i] - 1));
    std::printf("%-26s thr %5.2f cyc  lat %5.2f cyc  max rel err %.1e\n", name, double(best) / in.size(), double(l1 - l0) / in.size(), maxrel);
}
```

Measured (GCC 13.3; the Clang 18.1 column where it differs):

```
                              throughput   latency    max error vs libm        notes
exp(x), x in [-20, 20]
  libm exp                     8.0 cyc     28 cyc     (reference)              glibc 2.39: table + poly, 0.5 ULP
  Taylor deg-6, Horner+FMA     4.9         32         1.6e-7 rel               same cost as the next row, 60x the error
  minimax deg-6, Horner+FMA    4.9         32         2.5e-9 rel               Clang: 1.25 cyc thr (8-wide AVX-512 loop)
  minimax deg-8, Horner+FMA    5.7         37         1.1e-12 rel              two more FMAs
  minimax deg-6, Estrin+FMA    5.6         26         2.5e-9 rel               shorter chain, two extra muls
  table-256 + deg-3            4.8         29         1.4e-13 rel              2 KiB table, L1-hot here
  expf (float, libm)           6.1         36         9.9e-7 rel               single precision is not faster here
log(x), x in [1e-3, 1e3]
  libm log                     7.5         34         (reference)
  atanh series deg-9           15.8        48         1.3e-9 rel               the division costs it the race
  table-64 + deg-4 log1p       4.9         27         2.7e-11 rel              1 KiB of tables
erf(x), x in [-4, 4]  /  N(x)
  libm erf                     37          78         (reference)
  A&S 7.1.26 + poly exp        8.7         41         1.4e-7 abs               4.3x; abs error is the right unit here
  libm N(x) via erfc           39          74
  fast N(x)                    10.4        46         1.4e-7 abs
tail inputs (throughput)
  libm exp(700)                12.5                                            near overflow: special-case checks
  libm exp(-745)               126                                             subnormal result: SOFTWARE slow path, 15x
  libm log(1e-310)             118                                             denormal input: slow path
  poly exp(-745)               4.9                                             no slow path (and no subnormal: returns 0 by construction)
Black-Scholes call + vega, S=100, K in [80,120], T=0.25, sigma=0.2 (cycles per quote)
  libm (exp, log, sqrt, erfc)  87          136
  fast (poly exp, table log, fast N)  53   124        Clang: 12.9 cyc thr — the whole quote vectorized 8-wide
implied vol, Newton from the Manaster-Koehler start, fixed iteration count
  libm, 4 iterations           434         510        max err 4.9e-2 (deep OTM not converged)
  fast, 2 iterations           169         258        max err 4.6e-1
  fast, 4 iterations           358         444        max err 4.9e-2
  fast, 6 iterations           550         636        max err 8.1e-5
```

Read it the Ch. 1 way: the **mean** case is a 1.6× win for `exp` and a 4× win for `erf`, which is the difference between a quote at 87 and 53 cycles; the **tail** case is the reason to do it even if the mean were equal — `exp(−745)` costs libm 15× its normal price, and a strategy whose decay kernel reaches that input will pay it exactly when the signal is quietest. The **accuracy** column is the discipline: Taylor and minimax cost the same and differ 60×; the table `log` beats libm on both axes *because its table is L1-hot in this loop* (§36.2.4). The Clang column shows the real prize — the polynomial quote vectorizes to **12.9 cycles per series** across a chain, which is 2,000 series re-priced in under 10 µs on one core; libm is stuck at 87. And the implied-vol rows are a warning this chapter takes seriously in §36.4.4: Newton at a fixed iteration budget has an accuracy that *depends on moneyness*, and the number you must measure is the error over the strikes you quote, not the mean.

## 36.4 Techniques

### 36.4.1 Build flags first: what to turn on, and what not to

Before writing a line of approximation, take the free wins and refuse the dangerous ones:

- **`-fno-math-errno -fno-trapping-math`** on every hot-path TU (or the whole build). The first lets `sqrt` become `vsqrtsd` and the transcendentals become pure (hoistable, CSE-able); the second lets the compiler drop the exception-ordering constraints. Results are unchanged. Verified in §36.5. (`-fno-signed-zeros` and `-fno-rounding-math` — the latter is the default — are also result-preserving for code that does not inspect the sign of zero.)
- **Not `-ffast-math`, not `-ffinite-math-only`, not `-fassociative-math`** as a blanket. They change results (Ch. 35 §35.4.3), break replay, and — `-ffinite-math-only` in particular — make `std::isnan` constant-fold to `false`, so your NaN guard at the boundary (Ch. 35 §35.6) silently vanishes. If a specific kernel benefits from reassociation, scope it with `#pragma GCC optimize` / `__attribute__((optimize("fast-math")))` on that function or `#pragma clang fp reassociate(on)`, and record it (Appendix D.3).
- **`-march=x86-64-v3` or `native`** so FMA, `vroundsd` and AVX2 are available; the polynomial without FMA is twice the latency and *different bits*.
- **`-fno-plt`** (Ch. 27) if any libm call remains, so it is a direct `call` through the GOT rather than a PLT stub.

### 36.4.2 A polynomial `exp`: reduction, fitted coefficients, exponent-bit reconstruction

The listing in §36.3 is the template. The decisions in it, in order:

1. **Reduction with an FMA.** `r = fma(-k, LN2, x)` computes `x − k·ln2` with one rounding; the naive `x - k * LN2` rounds the product first and loses up to 2⁻⁵³·|k·ln2| ≈ 10⁻¹⁴ absolute — invisible at 10⁻⁹ but the limit if you go to degree 8+. For full double accuracy, split `ln2` into a high part exactly representable with trailing zero bits and a low correction (`LN2_HI`, `LN2_LO`) and subtract both (Cody–Waite).
2. **Coefficients from a fit, not a series.** Generate them with `sollya` (`fpminimax(exp(x), 6, [|D...|], [-log(2)/2; log(2)/2], relative)`) or, with nothing but NumPy, interpolate at Chebyshev nodes — the §36.3 coefficients came from a twelve-line script of the latter and are within a few percent of true minimax. Keep the script with the code; the coefficients are a build artefact with a provenance.
3. **Degree from the error budget, not habit.** Decide what the *consumer* needs: a theoretical price compared against a tick grid of 0.01 on a 100-dollar option needs 10⁻⁶ relative at most; degree 6 at 2.5 × 10⁻⁹ is three orders of margin; degree 4 (≈ 10⁻⁶) would do and saves two FMAs. A Greek that is differenced against the previous value needs more. Write the requirement in the comment above the coefficients.
4. **Reconstruction by exponent arithmetic.** `((int64)k + 1023) << 52` builds `2^k` directly; multiply once. Declare the domain: `|x| < 700` (so `k ∈ [−1010, 1010]`). If an input can violate it, clamp `x` first (`std::clamp(x, -700.0, 700.0)` — two `vmaxsd`/`vminsd`, branchless) *or* assert it in debug builds and let input validation upstream guarantee it in release. Never let the exponent trick see `k = −1100`: the shift wraps into the sign bit and the result is garbage with no NaN to tell you.
5. **`nearbyint`, not `round`.** `std::nearbyint` is `vroundsd` with the current rounding mode (one instruction); `std::round` (half away from zero) is a small sequence because x86 has no such mode. `vcvtsd2si` is also fine when `k` is only needed as an integer.

The same template gives `exp2` (drop the `LOG2E` multiply), `expm1` for small arguments (skip the `+1.0` coefficient and the reconstruction when `|x|` is tiny — an EWMA's `1 − exp(−dt/τ)` needs this or it cancels to zero), and a `float` version with degree 4 at 10⁻⁷ for signal math that lives in `float` SoA arrays (Ch. 10).

### 36.4.3 Table-driven `log` (and `exp`) with `constexpr` tables

`log` resists a pure polynomial because its natural reduction leaves `m ∈ [1,2)` — too wide for a short series — and the alternative (`s = (m−1)/(m+1)`, an odd `atanh` series) carries a division, which §36.3 shows costs it the race (15.8 cycles). The table design:

```cpp
// log_table.hpp — log(x) for normal positive x: e*ln2 + log(c_j) + log1p(u), u = m/c_j - 1, |u| <= 1/128
struct LogTables {
    double inv[64], lg[64];
    constexpr LogTables() : inv{}, lg{} {
        for (int j = 0; j < 64; ++j) { double c = 1.0 + (j + 0.5) / 64.0; inv[j] = 1.0 / c; lg[j] = cx_log(c); }   // cx_log: a constexpr log (Ch. 21)
    }
};
inline constexpr LogTables LT{};                                       // .rodata, 1 KiB, warmed with the code (Ch. 62)

inline double log_table(double x) {                                    // domain: x > 0, normal (no denormals, no 0/Inf/NaN)
    const std::uint64_t b = std::bit_cast<std::uint64_t>(x);
    const std::int64_t  e = std::int64_t((b >> 52) & 0x7ff) - 1023;     // exponent
    const double m = std::bit_cast<double>((b & 0x000fffffffffffffULL) | 0x3ff0000000000000ULL);   // mantissa in [1,2)
    const int j = int((b >> 46) & 63);                                 // top 6 mantissa bits pick the sub-interval
    const double u = std::fma(m, LT.inv[j], -1.0);                     // exact to one rounding
    double p = -0.25; p = std::fma(p, u, 1.0 / 3); p = std::fma(p, u, -0.5); p = std::fma(p, u, 1.0); p *= u;   // log1p(u), deg 4
    return std::fma(double(e), 0.6931471805599453, LT.lg[j] + p);
}
```

4.9 cycles and 2.7 × 10⁻¹¹ relative error in §36.3 — the division is gone (a table load of `1/c_j` replaced it), the polynomial is four FMAs because `|u| ≤ 1/128`. The `constexpr` constructor needs a `constexpr`-evaluable `log` for the table entries (a Taylor series to full precision is fine at compile time — Ch. 21 — because compile-time cost is free); the table lands in `.rodata` next to the code and is pre-faulted and warmed with it (Ch. 62 §62.4.1). The same shape gives the 256-entry `exp` table of §36.3 (`2^(j/256)` and a degree-3 polynomial on `|r| ≤ ln2/512`). Size the table to the cache budget of the *call site*: 1–2 KiB is sixteen to thirty-two lines of L1 that the rest of the decision path cannot use; a 16 KiB table "for accuracy" is a quarter of L1 and will not be hot when the tick arrives.

### 36.4.4 `N(x)`, the Black-Scholes quote, and an implied-vol solve on a budget

The normal CDF is the pricing kernel's expensive call: libm has no `N(x)`, so it is `0.5·erfc(−x/√2)` at 39 cycles. Two cheaper routes with stated bounds:

- **Abramowitz & Stegun 7.1.26**: `erf(x) ≈ 1 − (a₁t + … + a₅t⁵)·exp(−x²)`, `t = 1/(1 + p·x)` — five FMAs, one division, one `exp` (the polynomial one): **8.7 cycles, 1.4 × 10⁻⁷ absolute**. Enough for a price (the error in `N(d₁)` is scaled by `S`, so 1.4 × 10⁻⁷ × 100 = 1.4 × 10⁻⁵ dollars, far inside a 0.01 tick) and for a delta; not enough for a gamma that will be differenced.
- **Cody's rational minimax for `erfc`** (what libm itself uses, in three ranges) or **Hart's `N(x)`** — 10⁻¹⁶ absolute at ~2× the cost — when the consumer is a Greek or a calibration.

With `exp_poly6`, `log_table` and the A&S `N(x)`, the Black-Scholes call price and vega are **53 cycles** scalar and **12.9 cycles per series when Clang vectorizes the loop over a chain** (§36.3) — the loop must be written over SoA arrays of `K`, `T`, `σ` (Ch. 10) with no early-outs for that to happen.

**Implied volatility** is a root find, and §36.3's numbers are the whole lesson: Newton's method on `C(σ) − C_market` with the vega as the derivative converges quadratically *once it is close*, and the Manaster–Koehler starting point `σ₀ = √(2|ln(S/K) + rT| / T)` guarantees monotone convergence — but *how many* iterations "close" takes depends on moneyness: 4 iterations leave a 5 × 10⁻² error on the deep-OTM strikes of the test chain while the at-the-money strikes were at 10⁻¹⁰ after two. Three ways to make the budget deterministic: (a) take **Jäckel's "Let's Be Rational"** (two Householder iterations from a rational initial guess, full double precision for *every* strike, ~2× the cost of 2 Newton steps) — the right answer for production; (b) keep Newton but **seed it from the previous tick's vol** for the same series — warm starts converge in one step on a quiet tick, and the series' state is already in the SoA row; (c) solve on the **batch tier** (Ch. 89) and publish the surface, so the hot path never solves at all — only re-prices from `σ` with the 53-cycle kernel. Whichever you choose, the gate is a *measured error over the live strike range*, not a fixed iteration count chosen on an ATM example.

### 36.4.5 Vectorizing across the chain, and the vector libraries

The quoting engine's loop is "for each series in the chain, theo = BS(S, K[i], T[i], σ[i])" — thousands of independent evaluations per tick, the textbook SIMD case (Ch. 39). What makes the hand-written polynomial vectorize where libm cannot:

- **No calls, no branches, no `errno`** inside the loop body — the three things that stop the auto-vectorizer. `std::fma`, `std::nearbyint`, `std::bit_cast` and the table loads (a `vgatherdpd`, or better: keep the table small enough that the gather is cheap, or restructure so the table index is shared) all vectorize.
- **SoA inputs and outputs**, `__restrict` or `std::span` with no aliasing, and a trip count the vectorizer can see (Ch. 39 §39.4.1).
- **Width**: AVX2 gives 4 doubles, AVX-512 8 (with the frequency-licence caveat of Ch. 39 §39.2.3 — on the reference box a 512-bit loop of this length is a net win, measured there; on a shared core it may not be). Clang's 1.25 cycles/`exp` in §36.3 is the 8-wide case.

When the kernel is not yours to restructure — a vendor pricing library, a research model ported to C++ — link **SLEEF** (`Sleef_expd4_u10`, ≤ 1 ULP, BSL licence) or glibc's `libmvec` (`-ffast-math` plus `-lmvec`, or the `__attribute__((__simd__))` declarations in `<math.h>` under `-fopenmp-simd`) and call the vector variants explicitly on 4/8-lane chunks; both are within 1.5× of the hand-written polynomial in throughput and give you `pow`, `sin`, `atan` and friends with audited accuracy. Intel's SVML (`-fveclib=SVML` with ICX/Clang, or oneMKL VM's `vdExp`) is the fastest on Intel parts and the least portable.

### 36.4.6 Determinism: the approximation is yours, libm is not

A hand-written `exp` is bit-identical on every build that uses explicit `std::fma` and the same `-march` baseline — the sequence of operations is fixed in the source, and FMA contraction cannot change it because there is nothing left to contract. libm is not: glibc 2.27 → 2.28 replaced `exp`/`log`/`pow` with new implementations that differ in the last bit for some inputs, and a `libmvec` vector variant differs from the scalar one. For the deterministic core (Ch. 103) and bit-exact replay (Ch. 108), that makes the approximation *more* reproducible than the library, with two rules: pin `-ffp-contract` (explicit `fma` makes it moot, but keep it `off` for the surrounding arithmetic — Ch. 35 §35.4.3) and never mix a vectorized and a scalar evaluation of the same function on the same tick (a tail loop that evaluates the last three series scalar while the first 1,997 went 8-wide is fine *only* if both use the same coefficients and the same operation order — the polynomial does, `libmvec` vs libm does not). Record the approximation's version (the coefficient file's hash) in the build fingerprint (Ch. 117 §117.4.1, Appendix N) so a replay knows which `exp` priced the quote.

## 36.5 Verify the codegen: no `call`, an FMA chain, and the widened loop

Snippets are **verified** Clang 18.1 output (`-O2 -std=c++20 -march=x86-64-v3 -fno-math-errno -fno-trapping-math`, Appendix L.3); GCC 13.3 is the same shape with `vfmadd132sd`/`vfmadd231sd` forms.

**`exp_poly6` is sixteen instructions and no call** — one multiply, one `vroundsd`, the reduction FMA, six Horner FMAs with memory-operand coefficients, the exponent build (`vcvttsd2si`, `shlq $52`, `movabsq`/`addq`, `vmovq`) and the final multiply:

```asm
_Z9exp_poly6d:
        vmulsd    .LCPI0_0(%rip), %xmm0, %xmm1      # x * log2e
        vroundsd  $12, %xmm1, %xmm1, %xmm1          # k = nearbyint (current mode)
        vfmadd231sd .LCPI0_1(%rip), %xmm1, %xmm0    # r = fma(-k, ln2, x)
        vmovsd    .LCPI0_2(%rip), %xmm2             # c6
        vfmadd213sd .LCPI0_3(%rip), %xmm0, %xmm2    # p = fma(p, r, c5)
        vfmadd213sd .LCPI0_4(%rip), %xmm0, %xmm2
        vfmadd213sd .LCPI0_5(%rip), %xmm0, %xmm2
        vfmadd213sd .LCPI0_6(%rip), %xmm0, %xmm2
        vfmadd213sd .LCPI0_7(%rip), %xmm0, %xmm2
        vfmadd213sd .LCPI0_8(%rip), %xmm0, %xmm2    # p = fma(p, r, 1.0)
        vcvttsd2si %xmm1, %rax                      # (int64)k
        shlq      $52, %rax
        movabsq   $4607182418800017408, %rcx        # 0x3FF0000000000000 = bits of 1.0  (+1023 << 52)
        addq      %rax, %rcx
        vmovq     %rcx, %xmm0                       # 2^k
        vmulsd    %xmm0, %xmm2, %xmm0
        retq
```

The critical path is the seven FMAs (28 cycles at 4 each) plus the multiply and the `vroundsd` — the 32-cycle latency §36.3 measured. The Estrin variant shows two `vmulsd` for `r²`/`r⁴` and FMAs that pair up in the schedule.

**The loop over a chain is widened**: `exp_chain(const double* in, double* out, int n)` compiles to a body of `vbroadcastsd` for the nine constants hoisted out of the loop, `vmovupd (%rdi,%r8), %ymm10` / `vmulpd` / `vroundpd` / eleven `vfmadd...pd` / `vcvttpd2dq` + `vpsllq $52` + `vpaddq` for the four exponents at once / `vmulpd` / `vmovupd` to `out` — 4 doubles per iteration at `-march=x86-64-v3`, 8 with `-march=native` on the AVX-512 guest, with a scalar remainder loop of the same instruction sequence (the determinism rule of §36.4.6 holds because both are the same source). Count the `vfmadd` with `grep -c vfmadd...pd`: 14 in the vector body (7 per unrolled copy). **What to look for in the negative:** `call exp@PLT` or `call erf@PLT` anywhere inside a hot loop means a call survived — a libm fallback you did not intend, or a `std::pow(x, 0.5)` that should have been `sqrt`.

**`-fno-math-errno` on `sqrt`**: with the flag, `double f(double x) { return std::sqrt(x); }` is `vsqrtsd %xmm0, %xmm0, %xmm0; ret`. Without it (GCC 13.3, `-O2 -march=native`):

```asm
        vxorpd   %xmm1, %xmm1, %xmm1
        vucomisd %xmm0, %xmm1          # 0 > x ?
        ja       .L10                  # negative: take the call so errno can be set
        vsqrtsd  %xmm0, %xmm0, %xmm0
        ret
.L10:   jmp      sqrt@PLT
```

A branch and a PLT call on the slow side — predicted away for positive inputs, but a dependency the vectorizer refuses to widen.

## 36.6 Pitfalls & anti-patterns

- **An approximation with no measured bound.** "Fast exp from a blog" with an unknown error over an unknown domain is a pricing bug waiting for a deep strike. Every approximation ships with its interval, its max error *measured against libm* over that interval (dense sampling, not three points), and the consumer's requirement it was checked against (§36.2.3, §36.4.2).
- **Taylor coefficients.** The same seven FMAs, 60× the error (§36.3). Fit, don't expand.
- **Breaking the reduction.** `x - k * LN2` without an FMA, or a single-constant `ln2` at degree 8+, leaves a cancellation error larger than the polynomial's — the function is less accurate than its coefficients promise and you will not notice until you measure the bound (§36.4.2).
- **Letting the exponent trick see an out-of-domain `k`.** `exp(-800)` through `(k + 1023) << 52` with `k = −1154` wraps into the sign bit: a huge negative number, no NaN, no trap. Clamp or validate at the boundary (§36.4.2).
- **A table that is not hot.** The 4.8-cycle table `exp` of §36.3 is a 14+-cycle one when its line is in L2 and a 60-cycle one from L3 (Ch. 8). Tables belong in warm throughput loops; the latency path uses the pure polynomial (§36.2.4).
- **Fixed-iteration Newton for implied vol, validated at the money.** Converged in two steps for the ATM example, 5 × 10⁻² wrong deep OTM (§36.3). Use a method with a uniform convergence guarantee, a warm start from the last tick, or move the solve to the batch tier (§36.4.4).
- **`-ffast-math` instead of an approximation.** It makes libm calls go to `libmvec`'s laxer variants, turns on reassociation and `-ffinite-math-only` (so `isnan` folds to `false` and your NaN guard is gone), and changes replay bits. Take `-fno-math-errno -fno-trapping-math` and write the polynomial (§36.4.1).
- **`float` because "it's faster."** §36.3: libm `expf` is *slower* than the double polynomial. Single precision pays off only when it halves the SoA footprint and doubles the SIMD width of a loop that is already vectorized — not as a scalar substitute.
- **Mixing implementations on one tick.** Vector `libmvec` for the first 1,996 series and scalar libm for the last 4 give two different `exp`s in one quote update; a replay that vectorizes differently diverges (§36.4.6).
- **Re-solving on the hot path what the batch tier already solved.** A vol surface, the Greeks, a calibration — computed once per second on the GPU (Ch. 89) and published through a seqlock (Ch. 46) — should be *read*, not recomputed per tick; the hot path's job is the 53-cycle re-price from the published `σ`.

## 36.7 Exercises & checklist

**Exercises**

1. **Fit your own.** Generate near-minimax coefficients for `exp` on `[−ln2/2, ln2/2]` at degrees 4–8 (Chebyshev-node interpolation in NumPy, or `sollya`'s `fpminimax`); measure the max relative error of each against libm over 10⁷ points; plot error vs degree vs cycles. Find the degree at which the reduction error (single-constant vs Cody–Waite `ln2`) becomes the limit.
2. **Horner vs Estrin.** Measure both on the §36.3 harness in throughput and latency mode; then put the `exp` inside a dependent loop (an EWMA update) and inside an independent loop (a chain re-price) and confirm which wins where. Read the asm for the pairing of FMAs in the Estrin version.
3. **The tail.** Feed libm `exp` a sweep from −700 to −750 and plot cycles per call; find the input at which the subnormal slow path begins. Repeat with FTZ/DAZ on (Ch. 35 §35.4.2) and show it does *not* help. Then show the polynomial's constant cost across the sweep — and its wrong answer below the clamp if you forget §36.4.2 step 4.
4. **Black-Scholes across a chain.** Price 2,000 series with libm and with the fast kernels, scalar; then write the loop over SoA arrays and confirm (asm, `-Rpass=loop-vectorize`) that only the fast version widens. Measure µs per chain re-price at AVX2 and AVX-512 widths and note the frequency effect (Ch. 39 §39.3).
5. **Implied vol, honestly.** Compute the IV error over strikes from 50% to 150% moneyness for Newton at 2/4/6/8 iterations from the Manaster–Koehler start, from a warm start (previous σ + 1% perturbation), and with Jäckel's method; tabulate max error vs cycles. Decide a production policy and write down the strike range it was validated on.
6. **Determinism.** Build the fast `exp` with GCC and Clang, `-O2` and `-O3`, AVX2 and AVX-512; hash the outputs over 10⁷ inputs and show they are identical. Then do the same with libm across two glibc versions (a container will do) and show they are not.

**Checklist — fast transcendentals**

- [ ] Hot-path TUs build with **`-fno-math-errno -fno-trapping-math`** and *not* with blanket `-ffast-math`; `sqrt` is `vsqrtsd` and no `call exp@PLT` remains in a hot loop (§36.4.1, §36.5).
- [ ] Every approximation declares its **input domain** and ships with a **measured max error** (relative or absolute, as the consumer needs) over that domain, dense-sampled against libm (§36.2.3, §36.4.2).
- [ ] Coefficients are **fitted** (Remez/Chebyshev), stored `constexpr` with the generating script and the bound; the reduction uses an **FMA** (and Cody–Waite constants above degree 8) (§36.4.2).
- [ ] Reconstruction by **exponent bits** is guarded by a clamp or an upstream validation; no out-of-domain `k` can reach the shift (§36.4.2, §36.6).
- [ ] **Tables** are small (≤ 2 KiB), `constexpr`, in `.rodata`, warmed with the code, and used only on warm throughput paths; the latency path uses the pure polynomial (§36.2.4, §36.4.3).
- [ ] **Horner** for throughput loops, **Estrin** for the single evaluation on the decision path — chosen per call site, measured (§36.2.3).
- [ ] `N(x)` uses a bound that matches the consumer (A&S 10⁻⁷ for prices and deltas; Cody/Hart for Greeks that are differenced) (§36.4.4).
- [ ] Implied vol has a **uniform** accuracy guarantee over the live strike range (Jäckel, warm start, or batch-tier solve) — not a fixed iteration count validated ATM (§36.4.4, §36.6).
- [ ] The chain re-price loop is **SoA, branch-free and vectorized** (confirmed in asm); vector libraries are used for batches, never for the single value on the decision path (§36.4.5).
- [ ] The approximation's **version is in the build fingerprint**; one implementation per function per tick; replay is bit-exact (§36.4.6).

## 36.8 References

- J.-M. Muller et al., *Handbook of Floating-Point Arithmetic*, 2nd ed., and J.-M. Muller, *Elementary Functions: Algorithms and Implementation*, 3rd ed. — range reduction (Cody–Waite, Payne–Hanek), polynomial and rational approximation, table-driven methods, and error analysis (§36.2.2–§36.2.4).
- W. J. Cody and W. Waite, *Software Manual for the Elementary Functions* — the original reduction-and-approximation recipes libm descends from (§36.2.2).
- M. Abramowitz and I. A. Stegun, *Handbook of Mathematical Functions*, §7.1.26 — the `erf` approximation of §36.4.4 and its 1.5 × 10⁻⁷ bound; W. J. Cody, "Rational Chebyshev approximations for the error function," *Math. Comp.* 23 (1969) — the `erfc` libm uses.
- S. Chevillard, M. Joldeş, C. Lauter, "Sollya: an environment for the development of numerical codes" (ICMS 2010) — `fpminimax` and `supnorm`, the tool for §36.4.2's coefficients and bounds.
- P. Jäckel, "Let's Be Rational" (*Wilmott*, 2015) — full-precision implied volatility in two iterations for every strike (§36.4.4); S. Manaster and G. Koehler, "The calculation of implied variances from the Black-Scholes model: a note," *J. Finance* 37 (1982) — the convergent starting point.
- The GCC and Clang manuals: `-fno-math-errno`, `-fno-trapping-math`, `-ffp-contract`, `-fveclib`, `#pragma clang fp`; glibc's `libmvec` documentation and `NEWS` for 2.28 (the new `exp`/`log`/`pow`) (§36.4.1, §36.4.6).
- N. Shibata and F. Petrogalli, "SLEEF: a portable vectorized library of C standard mathematical functions," *IEEE TPDS* 31 (2020) (§36.4.5).
- A. Fog, *Instruction Tables*; uops.info — FMA/`vroundsd`/`vcvttsd2si` latency and throughput behind §36.2.3 and §36.5.

## 36.9 Additional Reading

- The glibc `sysdeps/ieee754/dbl-64/e_exp.c`, `e_log.c` and `s_erf.c` sources — read what a production libm does per call (the table sizes, the special-case ladder, the slow paths of §36.2.1).
- Intel, *Optimization Reference Manual* — the "approximate reciprocal/rsqrt + Newton" patterns and the AVX-512 frequency licences that bound §36.4.5.
- Ch. 35 (*Fixed/Floating-Point*) — the determinism and denormal rules this chapter inherits; Ch. 13 (*Pipelines*) — why Horner is a chain; Ch. 39 (*SIMD*) — the vectorization mechanics; Ch. 40 (*Streaming Statistics*) — the EWMA/log-return consumers; Ch. 89 (*GPU*) — the batch tier that publishes surfaces; Ch. @QUOTING@ (*Quoting Engine*) — what the 53-cycle quote feeds.
- **Appendix D.3** — the math flags, consolidated; **Appendix E** — the `exp`/`log`/`erf` rows; **Appendix L** — the harness.

---

*Next: Ch. 37 — Bit Manipulation & Integer Tricks, the integer side of Part V's numerics: `popcnt`/`lzcnt`/`tzcnt`/`pdep`/`pext`, bitmaps for order-book occupancy and free-lists, branchless min/max/abs/sign, and power-of-two rounding — the instructions this chapter's exponent trick and Ch. 35's scaled-integer prices run on.*
