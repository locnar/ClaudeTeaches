# Part I — Foundations & Methodology

# Chapter 3 — Micro-benchmarking Done Right

> **Prerequisites:** Ch. 1 (*The Latency Mindset*) — you reason about the distribution and care about p99/p99.9, not the mean. Ch. 2 (*Measure First*) — you can read PMU counters and know whether a workload is memory- or compute-bound. For the hands-on parts: a Linux box, a C++20 compiler, [Google Benchmark](https://github.com/google/benchmark) and [HdrHistogram_c](https://github.com/HdrHistogram/HdrHistogram_c) (or the C++ port).
>
> **Leads into:** Ch. 4 (*Reading the Machine*) — when a benchmark result surprises you, the asm tells you why. The statistical rigor and tail-capture here underpin every "before/after" measurement in Parts II–IX and the latency-regression gates of Ch. 76.

---

## 3.1 Why it matters: bad benchmarks lie confidently

A microbenchmark is a small program that times one isolated operation — a hash lookup, a queue push, a parse — so you can compare implementations. It is the workhorse of low-latency development: the tool you use hundreds of times to answer "did that change actually help?" And it is, by a wide margin, **the easiest measurement to get catastrophically wrong while feeling completely confident.**

The danger is specific. A microbenchmark runs your snippet in a tight loop, in isolation, with everything warm and predictable — which is *exactly the environment your hot path is not in*. So a microbenchmark can mislead in two opposite directions at once:

- **It can flatter code that is actually slow.** Run a snippet ten million times in a loop and the branch predictor learns every branch, the caches hold the entire working set, the prefetcher nails every access, and the CPU clocks up to full turbo. Your benchmark reports 3 ns. In production — cold caches, an unpredictable branch on real data, a working set that doesn't fit — the same code costs 60 ns. The benchmark measured the *best case* and you shipped the *worst case*.
- **It can punish code that is actually fast**, or report pure noise as signal, because the optimizer deleted the work you meant to measure (§3.4.1), or because frequency scaling made one run faster than another for reasons that have nothing to do with your code (§3.4.2).

And the worst failure of all is the one Ch. 1 warned about: a benchmark that reports a **mean** and hides the **tail**. A queue that is 20 ns on average but stalls to 5 µs every thousandth push will show a beautiful average and lose you races in production. A naïve benchmark loop doesn't just *fail* to show that tail — through **coordinated omission** (§3.2.2) it can actively *erase* it, turning a system with a vicious tail into a benchmark that looks flat.

The thesis of this chapter: a microbenchmark is a controlled experiment, and like any experiment it is worthless without controls. You must (1) make sure you're measuring the work and not the optimizer's removal of it, (2) control the environment so the *only* variable is your code, (3) capture the **whole distribution**, not a mean, without coordinated omission, and (4) apply enough statistical rigor to know whether a difference is real. Skip any one and your benchmark will lie to you — confidently.

---

## 3.2 Mental model

### 3.2.1 What a microbenchmark actually measures

A microbenchmark does not measure "how fast this code is." It measures **how fast this code is, in this loop, on this machine, in this microarchitectural state.** That last clause is where benchmarks go to die. The state includes:

- **Cache contents.** A loop touching the same small buffer runs entirely from L1; the same code in production may miss to L3 or DRAM on every call (Ch. 7). A benchmark over a 64-element array and the same code over a 64-million-element order book are different measurements of the same function.
- **Branch-predictor training.** Feed a branch the same outcome a million times and it predicts perfectly (near-zero cost); feed it random real-world data and it mispredicts (~15–20 cycles, Ch. 13). Benchmarks with fixed or trivially patterned inputs measure a branch predictor that has memorized the test.
- **CPU frequency.** Turbo, C-states, and thermal throttling (Ch. 6) mean the core's clock depends on what ran before and how hot the chip is. The same instructions take a different number of *nanoseconds* at 2.0 GHz vs 3.5 GHz.
- **Data layout and alignment** of the specific objects the benchmark allocated, which may differ from production (Ch. 9).

The discipline that follows: **make the benchmark's microarchitectural state resemble production, and hold it constant across the variants you compare.** If you're comparing container A vs B, both must see the same input distribution, the same working-set size, the same warm-up, the same pinned core and frequency. The benchmark is only valid as a *relative* comparison under identical conditions — and even then, treat the absolute nanoseconds as the *best case* unless you've deliberately reproduced production's cold, unpredictable reality.

This is also why Ch. 2's PMU is the benchmark's essential companion: when A beats B, the counters tell you *why* (fewer cache misses? fewer mispredicts?), and a "why" you can explain is a result you can trust to generalize. A speedup you can't explain is often the optimizer or the environment, not your code.

### 3.2.2 Coordinated omission and why it hides the tail

This is the most important — and most counterintuitive — idea in the chapter, named and popularized by Gil Tene. It is a measurement bug that **systematically deletes the worst of the tail**, and it hides in the most natural-looking benchmark loop you can write.

Consider a benchmark that wants to measure an operation under a fixed *request rate* — say one operation every 1 µs, because that's the load your feed handler sees. The obvious loop:

```cpp
for (int i = 0; i < N; ++i) {
    auto t0 = clock::now();
    do_operation();                  // intended: once per microsecond
    auto t1 = clock::now();
    record(t1 - t0);
    sleep_until(next_tick(i));       // pace to 1 op / us
}
```

Now suppose one call to `do_operation()` stalls for 100 µs (a page fault, a GC-like hiccup, a lock). Here is the trap: during that 100 µs stall, **100 operations *should* have been issued at 1 µs intervals — but the loop was stuck inside the one slow call, so it never issued them.** When the stall ends, the loop records *one* 100 µs sample and moves on. The 99 requests that *would have piled up behind the stall* — each experiencing progressively less but still large delay — are simply never measured. They were *coordinated* away by the measurement loop's own stall.

The result: a system that, under real constant load, would show a fat tail (hundreds of requests delayed by the stall) instead reports a single outlier among a sea of fast samples. Your p99.9 looks fine. Production is on fire. The benchmark and reality disagree precisely about the thing Ch. 1 said is the entire product — the tail.

```
What actually happens under constant arrival rate (a 100us stall):
  requests arriving:  | | | | | |X| | | | | | | | | | | | | | |   X = the stall
  latency they feel:  . . . . . /‾‾‾‾‾‾\. . . . .   <- a WHOLE BLOCK is slow
                                 ^ 100 requests queue behind the stall

What the naive loop records:
  one big sample (the stall) + many fast samples  <- the queue is INVISIBLE
```

There are two correct ways to defeat it:

1. **Measure against an intended schedule, not the previous operation.** Record latency as `now - intended_start_time(i)`, where `intended_start_time(i)` is when request `i` *should* have begun under the target rate. A request that couldn't start until after the stall correctly records its full delay. This is what a *coordinated-omission-aware* load generator (e.g. `wrk2`, or YCSB's intended-time mode) does.
2. **Don't impose a rate at all — measure pure per-op service time back-to-back** (no `sleep_until`), and be explicit that you are measuring *service time*, not *response time under load*. This is valid and useful (it's what Google Benchmark does by default), but it answers a different question and will not reveal queueing tails.

