# Part II — CPU Microarchitecture

# Chapter 8 — The Memory Hierarchy & Caches

> **Prerequisites:** Part I (Ch. 1–7). You think in distributions (Ch. 1), can read PMU cache counters (Ch. 2 — `LLC-load-misses`, MPKI), benchmark honestly on a quiet, pinned, fixed-frequency box (Ch. 3, 6), and can read a hot loop in asm (Ch. 4). This chapter opens **Part II — CPU Microarchitecture** and is the foundation the rest of the part builds on.
>
> **Leads into:** Ch. 9 (the DRAM subsystem — what the "~80 ns" at the bottom of §8.2.1 actually is: bimodal, loaded, shared by channel), Ch. 10 (data-oriented layout — AoS vs SoA) and Ch. 11 (object layout, alignment, padding) apply this directly; Ch. 12 (software prefetch & non-temporal stores) extends it; Ch. 17 (TLB & huge pages) and Ch. 18 (NUMA) handle translation and multi-socket effects. The false-sharing flip side of coherence is Ch. 44. Reference costs are consolidated in **Appendix E**.

---

## 8.1 Why it matters: DRAM is ~55× an L1 hit, ~250 cycles away

Here is the single fact that governs more hot-path performance than any other in this book: **the CPU has not waited the same number of cycles for memory in over twenty years.** A core that retires several instructions per nanosecond must, on a last-level cache miss, *stop and wait roughly 80–100 nanoseconds* for DRAM to answer — some **250 cycles, several hundred instructions' worth of idle time**, per miss. The arithmetic in your hot path is essentially free; **where your data lives, and in what order you touch it, is the program.**

Order-of-magnitude costs on a modern x86-64 server core (reference machine **Intel Xeon Gold 6326**, Ice Lake-SP, turbo off at 2.9 GHz — Appendix L.3; these are the numbers Appendix E develops and that you should commit to memory):

| Access | Latency | ≈ cycles @ 2.9 GHz | Relative to L1 |
|---|---:|---:|---:|
| L1 data cache hit | ~1.7 ns | 5 | 1× |
| L2 hit | ~5 ns | ~14 | ~3× |
| L3 (LLC) hit | ~15–25 ns | ~45–70 | ~10–14× |
| Local DRAM (LLC miss) | ~80–100 ns | ~230–290 | ~50–60× |
| NUMA-remote DRAM | ~130–180 ns | ~380–520 | ~80–100× |

(The multiplier moves with the clock and the L1 design: ~55× on this 5-cycle-L1 Ice Lake core, ~80–100× on a 4-cycle-L1 Skylake-class core at ~1 ns per hit, and some texts quote "~200×" from a faster clock and a 1-cycle L1 — but the lesson, **two orders of magnitude**, does not.)

This is why Ch. 1's tail is so often a *memory* tail. A hot path that hits L1 every time runs at a tight, predictable ~1.7 ns/access; the same code that occasionally misses to DRAM gets an ~80–100 ns spike *per miss*, and a handful of those land you in the p99.9. Ch. 2's `perf_demo` already showed it: identical instructions, ~50× the cycles, purely from access pattern. The order book (Ch. 32) lives or dies here — a book update that touches one cache-resident price level is a different animal from one that chases pointers across DRAM.

The good news is that the hardware is *trying to help you*: caches keep recently-used data close, and prefetchers guess what you'll need next. The bad news is that they help only if your **access pattern cooperates** — sequential, predictable, locally-clustered. The entire craft of cache-aware programming (Ch. 8–12) is arranging your data and your accesses so the hardware's bets pay off. This chapter builds the mental model; the next three spend it.

---

## 8.2 Mental model

### 8.2.1 Latencies by level (L1/L2/L3/DRAM)

Memory is a **hierarchy** of progressively larger, slower, cheaper levels, each a cache of the one below it. A modern core sees roughly:

```
   core ── L1d (32-48 KiB, ~1ns, per-core) ── L1i (instructions; Ch.14)
            │
            L2 (~1-2 MiB, ~4ns, per-core)
            │
            L3 / LLC (tens of MiB, ~15-20ns, SHARED across cores on the socket)
            │
            DRAM (local node ~80ns; remote NUMA node ~130ns+, Ch.18)
            │
            (… and far below: NVMe/disk, Ch.107 — irrelevant to the hot path)
```

