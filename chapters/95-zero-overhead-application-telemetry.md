# Part XIII — Observability & Operations in Production

# Chapter 95 — Zero-Overhead Application Telemetry

> **Prerequisites:** Ch. 93 (zero-overhead logging — the same capture-here/format-there discipline, applied to numbers instead of events), Ch. 94 (in-band tracing — the per-stage deltas this chapter's histograms record arrive as a finished trace header at the gateway), Ch. 1 and Ch. 3 (percentiles, HdrHistogram, coordinated omission — this chapter is their production sequel), Ch. 39–40 and Ch. 42 (relaxed atomics, per-thread state, false sharing — why a counter's *placement* is its cost), Ch. 44 (seqlocks — the multi-word gauge snapshot), Ch. 65 (shared-memory segments with a versioned header), Ch. 54 (housekeeping vs isolated cores — where the scraper lives), Ch. 20 (the TSC stamp on every sample).
>
> **Leads into:** Ch. 98 (process topology — heartbeats, readiness state and the control plane live in this segment), Ch. 108 and Ch. 109 (host and network health — the OS- and NIC-side signals that join the application's in the same pipeline), Ch. 113 (the case study — its "continuous monitoring" half is this chapter's segment and scraper), Appendix M (the readiness gate reads the same segment). The metrics sibling of Ch. 93: how a counter, a gauge and a latency histogram leave a pinned busy-poll process without a syscall, a format call or a cache-line fight — and how to turn them into a tail-latency SLO you can actually alert on.

---

## 95.1 Why it matters: the counter that has to leave a busy-poll process

Ch. 113 will assert, as every production chapter in this Part has assumed, that the system is observed *continuously* — per-stage latency histograms, drop and gap counters, queue depths, restarts — at a cost the hot path does not notice. Ch. 93 showed how an *event* leaves the hot path for a few nanoseconds. Nobody has yet shown how a *number* does. The naive answers are all wrong in instructive ways. A global `std::atomic<uint64_t> messages_processed` shared by eight feed-handler threads is a cache line bouncing between eight cores: ~100 ns per increment under contention (Ch. 42 §42.3, and §95.3 below) on a path with a 1 µs budget. A `stats` thread that calls `getrusage` or reads `/proc/self` on the hot thread's behalf is a syscall storm. A "metrics library" whose `histogram.observe(x)` takes a mutex, formats a label set into a `std::string` and allocates is Ch. 93's naive logger with a different name. And a Prometheus client that opens an HTTP listener *in the trading process* puts the kernel socket stack, a text formatter and a GC-less-but-allocating serializer one thread away from the isolated core — the exact things Ch. 52–59 spent six chapters removing.

The requirement is the same one Ch. 93 resolved for logging, and so is the resolution: **do the cheap part on the hot path, everything else off it.** The hot thread owns a few cache lines of plain memory in a shared segment; it increments a counter with a single store, records a latency sample with a dozen instructions, and publishes a multi-word gauge set under a seqlock (Ch. 44) — never a lock, never a syscall, never a line another core writes. A *scraper* on a housekeeping core maps that segment read-only, snapshots it at a fixed cadence, computes interval histograms by subtracting cumulative bucket counts, merges them across threads, hosts and venues by *adding counts*, and exports to the time-series store and the alerting system. The hot process never knows the scraper exists; if the scraper dies, the counters keep counting into memory and nothing on the trading path changes.

Two things make this chapter more than a performance exercise, and they are why the percentile mathematics sits in the mental model rather than in a footnote. First, the *segment* becomes the substrate for everything else in Part XIII that must be seen from outside: the heartbeats and progress sequences the supervisor watches (Ch. 98 §98.4.3), the readiness state the pre-open gate reads (Appendix M), the feed-age gauges Ch. 71's watchdog publishes, the health signals Ch. 108–109 collect from the OS and NIC. Second, the moment these numbers reach a dashboard, the book's north-star is in danger: **a desk that averages per-host p99.9s has reinstated the mean that Ch. 1 spent a chapter denouncing**, and nothing in a typical metrics stack warns them. §95.2 gives the counter taxonomy and the percentile math (with a worked example where the averaged p99.9 is off by 2× from the merged one, and could be off by any factor you like), §95.3 measures what each instrument costs on the hot thread and what the scraper costs it, §95.4 builds the segment, lists what a trading process must expose, writes the scraper and export, and closes with SLOs and alerts that fire on symptoms without paging storms; §95.5 proves in the asm that the instrumentation is a few stores; §95.6 and §95.7 are the pitfalls, exercises and checklist.

## 95.2 Mental model

### 95.2.1 Counter, gauge, high-water mark, histogram — and what each may cost

Four kinds of number cover everything a trading process needs to expose, and each has a cost model and a snapshot semantics the scraper must respect:

```
   kind              writer op (hot thread)             value semantics          snapshot semantics            examples
   ────────────────  ─────────────────────────────────  ───────────────────────  ────────────────────────────  ─────────────────────────────
   counter           load+add+store to an OWNED 8 B     monotonic, never reset   cumulative; scraper diffs      msgs, gaps, drops (per layer),
                     slot on the thread's own line      by the hot thread        for a rate; a drop in value =  orders, rejects, throttles,
                     (~1 ns; one `add [mem],1`)                                  restart (count it)             heartbeat sequence
   gauge             single 8 B store (or a seqlock-     last value wins          read-as-is; multi-word sets    queue depth, book age (ns),
                     published SET of words, Ch. 44)                             need the seqlock (torn pairs)  position, last seq processed,
                                                                                                                readiness state, feed age
   high-water mark   compare + conditional store         max since epoch          scraper RESETS by bumping an   queue depth HWM, worst stage
                     (predicted not-taken)                                        epoch the thread acknowledges  latency, deepest reorder window
   histogram         index (lzcnt + shift) + increment   bucket counts, each      cumulative per bucket; the     per-stage latency (decode, book,
                     of ONE bucket (~2–4 ns)              a monotonic counter      scraper diffs for an INTERVAL  strategy, send, wire-to-wire),
                                                                                  histogram, merges by ADDING    ack latency, inter-arrival time
```

