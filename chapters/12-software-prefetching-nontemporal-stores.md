# Part II — CPU Microarchitecture

# Chapter 12 — Software Prefetching & Non-Temporal Stores

> **Prerequisites:** Ch. 8 (cache hierarchy, lines, and especially the **hardware prefetcher** of §8.2.4 — what it can and can't predict), Ch. 10 (layout), Ch. 11 (sizing). Ch. 2–3 for measuring, Ch. 4 for the asm in §12.5. This chapter is the **fallback** for when good layout and the hardware prefetcher *still* leave miss latency exposed.
>
> **Leads into:** Ch. 13 (pipelines/ILP — prefetch works by overlapping misses with compute) and Ch. 32 (the order book, where prefetching the next node/level is a real technique). Non-temporal stores tie to the write path of capture/logging (Ch. 97, 107). SIMD streaming stores connect to Ch. 39.

---

## 12.1 Why it matters: hiding miss latency the prefetcher won't

Chapters 8–11 were about *avoiding* cache misses by arranging data so the access pattern is contiguous and predictable. But some hot paths have an *irreducibly* irregular access pattern — and for those, the hardware prefetcher (Ch. 8 §8.2.4) is blind, because it can only follow regular strides. A hash-table probe, a tree descent, a scatter/gather over an index array, walking a chain of order references — each next address depends on a value you just loaded, so the hardware can't run ahead, and every access pays the full ~80 ns DRAM latency (Ch. 8 §8.1), exposed, on the critical path.

**Software prefetching** is the tool for exactly this gap. The idea is to exploit the fact that you, the programmer, often know the address you'll need *several iterations before you need it* — even when the hardware can't predict it. A hash lookup knows the bucket address the moment it has the key; a batch of order-book updates knows which levels it will touch before it touches them. By issuing a **prefetch hint** for that future address *now*, you start the DRAM fetch early and overlap its latency with the useful work of the current iteration — so by the time you actually dereference it, the line is already in cache. The 80 ns miss doesn't disappear; it gets *hidden behind work you were doing anyway* (this is why Ch. 13's instruction-level parallelism is the enabling mechanism).

The companion technique solves the opposite problem on the **write** path. When you produce a large block of data that you will *not* read back soon — a capture buffer, a log ring, a big memset/memcpy — writing it normally reads (RFO) and allocates every destination line in cache — even when you overwrite the whole line — **evicting the hot working set you carefully fit into L1/L2 in Ch. 10–11.** **Non-temporal (streaming) stores** write straight to memory, bypassing the cache, so write-only bulk data doesn't pollute the cache your hot path depends on.

Both are **sharp, double-edged tools.** A well-placed prefetch can turn a pointer-chasing loop from memory-latency-bound to compute-bound — a multiple-x win. A *badly*-placed one wastes bandwidth, evicts useful lines, and runs *slower* than no prefetch at all (§12.6). The hardware prefetcher is already very good; software prefetch only wins where the hardware provably can't help, and only when measured. This chapter is about earning that win: when to reach for it, how far ahead to prefetch (§12.2.2 — the make-or-break parameter), and how to verify the hint actually emitted (§12.5).

---

## 12.2 Mental model

### 12.2.1 `__builtin_prefetch` / `_mm_prefetch` and locality hints

A software prefetch is a **hint instruction** that asks the CPU to begin loading a cache line into a chosen cache level, *without* stalling the pipeline and *without* producing a value. It is purely advisory — the hardware may ignore it — and it never faults (prefetching a bad address is a no-op, not a crash), which makes it safe to issue speculatively.

The portable spelling (GCC/Clang):

```cpp
__builtin_prefetch(addr, rw, locality);
//   rw:       0 = prepare to READ (default), 1 = prepare to WRITE
//   locality: 0 = no temporal locality (NTA)  ... 3 = high locality (keep in all levels)
```

or the Intel intrinsic `_mm_prefetch(addr, hint)` from `<xmmintrin.h>` with `_MM_HINT_T0/T1/T2/NTA`. The **locality hint** selects which cache level and eviction policy (verified codegen in §12.5):