Two structural facts shape everything:

- **Each level is roughly an order of magnitude larger and slower than the one above.** The hierarchy exists because fast memory is small and expensive; the design bet is that programs exhibit **locality**, so a small fast cache captures most accesses.
- **L1 and L2 are private to a core; L3 is shared — across the socket on a monolithic die like the Ice Lake-SP reference machine, across one *L3 domain* (a CCD, a tile or an SNC node) on chiplet silicon (Ch. 19).** This sharing is what makes inter-core communication possible (Ch. 45–46) and also what makes coherence traffic and *false sharing* (Ch. 44) a thing. It also means another busy core on the socket can evict your data from L3 — a noisy-neighbor tail source that core pinning alone does not cure (Ch. 6, 18; partitioning the L3 with CAT is Ch. 58).

The two forms of locality the hierarchy rewards:

- **Temporal locality:** if you used a byte recently, you'll likely use it again soon — so keeping it cached pays. (Reuse your data while it's hot.)
- **Spatial locality:** if you used a byte, you'll likely use its neighbors soon — so the cache fetches a whole **line** (§8.2.2), not a byte. (Lay data out so the neighbors you fetched are the ones you want.)

Almost every technique in Ch. 8–12 is a way of manufacturing one of these two localities where your natural data structure didn't have it.

### 8.2.2 Cache lines, sets, and associativity

The cache does not deal in bytes. Its unit of transfer and tracking is the **cache line** — **64 bytes** on all current x86-64 (and the value of `std::hardware_constructive_interference_size`; ARM may use 128, Appendix A). Three consequences you must internalize:

1. **You always pay for 64 bytes.** Touch one `int`, and the hardware fetches the entire aligned 64-byte line containing it. If you'll use the other 60 bytes (sequential access), that fetch was an investment; if you won't (pointer-chasing, scattered access), it was 60 bytes of wasted bandwidth and a wasted cache slot. **Spatial locality is literally "use the rest of the line you already paid for."**
2. **Alignment matters.** A 64-byte object straddling two lines costs two line fetches and two cache slots; the same object aligned to a line costs one. This is the lever behind Ch. 11 (alignment & padding) and Ch. 44 (false sharing).
3. **Bandwidth is counted in lines.** When you reason about whether the prefetcher or the memory bus can keep up, the currency is lines/second, not bytes or elements.

**Associativity** governs *where* a line may live. A cache is divided into **sets**; a given memory address maps (by its middle address bits) to exactly one set, and within that set may occupy any of *N* **ways** (an *N-way set-associative* cache; e.g. L1 is often 8-way). The trap this creates is the **conflict miss**: if your access pattern repeatedly touches addresses that map to the *same set* — classically, striding by a large power of two (4 KiB, 2 MiB) — you can thrash a single set, evicting lines you still need, *even though the cache as a whole has plenty of free space elsewhere*. A power-of-two-strided loop or a power-of-two-sized 2D array can run far slower than a "+1" version for exactly this reason (§8.5; exercise 3 in §8.6). The defense is to avoid pathological power-of-two strides and alignments — sometimes deliberately *padding* an array to a non-power-of-two row width.

### 8.2.3 Coherence and the MESI protocol

Because L1/L2 are private, the same cache line can sit in multiple cores' caches at once — so the hardware must keep them **coherent**: a write by one core must not leave another core reading a stale copy. x86 implements this with a snooping protocol, **MESI** (and extensions MOESI/MESIF), tracking each cached line in one of four states:

- **M (Modified):** this core has the only copy and has written it (dirty); memory is stale.
- **E (Exclusive):** this core has the only copy, clean (matches memory).
- **S (Shared):** multiple cores may hold clean copies.
- **I (Invalid):** the line is not valid here.

The rule that matters for latency: **a core that wants to *write* a line must first own it exclusively (M/E), which means *invalidating* every other core's copy.** When two cores repeatedly write the same line, they bounce ownership back and forth, each write forcing an invalidation and a cache-line transfer across the interconnect — a **HITM** ("hit-modified") event costing ~40–100+ ns (Appendix E). This is the mechanism behind:

