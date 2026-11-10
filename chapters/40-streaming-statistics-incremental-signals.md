# Part V — Numerics & Data Parallelism

# Chapter 40 — Streaming Statistics & Incremental Signal Computation on the Hot Path

> **Prerequisites:** Ch. 35 (scaled integers for prices, FTZ/DAZ, cross-build FP determinism — this chapter is where those hazards bite), Ch. 13 (dependency chains — an EWMA *is* one), Ch. 10 (SoA vs AoS — the state layout question), Ch. 33 (the book the signals read), Ch. 37 (power-of-two masks for the rings), Ch. 103 §103.2.2 (the pure `apply(state, input)` core and time as an input — this chapter is that core's arithmetic), Ch. 3 (what to measure).
>
> **Leads into:** Ch. 104 (the risk gate the actions pass through), Ch. 106 §106.4.5 (the throttle's decayed counter is §40.4.6's), Ch. 109 and Ch. 116 (replay and golden-master tests, which need §40.4.6's bit-exactness), Ch. 119 §119.3 (the "book update / strategy" stage). The alpha — *which* signals and *why* — stays outside this book, as Appendix J.7 says; the mechanics of computing any of them in O(1), in fixed memory, deterministically, are here. Closes **Part V**.

---

## 40.1 Why it matters: the strategy stage is the tail, and its arithmetic is the stage

Ch. 119 §119.3 puts "book update / strategy" at ~150 ns p50 and ~500 ns p99.9 and names it the dominant contributor to the tick-to-trade *tail*. Ch. 33 just took the book's share of that; what remains is the decision — a handful of signals recomputed on every event and compared against thresholds. Nothing about that is hard in the abstract; what makes it a latency problem is that the obvious implementations are all wrong in the same three ways. They **recompute**: a 1024-sample rolling mean written as a loop over the window costs 300 ns per event (§40.3) where the incremental form costs the same ~20 ns at any window length. They **drift**: the incremental form, done in floating point as add-new-minus-old, accumulates rounding error without bound, and the fix (compensated summation, periodic exact recompute, or — for prices — not using floating point at all) has to be designed in. And they are **not reproducible**: the same source over the same capture yields different bits under a different layout, compiler or flag, because floating-point contraction decisions differ — §40.3 catches GCC 13 producing two different results from one function template instantiated over two state layouts — and a strategy whose replay disagrees with its production run cannot be debugged (Ch. 109) or regression-tested (Ch. 116).

So this chapter is the strategy stage as *computation*: a pure `on_event(state, event) → actions` core over per-instrument state, every statistic maintained in O(1) with fixed memory, the numerics chosen so that a day of updates does not erode them, and the build pinned so that the bits are the same on the replay box. It is deliberately silent on what the signals *mean*; it is loud about what they *cost*.

## 40.2 Mental model

### 40.2.1 The strategy as `on_event(state, event) → actions`

Ch. 103 §103.2.2 made the architectural argument: the core is a pure function of its state and its input, time is a *field* of the input and never read inside, randomness is an input, and every side effect — an order, a log line, a telemetry sample — is a *returned* action that the I/O layer performs. This chapter adds the arithmetic consequences. Because the core never reads a clock, every time-dependent statistic (an exponential decay over irregular arrivals, a rate limiter, a staleness check) is a function of the event's timestamp and the state's last timestamp — which is what makes it replayable at all. Because it never allocates, every window is fixed-size and pre-sized. Because it returns actions rather than performing them, its cost is its arithmetic plus its state's cache footprint and nothing else, which is why §40.3 can measure it in isolation at ~20 ns per event with nine signals maintained.

### 40.2.2 Per-instrument state: what is hot, what is cold, and which layout

An event names one instrument and the core touches *all* of that instrument's hot scalars (means, decays, ring heads, the previous BBO) and a few slots of its windows. Ch. 10's SoA advice — one array per field — is for the *other* access pattern, a sweep over all instruments one field at a time (end-of-interval risk, a cross-sectional ranking, telemetry). For the per-event pattern the hot scalars of one instrument want to be *together*: ~120 bytes, two cache lines, fetched once. §40.3 measures both at 1024 instruments and finds them within 5 % of each other — the entire state is L2-resident either way — and names the regime where it matters: when `instruments × fields` outgrows L2 (a venue-wide strategy over 10⁵ symbols), SoA touches ~15 lines per event and AoS two. The windows (rings, deques) are separate arrays indexed by instrument regardless: they are the cold part of the state, touched one slot per event.

### 40.2.3 Streaming statistics: O(1) updates, and the numerics of each

| Statistic | Incremental form | Per-event cost | Numerical hazard |
|---|---|---|---|
| mean, variance, covariance | Welford / West: `d = x − μ; μ += d/n; M2 += d·(x − μ)` | one division, a few FMAs | none worth naming — that is the point (the naive `E[x²] − E[x]²` loses everything: §40.3) |
| EWMA, EW variance | `μ += (1−w)(x − μ)`; `v = w·(v + (1−w)·d²)` | two FMAs, no division; `w` precomputed, `exp(−Δt/τ)` from a table for irregular time | denormals when the input goes quiet (§40.3: 33×); a loop-carried chain (§40.5) |
| rolling sum / mean over W | ring + running sum: `S += x − ring[i]` | one load, one store, one add | drift in floating point (§40.3: float is wrong after 10⁹ updates); exact in integers |
| rolling min / max over W | monotonic deque of (value, position) | amortised O(1): a short pop loop, one push | none; the pop loop is data-dependent (short, usually zero) |
| order-flow imbalance (OFI) | `e_bid − e_ask` from the BBO delta | six compares and adds | none (integers) |
| microprice, VWAP | `(bid·askQ + ask·bidQ) / (bidQ + askQ)`; `Σ px·qty / Σ qty` | one division *at read*, not per update | integer overflow in Σ px·qty over a day (use 64-bit; 10⁵ ticks × 10⁹ shares fits) |
| decayed rate (throttle) | `c = c·w(Δt) + 1` | one FMA + table lookup | denormals, as EWMA |

The two design rules that fall out: **accumulate in integers whenever the quantity is a price or a size** (Ch. 35 — exact, drift-free, overflow-checked at the type), and when a statistic must be floating point, choose the *form* with the good numerics (Welford, not sum-of-squares; EWMA, which forgets its errors geometrically, over a long windowed sum, which does not).

### 40.2.4 Determinism: the same bits on the replay box

Bit-exact replay (Ch. 109, Ch. 116's golden masters) requires that the same event stream produce the same state bits. Integers are deterministic by construction. Floating point is deterministic under three conditions, all of which §40.3 tests: **no reassociation** (`-ffast-math`, `-Ofast` and `-fassociative-math` are off — Ch. 35 §35.4.3), **no implicit FMA contraction** (`-ffp-contract=off`, or every FMA written explicitly as `std::fma`), and **the same operation order in the source** — which a template instantiated over two layouts does not guarantee unless contraction is off, because the optimiser's decision to fuse `a*b + c` is made per instantiation. The table-driven `exp` (§40.4.3) is also a determinism device: `std::exp` is correctly rounded on neither libm, and two libm versions can differ in the last bit.

## 40.3 Measure it: incremental vs recompute, two layouts, drift, cancellation, the denormal cliff, and the FMA hash

**Machine, toolchain, flags.** `signals.cpp` (the core of §40.4.1, both state layouts, the recompute baseline, the drift, cancellation and denormal experiments and a bit-exact state hash — 220 lines) was **run on the author's 4-vCPU KVM guest** (Appendix L.2: TSC 2.70 GHz, Linux 6.18, no isolation), GCC 13.3 `-O2 -std=c++20 -march=native` and Clang 18.1 `-march=x86-64-v3`, pinned, 20 M events over 1024 instruments (70 % quotes, 30 % trades; 1–20 ms gaps; a random-walking price in ticks). VM-measured; every ratio transfers, the absolute per-event figures are L2-resident and should land within ±20 % on the reference machine.

```
   per event, 20 M events, 1024 instruments, nine signals maintained (GCC 13.3 / Clang 18.1)
   incremental O(1), SoA state ............................  18.9 ns / 17.3 ns     <- flat in the window length
   incremental O(1), AoS state (128-byte hot block) ........  20.0 ns / 18.0 ns     <- within 5 %: everything is L2-resident at 1024 instruments
   recompute mean/var/min/max over the ring, W =   64 ......  20.3 ns / 17.9 ns     <- the AVX-512 sweep of 64 int64 is as cheap as the O(1) update
   recompute ...,                          W =  256 ......  78.2 ns / 76.9 ns
   recompute ...,                          W = 1024 ...... 301.3 ns / 303.7 ns     <- linear in W: 15x the incremental form, and still fewer signals

   drift, 1e9 add-new/subtract-old updates of a 64-sample ring of prices ~123.45–133.44 (6.0 s):
     double running sum 8223.2300000154  vs the window recomputed 8223.2300000000  -> error 1.5e-8  (grows without bound, slowly)
     float  running sum 8249.1094        -> error 25.88 = 0.3 % of the sum: WRONG after a morning
     int64  cents       822323           -> exact
   variance of 1e6 prices ~123456.78 ± 0.01 (true 6.667e-5):  naive E[x²]−E[x]²  2.26e-1   Welford  6.657e-5   <- naive is off by 3400x
   EWMA of a signal that went to zero, 2e7 steps:  FTZ/DAZ off  34.5 ns/step   FTZ/DAZ on  1.04 ns/step    <- the 33x denormal cliff (Ch. 35)
   state hash after 20 M events (bit-exact FNV over the doubles' bits):
     GCC  -O2 -march=native             SoA 90b005747b00df40   AoS 35ebdd7beeb99618   <- SAME source, two layouts, DIFFERENT bits (16 vfmadd in the binary)
     GCC  ... -ffp-contract=off          SoA 1f1483e57dc2e192   AoS 1f1483e57dc2e192   <- identical: no contraction, no choice to differ on
     GCC  ... -ffast-math                SoA 12bcb57b20b728ea   AoS eb19bfeb53742f05   <- reassociation: different again
     Clang -O2 -march=x86-64-v3          SoA 90b005747b00df40   AoS 90b005747b00df40   <- Clang contracted both the same way (69 vfmadd) — by luck, not by rule
```

Six readings. **Incremental is flat; recompute is linear** — at W = 64 the vectorised sweep is as cheap as the O(1) update (so "always incremental" is dogma until W exceeds a cache line or two), at W = 1024 it is 15× slower, and the incremental version maintains nine statistics where the recompute maintains four. **Layout did not matter here**, and the text says why (§40.2.2): 1024 × ~120 bytes of hot state is L2-resident under either layout; exercise 2 is the venue-wide case where it does. **Floating-point drift is real and type-dependent**: a `double` running sum drifts 1.5 × 10⁻⁸ in 10⁹ updates (harmless for a signal, fatal for a P&L), a `float` one is wrong in the second decimal, and the integer sum is exact — prices and sizes are integers (Ch. 35), so their sums are too. **The naive variance is not a variance**: for prices near 10⁵ with a spread of 10⁻², `E[x²] − E[x]²` subtracts two numbers that agree to twelve digits and keeps the noise; Welford's recurrence keeps the four significant digits the data has. **The denormal cliff is 33×** on this core — an EWMA of a feed that went quiet decays into the subnormal range after ~7 000 steps and every step after that takes a microcode assist until FTZ/DAZ is set (Ch. 35 §35.4.2 — the fast-math build sets it at startup, which is the *only* thing fast-math did right here). **And the determinism result**: GCC 13 compiled the one `on_event` template differently for the two layouts — it contracted `d * (x − μ)` into an FMA in one instantiation and not the other — so the same source over the same stream produced different state bits; with `-ffp-contract=off` the two agree, and agree with each other across runs; Clang happened to make the same choice twice. A replay box built with a different layout, compiler or `-march` than production would have disagreed with it on the fourth decimal of every EWMA, and §40.4.6 is the fix.

## 40.4 Techniques

### 40.4.1 The core and its state

```cpp
// signals.cpp (excerpt) — the pure core over per-instrument state. Time is a field of the event; nothing here reads a clock,
// allocates, or performs I/O. `S` is either the SoA or the AoS layout behind the same accessors (§40.2.2); W is a power of two.
struct Event { u32 inst; u32 kind; i64 ts_ns; std::int32_t px, qty, bid_px, ask_px, bid_qty, ask_qty; };   // 40 bytes; kind 0 = trade, 1 = quote
struct Params { double alpha; double decay[256]; double tau_ns; };     // decay[q] = exp(-q·1ms/tau), built ONCE at startup: no exp() on the hot path
struct Action { u32 inst; i64 signal; };                              // what the core RETURNS; the I/O layer acts on it

struct alignas(64) InstHot {                                          // AoS: one instrument's hot scalars in two cache lines
    double mean, m2, ewma, ewvar; u64 n; i64 last_ts, rsum, ofi, vn, vd;
    std::int32_t pbp, pap; u32 pbq, paq; u32 head, dmin_h, dmin_t, dmax_h, dmax_t, events; u32 pad[2]; };
static_assert(sizeof(InstHot) == 128, "two cache lines per instrument");   // Ch. 29 §29.4.3

template <class S> static inline Action on_event(S& s, const Params& p, const Event& e) {
    const u32 k = e.inst; ++s.events(k);
    if (e.kind == 1) {                                                // quote: order-flow imbalance from the BBO delta (§40.4.4)
        i64 eb = (e.bid_px >= s.pbp(k) ? i64(e.bid_qty) : 0) - (e.bid_px <= s.pbp(k) ? i64(s.pbq(k)) : 0);
        i64 ea = (e.ask_px <= s.pap(k) ? i64(e.ask_qty) : 0) - (e.ask_px >= s.pap(k) ? i64(s.paq(k)) : 0);
        s.ofi(k) += eb - ea;
        s.pbp(k) = e.bid_px; s.pap(k) = e.ask_px; s.pbq(k) = e.bid_qty; s.paq(k) = e.ask_qty;
        return {k, s.ofi(k)};
    }
    const double x = double(e.px);                                    // trade
    const u64 n = ++s.n(k); const double d = x - s.mean(k); s.mean(k) += d / double(n); s.m2(k) += d * (x - s.mean(k));   // Welford (§40.4.2)
    const i64 dt = e.ts_ns - s.last_ts(k); s.last_ts(k) = e.ts_ns;
    const double w = p.decay[u32(std::min<i64>(dt / 1'000'000, 255))];   // irregular-time decay, one load (§40.4.3)
    const double delta = x - s.ewma(k);
    s.ewma(k) += (1.0 - w) * delta;
    s.ewvar(k) = w * (s.ewvar(k) + (1.0 - w) * delta * delta);
    const u32 h = s.head(k); const u32 pos = h & (W - 1); i64* ring = &s.ring[k * W];                   // rolling sum (§40.4.4)
    const i64 leaving = ring[pos]; ring[pos] = e.px; s.rsum(k) += e.px - leaving; s.head(k) = h + 1;
    /* rolling min and max: the monotonic deques of §40.4.4 */
    s.vn(k) += i64(e.px) * e.qty; s.vd(k) += e.qty;                   // VWAP numerator/denominator: integers, divided at READ
    const i64 wmean = s.rsum(k) / i64(std::min<u32>(h + 1, W));
    return {k, i64(s.ewma(k)) - wmean};                               // the decision (a stand-in): what the I/O layer acts on
}
```

The shape to copy: inputs are a `const Event&` and a `const Params&` (decay tables, thresholds — built at startup, reloaded through Ch. 102, never computed here); the state is addressed by instrument index, never by a map lookup (the feed handler resolved the symbol once — Ch. 70); every window is a power-of-two ring indexed by a mask; and the function returns. The nine statistics cost ~19 ns together (§40.3), of which the two divisions (Welford's `d/n` and the window mean) are about a third — the window mean division belongs at *read* time too, like VWAP's, when the signal is compared rather than reported.

### 40.4.2 Welford: mean, variance and covariance without the cancellation

West's 1979 form of Welford's 1962 recurrence — `d = x − μ; μ += d / n; M2 += d · (x − μ)` — keeps the running mean and the sum of squared deviations *about the current mean*, so the subtraction that destroys the naive formula never happens; variance is `M2 / n` (or `/(n−1)`) when read. Covariance of two series is the same recurrence with `C += d_x · (y − μ_y)`; a weighted version replaces `n` with the running weight; and two partial accumulators merge exactly (Chan–Golub–LeVeque), which is how a per-shard statistic becomes a fleet-wide one in Ch. 100 without re-reading the data. The cost is one division per update — the only division in the core — and it can be removed for a *fixed-count* window by precomputing `1/n` into a table, or kept because at ~15 cycles unpipelined it is a third of the budget and the replacement (a multiply by a table entry) costs a load; measure. For prices, scale to integers first (`x` in ticks), so the recurrence works on exactly representable inputs and only the mean carries rounding.

### 40.4.3 EWMA and EW variance with precomputed decay; irregular time without `exp`

The regular-time EWMA `μ += α (x − μ)` is one FMA with a constant; the exponentially weighted variance `v = (1−α)(v + α d²)` two more. Both are **one-pole IIR filters** and inherit their properties: they forget initialisation and rounding error geometrically (no drift — the opposite of a windowed sum), they need no memory beyond the two doubles, and their response to a step is a known curve whose time constant is `τ = −Δt / ln(1−α)`. With *irregular* arrivals the right weight is `w = exp(−Δt/τ)` per event — and `std::exp` is 20–40 ns and not bit-exact across libms. The core quantises `Δt` (to 1 ms here; to the tick-rate's granularity in general), looks `w` up in a 256-entry table built once at startup (`decay[q] = exp(−q·quantum/τ)`, saturating at the last entry), and never calls `exp` on the hot path: one load, deterministic by construction. A degree-3 polynomial on `[0, 1)` with a range reduction is the alternative when the table would be large; the table wins at 2 KiB. Two more rules: initialise the EWMA to the first observation, not to zero (a zero start takes `5τ` to forget — and spends it near the denormal range), and set **FTZ/DAZ** on every thread that runs this code (Ch. 35 §35.4.2; §40.3's 33×).

### 40.4.4 Rolling sums, rolling min/max, and the signals read straight off the book

- **Rolling sum / mean** over `W` samples: a ring of `W` (power of two, masked index — Ch. 37 §37.4.3) and a running sum updated as `S += x − ring[pos]` before the overwrite. In integers it is exact forever; in `double` it drifts (§40.3) and wants either Kahan/Neumaier compensation (one extra add and a `fabs` compare per update) or a **periodic exact recompute** — every `W` updates, re-sum the ring (W loads, vectorised, amortised to nothing) and replace `S`. The second is simpler and also deterministic, and §40.4.6 uses it.
- **Rolling min / max** over `W`: the **monotonic deque** (also "sliding-window minimum", Harter/Richter): keep candidates in a ring with their positions; on push, pop from the back every candidate ≥ the new value (for a min — they can never be the minimum again), push the new one, and pop from the *front* the candidate whose position has left the window. The front is the answer. Amortised O(1) — every element is pushed and popped once — in a fixed `W`-slot ring; the pop loop's length is data-dependent but tiny on price data (a new low pops everything; a typical tick pops zero or one). Two deques per instrument (min and max), `2 × W × 12` bytes.
- **Order-flow imbalance** (Cont, Kukanov & Stoikov 2014) reads the BBO line Ch. 33 §33.4.4 publishes: with the previous and current best bid price/size `(b, q_b)` and ask `(a, q_a)`, `e_bid = [b ≥ b'] q_b − [b ≤ b'] q_b'`, `e_ask = [a ≤ a'] q_a − [a ≥ a'] q_a'`, `OFI += e_bid − e_ask` — six integer compares and adds (the code above), no floating point, and a running sum that is exact. Its short-horizon variant is the OFI over the last `W` quotes, which is the rolling-sum ring again.
- **Microprice** `(b · q_a + a · q_b) / (q_a + q_b)` and **VWAP** `Σ px·qty / Σ qty`: integer numerators and denominators updated per event (two multiplies, two adds), the division deferred to the *read* — compared against a threshold, the comparison can even be done cross-multiplied without dividing.
- **Skipping work the update cannot have changed.** Depth-based signals ("quantity within five ticks") depend on the ladder beyond the touch; an event that touched level 12 cannot have changed them. The book (Ch. 33 §33.4.4) exposes the depth of the touched level; the core recomputes the depth signal only when `touched_depth ≤ signal_depth`, which on the §33.3 stream is a minority of events. The same gate applies to every signal with a cheap *invalidation* test.

### 40.4.5 Fixed-memory windows and the throttle's decayed counter

Every window above has a fixed, pre-sized footprint: `W` slots per ring, `W` per deque, two doubles per EWMA — and `W` is a compile-time constant (`static_assert(has_single_bit(W))`) so the mask folds. A window defined in *time* ("the last 100 ms") rather than in events is an EWMA with `τ` set to the horizon, or a ring indexed by a quantised time bucket (one slot per 10 ms, 10 slots, the sum of the slots is the window — a histogram ring); it is never a `std::deque` grown to whatever the burst brought, because the burst is exactly when allocation is fatal (Ch. 30). The same construction gives Ch. 106 §106.4.5's **gateway throttle** its state: a decayed counter `c = c · w(Δt) + 1` is "orders per second with exponential forgetting" in one double, compared against the venue's limit without a window at all — and when the venue's limit is a hard "n per rolling second" the time-bucket ring is the exact form.

### 40.4.6 Determinism in practice

The §40.3 hash experiment is the test to keep in CI (Ch. 117): replay a fixed capture through the core and compare the FNV hash of the state's bits against the committed value, on every compiler and flag change. What makes it pass:

- **`-ffp-contract=off`** on every TU that holds the core (GCC *and* Clang: GCC 13 contracts by default even under `-std=c++20` with `-march=native`, as §40.3's 16 FMAs show), or write every intended FMA as `std::fma(a, b, c)` — explicit, portable and the same bits everywhere `-mfma` is available. The former is a one-flag policy; the latter is the choice where the FMA's extra precision is wanted.
- **No `-ffast-math`, `-Ofast`, `-fassociative-math`, `-freciprocal-math`** on the core (Ch. 35 §35.4.3) — and FTZ/DAZ set *explicitly* at thread start, since fast-math was the only thing setting it in the §40.3 build.
- **Integers for everything that is a price or a size**, including their sums and products; `double` only for the ratios and decays, in the forms (Welford, EWMA) whose error does not accumulate.
- **Periodic exact recompute** of any windowed floating-point sum, at a fixed event count so the replay does it at the same event.
- **Tables, not libm**, for `exp`/`log`/`sqrt` on the hot path (§40.4.3); the table is data, built once, and part of the configuration's hash (Ch. 102).
- **The same layout, compiler, flags and `-march`** on the replay box as in production — Ch. 28's hermetic toolchain makes that a build-id check (Appendix N), and the hash test is what proves it mattered.

## 40.5 Verify the codegen: the EWMA chain and where the FMAs went

The EWMA update is a loop-carried dependency chain (Ch. 13 §13.4.1): `ewma[k]` is loaded, the new value depends on it, and the store feeds the next event *for that instrument*. Across instruments the chains are independent and the out-of-order core overlaps them; within one instrument's burst they serialise at the FMA's 4-cycle latency plus the load — which is why a per-instrument burst (one symbol hammered at the open) runs the core at its latency bound and a mixed stream at its throughput bound, and why §40.3's 19 ns is a throughput figure (exercise 3 measures the burst). GCC 13.3 `-O2 -march=native -S -masm=intel` on `on_event<SoA>`:

```
   vfmadd213sd  xmm2, xmm3, QWORD PTR [rax]      ; m2 += d * (x - mean)        <- contracted: ONE rounding, not two
   ...
   vfmadd231sd  xmm2, xmm0, xmm0                 ; ewvar: v + (1-w)*d*d        <- contracted
   vfnmadd132sd xmm1, xmm0, xmm1                 ; ewma: mu + (1-w)*delta      <- contracted
```

and under `-ffp-contract=off` the same lines are `vmulsd` + `vaddsd` pairs — two roundings, the C++ semantics the source actually wrote, and the same bits under every layout and compiler. The check to automate (Ch. 29 §29.4.4): `objdump -d core.o | grep -c vfmadd` is zero in a build that claims bit-exactness unless every FMA is a `std::fma` call. Two more things to see: `decay[q]` is one `vmovsd` from a table with the index clamped by `cmov` (no `call exp`); and the Welford division is a `vdivsd` with ~13-cycle latency on the chain — the one instruction to move to read time if the budget demands it.

## 40.6 Pitfalls & anti-patterns

- **`E[x²] − E[x]²`.** Catastrophic cancellation on any series whose mean is large relative to its spread — every price series. 3400× wrong in §40.3; Welford, always.
- **Reading the clock inside the core.** `rdtsc`/`clock_gettime` in `on_event` makes the decay, the throttle and the staleness check non-replayable (Ch. 103 §103.2.2); time is the event's timestamp.
- **An EWMA of a quiet input without FTZ/DAZ.** 33× per update (§40.3), on exactly the instrument that just stopped trading — and the slowdown lands on the hot thread, not the quiet instrument.
- **`float` for a running sum; `double` for a price.** The first drifts visibly in a morning (§40.3), the second cannot represent the price (Ch. 35). Integers for prices and sizes; `double` for ratios and decays.
- **Add-new/subtract-old forever.** The drift is slow in `double` but unbounded, and it is *different* on the replay box if the order of operations differs. Recompute exactly every `W` events.
- **`std::exp`/`std::log` on the hot path.** 20–40 ns, a `call`, and last-bit differences between libm versions; a table (§40.4.3).
- **Trusting "no fast-math" for bit-exactness.** Contraction is on by default (§40.3); `-ffp-contract=off` or explicit `std::fma`, and the hash test in CI.
- **Growing windows.** A `std::deque` or `std::vector` that grows with the burst allocates during the burst (Ch. 30). Every window is `W` slots, decided at compile time; a time-based window is an EWMA or a bucket ring (§40.4.5).
- **Recomputing depth signals on every event.** The update that touched level 12 did not change the five-level depth; gate the recompute on the touched depth (§40.4.4).

## 40.7 Exercises & checklist

**Exercises**

1. **Your capture.** Replay a day of Ch. 109's journal through `on_event` (feed handler → Ch. 33 book → this core), record the state hash at every 10⁶ events, and commit the hashes; rebuild with `-ffp-contract=off`, with Clang, with `-march=x86-64-v2`, and report which builds agree. Then make them all agree (§40.4.6).
2. **The layout regime.** Rerun §40.3 at 100 k instruments (the state no longer fits L2); report SoA vs AoS per event and `perf stat -e L1-dcache-load-misses,l2_rqsts.miss` per event. Where is the crossover on your machine?
3. **Burst latency.** Feed 10 000 consecutive trades for *one* instrument and time each `on_event` with `rdtsc`: the chain of §40.5 serialises. Compare with the mixed stream's per-event cost, and with the two divisions moved to read time.
4. **Compensated vs periodic.** Implement Neumaier summation for the rolling sum and the periodic exact recompute; measure both the error after 10⁹ updates and the per-event cost. Which would you ship, and why does the answer change if the sum is a P&L?
5. **The throttle.** Build Ch. 106 §106.4.5's decayed-counter throttle and the 10-bucket ring form (§40.4.5); drive both with a bursty order stream and compare what each lets through against the venue's "n per rolling second" rule. Which one is exact, and what does the other buy?
6. **Table vs polynomial `exp`.** Replace the 256-entry decay table with a degree-3 polynomial on a reduced argument; measure per-event cost, maximum error over the `Δt` range, and whether the state hash is stable across `-O2`/`-O3`.

**Checklist — the strategy core**

- [ ] The core is `on_event(state, params, event) → actions`: no clock, no allocation, no I/O, time as a field (§40.2.1, Ch. 103).
- [ ] Every statistic is **incremental** (Welford, EWMA, ring sums, monotonic deques, OFI/VWAP as integers) and the per-event cost is flat in the window length (§40.3, §40.4.2–§40.4.4).
- [ ] Prices and sizes are **integers** end to end; `double` only for ratios and decays, in drift-free forms; any floating-point windowed sum is **recomputed exactly** every `W` events (§40.4.6).
- [ ] **FTZ/DAZ** is set on every thread running the core; EWMAs are initialised to the first sample (§40.4.3).
- [ ] No `exp`/`log` on the hot path: decays come from a **table** built at startup (§40.4.3).
- [ ] The core's TUs are built with **`-ffp-contract=off`** (or explicit `std::fma`), without fast-math, and the **state-hash replay test** is in CI on every toolchain change (§40.4.6, §40.5).
- [ ] Windows are **fixed-size**, power-of-two, decided at compile time; depth signals are recomputed only when the update reached their depth (§40.4.4–§40.4.5).

## 40.8 References

- B. P. Welford, *"Note on a Method for Calculating Corrected Sums of Squares and Products"* (Technometrics 4(3), 1962) and D. H. D. West, *"Updating Mean and Variance Estimates: An Improved Method"* (CACM 22(9), 1979) — the recurrences of §40.4.2; T. F. Chan, G. H. Golub & R. J. LeVeque (1983) for the exact merge of partial accumulators.
- R. Cont, A. Kukanov & S. Stoikov, *"The Price Impact of Order Book Events"* (J. Financial Econometrics 12(1), 2014) — order-flow imbalance as §40.4.4 computes it.
- W. Kahan, *"Further remarks on reducing truncation errors"* (CACM 8(1), 1965) and A. Neumaier, *"Rundungsfehleranalyse einiger Verfahren zur Summation endlicher Summen"* (ZAMM 54, 1974) — compensated summation (exercise 4).
- D. Goldberg, *"What Every Computer Scientist Should Know About Floating-Point Arithmetic"* (ACM Computing Surveys 23(1), 1991) — cancellation, contraction and why §40.3's hashes differ.
- GCC and Clang documentation for `-ffp-contract` and the FMA contraction defaults — the flag §40.4.6 pins.
- Ch. 35 §35.2.3/§35.4.2 (denormals, FTZ/DAZ) and §35.4.3 (cross-build determinism); Ch. 103 §103.2.2 (the pure core); Ch. 33 §33.4.4 (the BBO line the signals read).

## 40.9 Additional Reading

- M. Stoikov, *"The micro-price: a high-frequency estimator of future prices"* (Quantitative Finance, 2018) — what the microprice of §40.4.4 estimates.
- N. J. Higham, *Accuracy and Stability of Numerical Algorithms* (SIAM) — the error analysis behind "which form accumulates and which forgets".
- The `simdjson`/`fast_float` authors' and D. Lemire's blog on branch-free numeric code — the style of §40.4's loops.

---

*Next: Ch. 41 — The C++ Memory Model & Atomics, opening Part VI (Concurrency). The core above runs on one thread; everything it publishes — the BBO line it reads, the actions it returns, the state a replica must see in the same order — crosses a thread or process boundary, and the rules for that crossing are the memory model: `std::atomic`, `memory_order`, acquire/release, fences, and the reorderings the compiler and the hardware are allowed.*
