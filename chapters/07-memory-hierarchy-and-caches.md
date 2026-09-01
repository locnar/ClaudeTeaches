# Part II — CPU Microarchitecture

# Chapter 7 — The Memory Hierarchy & Caches

> **Prerequisites:** Part I (Ch. 1–6). You think in distributions (Ch. 1), can read PMU cache counters (Ch. 2 — `LLC-load-misses`, MPKI), benchmark honestly on a quiet, pinned, fixed-frequency box (Ch. 3, 6), and can read a hot loop in asm (Ch. 4). This chapter opens **Part II — CPU Microarchitecture** and is the foundation the rest of the part builds on.
>
> **Leads into:** Ch. 8 (data-oriented layout — AoS vs SoA) and Ch. 9 (object layout, alignment, padding) apply this directly; Ch. 10 (software prefetch & non-temporal stores) extends it; Ch. 15 (TLB & huge pages) and Ch. 16 (NUMA) handle translation and multi-socket effects. The false-sharing flip side of coherence is Ch. 33. Reference costs are consolidated in **Appendix E**.

---

## 7.1 Why it matters: DRAM is ~200× an L1 hit

Here is the single fact that governs more hot-path performance than any other in this book: **the CPU has not waited the same number of cycles for memory in over twenty years.** A core that retires several instructions per nanosecond must, on a last-level cache miss, *stop and wait roughly 50–100 nanoseconds* for DRAM to answer — on the order of **200 instructions' worth of idle time**, per miss. The arithmetic in your hot path is essentially free; **where your data lives, and in what order you touch it, is the program.**

Order-of-magnitude costs on a modern x86-64 server core (reference machine **Intel Xeon Gold 6326**, Ice Lake-SP; these are the numbers Appendix E develops and that you should commit to memory):

| Access | Latency | ≈ cycles @ ~3 GHz | Relative to L1 |
|---|---:|---:|---:|
| L1 data cache hit | ~1 ns (4–5 cyc) | 4–5 | 1× |
| L2 hit | ~4 ns | ~14 | ~4× |
| L3 (LLC) hit | ~15–20 ns | ~45–60 | ~15× |
| Local DRAM (LLC miss) | ~80 ns | ~240 | ~80× |
| NUMA-remote DRAM | ~130–140 ns | ~400 | ~130× |

(Some texts quote the L1-to-DRAM ratio as "~200×" using a faster core clock and a 1-cycle L1; the exact multiplier shifts with frequency, but the lesson — **two-plus orders of magnitude** — does not.)

This is why Ch. 1's tail is so often a *memory* tail. A hot path that hits L1 every time runs at a tight, predictable ~1 ns/access; the same code that occasionally misses to DRAM gets an 80 ns spike *per miss*, and a handful of those land you in the p99.9. Ch. 2's `perf_demo` already showed it: identical instructions, 16× the cycles, purely from access pattern. The order book (Ch. 25) lives or dies here — a book update that touches one cache-resident price level is a different animal from one that chases pointers across DRAM.

The good news is that the hardware is *trying to help you*: caches keep recently-used data close, and prefetchers guess what you'll need next. The bad news is that they help only if your **access pattern cooperates** — sequential, predictable, locally-clustered. The entire craft of cache-aware programming (Ch. 7–10) is arranging your data and your accesses so the hardware's bets pay off. This chapter builds the mental model; the next three spend it.

---

## 7.2 Mental model

### 7.2.1 Latencies by level (L1/L2/L3/DRAM)

Memory is a **hierarchy** of progressively larger, slower, cheaper levels, each a cache of the one below it. A modern core sees roughly:

```
   core ── L1d (32-48 KiB, ~1ns, per-core) ── L1i (instructions; Ch.11)
            │
            L2 (~1-2 MiB, ~4ns, per-core)
            │
            L3 / LLC (tens of MiB, ~15-20ns, SHARED across cores on the socket)
            │
            DRAM (local node ~80ns; remote NUMA node ~130ns+, Ch.15)
            │
            (… and far below: NVMe/disk, Ch.63 — irrelevant to the hot path)
```

Two structural facts shape everything:

- **Each level is roughly an order of magnitude larger and slower than the one above.** The hierarchy exists because fast memory is small and expensive; the design bet is that programs exhibit **locality**, so a small fast cache captures most accesses.
- **L1 and L2 are private to a core; L3 is shared across the socket.** This sharing is what makes inter-core communication possible (Ch. 34–35) and also what makes coherence traffic and *false sharing* (Ch. 33) a thing. It also means another busy core on the socket can evict your data from L3 — a noisy-neighbor tail source (Ch. 6, 16).