- **False sharing** (Ch. 44): two threads writing *different variables that happen to share one 64-byte line* suffer this ping-pong even though they share no data logically — one of the nastiest and most invisible scalability killers.
- **Atomics and lock contention** (Ch. 41–45): every contended atomic is a coherence transaction; the cost of a spinlock or a CAS loop *is* coherence traffic.
- The reason **single-writer designs** (Ch. 46, 102) and **per-core data** are so valued: no sharing, no coherence ping-pong, no HITM tail.

For now the takeaway is conceptual: **shared writable data has a hardware cost measured in coherence traffic**, and a huge fraction of Part VI exists to minimize it.

### 8.2.4 The hardware prefetchers

The CPU doesn't only react to misses — it *predicts* them. Each core has several **hardware prefetchers** that watch your access stream and speculatively pull lines into L1/L2 *before* you ask, hiding DRAM latency entirely when they guess right:

- A **next-line / adjacent-line** prefetcher (the neighboring line).
- A **stride / streaming** prefetcher (detects constant-stride sequences — `a[i]`, `a[i+8]`, `a[i+16]`, … — and runs ahead).
- L2 **stream** prefetchers tracking multiple independent streams.

The crucial property: **prefetchers love regular, predictable, forward access** and are **useless against unpredictable, data-dependent access.** A sequential array scan is fully prefetched — you see near-L1 latency even on a DRAM-sized array, because the prefetcher stayed ahead. A **pointer chase** (each address depends on the value just loaded — a linked list, a hash chain, a tree of pointers) is the prefetcher's kryptonite: it cannot predict the next address until the current load *completes*, so every access is a full latency-exposed miss. This is precisely the `seq` vs `rand` gap in Ch. 2's `perf_demo`, and it is *the* argument for flat, array-based, contiguously-laid-out data structures over node-based ones (Ch. 32) on the hot path. When the hardware prefetcher can't help, you sometimes prefetch manually (Ch. 12) — but the first-line defense is an access pattern the hardware can predict. The prefetchers are also *switchable*, per core, through MSR 0x1A4 (Intel) or the BIOS (AMD) — and on a hot core whose working set fits L2 they are often turned *off*, because their speculative streams evict the book; Ch. 12 §12.3–§12.4.3 is the experiment and the policy.

---

## 8.3 Measure it: a latency-vs-stride / cache-size sweep

The classic experiment that makes the whole hierarchy *visible* is a **pointer-chase latency sweep** across working-set sizes — a self-contained variant of the membench / "lmbench latency" method. We build, for each size, a randomized cyclic pointer-chase within a buffer of that size, then measure average time per dependent load. Because each load depends on the previous, the prefetcher can't help and we measure *true access latency* at that size — which jumps each time the working set spills out of a cache level.

```cpp
// cache_sweep.cpp — reveal L1/L2/L3/DRAM by measuring pointer-chase latency vs size.
// Build: g++ -O2 -std=c++20 cache_sweep.cpp -o cache_sweep
// Run pinned with fixed frequency (Ch. 3, 6):  taskset -c 2 ./cache_sweep
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <numeric>
#include <random>
#include <vector>

int main() {
    // Each slot holds the index of the next slot: a randomized single cycle,
    // so traversal is a dependent load chain the prefetcher cannot predict.
    for (std::size_t kib = 4; kib <= 256u * 1024; kib *= 2) {
        const std::size_t n = (kib * 1024) / sizeof(std::uint32_t);
        std::vector<std::uint32_t> next(n);
        std::vector<std::uint32_t> order(n);
        std::iota(order.begin(), order.end(), 0u);
        std::shuffle(order.begin(), order.end(), std::mt19937(1));
        for (std::size_t i = 0; i < n; ++i)
            next[order[i]] = order[(i + 1) % n];

        // Chase the cycle many times; total steps fixed so each size does equal work.
        const std::size_t steps = 200u * 1000 * 1000;
        std::uint32_t idx = 0;
        for (std::size_t s = 0; s < n; ++s) idx = next[idx];        // warm-up lap (untimed)
        auto t0 = std::chrono::steady_clock::now();
        for (std::size_t s = 0; s < steps; ++s) idx = next[idx];    // dependent load
        auto t1 = std::chrono::steady_clock::now();

        asm volatile("" :: "r"(idx));                     // keep the chain live (Appendix L)
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
        std::printf("%8zu KiB   %6.2f ns/access\n", kib, ns / steps);
    }
}
```