Three rules fall out of the table. **Every writer op is a store to memory the thread owns** — no RMW on shared state, no `lock` prefix, no line another core writes (Ch. 42): the counter's *placement* is its cost. **Monotonic counters make the scraper stateless and the snapshot tear-free**: a cumulative count read mid-increment is simply the count a nanosecond earlier (one naturally aligned 64-bit store is atomic on x86-64 — Ch. 39), and a rate is a difference of two reads; the hot thread never resets anything. **Only multi-word *sets* that must be read consistently need a seqlock** (queue depth together with the sequence number it was measured at; a position with its timestamp), and the seqlock is Ch. 44's single-writer version: two relaxed stores around the words, an acquire-read-retry loop on the scraper side, and *the writer never waits for the reader*.

### 95.2.2 Percentiles are not additive: merge histograms, never average percentiles

A percentile is a property of a *set of samples*. The p99.9 of host A and the p99.9 of host B tell you nothing — not approximately, not within a factor — about the p99.9 of A ∪ B, because the merged p99.9 is the value exceeded by 0.1 % of *all* samples, and where those exceedances sit depends on the *shape* of each distribution's tail, not on one number from it. Means add (weighted by count); counts add; percentiles do not. Yet the default aggregation in every metrics stack — "average of `p99_9` across the `host` label", "p99.9 per 10 s window, averaged over the minute" — does exactly that, and the result is a number that *looks* like a tail and *behaves* like a mean.

The §95.4.3 scraper's self-test makes it concrete with two threads at equal message rates, 10⁶ samples each (GCC 13.3, the authoring host; the arithmetic is exact and machine-independent):

```
   thread A: a tight 1.1–1.3 µs decode path                               p99.9 =  1,407 ns
   thread B: the same path, but 2 % of messages take a 40 µs queueing hit   p99.9 = 45,055 ns

   AVERAGE of the two p99.9s      = 23,231 ns   <- what a dashboard averaging over the `thread` label shows
   p99.9 of the MERGED histogram  = 45,055 ns   <- what 0.1 % of ALL messages actually exceeded
   merged p99 = 40,959 ns   merged p50 = 1,279 ns   max = 41,999 ns   samples = 2,000,000
```

Follow the counts: thread B's 2 % stall is 20,000 samples above 40 µs; merged, that is 1 % of 2,000,000 — ten times the 0.1 % the p99.9 asks about, so the merged p99.9 is *entirely* inside B's stall and the merged **p99** is already 41 µs. The average says 23 µs and is wrong by 2×; give B a 0.3 % stall instead and the average says ~14 µs while the merged p99.9 is still 45 µs (3.2×); make B one of 20 threads and the average says ~3.5 µs against a merged p99.9 of ~1.4 µs (the stall is now 0.1 %/20 of all samples and sits *below* the 99.9th percentile — wrong in the other direction). There is no correction factor. The same applies across *windows*: the p99.9 of a minute is not the mean of sixty one-second p99.9s — it is dominated by the one second of the open burst, which also has ten times the samples. (The 12.5 % bucket granularity in the listing's histogram is why 40–42 µs reports as 45,055: a bucket's upper bound, Ch. 3 §3.4.3's precision argument; HdrHistogram at 3 significant figures tightens it to 0.1 %.)

The rule, then, is structural: **the unit of export, storage and aggregation is the histogram (bucket counts), which is additive; a percentile is computed from a merged histogram at the last possible moment, and is never stored, transported or aggregated as a number.** HdrHistogram calls the merge `hdr_add`; Prometheus's `histogram_quantile(0.999, sum by (le) (rate(bucket[1m])))` is the same idea; the §95.4.3 scraper does it by adding `uint64_t` arrays.

### 95.2.3 Interval vs cumulative, windows and minimum samples

The hot thread's buckets are **cumulative** — they only grow. The scraper turns them into **interval** histograms by subtracting consecutive snapshots (bucket by bucket), which is what "p99.9 over the last second" means: the distribution of the samples recorded *in that second*. Cumulative-since-start percentiles are a different, usually useless, quantity (the open's spike is diluted by six hours of quiet by mid-afternoon). Keep both: the scraper stores interval histograms at its cadence (they can be re-merged into any coarser window later — a minute, a session phase, a day — because they add), and reports cumulative only as a sanity count.

Then respect the sample count. A p99.9 is the 0.1 % worst; in a window with 10,000 samples that is *ten* samples, and the reported value is the 10th-worst of a noisy set. Ch. 3 §3.4.4 asked for ≫10⁵ samples before trusting a p99.9 in a benchmark; production is no different. A quiet instrument with 300 messages a second has no meaningful one-second p99.9 at all — report the *count* with every percentile, compute tails over windows long enough to hold ≥10⁵ samples (or merge across instruments/threads, which is now legal because you merged histograms), and let the alert rules (§95.4.4) require a minimum count before they evaluate. A dashboard that plots p99.9 of a 1 s window on a 100 msg/s feed is plotting its own noise.

### 95.2.4 Coordinated omission in production

Ch. 3 §3.2.2 described coordinated omission as a benchmark bug: when the system stalls, the load generator pauses with it and the stall is recorded once instead of for every request that should have arrived. In production the "load generator" is the exchange and it does not pause — but your *instrument* does. A per-stage histogram that records `t_done − t_start` for each message, where `t_start` is taken when the hot thread *picks the message up*, measures **service time**; during a 40 µs stall the fifty packets that accumulated in the NIC ring and the socket buffer are each recorded with a perfect 1.2 µs service time once the thread wakes, and the 40 µs appears in *one* sample, or none. The tail is deleted exactly as in the benchmark. Two fixes, both structural:

- **Timestamp arrival at the wire, not at pickup.** The NIC's hardware RX timestamp (Ch. 72, Ch. 75) or, failing that, the kernel's `SO_TIMESTAMPING` stamp is `t_arrival`; record `t_done − t_arrival` (**response time**, the number the market experiences) per stage boundary, with `t_start − t_arrival` as the queueing stage in its own histogram. The stall now lands on every message it delayed.
- **Count what you did not record.** The hot thread's `msgs` counter and the NIC's `rx_packets` (Ch. 109) must agree per window; a window in which fewer samples were recorded than packets arrived — or in which the hot thread's heartbeat counter advanced fewer times than its cadence — is a window of omission, and the scraper flags it rather than reporting a flattering percentile from the samples that did get recorded.

The model: **four kinds of number, each a store to owned memory; cumulative counts the scraper diffs and adds; percentiles computed last from merged histograms, never averaged; windows sized by sample count; arrival stamped at the wire so a stall is counted for every message it delayed.**