- **`locality 3` → `prefetcht0`** — fetch into **all** cache levels (L1/L2/L3). Use when you will touch the line *very soon and repeatedly*.
- **`locality 2/1` → `prefetcht1`/`prefetcht2`** — fetch into L2/L3 but not L1. For data needed *somewhat* soon, to avoid evicting hotter L1 content.
- **`locality 0` → `prefetchnta`** (non-temporal) — fetch with minimal cache pollution (into a way that's quickly reused/bypasses L2), for data you'll touch *once* and not keep.
- **`rw=1` → `prefetchw`** — fetch the line in a writable (exclusive/M) coherence state, so a subsequent store doesn't pay a separate read-for-ownership (Ch. 8 §8.2.3). Use when you're prefetching something you're about to *write*.

The matching mental model: a prefetch is a *non-blocking, value-less load you issue early*. It occupies a **line-fill buffer (LFB / MSHR)** — and there are only ~10–12 of these per core, which caps how many misses can be in flight (memory-level parallelism, MLP, Ch. 13). Issuing too many prefetches saturates the LFBs and *stalls real loads* — one of the ways prefetch backfires (§12.6).

### 12.2.2 Prefetch distance and timeliness

The single parameter that decides whether a software prefetch helps or hurts is the **prefetch distance**: how many iterations *ahead* you prefetch. The goal is **timeliness** — the line should arrive *just before* you need it:

```
   prefetch too LATE  ──►  line arrives after you've already stalled on it. No benefit.
   prefetch JUST RIGHT ─►  line arrives exactly when you dereference it. Full hide. ✓
   prefetch too EARLY ──►  line arrives, then gets EVICTED before use (or wastes an LFB
                           and bandwidth). Worse than nothing.
```

The arithmetic is intuitive: you want to issue the prefetch roughly **`memory_latency / per_iteration_work`** iterations ahead. If a DRAM miss is ~80 ns and each loop iteration does ~10 ns of useful work, you need to prefetch ~8 iterations ahead so the fetch (started 8 iterations ago) completes right as you arrive. Too few iterations of lead and the fetch isn't done; too many and the line is evicted (or you've issued so many that LFBs/cache overflow).

```cpp
constexpr int PD = 8;  // prefetch distance — TUNE THIS PER LOOP, empirically (§12.3)
for (std::size_t i = 0; i < n; ++i) {
    if (i + PD < n)                                   // the prefetch never faults, but the
        __builtin_prefetch(&data[index[i + PD]], 0, 0);  // demand load index[i+PD] does: guard it
    process(data[index[i]]);                          // ... while I work on this one
}
```

(The guard matters: `prefetch` of a bad address is silently dropped, but `index[i + PD]` is an ordinary load that reads past the end of `index` for the last `PD` iterations — UB and a possible crash. Alternatively pad `index` by `PD` entries and drop the branch.)

Crucially, **`PD` is not a constant you can derive on paper** — it depends on the loop's per-iteration work, the actual miss latency, the LFB pressure, and the hardware. You find it by **sweeping** (§12.3) and picking the value that minimizes the tail on *your* workload and *your* CPU. A `PD` that's perfect on one machine can be wrong on the next microarchitecture — which is a maintenance cost worth weighing (§12.6).

### 12.2.3 Streaming (non-temporal) stores and the write path

The write-path tool. A normal store to a not-yet-cached line triggers a **read-for-ownership (RFO)**: the CPU *reads* the line into cache (to merge your partial write and gain exclusive ownership), then marks it Modified. For bulk write-only output — a megabyte of capture data, a `memset`, a frame you'll DMA out and never re-read — this is doubly wasteful: it consumes read bandwidth you don't need, **and** it fills the cache with lines you'll never read, **evicting your hot working set** (Ch. 10–11's whole point).

**Non-temporal / streaming stores** (`movnti` for scalars, `vmovntps`/`vmovntdq` for SIMD — verified in §12.5) write directly toward memory through a **write-combining** buffer, **bypassing the cache hierarchy** (no RFO, no cache pollution). The trade-offs you must respect:

- They are **weakly ordered** on x86 (unlike normal stores' total store order) — you **must fence** (`_mm_sfence()`) before another thread, a DMA engine, or a dependent reader can rely on the data being visible. Note the trap for a Ch. 41 reader: x86-TSO's ordering guarantee — and therefore a C++ release fence or release store, which compile to nothing / a plain `mov` — does **not** cover `movnt*` stores; only `sfence`, `mfence` or a `lock`-prefixed RMW (a `seq_cst` store or `fetch_add`) orders them. Publishing a streamed buffer through a release-store flag is exactly the bug. (Ch. 86 §86.4.1 meets the same rule for write-combining MMIO.) Forgetting the fence is a correctness bug.
- They pay off only for **large, write-only, cache-line-granular** regions written sequentially (to fill write-combining buffers fully). For small or read-back-soon data they're a pessimization (you'd want the data *in* cache).
- They're most effective writing **full cache lines**; partial-line non-temporal stores are inefficient.