Representative output — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz base: 48 KiB L1d, 1.25 MiB L2 per core, 24 MiB L3 shared across the socket — 1.5 MiB slice per core), GCC 13 `-O2 -std=c++20`, Linux 6.18, 4 KiB pages, pinned, turbo off (illustrative; the *plateaus and cliffs* are the point, not the exact ns):

```
       4 KiB     1.72 ns/access     ┐
      32 KiB     1.75 ns/access     ├─ fits in L1d            (5-cycle load-to-use: ~1.7 ns plateau)
      64 KiB     4.80 ns/access     ┐
     512 KiB     5.20 ns/access     ├─ spilled to L2          (~14 cycles: ~5 ns plateau)
    2048 KiB    20.50 ns/access     ┐
   16384 KiB    24.00 ns/access     ├─ spilled to L3          (~60-70 cycles: ~20 ns plateau; STLB misses creep in)
   65536 KiB    95.00 ns/access     ┐
  262144 KiB   105.00 ns/access     ├─ spilled to DRAM        (~100 ns plateau = DRAM + page walk)
```

This single table *is* §8.2.1, measured. Read it the Ch. 1–2 way:

- **The plateaus reveal the cache sizes.** Latency is flat while the working set fits a level, then jumps at each capacity boundary — the cliff edges sit right at ~L1, ~L2, ~L3 sizes. You just measured your machine's cache hierarchy with a 20-line program. The L1 plateau is the floor the hardware allows: a dependent `idx = next[idx]` cannot go faster than the L1d load-to-use latency, **5 cycles on Ice Lake** (4 on Skylake-class cores) — 1.72 ns at 2.9 GHz. If you see less, the chain was broken (or turbo was on).
- **The cliffs are the cost of a miss.** Each step up (1.7→5→20→100 ns) is the latency of the *next* level. The ~100 ns DRAM plateau is the "~80-100 ns" number from §8.1 and Appendix E, in your own hands — plus translation: above ~8 MiB on 4 KiB pages the chase also misses the STLB (~2 K entries ≈ 8 MiB of reach — Ch. 17) and pays a page walk on nearly every access, so the DRAM plateau is **DRAM + page walk**. Re-run the buffer on 2 MB pages (`madvise(MADV_HUGEPAGE)`, Ch. 17) and it drops toward ~85-90 ns: the difference *is* the walk.
- **This is the *latency-exposed* worst case** (dependent pointer chase, no prefetch). Replace the chase with a *sequential* scan and the DRAM-sized case drops back toward L1-like throughput — because the prefetcher (§8.2.4) hides the latency. Ch. 2's `perf_demo` measured exactly that contrast on this machine: the same 134 M dependent loads over a 256 MiB table ran ~53× faster in sequential order than in shuffled order (IPC 0.67 vs 0.0125), and exercise 2 below asks you to reproduce it with this sweep. That contrast, measured, is the entire argument for cache-friendly access. Confirm both with `perf stat -e LLC-load-misses,L1-dcache-load-misses` (Ch. 2): the chase shows miss-per-access at large sizes; the sequential scan does not.

---

## 8.4 Techniques

### 8.4.1 Sizing the working set to a cache level

The first and highest-leverage technique is simply: **know how big your hot working set is, and make it fit.** The sweep in §8.3 gives you the budget — the bytes that fit in L1, L2, L3 on *your* hardware — and the goal is to keep the data your hot path touches per event inside the smallest level you can.

Concretely, for the hot path:

- **Count the bytes touched per event**, not the total data size. A 1 GiB order book is fine *if* a single update touches only a few cache-resident lines (the top-of-book and the affected level). What kills you is a working *set* — the lines touched per event — that exceeds a cache level.
- **Shrink the per-event footprint.** Smaller structs (Ch. 11), hot/cold splitting so only hot fields are resident (Ch. 10), and removing pointer indirection (each pointer hop is a potential extra line and a prefetch-defeating dependency) all reduce the lines touched.
- **Block / tile** large sweeps so each block fits a cache level and is fully reused before moving on (the classic loop-blocking transform — turns a stream of capacity misses into cache hits). Relevant to batch/research code (Ch. 88) more than the tick path, but the principle — *reuse data while it's hot* (temporal locality) — is universal.
- **Mind L3 sharing.** L3 is shared across the socket (§8.2.1), so your *effective* L3 budget shrinks under noisy neighbors; core isolation (Ch. 6) does *not* protect it — partitioning the L3 with CAT/resctrl (Ch. 58) does.

