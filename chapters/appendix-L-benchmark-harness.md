# Appendix L — Reproducible Benchmark & Measurement Harness

> **Consolidates:** the measurement methodology the whole book rests on — micro-benchmarking done right (Ch. 3), the profiling/tooling commands (Ch. 2 and Appendix I), TSC timing (Ch. 17), and CLAUDE.md's non-negotiable rule that *every performance claim is backed by a measurement*. Ch. 3 teaches the *method* (dead-code elimination, warm-up, frequency scaling, statistical rigor, coordinated omission); this appendix is the reusable **scaffold** — a copy-paste Google Benchmark + HdrHistogram skeleton, the pinning/warm-up boilerplate, and the checklist to run before trusting a number.

**How to use this:** L.1 gives the throughput/mean-latency micro-benchmark skeleton; L.2 the tail-latency (HdrHistogram) variant that avoids coordinated omission; L.3 the environment setup that makes runs repeatable; L.4 the pre-trust checklist. The golden rule from Ch. 3 governs all of it: **a benchmark measures what you built, on the machine you ran it on — reason about the distribution (p50/p99/p99.9), not the mean** (Ch. 1). Always record compiler, flags, CPU, and kernel alongside the numbers (CLAUDE.md authoring convention). Skeletons are GCC/Clang + Google Benchmark and the HdrHistogram C library, on Linux x86-64; the code compiles as shown with the flags given.

---

## L.1 A Google Benchmark skeleton (throughput & mean latency)

The minimal, correct micro-benchmark — defeating dead-code elimination (Ch. 3) is the part everyone gets wrong.

```cpp
   #include <benchmark/benchmark.h>
   #include <vector>
   #include "orderbook.hpp"   // the code under test (Ch. 24-25)

   // Setup (book construction) happens ONCE per benchmark instance, outside the
   // timed loop — this is the setup/steady-state split the book insists on (CLAUDE.md).
   static void BM_BookApplyAdd(benchmark::State& state) {
     OrderBook book;
     const auto msgs = make_representative_adds(state.range(0));  // realistic input
     std::size_t i = 0;

     for (auto _ : state) {                       // <-- only THIS loop is timed
       auto& m = msgs[i++ & (msgs.size() - 1)];   // pow2 mask, no modulo (Ch. 27)
       auto bbo = book.apply(m);                  // the operation under measurement
       benchmark::DoNotOptimize(bbo);             // force the result to be "used"
       benchmark::ClobberMemory();                // force pending writes to memory
     }
     state.SetItemsProcessed(state.iterations()); // report messages/sec
   }
   BENCHMARK(BM_BookApplyAdd)->Arg(1 << 12)->Arg(1 << 16);  // two book sizes

   BENCHMARK_MAIN();
```

Build and run:

```
   g++ -std=c++23 -O2 -march=native -DNDEBUG bench.cpp -lbenchmark -lpthread -o bench
   ./bench --benchmark_repetitions=20 --benchmark_report_aggregates_only=true \
           --benchmark_out=bench.json --benchmark_out_format=json
```