## 95.3 Measure it: what the instrument costs the hot thread

*Toolchain and machine: Appendix L.3.*

Three costs matter: the per-operation cost of each writer op on the hot thread, the cost of the *wrong* design (a shared counter), and what the scraper's reads do to the hot thread when they pull its lines into another core's cache. The §95.4.1 header plus a 50-line harness (`tele_bench.cpp`) measures the first two in place; representative figures on the reference box (GCC 13.3 `-O2 -march=icelake-server`, one hot thread pinned to an isolated core, 5 × 10⁷ ops per row; the authoring host, an untuned 4-vCPU VM at 2.7 GHz, gave 1.9 / 1.6 / 2.0 / 5.1 ns and 109 ns for the contended row with 4 threads):

```
   operation                                                  ns/op     what it is
   relaxed increment, own padded line                          ~1.0     load, add, store to an owned line: the store-forward latency
   histogram record (lzcnt index + bucket increment + max)     ~2.5     ~20 instructions, one predicted-not-taken branch (§95.5)
   seqlock gauge publish (4 words + 2 seq stores)              ~2.0     six plain stores; x86-TSO orders them (Ch. 39, 44)
   uncontended std::atomic fetch_add (lock add)                ~5.5     ~18 cycles for the locked RMW even with no sharer
   8 threads on ONE shared atomic counter                    ~120      the line bounces: a HITM per increment (Ch. 42, Appendix E)
   8 threads, own padded line each                            ~1.0     flat — the same as one thread (Ch. 40 §40.4's sharded counter)
```

The first lesson is the one this Part keeps relearning: **the instrument is free when it is a store to memory the thread owns, and a hundred times more expensive when it is shared.** A hot path recording one counter and one histogram sample per message pays ~3.5 ns — 0.3 % of a 1 µs budget — and the cost is *flat*: no lock, no allocation, no syscall, nothing that can stall. The contended row is the design most metrics libraries ship by default (one `std::atomic` per metric, shared by every thread that touches it), and at eight threads it costs more than the entire decode stage.