The measurement loop: pick the data structure, measure its per-event footprint and its MPKI (Ch. 2), and if it's missing, shrink the footprint or improve locality until it fits.

### 8.4.2 Stride and locality for prefetcher friendliness

The second technique is arranging accesses so the **hardware prefetcher** (§8.2.4) and the **cache line** (§8.2.2) work *for* you:

- **Prefer sequential, forward, unit-stride access.** A `for (i) sum += a[i]` over a contiguous array is the prefetcher's ideal: it runs ahead and hides DRAM latency entirely. This is why a flat `std::vector`/array beats a `std::list`/node-based structure on the hot path by margins that dwarf any algorithmic constant (Ch. 32).
- **Use the whole line.** Lay data so that the 60 other bytes the cache fetched are bytes you'll actually use (spatial locality). This is the heart of **Struct-of-Arrays** vs **Array-of-Structs** (Ch. 10): if your loop touches one field across many objects, store that field contiguously so each fetched line is all useful, not 7/8 wasted.
- **Avoid pointer chasing on the hot path.** A data-dependent load chain defeats the prefetcher and exposes full miss latency every hop (§8.2.4, §8.3's chase). Where you must traverse links, consider flattening, indices-into-arrays instead of pointers, or intrusive contiguous storage (Ch. 32); where you genuinely can't, *software prefetch* the next node (Ch. 12) — but that's the fallback, not the goal.
- **Avoid pathological power-of-two strides/alignments** that cause conflict misses (§8.2.2, §8.5). Sometimes the fix is counterintuitive: pad a power-of-two row width to break the set-mapping pattern.

The unifying idea: **make your access pattern predictable and contiguous**, and the hardware will quietly turn an 80 ns DRAM cost into a ~1 ns L1 cost for free. Everything in Ch. 10–12 is an instance of this.

---

## 8.5 Pitfalls & anti-patterns: conflict misses, pointer chasing

- **Pointer chasing on the hot path.** The dominant anti-pattern. Linked lists, node-based maps/sets (`std::map`, `std::unordered_map`'s chains), trees of pointers — each hop is a data-dependent load that *defeats the prefetcher* and exposes full miss latency (§8.2.4, §8.3). Measured factors of 10–50× vs contiguous access are routine. Prefer flat, index-based, contiguous structures (Ch. 10, 32); this is the single biggest cache mistake in trading code.
- **Conflict (associativity) misses.** A loop striding by a large power of two, or a 2D array with a power-of-two row width, maps repeatedly to the same cache set and thrashes it *despite the cache having free space* (§8.2.2). Symptom: a benchmark that's mysteriously slow at "nice round" sizes and fast at "+1" sizes. Fix: pad to a non-power-of-two stride; confirm with cache-miss counters that don't match the capacity-miss model.
- **Ignoring the cache line / sub-line waste.** Touching one 4-byte field of a large struct in a tight loop fetches 64 bytes and uses 4 — 94% wasted bandwidth and cache. AoS where you needed SoA (Ch. 10). Symptom: high L1/L2 miss rate despite a "small" logical working set, because the *effective* footprint is 16× the useful data.
- **False sharing.** Two threads writing different fields that share a cache line cause MESI ping-pong (§8.2.3) — invisible in the source, catastrophic in scaling. Full treatment and the `alignas(64)` fix in Ch. 44; flagged here so the coherence mechanism is on your radar.
- **Trusting the average, again.** A cache effect is a *tail* effect: most accesses hit, the misses spike. A mean hides it; the p99.9 and the `LLC-miss` MPKI reveal it (Ch. 1–2). "It's fast on average" usually means "it hits in the benchmark's warm L1" — the §8.3 and Ch. 3 trap.
- **Benchmarking with an unrepresentative working set.** Measuring a hot loop over a 64-element array that lives in L1, when production touches 64 MiB — you measured the best case (Ch. 3 §3.2.1). Size the benchmark's working set to production, or you'll "optimize" a cache effect you never reproduced.

---

## 8.6 Exercises & checklist

**Exercises**

1. **Map your hierarchy.** Build and run `cache_sweep.cpp`, pinned with turbo off (Ch. 6). Plot ns/access vs size on a log x-axis. Where are the plateaus and cliffs? Compare the cliff sizes to your `lscpu` cache sizes (Ch. 6). Did you find L1, L2, L3, DRAM?
2. **Prefetcher on vs off.** Replace the random `shuffle` with the identity order (sequential chase) and re-run. How much does the DRAM-sized case speed up? Explain via §8.2.4. Confirm with `perf stat -e L1-dcache-load-misses,LLC-load-misses` that the sequential version misses far less.
3. **Provoke a conflict miss.** Write a loop summing `a[i]` with stride = 4096 bytes (power of two) over a large array, then with stride = 4096+64. Compare the times and the `LLC-load-misses`. Make several passes over a *fixed* element count so both versions touch the same number of lines. Why is the power-of-two stride slower even though it touches the same number of lines? (§8.2.2.)
4. **Line, not byte.** Sum every 16th `int` (one per cache line) vs every `int` sequentially, over the same array. The first touches 1/16 the data but isn't 16× faster — why? (You pay per *line*, §8.2.2.) Measure both.
5. **Footprint matters.** Take a struct with one hot field and many cold ones; sum the hot field across a large array (AoS). Now copy the hot field into its own array and sum that (SoA). Compare times and L1 miss rate. You've just motivated Ch. 10.

**Checklist — cache-aware hot path**

- [ ] I know my machine's **L1/L2/L3 sizes and latencies** (measured via §8.3, not assumed).
- [ ] I counted the **bytes/lines touched per event**, not total data size, and it fits the smallest cache level it can (Ch. 10–11 to shrink it).
- [ ] My hot-path access is **sequential/contiguous and prefetcher-friendly**; I removed pointer chasing where I could (Ch. 32).
- [ ] I **use the whole cache line** (SoA where the loop touches one field across objects — Ch. 10).
- [ ] I avoided **power-of-two strides/alignments** that cause conflict misses.
- [ ] I checked for **false sharing** on any line written by multiple threads (Ch. 44).
- [ ] I verified with **PMU cache counters** (L1/L2/LLC miss MPKI, Ch. 2) on a **production-sized working set** (Ch. 3) — and judged the **tail**, not the mean (Ch. 1).

---

## 8.7 References

- U. Drepper, *"What Every Programmer Should Know About Memory,"* 2007 — the canonical, exhaustive treatment of caches, lines, associativity, coherence, and prefetching; still the single best long-form reference for this chapter.
- J. L. Hennessy and D. A. Patterson, *Computer Architecture: A Quantitative Approach*, 6th ed. — the memory hierarchy, associativity, and coherence protocols from first principles.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — the actual cache sizes, latencies, associativities, and prefetcher behavior for Intel cores (the source for §8.2's numbers); AMD's *Software Optimization Guide* for AMD parts.
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture cache and prefetcher details, measured.
- lmbench (McVoy & Staelin) and the membench/pointer-chase methodology — the basis for the §8.3 latency sweep.

## 8.8 Additional Reading

- CppCon talks on data-oriented design and "mechanical sympathy" — Mike Acton, *"Data-Oriented Design and C++"* (CppCon 2014) and Stoyan Nikolov, *"OOP Is Dead, Long Live Data-oriented Design"* (CppCon 2018) — the practical mindset that Ch. 10 formalizes.
- B. Gregg, *Systems Performance*, 2nd ed. — measuring memory/cache behavior with `perf` and PMU in production.
- The `perf c2c` (cache-to-cache) tool documentation — pinpointing false sharing and HITM coherence traffic (Ch. 44).
- **Appendix E — Latency Numbers Every Trading Developer Should Know** — the consolidated, annotated cost table behind §8.1; and **Appendix A** for ARM/Graviton cache-line and hierarchy differences.

---

*Next: Ch. 9 — The DRAM Subsystem Below L3, which opens the box at the bottom of this chapter's diagram: channels, ranks, bank groups and the row buffer that make an L3 miss bimodal (row hit vs conflict), the loaded-latency curve that lets a capture writer on another core slow *your* misses with no counter changing, refresh stalls in the tail, and HBM/CXL memory as CPU-less NUMA nodes to keep off the hot path.*