The rule: **decide whether you are measuring service time (back-to-back) or response time under a target load (scheduled), and if the latter, correct for coordinated omission.** Most hot-path queue/parser benchmarks want service time; most end-to-end "can we keep up with the feed at open" benchmarks want scheduled, CO-corrected response time. Confusing them is how a system passes its benchmark and fails at 09:30.

---

## 3.3 Measure it: Google Benchmark harness setup

[Google Benchmark](https://github.com/google/benchmark) is the de-facto C++ microbenchmark library. It handles iteration count selection (running until the timing is stable), reports mean/median/stddev, and — critically — provides the primitives to defeat the optimizer (§3.4.1). Here is a complete, real harness comparing two ways to look up a value, the running example continued from Ch. 2's memory theme:

```cpp
// bench_lookup.cpp — compare std::unordered_map vs a sorted flat vector + binary search.
// Build (with Google Benchmark installed):
//   g++ -O2 -std=c++20 -march=native bench_lookup.cpp -lbenchmark -lpthread -o bench_lookup
// Run, pinned and with stable frequency (see §3.4.2):
//   taskset -c 2 ./bench_lookup --benchmark_repetitions=20 --benchmark_report_aggregates_only=true
#include <benchmark/benchmark.h>
#include <algorithm>
#include <cstdint>
#include <random>
#include <unordered_map>
#include <vector>

constexpr int N = 4096;  // working-set size matters — pick it to resemble production.

static std::vector<std::uint32_t> make_keys() {
    std::vector<std::uint32_t> keys(N);
    std::mt19937 rng(7);
    for (auto& k : keys) k = rng();
    return keys;
}

static void BM_UnorderedMap(benchmark::State& state) {
    auto keys = make_keys();
    std::unordered_map<std::uint32_t, std::uint32_t> m;
    for (std::uint32_t i = 0; i < N; ++i) m[keys[i]] = i;

    std::mt19937 rng(99);
    std::uniform_int_distribution<int> pick(0, N - 1);
    for (auto _ : state) {
        auto k = keys[pick(rng)];               // unpredictable key each iter
        auto it = m.find(k);
        benchmark::DoNotOptimize(it->second);   // <- or the lookup is dead code (§3.4.1)
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_UnorderedMap);

static void BM_FlatBinarySearch(benchmark::State& state) {
    auto keys = make_keys();
    auto sorted = keys;
    std::sort(sorted.begin(), sorted.end());

    std::mt19937 rng(99);
    std::uniform_int_distribution<int> pick(0, N - 1);
    for (auto _ : state) {
        auto k = keys[pick(rng)];
        bool found = std::binary_search(sorted.begin(), sorted.end(), k);
        benchmark::DoNotOptimize(found);
    }
    state.SetItemsProcessed(state.iterations());
}
BENCHMARK(BM_FlatBinarySearch);

BENCHMARK_MAIN();
```

Representative aggregated output — reference machine **Intel Xeon Gold 6326** (Ice Lake-SP), Linux 6.x, GCC 13 `-O2 -march=native`, `taskset`-pinned, governor `performance` (illustrative numbers; reproduce on your box):

```
Benchmark                          Time     CPU   Iterations
--------------------------------------------------------------
BM_UnorderedMap_mean            18.7 ns  18.7 ns
BM_UnorderedMap_median          18.5 ns  18.5 ns
BM_UnorderedMap_stddev           0.9 ns
BM_FlatBinarySearch_mean        31.2 ns  31.2 ns
BM_FlatBinarySearch_median      31.0 ns  31.0 ns
BM_FlatBinarySearch_stddev       0.6 ns
```

Don't stop at "the hash map won." Ask Ch. 2's question — *why?* — because the answer decides whether the result generalizes. Binary search does ~12 dependent, branchy, cache-unfriendly comparisons (`log2(4096)`); the hash map does one hash and (usually) one probe. Confirm with `perf stat` on each binary: you'll see the binary search paying in branch-misses and L1 misses. And note the caveat that makes this a *teaching* result, not a verdict: at `N = 4096` with random keys the flat array doesn't fit in L1, so this is not the cache-friendly regime where flat structures shine — change `N`, change the key distribution (sequential? clustered like price levels?), and the winner can flip. **That sensitivity is the lesson**: the benchmark is only an answer for the conditions you gave it. The order-book case study (Ch. 25) revisits exactly this comparison under realistic access patterns and reaches a more nuanced conclusion.

---

## 3.4 Techniques

### 3.4.1 Defeating dead-code elimination (`DoNotOptimize` / `ClobberMemory`)

The optimizer's job is to delete work whose result is unused — and in a benchmark, the result *is* unused, so a `-O2` compiler will happily delete the very thing you're timing, leaving you measuring an empty loop. This is the single most common way a microbenchmark produces a nonsense number (often "0.0 ns" or a suspiciously round, tiny value).

Google Benchmark gives you two primitives:

- **`benchmark::DoNotOptimize(value)`** — tells the compiler the value is observed (forced into a register/memory and treated as escaping), so the computation that produced it can't be elided. Apply it to the *result* of the work you're measuring.
- **`benchmark::ClobberMemory()`** — a compiler barrier asserting that all memory may have been read/written, preventing the optimizer from sinking, hoisting, or merging stores across it. Use it after a loop that writes to a buffer whose writes would otherwise be dead.

```cpp
// WRONG: result unused -> the whole computation is deleted; you time nothing.
for (auto _ : state) {
    std::uint64_t h = hash(key);             // optimized away entirely
}

// RIGHT: the result escapes, so the hash must actually be computed.
for (auto _ : state) {
    std::uint64_t h = hash(key);
    benchmark::DoNotOptimize(h);
}

// RIGHT: when the "work" is stores into a buffer.
for (auto _ : state) {
    fill_buffer(buf);                        // writes buf[...]
    benchmark::ClobberMemory();              // stores can't be elided/merged away
}
```

The trap on the *other* side: over-applying these barriers can *pessimize* the code relative to production — forcing a value to memory that would normally stay in a register inflates the measurement. The skill is to escape exactly the production-observable result and nothing more. When in doubt, **read the asm** (Ch. 4): a correct benchmark's inner loop contains the instructions you meant to measure and not an empty decrement-and-jump. This is non-negotiable verification, not optional polish.

### 3.4.2 Warm-up, frequency scaling, and pinning

The environment is a variable you must nail down, or run-to-run noise will swamp the effect you're chasing. The standard pre-flight for any latency benchmark:

- **Pin the thread to an isolated core.** `taskset -c <cpu>` (or `sched_setaffinity`, Ch. 42) stops the scheduler from migrating your benchmark across cores mid-run, which trashes caches and confuses frequency. Ideally that core is `isolcpus`/`nohz_full` (Ch. 45) so nothing else runs on it.
- **Fix the CPU frequency.** Set the governor to `performance` and, for the tightest results, disable turbo (`/sys/devices/system/cpu/intel_pstate/no_turbo`) so the clock is *constant* rather than ramping with load and temperature (Ch. 6). A benchmark whose result depends on whether turbo kicked in is measuring the thermal state, not your code.
- **Warm up.** Discard the first iterations: the first calls pay cold I-cache/D-cache, cold branch predictor, cold TLB, and first-touch page faults (Ch. 23, 46). Google Benchmark's auto-iteration absorbs much of this, but for hand-rolled latency loops, explicitly run a warm-up phase before you start recording. **But** — and this ties back to §3.2.1 — warming up measures the *warm* path; if production runs cold (the first message after a quiet period), you must *also* measure the cold case deliberately. Both numbers are real; they answer different questions.
- **Quiesce the machine.** Close other load, disable `irqbalance` and steer IRQs off your core (Ch. 42), and prefer a tuned box (Appendix C). Noisy neighbors are a tail source.

A frequency-independent cross-check: measure in **cycles** (via `rdtscp` or PMU `cycles`, Ch. 17) as well as nanoseconds. If two runs disagree in ns but agree in cycles, the difference was frequency, not your code.

### 3.4.3 Tail-latency capture with HdrHistogram

Google Benchmark reports mean/median/stddev — useful for service-time *throughput* comparisons, but Ch. 1 told you that is not the number that matters. To measure the **tail**, you record every sample into a structure built for percentiles: **HdrHistogram** (High Dynamic Range Histogram, Gil Tene). It stores values in logarithmically-sized buckets giving constant *relative* precision across a huge range (nanoseconds to seconds), with O(1), allocation-free `record()` — so you can record on or near the hot path without perturbing it, and read out any percentile afterward.

```cpp
// latency_hist.cpp — per-op service-time distribution with HdrHistogram.
// Build: g++ -O2 -std=c++20 latency_hist.cpp -lhdr_histogram -o latency_hist
#include <hdr/hdr_histogram.h>
#include <chrono>
#include <cstdio>

extern std::uint32_t operation_under_test(std::uint32_t);  // your hot op

int main() {
    hdr_histogram* h = nullptr;
    // record 1 .. 10,000,000 ns with 3 significant figures of precision.
    hdr_init(1, 10'000'000, 3, &h);

    constexpr int WARMUP = 100'000, MEASURE = 5'000'000;
    std::uint32_t x = 1;
    for (int i = 0; i < WARMUP; ++i) x = operation_under_test(x);  // warm, untimed

    using clk = std::chrono::steady_clock;  // see Ch. 16 on clock choice
    for (int i = 0; i < MEASURE; ++i) {
        auto t0 = clk::now();
        x = operation_under_test(x);               // dependent chain: not elided
        auto t1 = clk::now();
        hdr_record_value(h,
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    }
    std::printf("ignore=%u\n", x);  // keep x live so the chain can't be deleted

    std::printf("p50   = %lld ns\n", (long long)hdr_value_at_percentile(h, 50.0));
    std::printf("p99   = %lld ns\n", (long long)hdr_value_at_percentile(h, 99.0));
    std::printf("p99.9 = %lld ns\n", (long long)hdr_value_at_percentile(h, 99.9));
    std::printf("max   = %lld ns\n", (long long)hdr_max(h));
    hdr_close(h);
    return 0;
}
```

Two caveats that matter here. First, **clock overhead and resolution** (Ch. 17): if the operation is only a few nanoseconds, two `clock::now()` calls may cost more than the work — measure the clock's own overhead and subtract it, or time *batches* of N ops and divide (accepting that batching hides per-op tail, a §3.2.2-flavored trade-off). Second, this back-to-back loop measures **service time**, so it is CO-immune by construction but does *not* show queueing tails; to measure response-time-under-load you'd drive it from a scheduled, CO-corrected generator (§3.2.2). Pick the one that matches your question.

### 3.4.4 Statistical rigor: repetitions, variance, regression

One run is an anecdote. The tail and the noise both demand repetition and a little statistics:

- **Repeat and report the distribution of runs.** `--benchmark_repetitions=N` (Google Benchmark) runs the whole benchmark N times and reports mean/median/**stddev** across runs. A result with stddev comparable to the effect you're claiming is not a result. Report the median of medians and the spread, never a lone number.
- **Enough samples for the percentile you quote.** You cannot honestly state p99.9 from 1,000 samples — that's *one* event in the tail (Ch. 1). For p99.9 you want ≫ 10⁵ samples; for p99.99, ≫ 10⁶. State your sample count alongside the percentile.
- **Compare, don't just measure.** The question is almost always "is B faster than A?", which is a *difference of distributions*, not of means. Tools like Google Benchmark's `compare.py` apply a U-test (Mann–Whitney) to tell you whether the difference is statistically significant or noise. A 2% "improvement" inside 5% run-to-run noise is nothing.
- **Watch for multimodality.** A bimodal histogram (two humps) usually means two regimes — e.g. cache-hit vs cache-miss, or turbo vs non-turbo — and a single percentile summary hides it. Look at the histogram shape (Ch. 1's picture), not just the quantiles.
- **Gate regressions in CI.** Once a benchmark is trustworthy, store its baseline and fail the build when p99/p99.9 regresses beyond noise. This is how the hard-won wins of Parts II–IX don't silently rot; it's built out in Ch. 76.

---

## 3.5 Pitfalls & anti-patterns: measuring the optimizer, not the code

- **Measuring an empty loop.** Forgot `DoNotOptimize`/`ClobberMemory`; the compiler deleted the work; you're timing loop overhead. Tell-tale: implausibly small or exactly constant times, or times that don't change when you alter the workload. Always confirm by reading the inner-loop asm (Ch. 4).
- **Reporting the mean of a heavy-tailed thing.** The Ch. 1 sin, reborn in benchmark form. A mean (and worse, a mean ± stddev that assumes normality) is meaningless on a right-skewed latency distribution. Report percentiles from a histogram.
- **Coordinated omission.** A scheduled/rate-limited loop that stalls inside a slow op and never records the requests that queued behind it (§3.2.2). Erases exactly the tail you care about. Use intended-time accounting or measure pure service time and say which.
- **Unrepresentative microarchitectural state.** Tiny working set that fits in L1, fixed/patterned inputs the branch predictor memorizes, perfectly aligned freshly-mmap'd buffers — all flatter the code vs production (§3.2.1). Size the working set and randomize inputs to match reality, and treat clean-room numbers as best-case.
- **Frequency and migration noise.** No pinning, no fixed governor, turbo ramping — the result reflects the thermal/scheduler state, not the code. Pin, fix frequency, cross-check in cycles (§3.4.2).
- **The clock costs more than the work.** Timing single sub-10 ns ops with two `clock::now()` calls measures the clock. Subtract clock overhead or batch (Ch. 17).
- **One run, one number, no significance test.** Claiming a 3% win that's inside the noise. Repeat, report spread, run a significance test (§3.4.4).
- **Benchmark drift / wrong build.** Benchmarking a debug build, a different `-march`, or stale flags vs what you ship (Ch. 2, 22). The benchmark must use production flags.

---

## 3.6 Exercises & checklist

**Exercises**

1. **Catch the optimizer red-handed.** Write a Google Benchmark of a small arithmetic function *without* `DoNotOptimize`. Note the (nonsense) time. Add `DoNotOptimize` and compare. Then read the inner loop in `perf annotate` or Godbolt (Ch. 4) and confirm the first version's work was deleted.
2. **Make the working set bite.** Take `bench_lookup.cpp` and sweep `N` over {64, 1K, 64K, 1M}. Plot the per-op time for both containers vs `N`. At which size does the flat array's cache-friendliness stop helping? Explain using `perf stat` cache-miss counts (Ch. 2, 7).
3. **Build a coordinated-omission demo.** Write a scheduled loop (one op per fixed interval) where the op occasionally `sleep`s for 100× the interval. Record latency two ways: `now - prev` (naïve) and `now - intended_start(i)` (CO-corrected). Compare the p99.9 of each. By how much did the naïve version understate the tail?
4. **Tail vs mean.** Use the HdrHistogram harness on an operation that occasionally misses cache. Report mean, p50, p99, p99.9, max. Which summary would have hidden the cache-miss tail? Tie back to Ch. 1's §1.3.
5. **Is the win real?** Microbenchmark two implementations that differ by ~3%. Run 20 repetitions each and apply Google Benchmark's `compare.py`. Is the difference statistically significant, or inside the noise?

**Checklist — a trustworthy microbenchmark**

- [ ] The work I mean to measure **escapes** (`DoNotOptimize`/`ClobberMemory`); I read the inner-loop asm to confirm it wasn't deleted.
- [ ] The **working set, input distribution, and alignment** resemble production — or I've labeled the result as best-case.
- [ ] The thread is **pinned**, the **frequency is fixed** (governor/turbo), and the path is **warmed** (and I measured cold separately if production runs cold).
- [ ] I record the **whole distribution** (HdrHistogram), and report **percentiles** (p50/p99/p99.9/max), not a mean.
- [ ] I decided **service time vs response-time-under-load**, and if the latter, corrected for **coordinated omission**.
- [ ] I have **enough samples** for the percentile I quote (≫10⁵ for p99.9).
- [ ] I ran **repetitions**, report the **spread**, and used a **significance test** before claiming a win.
- [ ] I can **explain *why*** the winner won via PMU counters (Ch. 2) — the result generalizes only if I understand it.
- [ ] The benchmark uses **production build flags** and is wired into a **regression gate** (Ch. 76).

---

## 3.7 References

- G. Tene, *"How NOT to Measure Latency"* (Strange Loop and other venues) — the definitive treatment of coordinated omission and percentile-based measurement.
- G. Tene et al., *HdrHistogram* — project documentation and the C/C++/Java implementations behind §3.4.3.
- Google Benchmark — library documentation, `DoNotOptimize`/`ClobberMemory` semantics, and the `tools/compare.py` statistical comparison utility.
- A. Fog, *Optimizing Software in C++* and *Instruction Tables* — methodology for measuring instruction-level cost and the microarchitectural caveats of §3.2.1.
- C. E. Leiserson et al. and the broader "performance engineering" literature on experimental rigor; and the `wrk2` load generator (Tene) as a CO-aware reference.

## 3.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — a practical, free book covering benchmarking methodology, `perf`, and top-down analysis alongside Ch. 2.
- B. Gregg, *Systems Performance*, 2nd ed. — chapters on benchmarking pitfalls and the "active benchmarking" method (always profile *while* benchmarking).
- nanobench (Martin Leitner-Ankerl) — a lightweight single-header alternative to Google Benchmark with built-in PMU integration and median/MdAPE reporting.
- Appendix E (*Latency Numbers Every Trading Developer Should Know*) — the reference costs your benchmark results should be sanity-checked against.
- Ch. 17 (*Timekeeping: TSC, rdtsc & Clock Sources*) — measuring nanoseconds reliably, clock overhead, and why `steady_clock` may not be precise enough.

---

*Next: Ch. 4 — Reading the Machine: Assembly & Compiler Output, where, when a benchmark surprises you, we open the hood and read the x86-64 the optimizer actually produced.*
