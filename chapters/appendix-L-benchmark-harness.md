# Appendix L — Reproducible Benchmark & Measurement Harness

> **Consolidates:** the measurement methodology the whole book rests on — micro-benchmarking done right (Ch. 3), the profiling/tooling commands (Ch. 2 and Appendix I), TSC timing (Ch. 20), and the book's non-negotiable rule (Preface, Ch. 3) that *every performance claim is backed by a measurement*. Ch. 3 teaches the *method* (dead-code elimination, warm-up, frequency scaling, statistical rigor, coordinated omission); this appendix is the reusable **scaffold** — a copy-paste Google Benchmark + HdrHistogram skeleton, the pinning/warm-up boilerplate, and the checklist to run before trusting a number.

**How to use this:** L.1 gives the throughput/mean-latency micro-benchmark skeleton; L.2 the tail-latency (HdrHistogram) variant that avoids coordinated omission; L.3 the environment setup that makes runs repeatable; L.4 the pre-trust checklist. The golden rule from Ch. 3 governs all of it: **a benchmark measures what you built, on the machine you ran it on — reason about the distribution (p50/p99/p99.9), not the mean** (Ch. 1). Always record compiler, flags, CPU, and kernel alongside the numbers (the book-wide convention stated in the Preface). Skeletons are GCC/Clang + Google Benchmark and the HdrHistogram C library, on Linux x86-64; the code compiles as shown with the flags given.

---

## L.1 A Google Benchmark skeleton (throughput & mean latency)

The minimal, correct micro-benchmark — defeating dead-code elimination (Ch. 3) is the part everyone gets wrong.