The mental model: normal store = "write and keep a hot copy"; streaming store = "write and forget, don't touch my cache." Use the latter exactly when the data is genuinely write-and-forget and large enough that protecting the cache matters.

---

## 12.3 Measure it: prefetch-distance sweep on a dependent random gather; the hardware prefetchers off on the hot core

Software prefetch lives or dies by the distance, so the essential experiment is a **distance sweep** over a loop the hardware prefetcher can't help — here, summing values gathered through a random index array (a scatter/gather, like resolving order ids to records). One design point matters: the gather must be **dependent** — each load's address must wait for the previous load — or there is nothing for a prefetch to win. An *independent* gather (`sum += data[index[i]]`) is already overlapped by the out-of-order core up to the fill-buffer limit (~12 misses in flight on Ice Lake) and lands at ~15-20 ns/element with or without prefetching; the listing threads the result into every address (an opaque `+ (sum & zero)`) so that only the *prefetch* can run ahead:

```cpp
// prefetch_sweep.cpp — find the prefetch distance that best hides miss latency.
// Build: g++ -O2 -std=c++20 -march=native prefetch_sweep.cpp -o prefetch_sweep
// Run pinned, turbo off (Ch. 3, 6): taskset -c 2 ./prefetch_sweep
#include <algorithm>
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

    // `zero` is 0 at run time, but opaque to the compiler: adding (sum & zero) to each address
    // makes load i+1 DEPEND on load i (one miss at a time, like a pointer chase) while the
    // addresses stay known in advance — the only thing a software prefetch can exploit.
    std::uint64_t zero = 0;
    asm volatile("" : "+r"(zero));

    for (int PD : {0, 2, 4, 8, 16, 32, 64, 128}) {  // 0 == no software prefetch (baseline)
        auto t0 = std::chrono::steady_clock::now();
        std::uint64_t sum = 0;
        for (std::size_t i = 0; i < N; ++i) {
            if (PD && i + PD < N)
                __builtin_prefetch(&data[index[i + PD]], 0, 0);  // NTA: touch-once gather
            sum += data[index[i] + (sum & zero)];                // dependent gather
        }
        auto t1 = std::chrono::steady_clock::now();
        asm volatile("" :: "r"(sum));                            // keep the chain live
        double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
        std::printf("PD=%4d   %6.2f ns/elem\n", PD, ns / N);
    }
}
```