The two forms of locality the hierarchy rewards:

- **Temporal locality:** if you used a byte recently, you'll likely use it again soon — so keeping it cached pays. (Reuse your data while it's hot.)
- **Spatial locality:** if you used a byte, you'll likely use its neighbors soon — so the cache fetches a whole **line** (§7.2.2), not a byte. (Lay data out so the neighbors you fetched are the ones you want.)

Almost every technique in Ch. 7–10 is a way of manufacturing one of these two localities where your natural data structure didn't have it.

### 7.2.2 Cache lines, sets, and associativity

The cache does not deal in bytes. Its unit of transfer and tracking is the **cache line** — **64 bytes** on all current x86-64 (and the value of `std::hardware_constructive_interference_size`; ARM may use 128, Appendix A). Three consequences you must internalize:

1. **You always pay for 64 bytes.** Touch one `int`, and the hardware fetches the entire aligned 64-byte line containing it. If you'll use the other 60 bytes (sequential access), that fetch was an investment; if you won't (pointer-chasing, scattered access), it was 60 bytes of wasted bandwidth and a wasted cache slot. **Spatial locality is literally "use the rest of the line you already paid for."**
2. **Alignment matters.** A 64-byte object straddling two lines costs two line fetches and two cache slots; the same object aligned to a line costs one. This is the lever behind Ch. 9 (alignment & padding) and Ch. 33 (false sharing).
3. **Bandwidth is counted in lines.** When you reason about whether the prefetcher or the memory bus can keep up, the currency is lines/second, not bytes or elements.

**Associativity** governs *where* a line may live. A cache is divided into **sets**; a given memory address maps (by its middle address bits) to exactly one set, and within that set may occupy any of *N* **ways** (an *N-way set-associative* cache; e.g. L1 is often 8-way). The trap this creates is the **conflict miss**: if your access pattern repeatedly touches addresses that map to the *same set* — classically, striding by a large power of two (4 KiB, 2 MiB) — you can thrash a single set, evicting lines you still need, *even though the cache as a whole has plenty of free space elsewhere*. A power-of-two-strided loop or a power-of-two-sized 2D array can run far slower than a "+1" version for exactly this reason (§7.5, §7.3). The defense is to avoid pathological power-of-two strides and alignments — sometimes deliberately *padding* an array to a non-power-of-two row width.

### 7.2.3 Coherence and the MESI protocol

Because L1/L2 are private, the same cache line can sit in multiple cores' caches at once — so the hardware must keep them **coherent**: a write by one core must not leave another core reading a stale copy. x86 implements this with a snooping protocol, **MESI** (and extensions MOESI/MESIF), tracking each cached line in one of four states:

- **M (Modified):** this core has the only copy and has written it (dirty); memory is stale.
- **E (Exclusive):** this core has the only copy, clean (matches memory).
- **S (Shared):** multiple cores may hold clean copies.
- **I (Invalid):** the line is not valid here.

The rule that matters for latency: **a core that wants to *write* a line must first own it exclusively (M/E), which means *invalidating* every other core's copy.** When two cores repeatedly write the same line, they bounce ownership back and forth, each write forcing an invalidation and a cache-line transfer across the interconnect — a **HITM** ("hit-modified") event costing ~40–100+ ns (Appendix E). This is the mechanism behind:

- **False sharing** (Ch. 33): two threads writing *different variables that happen to share one 64-byte line* suffer this ping-pong even though they share no data logically — one of the nastiest and most invisible scalability killers.
- **Atomics and lock contention** (Ch. 30–34): every contended atomic is a coherence transaction; the cost of a spinlock or a CAS loop *is* coherence traffic.
- The reason **single-writer designs** (Ch. 35, 74) and **per-core data** are so valued: no sharing, no coherence ping-pong, no HITM tail.

For now the takeaway is conceptual: **shared writable data has a hardware cost measured in coherence traffic**, and a huge fraction of Part VI exists to minimize it.

### 7.2.4 The hardware prefetchers

The CPU doesn't only react to misses — it *predicts* them. Each core has several **hardware prefetchers** that watch your access stream and speculatively pull lines into L1/L2 *before* you ask, hiding DRAM latency entirely when they guess right:

- A **next-line / adjacent-line** prefetcher (the neighboring line).
- A **stride / streaming** prefetcher (detects constant-stride sequences — `a[i]`, `a[i+8]`, `a[i+16]`, … — and runs ahead).
- L2 **stream** prefetchers tracking multiple independent streams.

The crucial property: **prefetchers love regular, predictable, forward access** and are **useless against unpredictable, data-dependent access.** A sequential array scan is fully prefetched — you see near-L1 latency even on a DRAM-sized array, because the prefetcher stayed ahead. A **pointer chase** (each address depends on the value just loaded — a linked list, a hash chain, a tree of pointers) is the prefetcher's kryptonite: it cannot predict the next address until the current load *completes*, so every access is a full latency-exposed miss. This is precisely the `seq` vs `rand` gap in Ch. 2's `perf_demo`, and it is *the* argument for flat, array-based, contiguously-laid-out data structures over node-based ones (Ch. 25) on the hot path. When the hardware prefetcher can't help, you sometimes prefetch manually (Ch. 10) — but the first-line defense is an access pattern the hardware can predict.

---

## 7.3 Measure it: a latency-vs-stride / cache-size sweep

The classic experiment that makes the whole hierarchy *visible* is a **pointer-chase latency sweep** across working-set sizes — a self-contained variant of the membench / "lmbench latency" method. We build, for each size, a randomized cyclic pointer-chase within a buffer of that size, then measure average time per dependent load. Because each load depends on the previous, the prefetcher can't help and we measure *true access latency* at that size — which jumps each time the working set spills out of a cache level.

```cpp
// cache_sweep.cpp — reveal L1/L2/L3/DRAM by measuring pointer-chase latency vs size.
// Build: g++ -O2 -std=c++20 cache_sweep.cpp -o cache_sweep
// Run pinned with fixed frequency (Ch.3, 5):  taskset -c 2 ./cache_sweep
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
        auto t0 = std::chrono::steady_clock::now();
        for (std::size_t s = 0; s < steps; ++s) idx = next[idx];  // dependent load
        auto t1 = std::chrono::steady_clock::now();

        volatile std::uint32_t sink = idx; (void)sink;  // keep the chain live
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
        std::printf("%8zu KiB   %6.2f ns/access\n", kib, ns / steps);
    }
}
```

Representative output — reference machine **Xeon Gold 6326** (Ice Lake-SP: 48 KiB L1d, 1.25 MiB L2, ~24 MiB L3 per core domain), pinned, turbo off (illustrative; the *plateaus and cliffs* are the point, not the exact ns):

```
       4 KiB     1.10 ns/access     ┐
      32 KiB     1.15 ns/access     ├─ fits in L1d            (~1 ns plateau)
      64 KiB     4.10 ns/access     ┐
     512 KiB     4.30 ns/access     ├─ spilled to L2          (~4 ns plateau)
    2048 KiB    16.80 ns/access     ┐
   16384 KiB    19.50 ns/access     ├─ spilled to L3          (~17 ns plateau)
   65536 KiB    78.00 ns/access     ┐
  262144 KiB    82.00 ns/access     ├─ spilled to DRAM        (~80 ns plateau)
```

This single table *is* §7.2.1, measured. Read it the Ch. 1–2 way:

- **The plateaus reveal the cache sizes.** Latency is flat while the working set fits a level, then jumps at each capacity boundary — the cliff edges sit right at ~L1, ~L2, ~L3 sizes. You just measured your machine's cache hierarchy with a 20-line program.
- **The cliffs are the cost of a miss.** Each step up (1→4→17→80 ns) is the latency of the *next* level. The 80 ns DRAM plateau is the ~80× number from §7.1, in your own hands.
- **This is the *latency-exposed* worst case** (dependent pointer chase, no prefetch). Replace the chase with a *sequential* scan and the DRAM-sized case drops back toward L1-like throughput — because the prefetcher (§7.2.4) hides the latency. That contrast, measured, is the entire argument for cache-friendly access. Confirm both with `perf stat -e LLC-load-misses,L1-dcache-load-misses` (Ch. 2): the chase shows miss-per-access at large sizes; the sequential scan does not.

---

## 7.4 Techniques

### 7.4.1 Sizing the working set to a cache level

The first and highest-leverage technique is simply: **know how big your hot working set is, and make it fit.** The sweep in §7.3 gives you the budget — the bytes that fit in L1, L2, L3 on *your* hardware — and the goal is to keep the data your hot path touches per event inside the smallest level you can.

Concretely, for the hot path:

- **Count the bytes touched per event**, not the total data size. A 1 GiB order book is fine *if* a single update touches only a few cache-resident lines (the top-of-book and the affected level). What kills you is a working *set* — the lines touched per event — that exceeds a cache level.
- **Shrink the per-event footprint.** Smaller structs (Ch. 9), hot/cold splitting so only hot fields are resident (Ch. 8), and removing pointer indirection (each pointer hop is a potential extra line and a prefetch-defeating dependency) all reduce the lines touched.
- **Block / tile** large sweeps so each block fits a cache level and is fully reused before moving on (the classic loop-blocking transform — turns a stream of capacity misses into cache hits). Relevant to batch/research code (Ch. 67) more than the tick path, but the principle — *reuse data while it's hot* (temporal locality) — is universal.
- **Mind L3 sharing.** L3 is shared across the socket (§7.2.1), so your *effective* L3 budget shrinks under noisy neighbors; isolation (Ch. 6) protects it.

The measurement loop: pick the data structure, measure its per-event footprint and its MPKI (Ch. 2), and if it's missing, shrink the footprint or improve locality until it fits.

### 7.4.2 Stride and locality for prefetcher friendliness

The second technique is arranging accesses so the **hardware prefetcher** (§7.2.4) and the **cache line** (§7.2.2) work *for* you:

