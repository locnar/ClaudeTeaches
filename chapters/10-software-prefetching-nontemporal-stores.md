# Part II — CPU Microarchitecture

# Chapter 10 — Software Prefetching & Non-Temporal Stores

> **Prerequisites:** Ch. 7 (cache hierarchy, lines, and especially the **hardware prefetcher** of §7.2.4 — what it can and can't predict), Ch. 8 (layout), Ch. 9 (sizing). Ch. 2–3 for measuring, Ch. 4 for the asm in §10.5. This chapter is the **fallback** for when good layout and the hardware prefetcher *still* leave miss latency exposed.
>
> **Leads into:** Ch. 11 (pipelines/ILP — prefetch works by overlapping misses with compute) and Ch. 25 (the order book, where prefetching the next node/level is a real technique). Non-temporal stores tie to the write path of capture/logging (Ch. 71, 75). SIMD streaming stores connect to Ch. 29.

---

## 10.1 Why it matters: hiding miss latency the prefetcher won't

Chapters 7–9 were about *avoiding* cache misses by arranging data so the access pattern is contiguous and predictable. But some hot paths have an *irreducibly* irregular access pattern — and for those, the hardware prefetcher (Ch. 7 §7.2.4) is blind, because it can only follow regular strides. A hash-table probe, a tree descent, a scatter/gather over an index array, walking a chain of order references — each next address depends on a value you just loaded, so the hardware can't run ahead, and every access pays the full ~80 ns DRAM latency (Ch. 7 §7.1), exposed, on the critical path.

**Software prefetching** is the tool for exactly this gap. The idea is to exploit the fact that you, the programmer, often know the address you'll need *several iterations before you need it* — even when the hardware can't predict it. A hash lookup knows the bucket address the moment it has the key; a batch of order-book updates knows which levels it will touch before it touches them. By issuing a **prefetch hint** for that future address *now*, you start the DRAM fetch early and overlap its latency with the useful work of the current iteration — so by the time you actually dereference it, the line is already in cache. The 80 ns miss doesn't disappear; it gets *hidden behind work you were doing anyway* (this is why Ch. 11's instruction-level parallelism is the enabling mechanism).

The companion technique solves the opposite problem on the **write** path. When you produce a large block of data that you will *not* read back soon — a capture buffer, a log ring, a big memset/memcpy — writing it normally pulls every destination line into cache (and, for a partial write, *reads* it first), **evicting the hot working set you carefully fit into L1/L2 in Ch. 8–9.** **Non-temporal (streaming) stores** write straight to memory, bypassing the cache, so write-only bulk data doesn't pollute the cache your hot path depends on.

Both are **sharp, double-edged tools.** A well-placed prefetch can turn a pointer-chasing loop from memory-latency-bound to compute-bound — a multiple-x win. A *badly*-placed one wastes bandwidth, evicts useful lines, and runs *slower* than no prefetch at all (§10.6). The hardware prefetcher is already very good; software prefetch only wins where the hardware provably can't help, and only when measured. This chapter is about earning that win: when to reach for it, how far ahead to prefetch (§10.2.2 — the make-or-break parameter), and how to verify the hint actually emitted (§10.5).

---

## 10.2 Mental model

### 10.2.1 `__builtin_prefetch` / `_mm_prefetch` and locality hints

A software prefetch is a **hint instruction** that asks the CPU to begin loading a cache line into a chosen cache level, *without* stalling the pipeline and *without* producing a value. It is purely advisory — the hardware may ignore it — and it never faults (prefetching a bad address is a no-op, not a crash), which makes it safe to issue speculatively.

The portable spelling (GCC/Clang):

```cpp
__builtin_prefetch(addr, rw, locality);
//   rw:       0 = prepare to READ (default), 1 = prepare to WRITE
//   locality: 0 = no temporal locality (NTA)  ... 3 = high locality (keep in all levels)
```