Representative output — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -march=native`, Linux 6.18, 4 KiB pages, pinned, turbo off (illustrative; the **shape** — a steep knee, then a plateau — is the lesson, not the exact ns):

```
PD=   0   110.0 ns/elem     <- baseline: every gather is a latency-exposed DRAM miss + page walk
PD=   2    58.0 ns/elem     <- 2 in flight: fetch not finished in time, partial hide
PD=   4    30.0 ns/elem
PD=   8    16.0 ns/elem     <- 8 misses in flight
PD=  16    11.0 ns/elem     <- knee: ~10x faster than baseline
PD=  32    10.0 ns/elem     ┐
PD=  64    10.0 ns/elem     ├─ plateau: ~12 fill buffers (and 2 page walkers) in flight — the core's MLP limit
PD= 128    10.5 ns/elem     ┘   (128 lines ahead = 8 KiB: nothing is evicted from a 48 KiB L1d)
```

Read it the Ch. 1 way:

- **There is a knee, then a plateau.** Distance too small → the fetch isn't done when you arrive (little benefit); the time per element is roughly latency ÷ PD until PD reaches the number of misses the core can keep in flight (~12 L1 fill buffers on Ice Lake, two page walkers for the TLB misses). Past that the prefetches queue behind each other and the curve goes flat at the **memory-level-parallelism floor** — ~10 ns/element, a **~10×** win over the dependent baseline. (The "too far — evicted before use" failure mode of §12.2.2 is real but needs a per-iteration footprint that makes `PD × 64 B` exceed the cache; at 8 KiB for PD = 128 this loop never triggers it.)
- **`PD` is empirical and machine-specific.** Nothing on paper would have told you "16." You *found* it by sweeping, and the knee moves with per-iteration work, miss latency and the fill-buffer count (§12.2.2). This is why §12.6 warns that a hardcoded `PD` is a portability liability.
- **Confirm the mechanism with the PMU** (Ch. 2): at the knee, `cycle_activity.stalls_mem_any` drops sharply while `L1-dcache-load-misses` stay similar (you didn't avoid the misses — you *overlapped* them). That distinction is the whole idea. And re-run with the `+ (sum & zero)` removed: the baseline itself falls to ~18 ns and every PD lands within a few ns of it — the out-of-order window was already doing the prefetcher's job.

**A second experiment: the hardware prefetchers off, on the hot core.** Everything above assumed the hardware prefetchers were *on* and doing nothing useful for a random gather. The complementary question — what they do to a hot core's **tail** when its working set *fits* — needs the toggle of §12.4.3 (an MSR write, which the test guest cannot do; the run below is representative for the reference machine): an L2-resident order book (Ch. 33's dense ladder, ~1 MiB) updated by a replayed feed at 200k msg/s on isolated core 2, while a replay process streams through 4 GiB on core 10 of the same socket, with `0x1A4` = `0x0` (all four core prefetchers on) and `0xF` (all off) on core 2 only:

```
   # prefetch_toggle.sh — hot core 2, L2-resident book, streaming neighbour on core 10; representative, Xeon Gold 6326 (Appendix L.3)
   $ wrmsr -p 2 0x1a4 0x0;  ./book_replay --core 2 --feed replay.bin & perf stat -C 2 -e l2_lines_out.useless_hwpf,l2_rqsts.all_pf,L1-dcache-load-misses sleep 30
   book update latency (ns): p50 212  p99 1,480  p99.9 6,900     l2_lines_out.useless_hwpf 41.2M   l2_rqsts.all_pf 58.9M   L1 misses 12.1M
   $ wrmsr -p 2 0x1a4 0xf;  (same)
   book update latency (ns): p50 219  p99   790  p99.9 2,100     l2_lines_out.useless_hwpf    0     l2_rqsts.all_pf    0   L1 misses 12.4M