```cpp
   #include <benchmark/benchmark.h>
   #include <bit>
   #include <vector>
   #include "orderbook.hpp"   // the code under test (Ch. 32 §32.4.5; its pools in Ch. 31)

   // Setup (book construction) happens ONCE per benchmark instance, outside the
   // timed loop — this is the setup/steady-state split Ch. 3 insists on.
   static void BM_BookApplyAdd(benchmark::State& state) {
     OrderBook book;
     // Realistic input AND a bounded book: a captured add/cancel (or add/execute) sequence
     // whose depth and order-ID set return to the start state each cycle, so replaying it for
     // millions of iterations keeps the book at steady state instead of growing it without
     // end (re-adding the same IDs, allocating as it grows, drifting per-op cost).
     const auto msgs = make_representative_flow(state.range(0));
     static_assert(std::has_single_bit(std::size_t{1} << 12)); // ranges below are powers of two
     std::size_t i = 0;

     for (auto _ : state) {                       // <-- only THIS loop is timed
       auto& m = msgs[i++ & (msgs.size() - 1)];   // pow2 mask, no modulo (Ch. 37)
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

**The three things that make it correct** (Ch. 3): `DoNotOptimize`/`ClobberMemory` stop the optimizer from deleting work whose result is unused (the #1 micro-benchmark bug — a "0.1 ns" result means the loop was optimized away); the input is **representative** (real order flow, not a degenerate constant the branch predictor learns perfectly — Ch. 15); and construction is **outside** the `for (auto _ : state)` loop so you time the steady-state operation, not allocation/setup. `--benchmark_repetitions` + aggregates give you mean/median/stddev (Ch. 3) — never trust a single run. `-O2 -march=native -DNDEBUG` because a debug/`-O0` build measures nothing real (Ch. 3; flags per Appendix D).

## L.2 Tail-latency capture with HdrHistogram (and avoiding coordinated omission)

Google Benchmark reports the *loop's* mean; a trading system lives or dies on the **p99/p99.9 tail** (Ch. 1). For that, record **individual** sample latencies into a high-dynamic-range histogram — and decide, *before* you write the loop, which of Ch. 3 §3.2.2's two questions you are asking: **service time** (what one `apply` costs, back-to-back — what the closed loop of Ch. 3 §3.4.3 measures) or **response time under the feed's arrival rate** (what an event that arrives on schedule waits in total, queueing included). The second question is where **coordinated omission** (Ch. 3) bites: a loop that issues the next event only when the previous one returns never observes the events that queued behind a stall. The harness below answers both questions in one pass by keeping **two histograms** and pacing the loop to the *intended* schedule, so every operation is recorded **exactly once in each**.

```cpp
   // tail.cpp — service time AND response time (CO-corrected by pacing): one pass, two histograms.
   // Build: g++ -std=c++23 -O2 -march=native -DNDEBUG tail.cpp -lhdr_histogram -lbenchmark -lpthread -o tail
   #include <benchmark/benchmark.h>   // DoNotOptimize only — this loop is hand-paced, not BENCHMARK()ed
   #include <hdr/hdr_histogram.h>
   #include <immintrin.h>             // _mm_pause
   #include <cstdint>
   #include <cstdio>
   #include <span>
   #include <time.h>
   #include "orderbook.hpp"           // OrderBook, Msg — the code under test, as in L.1

   static inline int64_t now_ns() {                 // CLOCK_MONOTONIC; or rdtscp (Ch. 20)
     timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
     return int64_t(ts.tv_sec) * 1'000'000'000 + ts.tv_nsec;
   }

   static void summarize(const char* name, const hdr_histogram* h) {
     std::printf("%-8s n=%-9lld p50 %7lld  p99 %8lld  p99.9 %9lld  max %10lld ns\n", name,
                 (long long)h->total_count,
                 (long long)hdr_value_at_percentile(h, 50.0),
                 (long long)hdr_value_at_percentile(h, 99.0),
                 (long long)hdr_value_at_percentile(h, 99.9),
                 (long long)hdr_max(h));
   }

   // `warm` is applied untimed first (Ch. 61). `msgs` is the timed capture, applied ONCE — a
   // bounded add/cancel sequence so book depth stays representative, not a cyclic replay of
   // adds that grows the book without end. `interval_ns` is the real feed's inter-arrival
   // time: the SCHEDULE the response-time question is asked against.
   void measure(OrderBook& book, std::span<const Msg> warm, std::span<const Msg> msgs,
                int64_t interval_ns) {
     hdr_histogram* service  = nullptr;   // end - start   : what the op itself costs
     hdr_histogram* response = nullptr;   // end - intended: what the event waited in total
     // Range 1 ns .. 60 s at 3 significant figures. The ceiling must exceed the WORST stall you
     // could see: hdr_record_value() returns false and records NOTHING above it, so a 10 ms
     // ceiling would silently drop the 10 ms freeze this section exists to catch. Memory grows
     // only with log2 of the range (tens of KB here).
     hdr_init(1, INT64_C(60'000'000'000), 3, &service);
     hdr_init(1, INT64_C(60'000'000'000), 3, &response);
     int64_t dropped = 0;

     for (const auto& m : warm) benchmark::DoNotOptimize(book.apply(m));   // untimed warm-up

     // Open loop paced to the INTENDED schedule: event i is due at t0 + i*interval_ns. Never
     // start early (spin until due). If the previous op stalled we arrive late, the spin falls
     // through at once, and end - intended carries the whole wait. The events that were due
     // during the stall are issued back-to-back afterwards — exactly as the queued packets would
     // be — each recording its own, progressively shorter, wait. Nothing is synthesized.
     int64_t intended = now_ns();
     for (const auto& m : msgs) {
       while (now_ns() < intended) _mm_pause();   // pace: spin, don't sleep (Ch. 42)
       const int64_t start = now_ns();
       benchmark::DoNotOptimize(book.apply(m));
       const int64_t end = now_ns();
       if (!hdr_record_value(service,  end - start))    ++dropped;   // once per op
       if (!hdr_record_value(response, end - intended)) ++dropped;   // once per op
       intended += interval_ns;                   // fixed schedule — never re-anchor to now
     }
     summarize("service",  service);
     summarize("response", response);
     std::printf("dropped (out of range) %lld\n", (long long)dropped);  // must be 0
     hdr_percentiles_print(response, stdout, 5, 1.0, CLASSIC);         // full distribution
     hdr_close(service);
     hdr_close(response);
   }
```

**Why two histograms, and why no `hdr_record_corrected_value` here.** The two numbers answer different questions and must never share a histogram. `service` is the closed-loop measurement of Ch. 3 §3.4.3 — CO-immune by construction, blind to queueing. `response` is method 1 of Ch. 3 §3.2.2 (what `wrk2` does): because `intended` advances by a fixed `interval_ns` whether or not the loop kept up, every event that was due during a stall is still issued — late — and records `end − intended`, its real wait, so the queue behind a stall lands in the tail by *measurement*, not by correction. Each op contributes exactly one sample to each histogram, so both `total_count`s equal the op count. HdrHistogram_c's `hdr_record_corrected_value(h, v, interval)` is a different tool for a different situation: it records `v` **and then back-fills** synthetic samples `v − interval, v − 2·interval, …` (while they remain above `interval`) to *stand in for* events a closed loop never issued. Use it only when all you have is a back-to-back loop and want a CO-*corrected estimate* of response time at an assumed rate — into its own histogram:

```cpp
   // Closed-loop variant (no pacing, no `intended`): ESTIMATE response time at `interval_ns`
   // from service times alone. Records end - start once and back-fills the events the loop
   // never issued. Separate histogram; its total_count will EXCEED the op count — those are
   // the synthetic samples, and that is the point.
   if (!hdr_record_corrected_value(estimate, end - start, interval_ns)) ++dropped;
```

Doing both — pacing *and* back-filling — counts every queued event twice (once as its own late sample, once synthesized from the stall that delayed it); and recording `end − start` *and* a corrected value into one histogram mixes service time, response time and synthetic samples into percentiles that mean nothing. (An earlier draft of this listing did exactly that: after one op the histogram held two samples. The pre-trust checklist in L.4 now asks you to check `total_count` for that reason.)

**Reading the result.** With no stall the two histograms nearly coincide: `response` ≈ `service` plus the pacing spin's overshoot, a few tens of nanoseconds of `clock_gettime` granularity. After a stall of *S*, `service` shows one sample of *S* at the max and a p99.9 that barely moves; `response` shows roughly *S* / `interval_ns` samples spread from *S* down toward zero, and its p99.9 jumps — the tail the real feed experiences. If `service` p50 exceeds `interval_ns`, the loop can never catch up and `response` grows without bound: the harness is telling you the handler cannot sustain that feed rate, which is the "can we keep up at 09:30" answer (Ch. 3), not a measurement error. Better than a constant interval is the capture's own arrival timestamps as the schedule (Ch. 105), which reproduces the feed's bursts. Timestamp with `rdtscp` (Ch. 20) if `clock_gettime` overhead or resolution is significant relative to the op or to the interval.

**Self-test the bookkeeping before trusting it.** Stand a stub in for `book.apply` that sleeps for 2 ms once every 100,000th call, run 1,000,000 events at `interval_ns = 1000`, and check the arithmetic, not the hardware: both `n` must equal 1000000 and `dropped` must be 0; `service` must show the stall only at `max` (≈ 2 ms) with p99 and p99.9 at the no-stall level; `response` must show it in the tail — ten stalls × (2 ms / 1 µs) = 20,000 late samples, 2% of all events, spread evenly from 2 ms down to zero, so p99 lands near 1 ms and p99.9 near 2 ms. Run against HdrHistogram_c on a 4-vCPU Intel Xeon VM (not the reference machine — only the bookkeeping is being checked), g++ 13.3 `-std=c++23 -O2 -march=native`, kernel 6.18, pinned with `taskset -c 2`:

```
   service  n=1000000   p50      21  p99       32  p99.9        42  max    2140159 ns
   response n=1000000   p50      60  p99  1162239  p99.9   1992703  max    2140159 ns
   dropped (out of range) 0
```

A `response` max *above* the `service` max is not a bug either: it is a stall that hit the pacing spin rather than the op (a VM preemption did exactly that in another run of this test, 3.56 ms vs 2.08 ms), and a feed arriving on schedule would have felt it. Reverse the two histograms in the `if` lines, or add a `hdr_record_corrected_value` call to the paced loop, and the self-test fails (`n` ≠ op count, or `service` and `response` percentiles converge) — which is what it is for.

## L.3 Making a run repeatable (environment & isolation)

A benchmark is only reproducible if the *machine state* is controlled — otherwise run-to-run variance (frequency scaling, migration, cold caches) swamps the effect you're measuring (Ch. 3, 6).

**Reference toolchain and machine.** Every representative number in this book is stated for one box and one toolchain unless its results block says otherwise, and every "Measure it" section either names these facts or cites this paragraph ("Appendix L.3"):

- **CPU:** Intel **Xeon Gold 6326** (Ice Lake-SP, Sunny Cove cores) — 16 cores / 32 threads, 2.90 GHz base, 3.50 GHz max turbo; 48 KiB L1d + 32 KiB L1i and 1.25 MiB L2 per core; 24 MiB L3 shared across the socket (16 × 1.5 MiB slices, 12-way); L1d load-to-use 5 cycles, L2 ~14 cycles, L3 ~45–70 cycles depending on slice distance, local DRAM ~80–100 ns; 8× DDR4-3200 per socket; PCIe 4.0. **Single socket** everywhere except the NUMA chapter (Ch. 18), which uses a **dual-socket 2× 6326** (32C/64T, two NUMA nodes).
- **SMT numbering:** Linux enumerates the single-socket box as CPUs 0–31 with the sibling of CPU *n* at *n*+16 (`0,16`, `2,18`); the dual-socket box puts it at *n*+32 (`2,34`). Always read `thread_siblings_list` rather than assuming `+N` (Ch. 56).
- **Frequency and idle policy:** `performance` governor, turbo **off** (`/sys/devices/system/cpu/intel_pstate/no_turbo = 1`), C-states capped at C1, so a pinned core runs at the 2.9 GHz base. The two experiments that need turbo say so in their block (Ch. 20's `cycles` vs `ref-cycles` run, Ch. 38's AVX-512 downclock table).
- **ns ↔ cycles:** at 2.9 GHz, **1 cycle = 0.345 ns and cycles ≈ ns × 2.9** — the conversion basis for every table in the book, Appendix E included.
- **Kernel:** **Linux 6.18 (LTS)**, `mitigations=auto` (KPTI on) unless a block says `mitigations=off`, THP `madvise`, `isolcpus`/`nohz_full`/`rcu_nocbs` per Appendix C wherever a chapter pins to an isolated core; `perf` is the kernel's own.
- **Compilers and library:** **GCC 14.2** (libstdc++ 14, glibc 2.39) and **Clang 18.1** (libc++ 18); `-std=c++20` by default and `-std=c++23` where a chapter uses a C++23 library feature (Appendix H); `-O2 -march=native` (`-march=icelake-server` on this box) unless the listing's `// Build:` line says otherwise. Results blocks that name GCC 13 / 13.3 record the release their listing was last rebuilt and checked with during the 2026-11 revision pass; the Phase-4 measurement pass re-runs everything under the versions above. The `§N.5 Verify the codegen` snippets are Clang 18.1 output unless labelled GCC.

The Preface carries the one-paragraph version of this statement; this is the one to cite.

```
   # 1. Pin to an isolated core (Appendix C) and raise priority, off the SMT sibling:
   taskset -c 3 chrt -f 80 ./bench ...          # one core, RT prio, sibling left idle (Ch. 55-56)

   # 2. Freeze frequency so the clock doesn't ramp/throttle mid-run (Ch. 6):
   sudo cpupower frequency-set -g performance
   #   (and disable turbo for stability while measuring; re-enable for production)

   # 3. NUMA-local memory (Ch. 18), so allocations aren't cross-socket:
   numactl --cpunodebind=0 --membind=0 ./bench ...

   # 4. Capture the microarchitectural context alongside the timing (Appendix I.2):
   perf stat -e cycles,instructions,cache-misses,branch-misses \
       taskset -c 3 ./bench ...

   # 5. Record the metadata block WITH the numbers (the book-wide convention — Preface, Ch. 3):
   #    CPU (lscpu), kernel (uname -r), compiler (g++ --version), flags, date.
```

**What each buys** (Ch. 3, 6): pinning stops the scheduler migrating the thread mid-measurement (a migration is a cold cache and a new tail); `performance` governor + no-turbo stops the clock changing under you (a benchmark that warms up into turbo then thermally throttles is unrepeatable); NUMA binding stops a stray remote-DRAM allocation adding a fixed penalty (Ch. 18); and `perf stat` alongside gives you *why* — an IPC or cache-miss change explains a latency change (Appendix I.2). an **explicit** Google Benchmark warm-up — `--benchmark_min_warmup_time=1` or `->MinWarmUpTime(1.0)` (v1.7+; the default is 0 s, so by default *nothing* is discarded beyond the short calibration runs that size the iteration count, which is not a controlled warm-up) — plus an explicit cache/branch-predictor warm of your data structure (Ch. 61) put you in steady state before the first recorded sample. Always emit the **metadata block** — a number without its CPU/kernel/compiler/flags is not reproducible and, per the authoring conventions, not publishable.

## L.4 The pre-trust checklist

Before believing a benchmark number — the questions Ch. 3 says to ask, in order:

- [ ] **Is dead-code elimination defeated?** `DoNotOptimize`/`ClobberMemory` present; a suspiciously-fast result (sub-nanosecond, zero-cost) means the loop was optimized away (L.1, Ch. 3).
- [ ] **Was it warmed up?** An explicit warm-up phase (`MinWarmUpTime` — Google Benchmark discards nothing by default), caches/TLB/branch predictor in steady state (Ch. 61). A cold first sample is a different measurement (Ch. 61 — warm vs. cold tail).
- [ ] **Is frequency pinned?** `performance` governor, turbo decision made and documented (L.3, Ch. 6) — otherwise variance is clock, not code.
- [ ] **Enough samples for the tail you claim?** p99.9 needs ≫1000 samples to be stable; don't quote a percentile the sample count can't support (Ch. 3).
- [ ] **Measured against a baseline?** Report *relative* to a control, not an absolute you can't reproduce elsewhere (Ch. 3) — "1.8× faster than the `std::map` version on this box" beats "42 ns."
- [ ] **Timing the hot path, not the setup?** Construction/allocation outside the timed loop (L.1); you're measuring steady state, not warm-up (Ch. 3).
- [ ] **Service time or response time — and no coordinated omission?** Decide which question you are asking (Ch. 3 §3.2.2) and keep the two in separate histograms. For response time, pace the loop to a fixed intended schedule and record `end − intended` so queued events land in the tail (L.2); use `hdr_record_corrected_value` *only* on a closed loop, into its own histogram — never both pacing and back-fill, never into the service-time histogram.
- [ ] **Does the histogram bookkeeping add up?** `total_count` equals the op count in the service histogram and in a paced response histogram (it exceeds it only in a closed-loop back-filled estimate, by design); `dropped` is 0, i.e. the histogram's range covered the worst stall — a sample above `highest_trackable_value` is silently not recorded (L.2).
- [ ] **Does the codegen match expectation?** Check the asm (Godbolt / `objdump` — Ch. 4, Appendix I.9): did it vectorize / inline / elide the branch you assumed? A benchmark of code the compiler transformed unexpectedly measures the wrong thing.
- [ ] **Is the effect above noise?** The difference exceeds run-to-run stddev across `--benchmark_repetitions` (Ch. 3) — a 2% "win" inside 5% noise is not a win.
- [ ] **Metadata recorded?** CPU, kernel, compiler, flags, date attached to the result (the book-wide rule — Preface, Ch. 3) — otherwise it isn't reproducible.

Passing this checklist is what turns "this is faster" into *evidence* — the standard every performance claim in this book is held to.

## L.5 Benchmarking across languages (the Ch. 94 protocol)

Everything above is C++; Ch. 94 §94.2.3 extends it to a cross-language comparison, and the extension is four rules on top of L.1–L.4:

1. **Same box, same core, same frequency state, nothing else running** — including the previous language's build daemon (`dotnet build-server shutdown`); the Appendix C quiet box, `taskset -c N`, governor and turbo fixed.
2. **Same replayed capture, same work, same digest** — every implementation reads the same capture (Ch. 105) and folds every result into a 64-bit digest it prints; unequal digests void the row (Ch. 94 §94.4.6).
3. **Warm and cold in separate windows** — pass 1 split at 1 k / 10 k / 100 k / 1 M messages, passes 2–3 whole; report the cold window and the steady state side by side and never compare one language's steady state with another's warm-up (a JIT's first hundred calls are 350× its steady state — Ch. 93 §93.3).
4. **Each toolchain's sink and its asm** — `benchmark::DoNotOptimize`/`asm volatile("" : "+r"(x))` (C++, Ch. 90 §90.2.3), `std::hint::black_box` (Rust), `std.mem.doNotOptimizeAway` (Zig), JMH `Blackhole.consume` (Java), BenchmarkDotNet `Consumer`/`GC.KeepAlive` (.NET), a package-level sink (Go), `Sys.opaque_identity` (OCaml); and the per-toolchain "verify the codegen" step of Appendix I.11, so a deleted, hoisted or differently-inlined loop is caught before the number is believed.

Report medians of *R* runs per row with the toolchain versions and flags in the header (Ch. 94 §94.3 is the worked example: nine toolchains, one kernel, one table), add the L.2 tail-latency variant for any row whose runtime can pause (Ch. 93), and state the box — numbers from a VM are not the reference machine's.

## L.6 Benchmarks as tests: the CI skeleton (Ch. 113)

The L.1–L.4 harness becomes a *gate* when its output is an artefact a pipeline compares against a stored baseline with the statistics of Ch. 113 §113.4.2. The skeleton, for a CMake/CTest project:

```
   # CMakeLists.txt — benchmarks are tests with a label; the per-PR job runs the label, the nightly runs everything on the tuned box
   add_executable(bench_book bench_book.cpp)  target_link_libraries(bench_book benchmark::benchmark)
   add_test(NAME bench_book COMMAND bench_book --benchmark_out=${CMAKE_BINARY_DIR}/bench/book.json --benchmark_out_format=json
                                              --benchmark_repetitions=20 --benchmark_report_aggregates_only=false --benchmark_min_time=0.5s)
   set_tests_properties(bench_book PROPERTIES LABELS "bench;perpr" RUN_SERIAL TRUE)      # RUN_SERIAL: never two benchmarks at once

   # per-PR job (shared runner class R):
   ctest -L perpr --output-on-failure                                                     # produces bench/*.json
   perf stat -e instructions:u,branches:u -r 10 -x, ./bench_book --benchmark_filter=Update --benchmark_min_time=0.1s 2> bench/book.counters   # deterministic proxy (Ch. 113 §113.4.3)
   python3 tools/compare.py benchmarks baseline/R/<last-green-rev>/book.json bench/book.json --no-color > bench/book.compare   # Mann–Whitney per benchmark (Ch. 3 §3.4.4)
   ./perfgate --aa baseline/R/aa.json --base baseline/R/<last-green-rev>/book.json --cand bench/book.json --stat p50,p99 \
              --min-effect-from-aa 1.5 --alpha 0.001 > bench/book.gate.json                                             # bootstrap CI vs A/A tolerance + rank test → PASS/FAIL (Ch. 113 §113.4.2)

   # artefact layout (keyed by revision AND runner class; a runner change starts a new series and a new A/A table):
   baseline/<runner-class>/aa.json                  the A/A table: per-statistic spread of R runs of one binary (Ch. 113 §113.4.1); re-measured weekly
   baseline/<runner-class>/<rev>/<bench>.json        Google Benchmark JSON (per-repetition samples, not just aggregates)
   baseline/<runner-class>/<rev>/<bench>.counters    perf stat -x, output
   baseline/<runner-class>/<rev>/<bench>.gate.json   the verdict line Ch. 113 §113.4.2 emits; the trend view reads these

   # nightly (tuned box, Appendix C; reserved for the gate):
   cyclictest -q -D 60 -p 90 -m -h 100 | tail -3                                           # quiet-box precondition (Appendix C.7, Appendix M.3)
   ctest -L bench                                                                           # all benchmarks, the L.2 tail variant included
   ./replay --speed 1 --pacer spin --capture captures/open-<id>.pcapng --target <box>   # the open onto the wire (Ch. 106 §106.4.3)
   ./perfgate --aa baseline/tuned/aa.json --base baseline/tuned/median-of-last-7/ --cand nightly/<rev>/ --stat p50,p99,p99.9 --per-stage   # per §115.3 stage
```

Two rules the skeleton encodes. **The sample, not the summary, is the artefact**: `--benchmark_report_aggregates_only=false` keeps every repetition so the bootstrap can resample them; aggregates alone cannot be gated honestly. **Tolerances come from `aa.json`, never from the YAML**: `--min-effect-from-aa 1.5` sets each statistic's minimum effect to 1.5× its A/A spread on that runner class, so the gate's promise ("catches +3 ns on the median, +6 ns on p99, nothing on p99.9 here") is a measured fact about the runner. The L.2 harness's two histograms feed the same gate for the tail variant, and the nightly's per-stage comparison is Ch. 115 §115.4.1's gate with Ch. 113 §113.4.4's operating procedure.

## L.7 The core-to-core map: placement input for every pinned pair (Ch. 19)

Every two-thread measurement in the book — the SPSC and MPMC queues (Ch. 44), the seqlock (Ch. 45), the Disruptor (Ch. 47), the shared-memory rings (Ch. 66), the futex and signal round-trips (Ch. 53) — is pinned to "two cores," and on chiplet or multi-tile silicon *which* two changes the answer by 4–5× (Ch. 19 §19.2.3). The harness below is Ch. 19 §19.3's `c2c.cpp` in the form the kit keeps it: run it **once per box and firmware mode** (NPS/SNC setting, SMT on/off, clock pinned per §L.3) before any pair measurement, store the matrix next to the metadata block, and quote the pair's one-way latency with every two-core result ("cores 4/5, same CCD, 19 ns one-way").

```
   # 1. Pin the clock and quiet the box exactly as for any benchmark (§L.3), then map every CPU pair:
   sudo chrt -f 50 ./c2c 200000 > c2c-$(hostname)-$(date +%F).txt          # ~1 s per pair; 32 CPUs ≈ 8 min
   # 2. Confirm the blocks against the kernel's L3 topology:
   for c in /sys/devices/system/cpu/cpu[0-9]*; do cat $c/cache/index3/shared_cpu_list; done | sort -u
   # 3. Pick the pair for the measurement FROM the map (same block, not SMT siblings), and record it:
   taskset -c 4,5 ./spsc_bench ...        # "cores 4/5: same CCD, 19.1 ns one-way (c2c 2026-11-10)"
   # 4. For a pre-trust check of a chiplet box: the map's block structure must match the recorded one;
   #    a ±15 % level shift is a clock/firmware change (Ch. 60), a changed block structure is a NPS/SNC change.
```

The protocol (Ch. 19 §19.3): two threads, one fresh cache line per pair, strict alternation (A writes 2*k*+1 and spins for 2*k*+2, B the reverse), 16 chunks of *N*/16 round trips, the **median** chunk reported as one-way = round-trip ÷ 2. A pair's number is only comparable to another box's at the same core clock, because the spin loop and the L2 lookup are core-clocked while the fabric is not — the §L.3 pinning applies. The ASCII heat-map the harness prints is relative to the run's own min/max; read the `min … max` header line before the picture, and note that on a VM the map is the *only* topology information you can trust (the virtual `lscpu` is whatever the hypervisor claims — Ch. 19 §19.3's guest run shows four vCPUs in one ~62 ns block).

What goes in the results block alongside §L.3's metadata: the two CPUs, their L3 domain (from step 2), the one-way latency from the map, and the map file's name. A two-core number without its pair is a number without a machine.

## L.8 References

- **Google Benchmark** documentation and User Guide — `benchmark::State`, `DoNotOptimize`/`ClobberMemory`, arguments/ranges, reporters and JSON output (L.1).
- **Gil Tene** on **coordinated omission** ("How NOT to Measure Latency" talk) and **HdrHistogram** (the `HdrHistogram` and `HdrHistogram_c` libraries) — the basis of L.2; `hdr_histogram.h` documents the record-and-back-fill semantics of `hdr_record_corrected_value` and the false-return-on-out-of-range behaviour of `hdr_record_value` that L.2 relies on.
- Ch. 3 (*Micro-benchmarking Done Right*) — the method this scaffold implements; Ch. 20 (TSC/`rdtscp`) — the timestamp source for L.2; Ch. 61 (warming) — the warm-vs-cold discipline in L.3.
- The `perf`, `taskset`, `chrt`, `cpupower`, `numactl` man pages (Appendix I) — the environment controls of L.3.

## L.9 Additional Reading

- **Appendix I** (Tooling Command Cookbook) — the `perf stat`/pinning/`objdump` commands to run *alongside* a benchmark so the counters explain the timing.
- **Appendix C** (System Tuning Checklist) — the box-level setup (isolation, governor, C-states) that makes a benchmark host quiet enough to trust.
- **Appendix E** (Latency Numbers) — the reference costs to sanity-check a result against (if your "L1 hit" benchmark reports 40 ns, something is wrong).
- **Appendix D** (Compiler Flag Reference) — the build flags (`-O2`/`-march`/`-flto`) a representative benchmark must use, and the ones (`-O0`, sanitizers) it must not.

---

*Next: Appendix M — Pre-Open Readiness Gate & Market-Open Playbook, the operational companion to Appendix C: the cold → warming → syncing → reconciled → armed → trading → disarmed state machine every trading process publishes, the T-60/T-30/T-5 gates (host attestation, clock, feed, network, session, positions, warm-up, kill-switch) as commands with expected values, re-arming after an intraday restart, the host fingerprint micro-suite built on this appendix's harness, and the gate runner as a script.*