- **Prefer sequential, forward, unit-stride access.** A `for (i) sum += a[i]` over a contiguous array is the prefetcher's ideal: it runs ahead and hides DRAM latency entirely. This is why a flat `std::vector`/array beats a `std::list`/node-based structure on the hot path by margins that dwarf any algorithmic constant (Ch. 25).
- **Use the whole line.** Lay data so that the 60 other bytes the cache fetched are bytes you'll actually use (spatial locality). This is the heart of **Struct-of-Arrays** vs **Array-of-Structs** (Ch. 8): if your loop touches one field across many objects, store that field contiguously so each fetched line is all useful, not 7/8 wasted.
- **Avoid pointer chasing on the hot path.** A data-dependent load chain defeats the prefetcher and exposes full miss latency every hop (§7.2.4, §7.3's chase). Where you must traverse links, consider flattening, indices-into-arrays instead of pointers, or intrusive contiguous storage (Ch. 25); where you genuinely can't, *software prefetch* the next node (Ch. 10) — but that's the fallback, not the goal.
- **Avoid pathological power-of-two strides/alignments** that cause conflict misses (§7.2.2, §7.5). Sometimes the fix is counterintuitive: pad a power-of-two row width to break the set-mapping pattern.

The unifying idea: **make your access pattern predictable and contiguous**, and the hardware will quietly turn an 80 ns DRAM cost into a ~1 ns L1 cost for free. Everything in Ch. 8–10 is an instance of this.

---

## 7.5 Pitfalls & anti-patterns: conflict misses, pointer chasing

- **Pointer chasing on the hot path.** The dominant anti-pattern. Linked lists, node-based maps/sets (`std::map`, `std::unordered_map`'s chains), trees of pointers — each hop is a data-dependent load that *defeats the prefetcher* and exposes full miss latency (§7.2.4, §7.3). Measured factors of 10–50× vs contiguous access are routine. Prefer flat, index-based, contiguous structures (Ch. 8, 25); this is the single biggest cache mistake in trading code.
- **Conflict (associativity) misses.** A loop striding by a large power of two, or a 2D array with a power-of-two row width, maps repeatedly to the same cache set and thrashes it *despite the cache having free space* (§7.2.2). Symptom: a benchmark that's mysteriously slow at "nice round" sizes and fast at "+1" sizes. Fix: pad to a non-power-of-two stride; confirm with cache-miss counters that don't match the capacity-miss model.
- **Ignoring the cache line / sub-line waste.** Touching one 4-byte field of a large struct in a tight loop fetches 64 bytes and uses 4 — 94% wasted bandwidth and cache. AoS where you needed SoA (Ch. 8). Symptom: high L1/L2 miss rate despite a "small" logical working set, because the *effective* footprint is 16× the useful data.
- **False sharing.** Two threads writing different fields that share a cache line cause MESI ping-pong (§7.2.3) — invisible in the source, catastrophic in scaling. Full treatment and the `alignas(64)` fix in Ch. 33; flagged here so the coherence mechanism is on your radar.
- **Trusting the average, again.** A cache effect is a *tail* effect: most accesses hit, the misses spike. A mean hides it; the p99.9 and the `LLC-miss` MPKI reveal it (Ch. 1–2). "It's fast on average" usually means "it hits in the benchmark's warm L1" — the §7.3 and Ch. 3 trap.
- **Benchmarking with an unrepresentative working set.** Measuring a hot loop over a 64-element array that lives in L1, when production touches 64 MiB — you measured the best case (Ch. 3 §3.2.1). Size the benchmark's working set to production, or you'll "optimize" a cache effect you never reproduced.

---

## 7.6 Exercises & checklist

**Exercises**

1. **Map your hierarchy.** Build and run `cache_sweep.cpp`, pinned with turbo off (Ch. 6). Plot ns/access vs size on a log x-axis. Where are the plateaus and cliffs? Compare the cliff sizes to your `lscpu` cache sizes (Ch. 6). Did you find L1, L2, L3, DRAM?
2. **Prefetcher on vs off.** Replace the random `shuffle` with the identity order (sequential chase) and re-run. How much does the DRAM-sized case speed up? Explain via §7.2.4. Confirm with `perf stat -e L1-dcache-load-misses,LLC-load-misses` that the sequential version misses far less.
3. **Provoke a conflict miss.** Write a loop summing `a[i]` with stride = 4096 bytes (power of two) over a large array, then with stride = 4096+64. Compare the times and the `LLC-load-misses`. Why is the power-of-two stride slower despite touching *fewer* elements? (§7.2.2.)
4. **Line, not byte.** Sum every 16th `int` (one per cache line) vs every `int` sequentially, over the same array. The first touches 1/16 the data but isn't 16× faster — why? (You pay per *line*, §7.2.2.) Measure both.
5. **Footprint matters.** Take a struct with one hot field and many cold ones; sum the hot field across a large array (AoS). Now copy the hot field into its own array and sum that (SoA). Compare times and L1 miss rate. You've just motivated Ch. 8.

**Checklist — cache-aware hot path**

- [ ] I know my machine's **L1/L2/L3 sizes and latencies** (measured via §7.3, not assumed).
- [ ] I counted the **bytes/lines touched per event**, not total data size, and it fits the smallest cache level it can (Ch. 8–9 to shrink it).
- [ ] My hot-path access is **sequential/contiguous and prefetcher-friendly**; I removed pointer chasing where I could (Ch. 25).
- [ ] I **use the whole cache line** (SoA where the loop touches one field across objects — Ch. 8).
- [ ] I avoided **power-of-two strides/alignments** that cause conflict misses.
- [ ] I checked for **false sharing** on any line written by multiple threads (Ch. 33).
- [ ] I verified with **PMU cache counters** (L1/L2/LLC miss MPKI, Ch. 2) on a **production-sized working set** (Ch. 3) — and judged the **tail**, not the mean (Ch. 1).

---

## 7.7 References

- U. Drepper, *"What Every Programmer Should Know About Memory,"* 2007 — the canonical, exhaustive treatment of caches, lines, associativity, coherence, and prefetching; still the single best long-form reference for this chapter.
- J. L. Hennessy and D. A. Patterson, *Computer Architecture: A Quantitative Approach*, 6th ed. — the memory hierarchy, associativity, and coherence protocols from first principles.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — the actual cache sizes, latencies, associativities, and prefetcher behavior for Intel cores (the source for §7.2's numbers); AMD's *Software Optimization Guide* for AMD parts.
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture cache and prefetcher details, measured.
- lmbench (McVoy & Staelin) and the membench/pointer-chase methodology — the basis for the §7.3 latency sweep.

## 7.8 Additional Reading

- I. Wright / S. Robison and various CppCon talks on data-oriented design and "mechanical sympathy" (Mike Acton, *"Data-Oriented Design and C++,"* CppCon 2014) — the practical mindset that Ch. 8 formalizes.
- B. Gregg, *Systems Performance*, 2nd ed. — measuring memory/cache behavior with `perf` and PMU in production.
- The `perf c2c` (cache-to-cache) tool documentation — pinpointing false sharing and HITM coherence traffic (Ch. 33).
- **Appendix E — Latency Numbers Every Trading Developer Should Know** — the consolidated, annotated cost table behind §7.1; and **Appendix A** for ARM/Graviton cache-line and hierarchy differences.

---

*Next: Ch. 8 — Cache-Aware & Data-Oriented Design, where we turn this mental model into layout decisions: Array-of-Structs vs Struct-of-Arrays, hot/cold field splitting, and an order-book update loop that touches only the lines it needs.*