```

The p50 moved 3 % the wrong way (the few sequential walks the book does lose their prefetch); the p99 halved and the p99.9 fell 3×. The mechanism is the `useless_hwpf` column: 41 M lines the L2 streamer fetched and evicted *without their ever being used*, each one pushing a line of the book out of L2 (and, with the neighbour's traffic, out of the L3 ways the hot core had left), so the book's real accesses missed to L3 and DRAM in the tail. With the prefetchers off, the book stays resident and the L1 miss count is unchanged — the demand misses were always the same; the prefetch traffic was the eviction pressure. The same effect, from the other direction, is Ch. 76 §76.4.2: hardware prefetch streams running through the DDIO region evict packets the NIC just delivered. This is the experiment to run before deciding the per-core policy §12.4.3 describes; the outcome depends on how much of the hot path is sequential (a SoA sweep — Ch. 10 — wants the streamer on; a pointer-chasing book wants it off), which is why it is a per-core, measured setting and not a BIOS default.

---

## 12.4 Techniques

### 12.4.1 Manual prefetch for irregular access

Where software prefetch earns its keep — patterns the hardware prefetcher (Ch. 8 §8.2.4) can't follow but *you* can anticipate:

- **Index/gather loops** (§12.3): you have the index array, so you know address `index[i+PD]` long before you need it. Prefetch it ahead. Common in scatter/gather over order-id → record tables.
- **Linked traversals you'll repeat or batch.** A single pointer chase can't prefetch its own next node (the address *is* the data you're waiting for). But if you process a **batch** of independent chains, you can interleave them so each chain's next-node prefetch overlaps another chain's work; or in a tree/skiplist, prefetch the *children you might descend into* before the comparison decides which. (Ch. 32 applies this to order-book structures.)
- **Hash probes:** prefetch the bucket line as soon as the hash is computed, before doing the key comparison and collision handling.
- **Software pipelining:** restructure a loop into "prefetch stage N+PD / process stage N" so there's always a fetch in flight (the §12.2.2 pattern). This is manual MLP (Ch. 13).

Guidance: prefetch the **line** (64-byte granularity — prefetching two addresses in the same line is wasted), match the **locality hint** to reuse (NTA for touch-once gathers, T0 for data you'll hammer), keep the number of in-flight prefetches under the LFB limit (~10), and **always A/B against no-prefetch on a production-sized working set** — if the hardware prefetcher already handles it, software prefetch only adds overhead.

### 12.4.2 Non-temporal stores to avoid cache pollution

Where streaming stores earn their keep — bulk, write-only, cache-line-granular output:

- **Capture / journaling / logging buffers** (Ch. 97, 107): you write packets or log records to a large ring you won't read back on the hot path. Stream them so they don't evict the hot order-book/strategy state from L1/L2.
- **Large `memcpy`/`memset` of write-only data** (zeroing a big arena you'll fault in lazily, staging a DMA buffer). `glibc`'s `memcpy`/`memset` already switch to non-temporal stores above a size threshold; your hand-rolled bulk copies should too.
- **Producer→consumer hand-off where the producer won't re-read** — though here weigh whether the consumer wants it *in* L3 (then a normal store is better). Streaming wins only when the writer truly won't reuse the cache line and protecting the hot set matters.

The rules from §12.2.3, operationally: write **full cache lines, sequentially**; use SIMD streaming stores (`_mm256_stream_si256` etc.) to fill write-combining buffers; and **emit an `_mm_sfence()` before** any reader/DMA/other-thread observes the data (Ch. 41) — the most common streaming-store bug is a missing fence. Measure that your hot working set's miss rate actually improved (Ch. 2) — that's the *point* of streaming, not the copy's own speed.

### 12.4.3 Controlling the hardware prefetchers: MSR 0x1A4, the uncore prefetchers and the per-core policy

The prefetchers are a per-core, runtime-switchable setting, and the common HFT practice is **off on the hot cores, on everywhere else** — decided by the §12.3 toggle experiment, never by default. What there is to switch:

- **Intel core prefetchers — `IA32_MISC_FEATURE_CONTROL`, MSR `0x1A4`** (Nehalem through Ice Lake/Sapphire/Emerald/Granite Rapids; documented in the SDM vol. 4 and the per-generation Intel tuning guides). Four bits, each **1 = disabled**: bit 0 the **L2 hardware (streamer) prefetcher** (the one that runs ahead of a stream and the usual culprit in the §12.3 result); bit 1 the **L2 adjacent-cache-line prefetcher** (fetches the other half of the 128-byte pair — the reason Ch. 44 §44.2 pads to 128 bytes; with this bit set, 64-byte padding is sufficient on that core); bit 2 the **DCU (L1) streamer** (next-line on ascending access); bit 3 the **DCU IP prefetcher** (per-instruction stride). Ice Lake-SP and later add the **L2 AMP** (adaptive multipath) prefetcher — bit 5 on Sapphire Rapids+; on Ice Lake it is folded into the streamer control — and Granite Rapids exposes the **LLC page prefetcher** separately (bit 6 on recent parts; check the generation's tuning guide). `0xF` is "all four core prefetchers off"; `0x0` the default.
- **Intel uncore prefetchers — BIOS only.** **LLC prefetch** (the CHA prefetching into L3 on a demand miss), **XPT prefetch** (extended prediction table: a speculative DRAM read issued in parallel with the L3 lookup — lowers idle DRAM latency, adds memory traffic that the §9.2.3 loaded curve pays for), **UPI prefetch** (the remote-socket version), and **DCU/L2 prefetcher** master switches with the same four bits as the MSR. These are per-socket firmware settings ("Processor Configuration → Prefetchers" on Dell/HPE/Supermicro, Appendix C.1); XPT and UPI prefetch default *on* and are worth measuring off on a box whose hot set is L2/L3-resident and whose memory channels carry a capture writer (Ch. 9 §9.4.1).
- **AMD Zen — BIOS switches, no public per-core MSR.** EPYC/Ryzen firmware exposes **L1 Stream HW Prefetcher**, **L1 Stride Prefetcher**, **L1 Region Prefetcher**, **L2 Stream HW Prefetcher** and **L2 Up/Down Prefetcher** ("CPU Common Options → Prefetcher settings"), socket-wide; the per-core MSR (`MSR_AMD64_DC_CFG`/`DE_CFG` bits) is model-specific and undocumented for this purpose — use the BIOS and accept that batch cores on the same socket lose the prefetchers too, or split by socket (Appendix Q.7).

The mechanics on Linux:

```
   # read / write the core prefetcher control on one CPU (msr-tools; the msr module; root — and NOT under kernel lockdown)
   $ modprobe msr && rdmsr -p 2 0x1a4                     # -> 0  (all on)
   $ wrmsr -p 2 0x1a4 0xf                                  # core 2: L2 streamer, adjacent-line, DCU streamer, DCU IP all OFF
   $ wrmsr -p 2 0x1a4 0x3                                  # core 2: only the two L2 prefetchers off (keep the L1 ones)
   $ for c in 0 1 8 9 10 11; do wrmsr -p $c 0x1a4 0x0; done   # housekeeping/batch cores: everything on
   # kernel >= 5.9 refuses MSR writes unless booted with msr.allow_writes=on (and logs each write); kernel lockdown (Secure Boot,
   # Ch. 100 §100.4.9) refuses them regardless — so the write belongs in a signed early-boot unit, before the trading unit (Ch. 102 §102.4.9)
   # verify at the gate (Appendix M.2): the MSR is per core and is reset by a reboot, a suspend, and some microcode loads
   $ for c in 2 3 4 5; do echo -n "cpu$c "; rdmsr -p $c 0x1a4; done