or the Intel intrinsic `_mm_prefetch(addr, hint)` from `<xmmintrin.h>` with `_MM_HINT_T0/T1/T2/NTA`. The **locality hint** selects which cache level and eviction policy (verified codegen in §10.5):

- **`locality 3` → `prefetcht0`** — fetch into **all** cache levels (L1/L2/L3). Use when you will touch the line *very soon and repeatedly*.
- **`locality 2/1` → `prefetcht1`/`prefetcht2`** — fetch into L2/L3 but not L1. For data needed *somewhat* soon, to avoid evicting hotter L1 content.
- **`locality 0` → `prefetchnta`** (non-temporal) — fetch with minimal cache pollution (into a way that's quickly reused/bypasses L2), for data you'll touch *once* and not keep.
- **`rw=1` → `prefetchw`** — fetch the line in a writable (exclusive/M) coherence state, so a subsequent store doesn't pay a separate read-for-ownership (Ch. 7 §7.2.3). Use when you're prefetching something you're about to *write*.

The matching mental model: a prefetch is a *non-blocking, value-less load you issue early*. It occupies a **line-fill buffer (LFB / MSHR)** — and there are only ~10–12 of these per core, which caps how many misses can be in flight (memory-level parallelism, MLP, Ch. 11). Issuing too many prefetches saturates the LFBs and *stalls real loads* — one of the ways prefetch backfires (§10.6).

### 10.2.2 Prefetch distance and timeliness

The single parameter that decides whether a software prefetch helps or hurts is the **prefetch distance**: how many iterations *ahead* you prefetch. The goal is **timeliness** — the line should arrive *just before* you need it:

```
   prefetch too LATE  ──►  line arrives after you've already stalled on it. No benefit.
   prefetch JUST RIGHT ─►  line arrives exactly when you dereference it. Full hide. ✓
   prefetch too EARLY ──►  line arrives, then gets EVICTED before use (or wastes an LFB
                           and bandwidth). Worse than nothing.
```

The arithmetic is intuitive: you want to issue the prefetch roughly **`memory_latency / per_iteration_work`** iterations ahead. If a DRAM miss is ~80 ns and each loop iteration does ~10 ns of useful work, you need to prefetch ~8 iterations ahead so the fetch (started 8 iterations ago) completes right as you arrive. Too few iterations of lead and the fetch isn't done; too many and the line is evicted (or you've issued so many that LFBs/cache overflow).

```cpp
constexpr int PD = 8;  // prefetch distance — TUNE THIS PER LOOP, empirically (§9.3)
for (std::size_t i = 0; i < n; ++i) {
    __builtin_prefetch(&data[index[i + PD]], 0, 0);  // fetch the line I'll need PD iters later
    process(data[index[i]]);                          // ... while I work on this one
}
```

Crucially, **`PD` is not a constant you can derive on paper** — it depends on the loop's per-iteration work, the actual miss latency, the LFB pressure, and the hardware. You find it by **sweeping** (§10.3) and picking the value that minimizes the tail on *your* workload and *your* CPU. A `PD` that's perfect on one machine can be wrong on the next microarchitecture — which is a maintenance cost worth weighing (§10.6).

### 10.2.3 Streaming (non-temporal) stores and the write path

The write-path tool. A normal store to a not-yet-cached line triggers a **read-for-ownership (RFO)**: the CPU *reads* the line into cache (to merge your partial write and gain exclusive ownership), then marks it Modified. For bulk write-only output — a megabyte of capture data, a `memset`, a frame you'll DMA out and never re-read — this is doubly wasteful: it consumes read bandwidth you don't need, **and** it fills the cache with lines you'll never read, **evicting your hot working set** (Ch. 8–9's whole point).

**Non-temporal / streaming stores** (`movnti` for scalars, `vmovntps`/`vmovntdq` for SIMD — verified in §10.5) write directly toward memory through a **write-combining** buffer, **bypassing the cache hierarchy** (no RFO, no cache pollution). The trade-offs you must respect:

- They are **weakly ordered** on x86 (unlike normal stores' total store order) — you **must fence** (`_mm_sfence()`) before another thread, a DMA engine, or a dependent reader can rely on the data being visible (Ch. 30). Forgetting the fence is a correctness bug.
- They pay off only for **large, write-only, cache-line-granular** regions written sequentially (to fill write-combining buffers fully). For small or read-back-soon data they're a pessimization (you'd want the data *in* cache).
- They're most effective writing **full cache lines**; partial-line non-temporal stores are inefficient.

The mental model: normal store = "write and keep a hot copy"; streaming store = "write and forget, don't touch my cache." Use the latter exactly when the data is genuinely write-and-forget and large enough that protecting the cache matters.

---

## 10.3 Measure it: prefetch-distance sweep on a linked traversal

Software prefetch lives or dies by the distance, so the essential experiment is a **distance sweep** over a pointer-chase-like loop the hardware prefetcher can't help — here, summing values gathered through a random index array (a scatter/gather, like resolving order ids to records):

```cpp
// prefetch_sweep.cpp — find the prefetch distance that best hides miss latency.
// Build: g++ -O2 -std=c++20 -march=native prefetch_sweep.cpp -o prefetch_sweep
// Run pinned, turbo off (Ch.3,5): taskset -c 2 ./prefetch_sweep
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <numeric>
#include <random>
#include <vector>

int main() {
    constexpr std::size_t N = 64u * 1024 * 1024;   // 64M * 8B values = 512 MiB, >> L3
    std::vector<std::uint64_t> data(N, 1);
    std::vector<std::uint32_t> index(N);            // random gather order (prefetcher-hostile)
    std::iota(index.begin(), index.end(), 0u);
    std::shuffle(index.begin(), index.end(), std::mt19937(1));

    for (int PD : {0, 2, 4, 8, 16, 32, 64, 128}) {  // 0 == no software prefetch (baseline)
        auto t0 = std::chrono::steady_clock::now();
        std::uint64_t sum = 0;
        for (std::size_t i = 0; i < N; ++i) {
            if (PD && i + PD < N)
                __builtin_prefetch(&data[index[i + PD]], 0, 0);  // NTA: touch-once gather
            sum += data[index[i]];
        }
        auto t1 = std::chrono::steady_clock::now();
        volatile std::uint64_t sink = sum; (void)sink;
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
        std::printf("PD=%4d   %6.2f ns/elem\n", PD, ns / N);
    }
}
```

Representative output — reference machine **Xeon Gold 6326** (Ice Lake-SP), GCC 13 `-O2 -march=native`, pinned, turbo off (illustrative; the **shape** — a U-curve with a sweet spot — is the lesson, not the exact ns):

```
PD=   0    62.0 ns/elem     <- baseline: every gather is a latency-exposed DRAM miss
PD=   2    48.5 ns/elem     <- too close; fetch not finished in time, partial hide
PD=   4    31.0 ns/elem
PD=   8    19.5 ns/elem     <- sweet spot: ~3.2x faster than baseline
PD=  16    20.0 ns/elem
PD=  32    27.5 ns/elem     <- too far; lines evicted before use / LFB pressure
PD=  64    41.0 ns/elem
PD= 128    58.0 ns/elem     <- nearly back to baseline; prefetch wasted
```

Read it the Ch. 1 way:

- **There is a sweet spot, and it's a U-curve.** Distance too small → the fetch isn't done when you arrive (little benefit). Too large → the line is evicted before use, *and* you've burned bandwidth/LFBs (active harm). The minimum (~PD 8–16 here) is where arrival timing matches dereference — a **~3×** win over the un-prefetched gather.
- **`PD` is empirical and machine-specific.** Nothing on paper would have told you "8." You *found* it by sweeping, and the curve's position shifts with per-iteration work and CPU (§10.2.2). This is why §10.6 warns that a hardcoded `PD` is a portability liability.
- **Confirm the mechanism with the PMU** (Ch. 2): at the sweet spot, `cycle_activity.stalls_ mem_any` drops sharply while `L1-dcache-load-misses` stay similar (you didn't avoid the misses — you *overlapped* them with work). That distinction is the whole idea.

---

## 10.4 Techniques

### 10.4.1 Manual prefetch for irregular access

Where software prefetch earns its keep — patterns the hardware prefetcher (Ch. 7 §7.2.4) can't follow but *you* can anticipate:

- **Index/gather loops** (§10.3): you have the index array, so you know address `index[i+PD]` long before you need it. Prefetch it ahead. Common in scatter/gather over order-id → record tables.
- **Linked traversals you'll repeat or batch.** A single pointer chase can't prefetch its own next node (the address *is* the data you're waiting for). But if you process a **batch** of independent chains, you can interleave them so each chain's next-node prefetch overlaps another chain's work; or in a tree/skiplist, prefetch the *children you might descend into* before the comparison decides which. (Ch. 25 applies this to order-book structures.)
- **Hash probes:** prefetch the bucket line as soon as the hash is computed, before doing the key comparison and collision handling.
- **Software pipelining:** restructure a loop into "prefetch stage N+PD / process stage N" so there's always a fetch in flight (the §10.2.2 pattern). This is manual MLP (Ch. 11).

Guidance: prefetch the **line** (64-byte granularity — prefetching two addresses in the same line is wasted), match the **locality hint** to reuse (NTA for touch-once gathers, T0 for data you'll hammer), keep the number of in-flight prefetches under the LFB limit (~10), and **always A/B against no-prefetch on a production-sized working set** — if the hardware prefetcher already handles it, software prefetch only adds overhead.

### 10.4.2 Non-temporal stores to avoid cache pollution

Where streaming stores earn their keep — bulk, write-only, cache-line-granular output:

- **Capture / journaling / logging buffers** (Ch. 71, 75): you write packets or log records to a large ring you won't read back on the hot path. Stream them so they don't evict the hot order-book/strategy state from L1/L2.
- **Large `memcpy`/`memset` of write-only data** (zeroing a big arena you'll fault in lazily, staging a DMA buffer). `glibc`'s `memcpy`/`memset` already switch to non-temporal stores above a size threshold; your hand-rolled bulk copies should too.
- **Producer→consumer hand-off where the producer won't re-read** — though here weigh whether the consumer wants it *in* L3 (then a normal store is better). Streaming wins only when the writer truly won't reuse the cache line and protecting the hot set matters.

The rules from §10.2.3, operationally: write **full cache lines, sequentially**; use SIMD streaming stores (`_mm256_stream_si256` etc.) to fill write-combining buffers; and **emit an `_mm_sfence()` before** any reader/DMA/other-thread observes the data (Ch. 30) — the most common streaming-store bug is a missing fence. Measure that your hot working set's miss rate actually improved (Ch. 2) — that's the *point* of streaming, not the copy's own speed.

---

## 10.5 Verify the codegen: prefetch and `movnt` emission

Prefetch and non-temporal stores are **hints** the compiler can drop or alter, and the *locality hint* must map to the instruction you intended — so verify with the asm (Ch. 4).

**Prefetch hints → the right instruction** (verified, Clang `--target=x86_64-linux-gnu -O2 -march=x86-64-v3`):

```cpp
__builtin_prefetch(p, 0, 3);   // read,  high locality
__builtin_prefetch(p, 0, 0);   // read,  no  locality (NTA)
__builtin_prefetch(p, 1, 1);   // write intent
```
```asm
        prefetcht0   byte ptr [rdi]    ; locality 3  -> into all levels
        prefetchnta  byte ptr [rdi]    ; locality 0  -> non-temporal, minimal pollution
        prefetchw    byte ptr [rdi]    ; rw=1        -> fetch for write (needs PRFCHW; -mprfchw)
```

The thing to *check*: that locality `0` actually became `prefetchnta` (not `prefetcht0`) when you wanted touch-once semantics, and that a write-intent prefetch became `prefetchw` (it requires the `PRFCHW` ISA bit — without the right `-march`/`-mprfchw`, the compiler may silently emit a plain `prefetcht0`, costing you the read-for-ownership saving). The hint you wrote and the instruction you got are not always the same — confirm it.

**Non-temporal stores → `movnt*`** (verified):

```cpp
__builtin_nontemporal_store(v, p);            // scalar long long
__builtin_nontemporal_store(vec256, vptr);    // 256-bit vector
```
```asm
        movnti     qword ptr [rdi], rsi       ; scalar non-temporal store
        vmovntps   ymmword ptr [rdi], ymm0    ; 256-bit streaming store (write-combining)
```

The check here is that your streaming store **didn't get lowered back to a normal `mov`/ `vmovdqu`** (which would silently reintroduce cache pollution and RFO), and — separately — that you emitted the **`sfence`** you need for visibility (the compiler will *not* insert it for you; grep the asm for `sfence` after the streaming region). A streaming store that became a normal store, or one with no following fence, is the two classic codegen-level bugs of this technique. **If you can't see `movnt*` in the asm, you aren't streaming.**

---

## 10.6 Pitfalls & anti-patterns: when manual prefetch hurts; prefetch the wrong line

- **Prefetching what the hardware already handles.** The hardware prefetcher nails sequential/constant-stride access (Ch. 7 §7.2.4). Adding software prefetch there does *nothing useful* and *costs* instructions, decode bandwidth, and LFB slots — a net loss. Software prefetch is **only** for access the hardware can't predict. A/B it; if no-prefetch ties or wins, delete the prefetch.
- **Wrong distance.** The U-curve (§10.3): too short → no hide; too long → eviction before use plus wasted bandwidth. A guessed `PD` is usually wrong. Sweep and measure; re-sweep on new hardware.
- **Prefetch the wrong line / stale address.** Prefetching `&x` when you'll actually dereference `&y`, prefetching an address that's recomputed before use, or prefetching past the array bounds into another page (harmless to correctness — prefetch never faults — but wasted, and can trigger a spurious page walk). Prefetch the *exact* line you'll touch.
- **Too many prefetches in flight.** Only ~10–12 LFBs per core; flooding them with prefetches starves real demand loads and *adds* latency. Keep in-flight prefetches modest; more is not better.
- **Portability/maintenance cost.** A tuned `PD` and locality hint are microarchitecture-specific; they can pessimize the next CPU and rot silently (no compile error when they stop helping). Gate them behind measurement and re-validate per platform (Appendix A for ARM, where the prefetch instruction and tuning differ).
- **Streaming-store without a fence.** Non-temporal stores are weakly ordered; a reader/DMA that observes them before `_mm_sfence()` sees torn/stale data — a real correctness bug, not just slow (§10.2.3, Ch. 30).
- **Streaming stores for the wrong data.** Using `movnt` for data you (or the consumer) *will* read back soon forces it out of cache and then misses on the re-read — slower than a normal store. Streaming is for genuinely write-and-forget bulk output only.
- **Optimizing the mean, not the tail.** Prefetch's job is to cut the *miss* tail (Ch. 1); a mean can hide whether you helped the p99.9 or just shuffled cycles. Measure the distribution and the `stalls_mem_any` counter, not just average throughput.

---

## 10.7 Exercises & checklist

**Exercises**

1. **Find your sweet spot.** Build `prefetch_sweep.cpp`, run it pinned/turbo-off, and plot ns/elem vs `PD`. Where's the minimum on your CPU? How much faster than `PD=0`? Confirm with `perf stat -e cycle_activity.stalls_mem_any,L1-dcache-load-misses` that the misses stayed but the stalls dropped (you overlapped, not avoided).
2. **Hardware already wins.** Change `index` to the identity (sequential) order and re-run the sweep. Does *any* `PD` beat `PD=0` now? Explain why (the hardware prefetcher handles sequential — §10.6).
3. **Locality matters.** In the gather, change the hint from `0` (NTA) to `3` (T0) and re-measure on a working set that's reused across multiple passes vs touched once. When does T0 beat NTA, and vice versa (§10.2.1)?
4. **See the streaming store.** Write a loop that copies a large `long long` array using `__builtin_nontemporal_store` + `_mm_sfence()`. Confirm `movnti`/`vmovnt*` in the asm (§10.5). Then measure the *hot working set's* L2 miss rate with and without streaming (Ch. 2) — did streaming protect the cache?
5. **Break it.** Remove the `sfence` from a streaming producer feeding another thread; build a stress test (Ch. 40) that reads the buffer. Can you observe a torn/stale read? (Demonstrate the §10.6 correctness hazard — then fix it.)

**Checklist — software prefetch & non-temporal stores**

- [ ] I confirmed the access is **irregular / hardware-prefetcher-hostile** (otherwise software prefetch is pure overhead) by A/B-ing against no-prefetch.
- [ ] I **swept the prefetch distance** and chose the empirical minimum on **this** hardware and a **production-sized** working set — not a guessed constant.
- [ ] I prefetch the **exact line** I'll dereference, with a **locality hint** matching reuse (NTA for touch-once, T0 for hammered), and keep in-flight prefetches under the LFB limit.
- [ ] I verified the **emitted instruction** (`prefetcht0`/`nta`/`prefetchw`, `movnti`/`vmovnt*`) in the asm — the hint became what I intended (§10.5).
- [ ] Streaming stores are used **only** for large, write-only, full-line, sequential output, and I emit **`_mm_sfence()`** before any reader/DMA observes them (§10.2.3, Ch. 30).
- [ ] I measured the **right effect**: prefetch → `stalls_mem_any`↓ (overlap), streaming → hot-set miss rate↓ (no pollution), and judged the **tail**, not the mean.
- [ ] I documented the **portability cost** of any tuned distance/hint and gated it behind measurement for re-validation per platform (Appendix A).

---

## 10.8 References

- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — software prefetch guidance, `prefetcht0/1/2/nta`, `prefetchw`, non-temporal stores, write-combining, and the fencing requirements (the authoritative source for this chapter); AMD's optimization guide for AMD parts.
- U. Drepper, *"What Every Programmer Should Know About Memory,"* §7 — software prefetching, prefetch distance, and non-temporal access, with measurements.
- Intel *Intrinsics Guide* and GCC/Clang docs for `__builtin_prefetch`, `__builtin_nontemporal_store`, `_mm_prefetch`, `_mm_stream_*`, and `_mm_sfence`.
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — line-fill-buffer counts, MLP limits, and prefetch behavior per microarchitecture (the LFB-pressure caveat of §10.2.1).
- Hennessy & Patterson, *Computer Architecture: A Quantitative Approach* — memory-level parallelism and latency-hiding, the theory behind why prefetch works (Ch. 11).

## 10.9 Additional Reading

- M. Kobeissi / various CppCon talks on data-structure prefetching (e.g. prefetching in hash tables and B-trees) — practical §10.4.1 patterns.
- The `glibc` `memcpy`/`memset` source and its non-temporal-store size thresholds — a production example of §10.4.2.
- Ch. 11 (*CPU Pipelines & Execution*) — instruction-level and memory-level parallelism, the mechanism prefetch exploits; Ch. 25 (*…Building the Limit Order Book*) — prefetching real book structures; Ch. 29 (*SIMD*) — streaming-store intrinsics in context.
- **Appendix A** — ARM prefetch (`PRFM`) and non-temporal instructions, and why the tuned distance differs from x86.

---

*Next: Ch. 11 — CPU Pipelines & Execution, where we open up the out-of-order core itself: superscalar execution, hazards, and the dependency chains that decide whether your prefetch has work to overlap with — and why breaking the critical path is the next lever.*
