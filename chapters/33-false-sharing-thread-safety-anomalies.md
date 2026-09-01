# Part VI — Concurrency

# Chapter 33 — False Sharing & Thread-Safety Anomalies

> **Prerequisites:** Ch. 7 (cache coherence / MESI — false sharing *is* coherence traffic on a line), Ch. 9 (alignment & padding — the cure), Ch. 30–32 (atomics/contention — false sharing is contention you didn't intend), Ch. 16 (NUMA — cross-socket false sharing is worst), Ch. 2 (the PMU/HITM counters that detect it).
>
> **Leads into:** Ch. 34 (lock-free queues — head/tail padding), Ch. 35–37 (seqlock/Disruptor — single-writer layout), Ch. 31 (the sharded designs whose per-thread state must be padded). The detection ties to Ch. 2 (`perf c2c`).

---

## 33.1 Why it matters: invisible cache-line ping-pong

False sharing is the bug that has been lurking behind every measurement in Part VI, and it's the most *invisible* performance problem in concurrent C++: two threads accessing **logically independent** variables — variables they each "own," that have nothing to do with each other — nonetheless contend, slowing each other down by an order of magnitude, because the variables happen to live on the **same cache line**. Cache coherence (Ch. 7) operates at cache-line granularity (64 bytes), not variable granularity: when thread A on core 0 *writes* its variable, the *entire line* is taken exclusive in core 0's cache, **invalidating** core 1's copy — so when thread B on core 1 reads *its* (different, adjacent) variable, it suffers a coherence miss and must pull the line back, invalidating core 0's copy in turn. The line **ping-pongs** between the cores (the HITM transfer of Ch. 7, ~tens of ns, and ~150 ns across sockets — Ch. 16) on every access, even though the threads never actually share any data. It's all the cost of sharing with none of the intent.

This is insidious precisely because it's *invisible in the source*. There's no shared variable, no lock, no atomic operation between the threads — the code *looks* perfectly shared-nothing (Ch. 31). Two threads each incrementing their own counter in an array `counters[thread_id]++` looks contention-free and *is* logically contention-free, yet runs ~10× slower than expected because `counters[0]` and `counters[1]` are 8 bytes apart on the same 64-byte line. The §31.3 "sharded counters" only scaled because they were *padded*; unpadded, they behave exactly like the *shared* atomic. False sharing is why "I removed all the sharing and it still doesn't scale" — the sharing you removed was logical; the physical sharing (the cache line) remained.

For HFT this is a first-class concern because the architecture is built on per-thread/per-core data (Ch. 31) and tight lock-free structures (Ch. 34–37) whose hot fields — a queue's head and tail indices, per-thread counters and accumulators, adjacent flags — are *exactly* the kind of small, hot, independently-written variables that fall victim. A queue whose producer-written `head` and consumer-written `tail` share a cache line will ping-pong that line between producer and consumer on every operation, halving throughput and spiking the latency tail (Ch. 1) — a textbook false-sharing bug in the most performance-critical structure in the system. The cure is trivial once you *see* it — **pad/align hot independently-written data to separate cache lines** (`alignas(std::hardware_destructive_interference_size)`, Ch. 9) — but seeing it requires the right tools (`perf c2c`, HITM counters — Ch. 2), because it's invisible to the eye and to most profilers. This chapter builds the coherence mental model (§33.2), shows how to *detect* it with HITM counters (§33.3), gives the padding/per-thread-data cures (§33.4), and warns about the over-correction (padding bloat) and the related thread-safety anomalies (§33.5).

## 33.2 Mental model: coherence traffic; `hardware_destructive_interference_size`

**Coherence is per-line, not per-variable.** The cache-coherence protocol (Ch. 7's MESI) tracks state — Modified/Exclusive/Shared/Invalid — at **cache-line (64-byte) granularity**. It has no idea your line holds two unrelated variables; it only knows "this line was written, so every other cache's copy is now Invalid." So a write to *any* byte of a line invalidates *every other core's* copy of the *whole* line:

```
   cache line (64 bytes):  [ counterA (8B) | counterB (8B) | ... 48 more bytes ... ]
                              ^owned by thread A   ^owned by thread B  (logically independent!)

   core0 (thread A): counterA++   → takes line EXCLUSIVE, invalidates core1's copy
   core1 (thread B): counterB++   → coherence MISS, pulls line back, invalidates core0's copy
   ... every access ping-pongs the line between core0 and core1 (HITM, ~tens of ns each) ...
   → both threads run ~10x slower, though they share NO data
```

This is **false sharing**: the *sharing* is false (no logical data is shared), but the *contention* is real (the line is physically shared). Contrast **true sharing** — threads contending on data they genuinely both use (a real shared counter, a lock) — where the contention is intrinsic to the algorithm (Ch. 30–32). False sharing is *accidental* and *fixable by layout*; true sharing is *intrinsic* and fixable only by *not sharing* (Ch. 31).

**Why it's invisible.** Nothing in the source signals it — no shared variable, no lock, no atomic between the threads. It doesn't show as a cache-*capacity* miss (the data fits in cache fine); it shows as **coherence misses / HITM** (hit-modified: a load that hit a line another core had modified), which only the right PMU events reveal (§33.3). Most profilers attribute the cost to the innocent load/store, not to the layout. You have to *look* for it with `perf c2c`.

**`hardware_destructive_interference_size` — the standard cure.** C++17 added `std::hardware_destructive_interference_size` (in `<new>`): the minimum offset between two objects to *avoid* false sharing — i.e. the cache-line size for padding purposes (typically 64 on x86, **128 on some ARM** — Appendix A, and Intel's adjacent-line prefetcher can make the effective figure 128 even on x86). Align/pad independently-written hot data to this size so each lands on its own line:

```cpp
struct alignas(std::hardware_destructive_interference_size) PerThread {
    std::uint64_t counter = 0;   // ... now on its own cache line; padding fills the rest
};
PerThread data[NUM_THREADS];     // each element on a separate line — no false sharing
```

(Its companion `hardware_constructive_interference_size` is the *opposite*: the max size to *keep* things together on one line when you *want* them co-located — for data a single thread accesses together.)

**Adjacent-line prefetching.** Intel CPUs prefetch the *adjacent* cache line (the 128-byte "buddy" pair), so two lines 64 bytes apart can *still* false-share via the prefetcher. This is why the effective padding is sometimes 128 bytes, and why `hardware_destructive_interference_size` may report 128. Measure (§33.3) rather than assume 64 is always enough.

The model: **coherence operates per-64-byte-line, so independently-written variables on the same line invalidate each other's caches on every write — false sharing: real contention, no shared data, invisible in source, detectable only via HITM/`perf c2c`. The cure is to pad/align hot independently-written data to `hardware_destructive_interference_size` so each owns its line.**

## 33.3 Measure it: HITM counters with/without padding

False sharing is invisible to the eye and to throughput-only profiling — you detect it with **HITM** (hit-modified) coherence counters and `perf c2c` (cache-to-cache). Measure the canonical case: per-thread counters in an array, **unpadded** (sharing lines) vs **padded** (own lines), same logical work.

```cpp
// falsesharing.cpp — per-thread counters, unpadded vs padded; watch HITM.
// Build: g++ -O2 -std=c++20 -march=native falsesharing.cpp -o fs -pthread
// Run:  ./fs unpadded 8 | ./fs padded 8   (threads pinned to distinct cores)
//   detect:  perf c2c record ./fs unpadded 8 ; perf c2c report
//   or:      perf stat -e mem_load_l3_hit_retired.xsnp_hitm ./fs unpadded 8
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>
#include <new>

struct Unpadded { std::uint64_t v; };                       // 8 bytes — adjacent ones share a line
struct alignas(std::hardware_destructive_interference_size) // own cache line
       Padded   { std::uint64_t v; };

template <class T>
double run(int M) {
    constexpr long N = 200'000'000;
    std::vector<T> data(M);                                 // M counters, contiguous
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::thread> ts;
    for (int t = 0; t < M; ++t) ts.emplace_back([&, t]{
        // (pin to core t — Ch. 40).  Each thread writes ONLY its own counter — logically independent.
        for (long i = 0; i < N; ++i) data[t].v++;           // but unpadded counters share lines!
    });
    for (auto& th : ts) th.join();
    auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count() / (double)N;
}

int main(int argc, char** argv) {
    bool padded = (argc > 1) && std::strcmp(argv[1], "padded") == 0;
    int M = argc > 2 ? std::atoi(argv[2]) : 8;
    double ns = padded ? run<Padded>(M) : run<Unpadded>(M);
    std::printf("%-8s M=%d  %.3f ns/increment  (hw_destructive=%zu)\n",
                padded ? "padded" : "unpadded", M, ns,
                std::hardware_destructive_interference_size);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, single socket), threads pinned to distinct cores, turbo off (illustrative):

```
   M=8 cores, each thread increments only its own counter:
                          ns/increment    HITM (xsnp_hitm)     note
   unpadded               ~9 ns           HIGH                 lines ping-pong — false sharing
   padded                 ~0.3 ns         ~0                   each counter on its own line — no coherence
                                                               (~30x faster, same logical work!)

   perf c2c report (unpadded) pinpoints:
     → the cache line, the offsets (counter[0], counter[1], ...), and the contending PIDs/cores
```

Read it: **same code, same logical work — each thread touches *only its own* counter, zero logical sharing — yet unpadded is ~30× slower.** The increments aren't the cost; the *cache line* is — adjacent counters share a 64-byte line, so each thread's write invalidates the others' caches, and the line ping-pongs among all 8 cores (the HITM coherence traffic of Ch. 7). Padding each counter to its own line eliminates the coherence misses entirely (HITM → ~0) and the work scales as it should. This is the §31.3 "sharded" result's hidden dependency made explicit: **sharding only scales if the per-thread data is padded.** The detection method is the key takeaway: false sharing doesn't show as a normal cache miss or in throughput-only profiling — you find it with **`perf c2c`** (which names the exact line, offsets, and contending cores) or the **HITM counters** (`mem_load_l3_hit_retired.xsnp_hitm` and friends). When per-thread/"shared-nothing" work mysteriously doesn't scale, suspect false sharing and run `perf c2c` (Ch. 2).

## 33.4 Techniques

### 33.4.1 Alignment and padding to a cache line

The direct cure: ensure independently-written hot data lands on separate cache lines.

- **`alignas(std::hardware_destructive_interference_size)`.** Align hot, independently-written objects (per-thread structs, queue indices, separate atomics) to the destructive-interference size so each occupies its own line (Ch. 9). The standard, portable way (handles x86's 64 and ARM's 128 — Appendix A). Where the constant isn't available, use a sensible literal (64, or 128 to also dodge adjacent-line prefetching — §33.2) and document it.
- **Pad the *struct*, and pad *between* hot fields.** Two patterns: (1) align a whole per-thread struct to a line so array elements don't share (the §33.3 fix); (2) within one struct, separate two independently-written hot fields with padding (e.g. a queue's producer-written `head` and consumer-written `tail` each on its own line — §33.4 below). Add explicit padding bytes or `alignas` on the field.
- **Separate reader-written and writer-written data.** In a single-writer/many-reader structure (seqlock — Ch. 35, Disruptor — Ch. 37), keep the writer's hot fields on a different line from anything readers write (e.g. their own sequence/cursor). The classic example: a ring buffer's `head` (producer) and `tail` (consumer) **must not share a line** — otherwise every push invalidates the consumer's view of `tail` and vice versa.
- **Pad the lock (Ch. 32).** A spinlock/mutex word on the same line as the data it protects bounces on every data access. Put the lock on its own line.
- **Verify with `perf c2c` (§33.3).** Padding is cheap to add and easy to get subtly wrong (wrong size, prefetcher buddy line); confirm HITM dropped to ~0 after padding. Don't assume — measure.

### 33.4.2 Per-thread/per-core data

The structural cure (ties Ch. 31): give each thread/core its own data, laid out so it doesn't collide.

- **Per-thread state, padded.** Counters, statistics, accumulators, histograms (Ch. 3) — one per thread, each padded to its own line, updated lock-free locally, aggregated off the hot path (Ch. 31.4.2). This *is* the §31.3 sharded design; the padding is non-optional.
- **Thread-local storage (TLS).** `thread_local` variables are per-thread by construction and typically don't false-share across threads (each thread's TLS is separate) — a clean way to get per-thread data without manual array indexing/padding. (Mind TLS access cost on the hot path and that aggregation must reach into each thread's copy.)
- **Avoid arrays indexed by thread/core id without padding.** `counter[thread_id]` (the §33.3 trap) packs per-thread data densely → guaranteed false sharing. Either pad each element to a line, or use TLS, or space the indices out. A dense per-core array is a false-sharing factory.
- **Co-locate what one thread uses together (constructive interference).** The flip side: data a *single* thread accesses together *should* share a line (better locality — Ch. 7–8). Use `hardware_constructive_interference_size` to *group* a thread's own hot fields, while *separating* different threads' data. Layout is a two-sided optimization: pack within a thread, separate across threads.
- **NUMA-place per-node data (Ch. 16).** Per-core data on the core's NUMA node (first-touch — Ch. 16); cross-node false sharing is the *worst* (the line bounces over the interconnect, ~150 ns). Per-thread + padded + NUMA-local is the full recipe.

## 33.5 Pitfalls & anti-patterns: adjacent hot counters; padding bloat

- **Adjacent hot counters / per-thread array without padding.** The headline false-sharing bug: `stats[thread_id]`, an array of small per-thread structs, two hot atomics declared next to each other — densely packed independently-written data on shared lines (§33.3). Pad to `hardware_destructive_interference_size` or use TLS.
- **Queue head/tail on the same line.** A ring buffer's producer-written `head` and consumer-written `tail` sharing a cache line ping-pongs it between producer and consumer every operation — false sharing in the hottest structure (Ch. 34, 37). Put them on separate lines.
- **"I sharded it but it doesn't scale."** The classic symptom: logically per-thread work that still contends — almost always false sharing (the per-thread data shares lines). Run `perf c2c` (§33.3); it's invisible otherwise.
- **Detecting with the wrong tools.** False sharing doesn't show as a capacity cache miss or in throughput-only/CPU-time profiling — it shows as **HITM**. Using a profiler that doesn't surface coherence misses, you'll blame the innocent load. Use `perf c2c` / HITM counters (Ch. 2).
- **Padding bloat (over-correction).** Padding *everything* to a cache line wastes memory and *hurts* cache utilization (64 bytes to hold an 8-byte counter — Ch. 7–9), and can pad apart data a single thread uses together (losing constructive locality). Pad only *hot, independently-written, actually-false-sharing* data — confirmed by measurement, not reflexively.
- **Assuming 64 bytes is always enough.** Intel's adjacent-line prefetcher (§33.2) can make two 64-apart lines false-share; some ARM cores have 128-byte lines (Appendix A). Use `hardware_destructive_interference_size` (may be 128) and verify with `perf c2c` on the target.
- **False sharing across sockets (Ch. 16).** Cross-NUMA false sharing bounces the line over the interconnect (~150 ns) — far worse than within-socket. Per-node data placement (Ch. 16) plus padding.
- **Thread-safety anomalies mistaken for false sharing (and vice versa).** A *correctness* bug (a data race — Ch. 30, a missing atomic, a torn read — Ch. 35) is different from false sharing (a *performance* bug on correct code). Don't "fix" a race with padding (it's still a race — UB); don't chase a coherence-cost problem with locks. Diagnose which you have (TSan — Ch. 40 — for races; `perf c2c` for false sharing).
- **Padding that breaks atomicity/alignment assumptions.** Adding padding can change a struct's size/layout in ways that affect `is_lock_free` (Ch. 30), ABI, or serialization (Ch. 53). Pad deliberately and re-check the constraints.

## 33.6 Exercises & checklist

**Exercises**

1. **See the ping-pong.** Build `falsesharing.cpp`; run `unpadded` vs `padded` at M=2,4,8 (pin threads). Confirm the ~10-30× slowdown unpadded. Run `perf c2c record`/`report` on the unpadded version — confirm it names the cache line, the offsets, and the contending cores. Run `perf stat -e ...xsnp_hitm` — HITM high vs ~0?
2. **Queue head/tail.** Build a tiny SPSC ring (Ch. 34) with `head` and `tail` on the *same* cache line, then on *separate* lines. Measure throughput producer↔consumer and HITM. Quantify the false-sharing cost in the hottest structure.
3. **Padding bloat.** Pad an array of 1M small counters to a full cache line each. Measure the memory blow-up and the *single-threaded* cache-miss increase (you destroyed locality — Ch. 7). Find the line between "pad the hot contended ones" and "padded everything wastefully" (§33.5).
4. **Adjacent-line prefetch.** Pad two hot per-thread variables exactly 64 bytes apart and measure HITM; then 128 bytes apart. Does Intel's adjacent-line prefetcher make 64 insufficient on your CPU (§33.2)?
5. **Cross-socket.** Run the unpadded counters with threads on one socket vs split across two (Ch. 16). Quantify how much worse cross-NUMA false sharing is; then pad and confirm the fix holds across sockets.

**Checklist — false sharing**

- [ ] Hot, **independently-written** data (per-thread counters/stats, queue indices, separate atomics, locks) is **padded/aligned to `std::hardware_destructive_interference_size`** so each owns its cache line.
- [ ] A queue's/structure's **producer-written and consumer-written hot fields** (e.g. `head`/`tail`) are on **separate cache lines** (Ch. 34, 37).
- [ ] Per-thread data uses **padded per-thread structs or TLS** — **not** a densely-packed `array[thread_id]` (§33.4.2); and is **NUMA-local** (Ch. 16).
- [ ] I **detected/verified with `perf c2c` / HITM counters** (Ch. 2) — confirmed coherence misses dropped to ~0 after padding — not by eyeballing or throughput-only profiling.
- [ ] I **padded only what actually false-shares** (hot + independently-written + measured) — no reflexive padding bloat that wastes memory or breaks within-thread locality (Ch. 7–9).
- [ ] I accounted for **adjacent-line prefetch / 128-byte lines** (`hardware_destructive_interference_size`, verified on the target) — not assumed 64 always suffices.
- [ ] I distinguished **false sharing (a perf bug on correct code) from a data race (a correctness bug)** — padding for the former, atomics/synchronization (Ch. 30, TSan — Ch. 40) for the latter.
- [ ] Padding didn't break **`is_lock_free`/ABI/serialization** assumptions (Ch. 30, 53).

## 33.7 References

- U. Drepper, *What Every Programmer Should Know About Memory* — cache coherence, line granularity, and false sharing with measurements (the foundation of §33.2-§33.3).
- ISO C++ / cppreference — `std::hardware_destructive_interference_size` / `hardware_constructive_interference_size` (`<new>`), C++17 (§33.2, §33.4.1).
- The `perf c2c` documentation (and Joe Mario's *"C2C - False Sharing Detection in Linux Perf"*) — the canonical tool/method for detecting false sharing and reading HITM (§33.3).
- Intel *SDM* / *Optimization Reference Manual* — MESI coherence, HITM, the adjacent-line prefetcher, and the memory performance events (§33.2-§33.3).
- A. Williams, *C++ Concurrency in Action* (2e) — false sharing, data layout for concurrency, and the destructive/constructive interference sizes in practice.

## 33.8 Additional Reading

- J. Mario / Red Hat blog posts on `perf c2c` and real-world false-sharing case studies — practical detection and fixes.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — coherence-miss analysis and false-sharing detection.
- Ch. 7 (*Caches/MESI*) — the coherence mechanics; Ch. 9 (*Alignment/Padding*) — the layout tools; Ch. 30–32 (*Atomics/Foundations/Spinlocks*) — the contention this is the accidental form of; Ch. 34–37 (*Lock-Free/Seqlock/Disruptor*) — structures whose indices must be padded; Ch. 16 (*NUMA*) — cross-socket false sharing; Ch. 40 (*Concurrency Tooling*) — distinguishing races from false sharing.
- **Appendix A** — cache-line-size differences (64 vs 128) on ARM and their effect on `hardware_destructive_interference_size`; **Appendix E** — HITM/coherence latency numbers; **Appendix F** — false-sharing/HITM glossary.

---

*Next: Ch. 34 — Lock-Free Data Structures, where the atomics (Ch. 30), contention control (Ch. 31–32), and false-sharing-aware layout (this chapter) combine into the workhorses of the low-latency data plane: SPSC/MPMC queues, ring buffers, CAS loops, the ABA problem, and progress guarantees.*