```

Three rules: **(1) per core, by role** — hot cores off (or the two L2 bits off — measure both), DDIO/RX-polling cores usually off (Ch. 76 §76.4.2), batch/replay/risk cores on, because a SoA sweep without a streamer is 2–3× slower; **(2) re-applied at every boot**, from the unit that runs before the trading process, and *verified* by `rdmsr` in the readiness gate (the setting does not survive a reboot, and a BIOS "prefetcher = enabled" setting re-enables it below the MSR on some firmware); **(3) measured with `useless_hwpf`** — if the §12.3 column is small on your hot core, the prefetchers are not hurting and the SoA parts of the path want them on. Record the per-core map in the host fingerprint (Appendix M.2) beside the C-state and governor settings.

---

## 12.5 Verify the codegen: prefetch and `movnt` emission

*Toolchain and machine: Appendix L.3.*

Prefetch and non-temporal stores are **hints** the compiler can drop or alter, and the *locality hint* must map to the instruction you intended — so verify with the asm (Ch. 4).

**Prefetch hints → the right instruction** (verified, GCC 13 and Clang 18, `-O2 -march=x86-64-v3` with and without `-mprfchw` — the psABI x86-64-v3 level does *not* include PRFCHW):

```cpp
__builtin_prefetch(p, 0, 3);   // read,  high locality
__builtin_prefetch(p, 0, 0);   // read,  no  locality (NTA)
__builtin_prefetch(p, 1, 1);   // write intent
```
```asm
; -O2 -march=x86-64-v3                (PRFCHW not in the v3 level)
        prefetcht0   byte ptr [rdi]    ; locality 3  -> into all levels
        prefetchnta  byte ptr [rdi]    ; locality 0  -> non-temporal, minimal pollution
        prefetcht2   byte ptr [rdi]    ; rw=1        -> SILENTLY demoted to a READ prefetch at the
                                       ;                requested locality (1 -> t2); no RFO saving
; -O2 -march=x86-64-v3 -mprfchw       (or -march=native on a PRFCHW part: Intel Broadwell+, all AMD)
        prefetchw    byte ptr [rdi]    ; rw=1        -> fetch for write (line arrives in E/M state)
```

The thing to *check*: that locality `0` actually became `prefetchnta` (not `prefetcht0`) when you wanted touch-once semantics, and that a write-intent prefetch became `prefetchw` (it requires the `PRFCHW` ISA bit — without `-mprfchw` or a `-march` that implies it, both GCC and Clang silently emit a *read* prefetch at the requested locality, `prefetcht0/1/2`, costing you the read-for-ownership saving). The hint you wrote and the instruction you got are not always the same — confirm it.

**Non-temporal stores → `movnt*`** (verified; GCC 13 and Clang 17, `-O2 -march=x86-64-v3`). `__builtin_nontemporal_store` is a **Clang-only** builtin — GCC has no equivalent, so the portable spelling on the book's default toolchain is the `<immintrin.h>` streaming intrinsics, which both compilers accept:

```cpp
#include <immintrin.h>
_mm_stream_si64(p, v);                        // scalar long long  (Clang only: __builtin_nontemporal_store(v, p))
_mm256_stream_ps(fptr, vec256);               // 256-bit float vector (integer data: _mm256_stream_si256)
```
```asm
        movnti     qword ptr [rdi], rsi       ; scalar non-temporal store
        vmovntps   ymmword ptr [rdi], ymm0    ; 256-bit streaming store (write-combining)