The third cost — **scraper interference** — is the one nobody measures. Each scrape reads every bucket of every thread's histograms; those lines are in the hot core's L1/L2 in Modified state, so the scraper's read pulls them to Shared (a snoop and a ~40–100 ns cache-to-cache transfer, paid by the *scraper*), and the hot thread's *next* write to each line then takes a request-for-ownership (RFO) miss instead of an L1 hit — ~40–100 ns, paid by the *hot thread* (Ch. 42, Appendix E's HITM row). The damage is one RFO per touched line per scrape. Representative measurement on the reference box: a hot thread recording into 5 histograms (5 × 288 × 8 B ≈ 11.5 KiB ≈ 180 lines) plus its counter line, with the scraper on a housekeeping core:

```
   scrape cadence     hot-thread p50     p99.9      p99.99     what happened
   none                1.21 µs           1.44 µs    1.61 µs    baseline
   1 Hz                1.21 µs           1.44 µs    1.63 µs    ~180 RFOs/s, lost in the noise
   10 Hz               1.21 µs           1.45 µs    1.72 µs    ~1,800 RFOs/s: visible only at p99.99
   1 kHz               1.22 µs           1.58 µs    2.9 µs     ~180k RFOs/s: every ~5th message pays a miss
   1 kHz, scraper on the SMT sibling     1.31 µs    2.1 µs     4.4 µs     plus front-end/L1 contention (Ch. 55)
```

Scrape at 1–10 Hz from a *different physical core*, and the hot thread cannot tell. Scrape at kHz rates, or from the sibling, and you have built a cache-line ping-pong with extra steps. The cadence that matters for alerting is seconds (§95.4.4); the cadence that matters for a stop-on-threshold trigger is handled by the *hot thread itself* writing a marker (Ch. 59 §59.4.2), not by a scraper polling faster.

## 95.4 Techniques

### 95.4.1 The shared-memory telemetry segment

The whole design is a fixed, versioned layout in a `shm_open`/`memfd` segment (Ch. 65 §65.4.2): a header, then one `ThreadStats` block per hot thread, each block a sequence of cache lines written by exactly one thread. The hot process creates and maps it read-write at start-up (Ch. 30: once, never on the path); scrapers, the supervisor (Ch. 98) and the readiness gate (Appendix M) map it **read-only**:

```cpp
// telemetry.hpp — the shared-memory telemetry segment: per-hot-thread stats lines (counters, gauges, high-water
// marks) and per-stage log-linear latency histograms, written by exactly one thread each with plain stores and
// read by an off-core scraper that never takes a lock the hot thread can see.  (Ch. 95 §95.4.1)
#pragma once
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <new>

namespace tele {

constexpr std::size_t kLine = 64;   // a WIRE FORMAT constant: not std::hardware_destructive_interference_size, which
                                    // may differ between the hot process's and the scraper's compilers (Ch. 42, Appendix A/H)

// ---- a log-linear histogram: 8 sub-buckets per power of two => <= 12.5 % relative error, 1 ns .. ~68 s in 288 buckets.
// Every bucket is a monotonic counter owned by ONE writer; the scraper diffs two reads to get an interval histogram.
struct Histogram {
    static constexpr unsigned kSubBits = 3, kSub = 1u << kSubBits, kOctaves = 36, kBuckets = kOctaves * kSub;
    std::uint64_t count[kBuckets];
    std::uint64_t total, max;                             // total = sum(count); max = largest value seen (a high-water mark)

    static constexpr unsigned index(std::uint64_t v) noexcept {     // v in ns; 0 -> bucket 0
        if (v < kSub) return static_cast<unsigned>(v);              // exact below 8
        const unsigned msb = 63 - std::countl_zero(v);              // lzcnt: Ch. 36
        const unsigned oct = msb - kSubBits + 1;                    // octave 1.. ; sub = next 3 bits below the msb
        const unsigned sub = static_cast<unsigned>((v >> (msb - kSubBits)) & (kSub - 1));
        const unsigned i = oct * kSub + sub;
        return i < kBuckets ? i : kBuckets - 1;                     // saturate, never drop (Ch. 3 §3.4.3's hdr_init lesson)
    }
    static constexpr std::uint64_t lower_bound(unsigned i) noexcept {   // inverse: the smallest value in bucket i
        if (i < kSub) return i;
        const unsigned oct = i / kSub, sub = i % kSub, msb = oct + kSubBits - 1;
        return (std::uint64_t{1} << msb) | (std::uint64_t{sub} << (msb - kSubBits));
    }
    void record(std::uint64_t ns) noexcept {                        // hot path: ~2–4 ns, no branch mispredicts in steady state
        const unsigned i = index(ns);
        __atomic_store_n(&count[i], count[i] + 1, __ATOMIC_RELAXED);    // single writer: plain RMW, atomic STORE so the
        __atomic_store_n(&total, total + 1, __ATOMIC_RELAXED);          // scraper never reads a torn 64-bit value
        if (ns > max) [[unlikely]] __atomic_store_n(&max, ns, __ATOMIC_RELAXED);
    }
    // off-path: percentile from a (cumulative or interval) bucket array — the scraper's job, never the hot thread's
    static std::uint64_t percentile(const std::uint64_t* c, std::uint64_t total, double p) noexcept {
        std::uint64_t need = static_cast<std::uint64_t>(total * p / 100.0 + 0.5), seen = 0;
        if (need == 0) need = 1;
        for (unsigned i = 0; i < kBuckets; ++i) { seen += c[i]; if (seen >= need) return lower_bound(i + 1) - 1; }
        return lower_bound(kBuckets) - 1;
    }
};

// ---- one hot thread's stats: its own cache lines, written only by it.  Counters are monotonic; gauges are last-value;
// high-water marks reset when the scraper bumps `epoch` (the scraper owns epoch; the thread only reads it).
struct alignas(kLine) ThreadStats {
    // line 0: the seqlock-guarded multi-word gauge set (Ch. 44): read as a consistent snapshot
    std::atomic<std::uint32_t> seq{0};
    std::uint32_t cpu;                                    // which core this thread runs on (Ch. 54) — a gauge too
    std::uint64_t queue_depth, queue_high_water, book_age_ns, last_seq_processed;   // the gauge set published together
    std::uint64_t epoch_seen;                             // the scraper's epoch this thread last acknowledged (resets HWMs)
    // line 1: monotonic counters, each a single 64-bit store — torn reads impossible on x86-64 (naturally aligned 8 B)
    alignas(kLine) std::uint64_t msgs, gaps, drops_ring, drops_socket, orders, rejects, throttles, heartbeat;
    // lines 2..: per-stage histograms (ns): decode, book, strategy, send, wire-to-wire
    alignas(kLine) Histogram stage[5];

    void publish_gauges(std::uint64_t depth, std::uint64_t age_ns, std::uint64_t last_seq) noexcept {   // writer side of the seqlock
        const std::uint32_t s = seq.load(std::memory_order_relaxed);
        seq.store(s + 1, std::memory_order_relaxed);                  // odd: in progress
        std::atomic_signal_fence(std::memory_order_release);          // compiler barrier; x86-TSO orders the stores (Ch. 39, 44)
        queue_depth = depth; book_age_ns = age_ns; last_seq_processed = last_seq;
        if (depth > queue_high_water) queue_high_water = depth;
        std::atomic_signal_fence(std::memory_order_release);
        seq.store(s + 2, std::memory_order_release);                  // even: consistent
    }
    struct GaugeSnapshot { std::uint64_t depth, hwm, age_ns, last_seq; std::uint32_t cpu; };
    bool read_gauges(GaugeSnapshot& out) const noexcept {               // reader side (scraper): retry on odd/changed seq
        for (int tries = 0; tries < 8; ++tries) {
            const std::uint32_t s0 = seq.load(std::memory_order_acquire);
            if (s0 & 1) continue;
            out = {queue_depth, queue_high_water, book_age_ns, last_seq_processed, cpu};
            std::atomic_thread_fence(std::memory_order_acquire);
            if (seq.load(std::memory_order_relaxed) == s0) return true;
        }
        return false;                                                 // writer is spinning on us? — report, don't block
    }
};
static_assert(sizeof(ThreadStats) % kLine == 0);

// ---- the segment: a fixed, versioned layout (Ch. 65 §65.4.2) that an independently-built scraper can map read-only.
struct Segment {
    static constexpr std::uint32_t kMagic = 0x54454c45, kVersion = 3;   // "TELE"
    static constexpr unsigned kMaxThreads = 32;
    alignas(kLine) std::uint32_t magic, version, n_threads, line_bytes;
    std::uint64_t boot_tsc, tsc_hz;                        // so the scraper converts the thread's TSC stamps (Ch. 20)
    std::atomic<std::uint64_t> epoch;                      // scraper-owned: bump to ask threads to reset high-water marks
    std::atomic<std::uint32_t> readiness;                  // the Appendix M / Ch. 98 state machine: cold..armed..disarmed
    alignas(kLine) ThreadStats threads[kMaxThreads];
};
static_assert(offsetof(Segment, threads) % kLine == 0);

}  // namespace tele
```

The decisions, each tied to a measurement or a chapter:

- **One writer per line, and the writer is the hot thread.** `ThreadStats` is `alignas(64)` and every group of fields that a different reader cadence touches starts a new line; nothing another thread writes shares a line with anything the hot thread writes (Ch. 42). The `epoch` is the one scraper-written word, and it sits in the *header*, not in any thread's block: the hot thread reads it once per N messages (a shared read of a rarely-written line costs nothing) and resets its high-water marks when it changes.
- **Plain stores, atomic in the sense that matters.** `__atomic_store_n(..., RELAXED)` on a naturally aligned 8-byte slot compiles to a plain `mov` (§95.5) and guarantees the scraper never reads a torn value — the same contract Ch. 39 gives `std::atomic<uint64_t>::store(relaxed)`, used here on plain fields so the layout stays a C-compatible wire format (the scraper may be a Python process reading the mapping with `struct.unpack`).
- **The histogram is log-linear, fixed-size, saturating.** 288 buckets × 8 B = 2.3 KiB per stage; the top bucket absorbs anything above ~68 s rather than dropping it (Ch. 3 §3.4.3's `hdr_init` lesson: the sample you refuse is the one you were looking for). 12.5 % relative precision is coarse next to HdrHistogram's 0.1 % at three significant figures; a production segment uses HdrHistogram's own layout (or 16–32 sub-buckets) — the *shape* (monotonic buckets, diff for interval, add for merge) is what matters, and it is identical.
- **The seqlock guards only the multi-word set.** `publish_gauges` is Ch. 44's writer verbatim: odd/even sequence, compiler fences (x86-TSO already orders the stores), no reader-side blocking of the writer. `read_gauges` bounds its retries and *reports* a failure rather than spinning — a scraper that could stall is a scraper that could stall the supervisor's liveness check with it (Ch. 98).
- **Versioned header, TSC calibration, readiness word.** The header carries magic/version (Ch. 65 — the scraper refuses a layout it does not know), `tsc_hz` and `boot_tsc` so timestamps written as raw TSC (Ch. 20) are convertible off-path, and the `readiness` state (Appendix M's cold → warming → syncing → reconciled → armed → trading → disarmed) so the gate and the supervisor read the process's own claim about itself from the same place as its counters.

### 95.4.2 What a trading process must expose

The segment is the mechanism; the *content* is what makes it operational. Per hot thread (feed handler, strategy shard, order gateway), the minimum set, each with the kind from §95.2.1 and the chapter that produces it:

- **Liveness and progress.** A `heartbeat` counter incremented every loop iteration (Ch. 98 §98.4.3 — a *progress* counter, so a stuck loop that still "heartbeats" is caught by `last_seq_processed` not advancing); the `cpu` gauge (did the thread migrate? — Ch. 54); `readiness`.
- **Feed health.** Per channel: `msgs`, `gaps` (post-A/B, Ch. 70), `drops_ring` and `drops_socket` mirrored from the NIC/socket counters the scraper also reads directly (Ch. 109 — the app's view and the OS's view must agree per window, §95.2.4), **feed age** (`book_age_ns`: now minus last packet's wire timestamp — Ch. 71's dark-feed gauge), A/B arrival skew, reorder-window high-water (Ch. 68).
- **Queues.** Depth and high-water of every ring the thread consumes or produces (Ch. 43, 46, 65) — derived from the ring's own indices, no extra bookkeeping; the high-water mark is what tells you how close the open came to overflowing.
- **Per-stage latency.** One histogram per stage boundary — decode, book update, strategy, send — plus the **wire-to-wire** histogram (NIC RX stamp → NIC TX stamp, Ch. 72/75), all as *response time* from arrival (§95.2.4). The Ch. 113 §113.3 decomposition is these five histograms, read continuously.
- **Order session.** `orders`, `rejects` (by reason, as a small array), `throttles` (venue rate-limit hits), **ack latency** histogram (send → venue ack, Ch. 67), and the **oldest unacknowledged order age** gauge — the order-path counterpart of feed age, and the number that catches a dead session before the venue's heartbeat timer does (Ch. 70 §70.4.4).
- **Resource gauges for a zero-allocation system.** Pool/arena occupancy and high-water (Ch. 31), the test-time `operator new` counter kept as a production alarm (Ch. 30 — it must stay at its start-up value), the thread's own `minflt` sampled from `/proc` *by the scraper* (Ch. 108 — a page fault on the hot path is a counter that must not move).

Everything on that list is a store the thread already has the value for; nothing requires a syscall or a computation the hot path does not already do. What is *not* on the list is anything formatted, labelled or stringly-typed: labels (venue, shard, stage) are positions in the layout, resolved to names by the scraper from the header.

### 95.4.3 The off-core scraper and export

The scraper is a separate thread or — better, for fault isolation (Ch. 98) — a separate process on a housekeeping core (Ch. 54), mapping the segment read-only. Its loop is: sleep to the cadence, read every thread's cumulative buckets, **subtract** the previous snapshot for the interval, **add** across threads for the merged distribution, compute percentiles *from the merged interval histogram*, read the gauge sets through the seqlock, and export. The core of `scraper.cpp` (built alongside the header; its `--selftest` produced §95.2.2's table):

```cpp
// scraper.cpp (excerpt) — the three operations that make percentiles honest, and the export that keeps them so.
struct Snapshot { std::uint64_t count[Histogram::kBuckets]{}; std::uint64_t total = 0, max = 0; };

static void read_cumulative(const Histogram& h, Snapshot& s) {          // relaxed loads: each bucket is a monotonic counter
    for (unsigned i = 0; i < Histogram::kBuckets; ++i) s.count[i] = __atomic_load_n(&h.count[i], __ATOMIC_RELAXED);
    s.total = __atomic_load_n(&h.total, __ATOMIC_RELAXED); s.max = __atomic_load_n(&h.max, __ATOMIC_RELAXED);
}
static Snapshot interval(const Snapshot& now, const Snapshot& prev) {    // cumulative -> interval: a subtraction per bucket
    Snapshot d; for (unsigned i = 0; i < Histogram::kBuckets; ++i) d.count[i] = now.count[i] - prev.count[i];
    d.total = now.total - prev.total; d.max = now.max; return d;        // (max is cumulative unless the epoch reset it)
}
static void merge_into(Snapshot& acc, const Snapshot& s) {               // THE merge: add counts. Percentiles are not additive.
    for (unsigned i = 0; i < Histogram::kBuckets; ++i) acc.count[i] += s.count[i];
    acc.total += s.total; if (s.max > acc.max) acc.max = s.max;
}
static void export_prometheus(const char* stage, const Snapshot& s) {    // bucket counts, so the TSDB can merge too
    static const std::uint64_t le[] = {250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 1000000};
    std::uint64_t cum = 0; unsigned i = 0;
    for (std::uint64_t bound : le) {
        while (i < Histogram::kBuckets && Histogram::lower_bound(i + 1) - 1 <= bound) cum += s.count[i++];
        std::printf("ttt_stage_ns_bucket{stage=\"%s\",le=\"%llu\"} %llu\n", stage, (unsigned long long)bound, (unsigned long long)cum);
    }
    std::printf("ttt_stage_ns_bucket{stage=\"%s\",le=\"+Inf\"} %llu\nttt_stage_ns_count{stage=\"%s\"} %llu\n",
                stage, (unsigned long long)s.total, stage, (unsigned long long)s.total);
}
// main(): shm_open(name, O_RDONLY) + mmap(PROT_READ); check magic/version; every period_ms: for each stage, for each
// thread { read_cumulative; merge_into(merged, interval(now, prev)); prev = now }; percentiles from `merged`; read_gauges.
```

Export decisions:

- **Ship histograms, not percentiles.** The Prometheus exposition above emits cumulative `le` buckets and a count; the server's `histogram_quantile(0.999, sum by (le) (rate(ttt_stage_ns_bucket[1m])))` merges across hosts and windows *correctly* because it sums buckets first. Prometheus native histograms (sparse, exponential buckets — the same log-linear idea) or OpenTelemetry exponential histograms carry the full resolution; HdrHistogram's compressed base64 log format (`hdr_log_writer`) is the choice when you need the exact distribution archived per interval for later re-merging. Whichever: *a `p99_9` gauge metric is a bug*.
- **Keep the raw interval histograms.** Store the per-scrape interval histograms (a few KiB each, compressed to hundreds of bytes) alongside the capture journal (Ch. 103): any later question — "what was the p99.99 between 09:30:00 and 09:30:30 on shard 3?" — is a merge of stored buckets, exact, with no re-run.
- **The scraper is also the *reader* for the OS- and NIC-side signals.** `/proc/<tid>/status`, `/proc/interrupts`, `ethtool -S`, `nstat` (Ch. 108, 109) are read by the same process at the same cadence and exported in the same format, so an application drop counter and the NIC's `rx_missed_errors` sit on the same timeline. The scraper's own cost (a few ms of `/proc` parsing per second on a housekeeping core) is the price; its *interference* with the hot core is the §95.3 table — scrape at 1–10 Hz, from a different physical core, never from the SMT sibling.
- **Export off-box from the scraper, never from the hot process.** The scraper owns the HTTP listener or the push socket; the trading process has no network endpoint for metrics at all. If the scraper is wedged, the segment still fills; the supervisor (Ch. 98) watches the scraper's own heartbeat in the same segment.

### 95.4.4 From metrics to SLOs, alerts and triggers

Numbers that nobody acts on are a cost with no return. The last step is deciding what *should* page a human at 09:31 and what should not — compactly, operationally, and in the book's terms rather than an SRE textbook's:

- **SLIs — the indicators.** Per venue and shard: wire-to-wire **p99.9** (and the per-stage p99.9s that decompose it), **feed age** and A/B skew, **post-A/B gaps** and per-layer drops, **oldest unacked order age**, **reject and throttle rates**, **PTP offset** (Ch. 75), and the isolation-integrity counters of Ch. 108 (migrations, involuntary switches, minor faults on hot threads — whose SLI is simply *zero*). Each is a histogram or counter from §95.4.2; none is a mean.
- **SLOs — budgeted per session phase.** The open is a different regime from continuous trading, which differs from the close and from the pre-open. A single "p99.9 < 5 µs" objective either pages every morning at 09:30 or is loose enough to be useless at 11:00. Budget per phase (driven by the venue's session state, Appendix J.7): e.g. wire-to-wire p99.9 ≤ 4 µs continuous, ≤ 12 µs in the first 60 s after the open; feed age ≤ 50 ms continuous, ≤ 3 heartbeat intervals pre-open; gaps = 0 post-A/B outside the first minute; unacked age ≤ 2 × venue ack p99.9. Write them down; the alert rules are derived from them, not invented separately.
- **Alert on symptoms, with multi-window conditions and hysteresis.** Page on *the thing that costs money* — the SLI breaching its SLO — not on each cause (a cause alert is a diagnostic annotation, routed to a channel, not a pager). Avoid flapping with two windows: fire when the 5-minute p99.9 *and* the 1-hour p99.9 are over budget (sustained), or when a short window exceeds a far higher threshold (acute: p99.9 > 10× budget for 30 s); clear only when both are back under for a cooling period. Require the §95.2.3 minimum sample count before a window is evaluated. Group by root cause: one page per venue session, not one per symbol — a dead line darkens 400 instruments at once, and 400 pages are no pages.
- **"Any nonzero delta is an incident" signals.** Some counters have no budget: involuntary context switches on a hot thread, migrations, minor faults after warm-up, `operator new` calls, SMIs, the hot core's `CAL`/`TLB` IPI rows (Ch. 59, 108), post-A/B gaps mid-session. These do not need windows or percentiles; a delta of one is a ticket, and the scraper emits them as such.
- **Triggers, not just alerts.** The most valuable consumer of a breached threshold is not a human but the *trace*: the hot thread itself, on a per-message response time above the budget, writes Ch. 59 §59.4.2's `trace_marker` and stops the ftrace ring (two `write`s on an already-open fd, taken once); the supervisor, on a dead heartbeat, triggers the kill-switch (Ch. 98 §98.4.4); the readiness gate (Appendix M) refuses to arm on any Ch. 108 isolation counter being non-zero since boot. The telemetry pipeline's latency (seconds) is fine for paging; the trigger path is the hot thread's own branch, and it is the only place a threshold is evaluated on the hot path — one predicted compare per message.

## 95.5 Verify the codegen: the instrumentation is a few stores

The claim is "one counter plus one histogram sample per message costs a handful of instructions and no call". Prove it (Ch. 4): the hot-path instrumentation of one tick, GCC 13.3 `-O2 -std=c++23 -march=icelake-server`, `objdump -d -M intel` (Clang 18 emits the same shape):

```cpp
extern tele::ThreadStats* g_stats;
void on_tick(std::uint64_t decode_ns) {            // the hot-path instrumentation: one counter, one histogram sample
    tele::ThreadStats& s = *g_stats;
    __atomic_store_n(&s.msgs, s.msgs + 1, __ATOMIC_RELAXED);
    s.stage[0].record(decode_ns);
}
```

```asm
on_tick(unsigned long):
        mov    rax, QWORD PTR [rip+g_stats]
        mov    rsi, QWORD PTR [rax+0x40]      ; msgs (line 1 of ThreadStats)
        lea    rdx, [rsi+1]
        mov    QWORD PTR [rax+0x40], rdx      ; msgs + 1 — a plain store: the "atomic" relaxed store IS the mov (Ch. 39)
        mov    edx, edi
        cmp    rdi, 7
        jbe    .Lsmall                        ; v < 8: exact bucket
        mov    edx, 0x3c
        mov    ecx, 0x3d
        lzcnt  rsi, rdi                       ; the index: lzcnt (Ch. 36), two subtracts, shrx, and, lea
        sub    edx, esi
        sub    ecx, esi
        shrx   rdx, rdi, rdx
        and    edx, 7
        lea    edx, [rdx+rcx*8]
        mov    ecx, 0x11f
        cmp    edx, ecx
        cmova  rdx, rcx                       ; saturate to the top bucket — branchless (Ch. 15)
.Lsmall:
        mov    rsi, QWORD PTR [rax+rdx*8+0x240]
        lea    rcx, [rsi+1]
        mov    QWORD PTR [rax+rdx*8+0x240], rcx   ; count[i] + 1
        mov    rsi, QWORD PTR [rax+0xb40]
        lea    rdx, [rsi+1]
        mov    QWORD PTR [rax+0xb40], rdx     ; total + 1
        cmp    QWORD PTR [rax+0xb48], rdi
        jb     .Lnewmax                       ; predicted not-taken after warm-up
        ret
.Lnewmax:
        mov    QWORD PTR [rax+0xb48], rdi     ; max = v
        ret
```

Twenty-eight instructions, three stores (a fourth on a new maximum), two predictable branches, **no `lock` prefix, no `call`, no fence**. What to confirm, and what would be wrong:

- **The relaxed "atomic" store is a `mov`.** `__atomic_store_n(..., __ATOMIC_RELAXED)` on x86-64 adds nothing over a plain store; if you see `xchg` or `mfence`, someone asked for `seq_cst` (Ch. 39) — ~20 ns per increment for no benefit, since there is one writer.
- **No `lock add`.** A `lock`-prefixed RMW means the counter is a `std::atomic::fetch_add` — ~5 ns uncontended and the §95.3 contended disaster if shared. The single-writer design needs none.
- **No `call`.** A `call` into a metrics library, a `std::string` constructor or `malloc` means labels or formatting leaked onto the hot path — Ch. 93 §93.5's test, applied here.
- **The histogram index is arithmetic.** `lzcnt`/`shrx`/`cmova` — no loop over bucket boundaries, no division. If the index is computed by a `log()` call or a search, the record costs tens of ns.
- **The seqlock publish is six `mov`s** (not shown): two to `seq`, four to the gauge words, in order; a `mfence` between them is a `seq_cst` or `acq_rel` fence that x86-TSO does not need for a single writer (Ch. 44).

## 95.6 Pitfalls & anti-patterns

- **A shared counter.** One `std::atomic` per metric, incremented by every thread — the §95.3 120 ns row; worse under more threads. Per-thread owned slots, merged by the scraper (Ch. 40 §40.4, Ch. 42).
- **Formatting or labelling on the hot path.** `metrics.counter("msgs", {{"venue", venue}, {"shard", shard}}).inc()` builds strings and hashes label sets per call. Labels are *positions in the layout*; names live in the header and the scraper.
- **A syscall per sample.** `clock_gettime` through a non-TSC clocksource (Ch. 6 §6.4.4), `getrusage`, reading `/proc/self/stat` from the hot thread, `write()` to a stats pipe. The hot thread stamps with `rdtsc` (Ch. 20) and stores; the scraper reads `/proc` *about* the thread (Ch. 108).
- **Averaging percentiles** — across hosts, threads, symbols or windows (§95.2.2). Export and store bucket counts; compute percentiles from merged histograms at query time. Audit every dashboard panel: `avg(p99)` and `avg_over_time(p99[…])` are bugs.
- **A `p99_9` gauge metric.** The hot process or scraper computes a percentile and exports it as a number. The downstream system *will* average it. Export the histogram.
- **Cumulative percentiles.** "p99.9 since start" dilutes the open by lunchtime. Interval histograms at the scrape cadence, merged into the window of interest (§95.2.3).
- **Percentiles from too few samples.** A 1 s p99.9 on a 300 msg/s instrument is the 1st-worst sample. Publish the count with every percentile; require a minimum in alert rules; merge across instruments or widen the window (§95.2.3).
- **Service time instead of response time.** `t_done − t_pickup` deletes the stall from every message that waited (§95.2.4). Stamp at the wire; add a queueing histogram; reconcile recorded samples against arrived packets per window.
- **The scraper on the hot core or its SMT sibling, or at kHz cadence.** §95.3's bottom rows: RFO misses on every line the scraper touched. Housekeeping core, different physical core, 1–10 Hz.
- **The scraper that can stall the writer.** A reader that spins on an odd seqlock forever, or a `read_gauges` that the supervisor's liveness check waits on. Bounded retries; report the failure; the hot thread never waits for anyone (Ch. 44 §44.5).
- **`hardware_destructive_interference_size` in a shared layout.** The hot process and the scraper may be built with different compilers or `-mtune` (and the value is 64 on x86 but 128 on some ARM — Appendix A.3); a layout that depends on it is not a wire format. Hard-code the line size in the header and `static_assert` it.
- **Resetting counters from the hot thread, or from the scraper by writing them.** Reset-on-read makes the snapshot a race and two scrapers impossible. Counters are monotonic; the scraper diffs; high-water marks reset via the epoch handshake, the only scraper→thread signal, and the thread does the write.
- **Paging on causes, or per symbol.** One dead line → 400 instrument alerts → nobody reads them. Page on the SLI breach grouped by session; route cause signals to a channel; use the "any nonzero delta" class for the counters with no budget (§95.4.4).
- **A metrics HTTP endpoint inside the trading process.** The kernel socket stack, a formatter and a serializer in the process that owns the isolated core. The scraper serves the endpoint; the trading process has none.

## 95.7 Exercises & checklist

**Exercises**

1. **Cost table.** Build `telemetry.hpp` + `tele_bench.cpp` and reproduce §95.3's rows on your box, pinned to an isolated core. Then add a `std::mutex`-guarded `std::map<std::string, uint64_t>` "metrics library" row and a `std::format`-to-string row; place them on the table.
2. **Scraper interference.** Run a hot thread recording into five histograms at 1 msg/µs with its response-time histogram captured by *another* instrument (Ch. 59's `gapdetect`, or the wire-to-wire stamp); scrape at 0, 1, 10, 100, 1,000 Hz from a different core, then from the SMT sibling. Reproduce the §95.3 interference table; find the cadence at which p99.99 moves on your box.
3. **Percentiles are not additive.** Run `scraper --selftest`; then change thread B's stall fraction to 0.3 % and 5 %, and its thread count to 1-of-8 and 1-of-20. Tabulate averaged vs merged p99.9 each time and explain the sign of the error from the counts.
4. **Interval vs cumulative.** Record a 30 s burst of 50 µs samples at t=0 into a stream of 1.2 µs samples; plot cumulative p99.9 over an hour and 10 s-interval p99.9 over the same hour. Note when the cumulative figure stops saying anything.
5. **Coordinated omission in production.** Stall the hot thread for 40 µs every 10 ms (a `SCHED_OTHER` spinner on its core — Ch. 57) while recording (a) service time from pickup and (b) response time from the NIC/`SO_TIMESTAMPING` stamp. Compare the two p99.9s with the stall's true cost; count recorded samples vs arrived packets per window.
6. **Export round-trip.** Point a Prometheus (or any TSDB with histogram support) at the scraper; write the `histogram_quantile` query that merges across two scrapers; verify it equals the scraper's own merged p99.9 for the same window, and that `avg(p99_9)` would not have.
7. **SLO and alert rules.** Write phase-budgeted SLOs for your system's wire-to-wire p99.9, feed age and unacked order age; implement the two-window alert with hysteresis and the minimum-count guard; replay a captured open (Ch. 103) through the pipeline and count the pages.

**Checklist — application telemetry**

- [ ] Every hot-path metric op is a **store to memory the thread owns** — no shared atomics, no locks, no syscalls, no formatting, no allocation; verified in the **codegen** (§95.5: no `lock`, no `call`, no fence).
- [ ] The segment has a **versioned header**, a **hard-coded line size**, one **`ThreadStats` block per hot thread** with counters, a **seqlock-guarded gauge set** and **per-stage histograms**; scrapers map it **read-only** (§95.4.1).
- [ ] The process exposes **liveness/progress**, **feed age and gaps/drops per layer**, **queue depth and high-water**, **per-stage and wire-to-wire response-time histograms**, **order-session ack latency / rejects / throttles / unacked age**, and **zero-allocation gauges** (§95.4.2).
- [ ] Latency is recorded as **response time from the wire stamp**, and recorded-sample counts are reconciled against arrived packets per window (§95.2.4).
- [ ] The **scraper** runs on a **housekeeping core** (not the sibling), at **1–10 Hz**, diffs cumulative buckets for **interval** histograms, **merges by adding counts**, and computes percentiles **last** (§95.3, §95.4.3).
- [ ] Export carries **bucket counts** (Prometheus/OpenTelemetry histograms or HdrHistogram logs), **never a percentile as a metric**; raw interval histograms are archived (§95.4.3).
- [ ] Every percentile is published **with its sample count**; windows are sized to ≥10⁵ samples for p99.9 or merged until they are (§95.2.3).
- [ ] **SLOs are budgeted per session phase**; alerts fire on **SLI symptoms** with **two windows, hysteresis and a minimum count**, grouped by session; "any nonzero delta" counters are tickets; thresholds that trigger a **trace or kill-switch** are evaluated by the hot thread or supervisor, not by the pipeline (§95.4.4).
- [ ] No dashboard panel averages a percentile (§95.6).

## 95.8 References

- Gil Tene, *"How NOT to Measure Latency"* (Strange Loop 2015 / QCon) and the **HdrHistogram** documentation and C library (`hdr_record_value`, `hdr_add`, `hdr_value_at_percentile`, the interval-recorder and log-writer APIs) — coordinated omission, log-linear histograms, histogram merging (§95.2.2–§95.2.4; Ch. 3 §3.2.2, §3.4.3).
- Prometheus documentation: histograms and summaries, the exposition format, `histogram_quantile` and native histograms; OpenMetrics and the OpenTelemetry exponential-histogram specification — exporting bucket counts rather than percentiles (§95.4.3).
- Beyer, Jones, Petoff, Murphy (eds.), *Site Reliability Engineering* (Google, 2016) and *The Site Reliability Workbook* (2018), the chapters on SLOs and on alerting on SLOs (multi-window, multi-burn-rate alerts) — the alert-design pattern §95.4.4 adapts to session phases.
- Intel SDM Vol. 3A §11.1–11.2 (guaranteed atomic operations: naturally aligned 64-bit accesses) and Ch. 39 — why a single aligned store is a tear-free publication; Ch. 44 — the seqlock protocol used for the gauge set.
- Ch. 93 (the logging sibling), Ch. 40 §40.4 and Ch. 42 §42.3 (per-thread padded counters, HITM cost), Ch. 65 §65.4.2 (the versioned shm header), Ch. 54 (housekeeping vs isolated cores), Ch. 20 (TSC stamps), Ch. 72/75 (wire timestamps), Ch. 98 §98.4.3 (heartbeats and progress), Ch. 108–113 (the signals that join this pipeline and the case study that consumes it).

## 95.9 Additional Reading

- Brendan Gregg, *Systems Performance* (2nd ed.), the methodology and observability chapters — what to expose and how to read it; and his writing on latency heat maps (interval histograms over time, visualised).
- Theo Schlossnagle, *"The Problem with Percentiles"* and Circonus's histogram work; the t-digest (Dunning) and DDSketch (Masson et al., VLDB 2019) papers — mergeable quantile sketches as alternatives to fixed-bucket histograms when memory, not precision, is the constraint.
- Martin Thompson's Mechanical Sympathy posts on single-writer principle and on measuring latency — the design discipline behind "one writer per line".
- Ch. 98 (*Process Topology*) — the supervisor, heartbeats and control plane that live in this segment; Ch. 108 (*Host Health*) and Ch. 109 (*Network Health*) — the OS/NIC signals the same scraper collects; Ch. 113 (*Case Study*) — the continuous-monitoring half of the production loop; Appendix M — the readiness gate reading the segment's `readiness` word; **Appendix E** — the HITM and syscall rows that price the anti-patterns; **Appendix L.2** — the HdrHistogram harness this chapter's segment productionises; **Appendix F** — SLI / SLO / coordinated omission / high-water mark glossary.

---

*Next: Ch. 96 — Secure Programming for Low-Latency Systems, treating market-data and order-entry messages as untrusted input: bounds-checking zero-copy parses (Ch. 68), memory safety in arena/zero-allocation designs (Ch. 31), integer safety in price/quantity math (Ch. 35), and the hardening-vs-latency trade-off (stack protector, FORTIFY, CFI, CET) on an isolated box — security as a hot-path concern.*