**The three things that make it correct** (Ch. 3): `DoNotOptimize`/`ClobberMemory` stop the optimizer from deleting work whose result is unused (the #1 micro-benchmark bug — a "0.1 ns" result means the loop was optimized away); the input is **representative** (real order flow, not a degenerate constant the branch predictor learns perfectly — Ch. 12); and construction is **outside** the `for (auto _ : state)` loop so you time the steady-state operation, not allocation/setup. `--benchmark_repetitions` + aggregates give you mean/median/stddev (Ch. 3) — never trust a single run. `-O2 -march=native -DNDEBUG` because a debug/`-O0` build measures nothing real (Ch. 3; flags per Appendix D).

## L.2 Tail-latency capture with HdrHistogram (and avoiding coordinated omission)

Google Benchmark reports the *loop's* mean; a trading system lives or dies on the **p99/p99.9 tail** (Ch. 1). For that, record **individual** sample latencies into a high-dynamic-range histogram — and correct for **coordinated omission** (Ch. 3), the trap that makes almost every naive latency benchmark optimistic.

```cpp
   #include <hdr/hdr_histogram.h>
   #include <cstdint>
   #include <time.h>

   static inline uint64_t now_ns() {              // CLOCK_MONOTONIC; or rdtscp (Ch. 17)
     timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
     return uint64_t(ts.tv_sec) * 1'000'000'000ull + ts.tv_nsec;
   }

   void measure(OrderBook& book, const std::vector<Msg>& msgs) {
     hdr_histogram* h = nullptr;
     hdr_init(/*min*/ 1, /*max_ns*/ 10'000'000, /*sig_digits*/ 3, &h);

     // Open-loop: events are SUPPOSED to arrive every `interval_ns` (the real feed
     // rate). If a slow op delays us, later events were "waiting" — record their FULL
     // wait, not just service time. Skipping this is coordinated omission (Ch. 3):
     // it hides exactly the stalls (a GC pause, a page fault, a lock) you care about.
     const uint64_t interval_ns = 200;            // e.g. one event per 200 ns
     uint64_t intended = now_ns();
     for (const auto& m : msgs) {
       uint64_t start = now_ns();
       benchmark::DoNotOptimize(book.apply(m));
       uint64_t end = now_ns();
       hdr_record_value(h, end - start);                       // service latency
       hdr_record_corrected_value(h, end - intended, interval_ns); // + omission fix
       intended += interval_ns;
     }
     // Full percentile spectrum — NOT just the mean:
     printf("p50   %10lld ns\n", hdr_value_at_percentile(h, 50.0));
     printf("p99   %10lld ns\n", hdr_value_at_percentile(h, 99.0));
     printf("p99.9 %10lld ns\n", hdr_value_at_percentile(h, 99.9));
     printf("max   %10lld ns\n", hdr_max(h));
     hdr_percentiles_print(h, stdout, 5, 1.0, CLASSIC);        // full distribution
   }
```

**Why this and not a `for`-loop mean** (Ch. 1, 3): the mean of a latency loop is dominated by the common case and *erases* the tail — but the tail is the product. HdrHistogram records every sample cheaply across a huge dynamic range, so you get p99/p99.9/max, not an average that lies. **Coordinated omission** (Gil Tene) is the subtle killer: a closed-loop benchmark that sends the next request only after the previous returns *never observes* the requests that would have queued behind a stall — so a system that freezes for 10 ms looks fine. `hdr_record_corrected_value` with the expected interval models the open-loop arrival the real feed imposes (Ch. 54, J.4) and puts the stall back in the tail where it belongs. Timestamp with `rdtscp` (Ch. 17) if `clock_gettime` overhead is significant relative to the operation.

## L.3 Making a run repeatable (environment & isolation)

A benchmark is only reproducible if the *machine state* is controlled — otherwise run-to-run variance (frequency scaling, migration, cold caches) swamps the effect you're measuring (Ch. 3, 6).

```
   # 1. Pin to an isolated core (Appendix C) and raise priority, off the SMT sibling:
   taskset -c 3 chrt -f 80 ./bench ...          # one core, RT prio, sibling left idle (Ch. 42-43)

   # 2. Freeze frequency so the clock doesn't ramp/throttle mid-run (Ch. 6):
   sudo cpupower frequency-set -g performance
   #   (and disable turbo for stability while measuring; re-enable for production)

   # 3. NUMA-local memory (Ch. 16), so allocations aren't cross-socket:
   numactl --cpunodebind=0 --membind=0 ./bench ...

   # 4. Capture the microarchitectural context alongside the timing (Appendix I.2):
   perf stat -e cycles,instructions,cache-misses,branch-misses \
       taskset -c 3 ./bench ...

   # 5. Record the metadata block WITH the numbers (CLAUDE.md convention):
   #    CPU (lscpu), kernel (uname -r), compiler (g++ --version), flags, date.
```

**What each buys** (Ch. 3, 6): pinning stops the scheduler migrating the thread mid-measurement (a migration is a cold cache and a new tail); `performance` governor + no-turbo stops the clock changing under you (a benchmark that warms up into turbo then thermally throttles is unrepeatable); NUMA binding stops a stray remote-DRAM allocation adding a fixed penalty (Ch. 16); and `perf stat` alongside gives you *why* — an IPC or cache-miss change explains a latency change (Appendix I.2). Google Benchmark's own **warm-up** iterations (it discards early ones) plus an explicit cache/branch-predictor warm of your data structure (Ch. 46) put you in steady state before the first recorded sample. Always emit the **metadata block** — a number without its CPU/kernel/compiler/flags is not reproducible and, per the authoring conventions, not publishable.

## L.4 The pre-trust checklist

Before believing a benchmark number — the questions Ch. 3 says to ask, in order:

- [ ] **Is dead-code elimination defeated?** `DoNotOptimize`/`ClobberMemory` present; a suspiciously-fast result (sub-nanosecond, zero-cost) means the loop was optimized away (L.1, Ch. 3).
- [ ] **Was it warmed up?** Early iterations discarded; caches/TLB/branch predictor in steady state (Ch. 46). A cold first sample is a different measurement (Ch. 46 — warm vs. cold tail).
- [ ] **Is frequency pinned?** `performance` governor, turbo decision made and documented (L.3, Ch. 6) — otherwise variance is clock, not code.
- [ ] **Enough samples for the tail you claim?** p99.9 needs ≫1000 samples to be stable; don't quote a percentile the sample count can't support (Ch. 3).
- [ ] **Measured against a baseline?** Report *relative* to a control, not an absolute you can't reproduce elsewhere (Ch. 3) — "1.8× faster than the `std::map` version on this box" beats "42 ns."
- [ ] **Timing the hot path, not the setup?** Construction/allocation outside the timed loop (L.1); you're measuring steady state, not warm-up (CLAUDE.md).
- [ ] **No coordinated omission?** For latency (not throughput), open-loop / corrected recording so stalls land in the tail (L.2, Ch. 3).
- [ ] **Does the codegen match expectation?** Check the asm (Godbolt / `objdump` — Ch. 4, Appendix I.9): did it vectorize / inline / elide the branch you assumed? A benchmark of code the compiler transformed unexpectedly measures the wrong thing.
- [ ] **Is the effect above noise?** The difference exceeds run-to-run stddev across `--benchmark_repetitions` (Ch. 3) — a 2% "win" inside 5% noise is not a win.
- [ ] **Metadata recorded?** CPU, kernel, compiler, flags, date attached to the result (CLAUDE.md) — otherwise it isn't reproducible.

Passing this checklist is what turns "this is faster" into *evidence* — the standard every performance claim in this book is held to.

## L.5 References

- **Google Benchmark** documentation and User Guide — `benchmark::State`, `DoNotOptimize`/`ClobberMemory`, arguments/ranges, reporters and JSON output (L.1).
- **Gil Tene** on **coordinated omission** ("How NOT to Measure Latency" talk) and **HdrHistogram** (the `HdrHistogram` and `hdr_histogram_c` libraries) — the basis of L.2.
- Ch. 3 (*Micro-benchmarking Done Right*) — the method this scaffold implements; Ch. 17 (TSC/`rdtscp`) — the timestamp source for L.2; Ch. 46 (warming) — the warm-vs-cold discipline in L.3.
- The `perf`, `taskset`, `chrt`, `cpupower`, `numactl` man pages (Appendix I) — the environment controls of L.3.

## L.6 Additional Reading

- **Appendix I** (Tooling Command Cookbook) — the `perf stat`/pinning/`objdump` commands to run *alongside* a benchmark so the counters explain the timing.
- **Appendix C** (System Tuning Checklist) — the box-level setup (isolation, governor, C-states) that makes a benchmark host quiet enough to trust.
- **Appendix E** (Latency Numbers) — the reference costs to sanity-check a result against (if your "L1 hit" benchmark reports 40 ns, something is wrong).
- **Appendix D** (Compiler Flag Reference) — the build flags (`-O2`/`-march`/`-flto`) a representative benchmark must use, and the ones (`-O0`, sanitizers) it must not.

---

*This completes Appendix L and the book. The chapters (Parts I–XII) and Appendices A–L together form the full tutorial series — from the latency mindset (Ch. 1) through the end-to-end tick-to-trade case study (Ch. 76), with the ARM port, the language survey, the tuning checklist, the flag reference, the latency numbers, the glossary, the bibliography, the feature-support matrix, the tooling cookbook, the market-structure primer, the connectivity guide, and this benchmark harness as the standing references that outlive any single chapter.*