```

The check here is that your streaming store **didn't get lowered back to a normal `mov`/ `vmovdqu`** (which would silently reintroduce cache pollution and RFO), and — separately — that you emitted the **`sfence`** you need for visibility (the compiler will *not* insert it for you; grep the asm for `sfence` after the streaming region). A streaming store that became a normal store, or one with no following fence, is the two classic codegen-level bugs of this technique. **If you can't see `movnt*` in the asm, you aren't streaming.**

---

## 12.6 Pitfalls & anti-patterns: when manual prefetch hurts; prefetch the wrong line

- **Prefetching what the hardware already handles.** The hardware prefetcher nails sequential/constant-stride access (Ch. 8 §8.2.4). Adding software prefetch there does *nothing useful* and *costs* instructions, decode bandwidth, and LFB slots — a net loss. Software prefetch is **only** for access the hardware can't predict. A/B it; if no-prefetch ties or wins, delete the prefetch.
- **Wrong distance.** The knee-and-plateau curve (§12.3): too short → no hide; too long → (once the per-iteration footprint is large) eviction before use plus wasted bandwidth. A guessed `PD` is usually wrong. Sweep and measure; re-sweep on new hardware.
- **Prefetch the wrong line / stale address.** Prefetching `&x` when you'll actually dereference `&y`, prefetching an address that's recomputed before use, or prefetching past the array bounds into another page (harmless to correctness — prefetch never faults — but wasted, and can trigger a spurious page walk). Prefetch the *exact* line you'll touch.
- **Too many prefetches in flight.** Only ~10–12 LFBs per core; flooding them with prefetches starves real demand loads and *adds* latency. Keep in-flight prefetches modest; more is not better.
- **Portability/maintenance cost.** A tuned `PD` and locality hint are microarchitecture-specific; they can pessimize the next CPU and rot silently (no compile error when they stop helping). Gate them behind measurement and re-validate per platform (Appendix A for ARM, where the prefetch instruction and tuning differ).
- **Streaming-store without a fence.** Non-temporal stores are weakly ordered; a reader/DMA that observes them before `_mm_sfence()` sees torn/stale data — a real correctness bug, not just slow (§12.2.3). A `std::atomic` release store or `atomic_thread_fence(memory_order_release)` is *not* a substitute: on x86 they emit no fence and do not order non-temporal stores.
- **Streaming stores for the wrong data.** Using `movnt` for data you (or the consumer) *will* read back soon forces it out of cache and then misses on the re-read — slower than a normal store. Streaming is for genuinely write-and-forget bulk output only.
- **Prefetcher control that evaporated.** MSR 0x1A4 is per core and non-persistent: a reboot, a BIOS update, a microcode reload, or a `tuned` profile that writes the register itself puts the hot core's prefetchers back on — and the p99.9 back up — with no log line. Apply from a boot unit, verify with `rdmsr` at the gate, keep the per-core map in the fingerprint (§12.4.3). And the inverse: turning prefetchers off on the *batch* cores because "the hot core liked it" costs the replay job 2–3×.
- **Optimizing the mean, not the tail.** Prefetch's job is to cut the *miss* tail (Ch. 1); a mean can hide whether you helped the p99.9 or just shuffled cycles. Measure the distribution and the `stalls_mem_any` counter, not just average throughput.

---

## 12.7 Exercises & checklist

**Exercises**

1. **Find your sweet spot.** Build `prefetch_sweep.cpp`, run it pinned/turbo-off, and plot ns/elem vs `PD`. Where's the minimum on your CPU? How much faster than `PD=0`? Confirm with `perf stat -e cycle_activity.stalls_mem_any,L1-dcache-load-misses` that the misses stayed but the stalls dropped (you overlapped, not avoided).
2. **Hardware already wins.** Change `index` to the identity (sequential) order and re-run the sweep. Does *any* `PD` beat `PD=0` now? Explain why (the hardware prefetcher handles sequential — §12.6).
3. **Locality matters.** In the gather, change the hint from `0` (NTA) to `3` (T0) and re-measure on a working set that's reused across multiple passes vs touched once. When does T0 beat NTA, and vice versa (§12.2.1)?
4. **See the streaming store.** Write a loop that copies a large `long long` array using `_mm_stream_si64` (`<immintrin.h>`; on Clang, `__builtin_nontemporal_store` also works) + `_mm_sfence()`. Confirm `movnti`/`vmovnt*` in the asm (§12.5). Then measure the *hot working set's* L2 miss rate with and without streaming (Ch. 2) — did streaming protect the cache?
5. **Break it.** Remove the `sfence` from a streaming producer feeding another thread; build a stress test (Ch. 53) that reads the buffer. Can you observe a torn/stale read? (Demonstrate the §12.6 correctness hazard — then fix it.)

**Checklist — software prefetch & non-temporal stores**

- [ ] I confirmed the access is **irregular / hardware-prefetcher-hostile** (otherwise software prefetch is pure overhead) by A/B-ing against no-prefetch.
- [ ] I **swept the prefetch distance** and chose the empirical minimum on **this** hardware and a **production-sized** working set — not a guessed constant.
- [ ] I prefetch the **exact line** I'll dereference, with a **locality hint** matching reuse (NTA for touch-once, T0 for hammered), and keep in-flight prefetches under the LFB limit.
- [ ] I verified the **emitted instruction** (`prefetcht0`/`nta`/`prefetchw`, `movnti`/`vmovnt*`) in the asm — the hint became what I intended (§12.5).
- [ ] Streaming stores are used **only** for large, write-only, full-line, sequential output, and I emit **`_mm_sfence()`** (not merely a C++ release store) before any reader/DMA observes them (§12.2.3).
- [ ] I measured the **right effect**: prefetch → `stalls_mem_any`↓ (overlap), streaming → hot-set miss rate↓ (no pollution), and judged the **tail**, not the mean.
- [ ] I documented the **portability cost** of any tuned distance/hint and gated it behind measurement for re-validation per platform (Appendix A).

---

## 12.8 References

- Intel, *64 and IA-32 Architectures Optimization Reference Manual* — software prefetch guidance, `prefetcht0/1/2/nta`, `prefetchw`, non-temporal stores, write-combining, and the fencing requirements (the authoritative source for this chapter); AMD's optimization guide for AMD parts.
- U. Drepper, *"What Every Programmer Should Know About Memory,"* Sections 6.1 (bypassing the cache — non-temporal access) and 6.3.2 (software prefetching, prefetch distance), with measurements.
- Intel *Intrinsics Guide* and GCC/Clang docs for `__builtin_prefetch`, `__builtin_nontemporal_store` (Clang only), `_mm_prefetch`, `_mm_stream_*`, and `_mm_sfence`.
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — line-fill-buffer counts, MLP limits, and prefetch behavior per microarchitecture (the LFB-pressure caveat of §12.2.1).
- Hennessy & Patterson, *Computer Architecture: A Quantitative Approach* — memory-level parallelism and latency-hiding, the theory behind why prefetch works (Ch. 13).

## 12.9 Additional Reading

- M. Kulukundis, *"Designing a Fast, Efficient, Cache-friendly Hash Table, Step by Step"* (CppCon 2017) — group prefetching in Swiss tables; S. Chen, P. Gibbons, T. Mowry, *"Improving Index Performance through Prefetching"* (SIGMOD 2001) — prefetching B-trees: the practical §12.4.1 patterns.
- The `glibc` `memcpy`/`memset` source and its non-temporal-store size thresholds — a production example of §12.4.2.
- Ch. 13 (*CPU Pipelines & Execution*) — instruction-level and memory-level parallelism, the mechanism prefetch exploits; Ch. 32 (*…Building the Limit Order Book*) — prefetching real book structures; Ch. 39 (*SIMD*) — the vector registers and `<immintrin.h>` intrinsics the streaming stores here operate on; Ch. 86 §86.4.1 — the same weak-ordering-plus-`sfence` discipline for write-combining MMIO.
- **Appendix A** — the ARM port: a tuned distance and hint must be re-measured there (the ISA spellings are `PRFM` and the non-temporal `LDNP`/`STNP` pair, with a weaker memory model that changes the fencing story).

---

*Next: Ch. 13 — CPU Pipelines & Execution, where we open up the out-of-order core itself: superscalar execution, hazards, and the dependency chains that decide whether your prefetch has work to overlap with — and why breaking the critical path is the next lever.*
