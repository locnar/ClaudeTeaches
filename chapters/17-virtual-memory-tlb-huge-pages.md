# Part II — CPU Microarchitecture

# Chapter 17 — Virtual Memory, the TLB & Huge Pages

> **Prerequisites:** Ch. 8 (the memory hierarchy — the TLB is a cache, and a page-table walk is *itself* a sequence of memory accesses that hit or miss those same caches), Ch. 10–11 (data layout — footprint and access pattern drive TLB pressure), Ch. 14 (the I-TLB side, for hot *code*), Ch. 2–3 (top-down and benchmarking — §17.3).
>
> **Leads into:** Ch. 18 (NUMA — page placement is *where* a translated physical page lives, on which node), Ch. 30 (memory-management fundamentals — page faults, first-touch, the cost of growing the working set), Ch. 34 (`mmap`/`mlock`/pre-faulting — the syscall-level controls for the pages this chapter measures). Explicit huge pages and `mlockall` reappear in the system-tuning checklist (Appendix C) and hot-path warming (Ch. 60).

---

## 17.1 Why it matters: TLB misses tax every memory access

Every load and store in your program uses a **virtual** address that the hardware must translate to a **physical** one before it can touch a cache or DRAM. That translation is not free: it's a lookup in a small per-core cache called the **TLB** (Translation Lookaside Buffer), and when the address you need isn't in the TLB, the CPU runs a **page-table walk** — a multi-level pointer chase through in-memory page tables (four levels on x86-64, five with LA57) that can itself miss in the data caches. A TLB miss that walks all the way to DRAM can cost *hundreds* of cycles, and it happens *in addition to* whatever cache/DRAM cost the actual data access incurs. Translation is a tax levied on every memory reference; most of the time the TLB hides it, but when your working set is large or scattered, the tax becomes visible — and, worse, *variable*, landing in the tail (Ch. 1).

This matters for low-latency code because the standard 4 KB page is *small* relative to modern working sets. A core's L1 data-TLB holds on the order of **64 entries**; at 4 KB/page that covers just **256 KB** of memory before you start missing — far smaller than the multi-megabyte order books, market-data ring buffers, and tick caches a trading system keeps hot. The second-level (L2/STLB) TLB extends the reach to a few thousand entries (a handful of MB), but a feed handler streaming a large book, or a strategy walking a big hash map (Ch. 32), routinely exceeds it. The result is a steady drip of TLB misses that don't show up as *cache* misses (the data may be in L2/L3) yet still cost you — an invisible bottleneck unless you measure the TLB counters specifically (§17.3).

The fix is **huge pages**: instead of 4 KB pages, map memory in **2 MB** (or **1 GB**) chunks, so one TLB entry covers 512× (or 256K×) more memory. The L1 dTLB's 2 MB pool (32 entries on Ice Lake-SP, beside the 64 entries for 4 KB pages) reaches **64 MB** where the 4 KB entries reached 256 KB, and the STLB's ~1024 2 MB-capable entries reach **~2 GB** where 4 KB pages reached 8 MB — enough to cover a large book, a capture buffer, or the hot code segment (Ch. 14) with near-zero TLB misses. Huge pages are one of the highest-leverage, lowest-effort wins for a large-footprint hot path, and they come in two flavors — *transparent* (automatic, convenient, with a jitter caveat) and *explicit* (reserved, deterministic, the HFT default) — whose trade-offs §17.4 makes precise. As always, measure first (Ch. 2): if your hot working set fits comfortably in the TLB's reach, this chapter's techniques buy nothing; if `dtlb_load_misses` is elevated, they can be transformative.

---

## 17.2 Mental model

### 17.2.1 Address translation and the page-table walk

Virtual memory gives each process its own flat address space; the hardware (the MMU) maps virtual pages to physical frames through **page tables** the OS maintains. On x86-64 with 4 KB pages, a virtual address is split into a page offset (low 12 bits) and four 9-bit indices, one per page-table level:

```
   virtual addr:  [ 9 bits | 9 bits | 9 bits | 9 bits | 12-bit offset ]
                     PML4      PDPT      PD       PT
                       │         │        │        │
   CR3 ─► PML4[idx] ─► PDPT[idx] ─► PD[idx] ─► PT[idx] ─► physical frame  + offset
          (a load)     (a load)    (a load)   (a load)
```

The page-table **walk** is a dependent chain of **four memory loads** (each level's entry gives the physical address of the next level's table), ending in the physical frame number. Two things follow that matter enormously for latency:

- **A walk is itself memory traffic.** Each of the four levels is a load that hits or misses the *data caches* (Ch. 8). A "warm" walk where the upper levels are cached costs maybe a handful of cycles per level; a "cold" walk that misses to DRAM at multiple levels costs *hundreds* of cycles. The hardware caches page-table entries (page-walk caches / paging-structure caches) precisely because walks are expensive.
- **Huge pages short-circuit the walk.** A **2 MB page** stops the walk at the PD level (the PD entry points directly at a 2 MB frame — three levels, not four) and, more importantly, *one TLB entry now covers 2 MB*. A **1 GB page** stops at the PDPT level (two levels) and one entry covers 1 GB. Fewer levels *and* vastly more coverage per entry.

The translation result — the virtual→physical mapping plus permission bits — is what gets cached in the TLB so the walk can be skipped next time. (Caches are physically tagged, so translation logically precedes the cache lookup; in practice the L1 overlaps them, but a TLB miss still stalls the access.)

### 17.2.2 TLB structure and miss cost

The TLB is a cache *of translations*, with the same structure ideas as the data cache (Ch. 8) — entries, associativity, levels — but indexed by virtual page number:

- **It's hierarchical and split.** A typical modern core has a small, fast **L1 TLB** split into **iTLB** (instruction, Ch. 14) and **dTLB** (data) — tens of entries each, often with separate sub-pools for 4 KB and 2 MB pages — backed by a larger unified **L2 TLB (STLB)** of ~1.5–2K entries. Representative Ice Lake-SP (Sunny Cove) numbers: dL1-TLB 64 entries for 4 KB pages, 32 for 2 MB/4 MB, 8 for 1 GB; STLB 2048 entries, all usable by 4 KB pages but at most ~1024 by 2 MB (or 1 GB) pages.
- **Reach, not just count, is the metric.** What you care about is **coverage** = entries × page size. Each entry reaches 512× further with 2 MB pages (the 2 MB pools are smaller, so total reach grows ~256×):

  ```
                       4 KB pages              2 MB pages
   dTLB (64×4K/32×2M)  64 × 4 KB  = 256 KB     32 × 2 MB  = 64 MB
   STLB (2048/≤1024)   2048 × 4 KB = 8 MB       ~1024 × 2 MB = ~2 GB
  ```

  A working set that overflows the 4 KB reach (8 MB at the STLB) but fits the 2 MB reach (~2 GB) goes from "TLB-miss-bound" to "TLB-resident" with no code change.
- **Miss cost is the walk.** An L1-TLB miss that hits the STLB is cheap (a few cycles). An STLB miss triggers the full page-table walk (§17.2.1): cheap if the page-walk caches are warm, *hundreds of cycles* if the walk misses to DRAM. Crucially, a TLB miss is **independent of** whether the *data* is cached — you can have the data hot in L2 and still pay a 100+-cycle translation miss, which is why TLB problems hide from cache-miss counters and need their own (§17.3).
- **What thrashes the TLB.** *Large* working sets (more pages than reach) and *scattered* access (touching many distinct pages with poor locality — pointer-chasing node containers, random hash probes, huge sparse tables). *Sequential* access over a large region still touches many pages but the hardware prefetches translations well; *random* access over the same region is far worse. This is the same locality story as Ch. 8–10, one level up: the TLB rewards dense, contiguous, predictable page access.

The model to carry: **the TLB is a tiny cache whose "lines" are whole pages; its reach is your real budget for hot memory, and huge pages multiply that reach by two orders of magnitude (512× per entry, ~256× in total on Ice Lake-SP).** When the working set exceeds the reach, translation — not data — can become the bottleneck.

---

## 17.3 Measure it: `dtlb_load_misses` with and without huge pages

The experiment isolates translation cost from data cost: **randomly** probe a large region (so the data access pattern and total bytes touched are identical) under two page regimes — default 4 KB pages vs 2 MB huge pages — and watch the TLB counters move while the cache behavior stays put. Random access defeats the translation prefetcher and maximizes the TLB's visibility. The probe is made **dependent** (each address waits for the previous value) so the loop measures the *latency* of one translation plus one data access — the number a pointer-chasing hot path pays — rather than the out-of-order core's throughput over a dozen overlapped misses, which hides most of a walk.

```cpp
// tlb.cpp — random probes over a large region; compare 4K vs 2M pages.
// Build: g++ -O2 -std=c++20 -march=native tlb.cpp -o tlb
// Run (4K; the code opts the region out of THP with madvise(MADV_NOHUGEPAGE)):  taskset -c 2 ./tlb
//   To test huge pages, allocate the buffer on explicit 2M pages (mmap MAP_HUGETLB below).
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>
#include <chrono>
#include <sys/mman.h>

int main(int argc, char** argv) {
    bool huge = (argc > 1) && std::strcmp(argv[1], "huge") == 0;
    constexpr std::size_t BYTES = std::size_t(1) << 30;     // 1 GiB: far exceeds 4K TLB reach
    constexpr std::size_t N = BYTES / sizeof(std::uint64_t);
    constexpr std::size_t PROBES = 50'000'000;

    int flags = MAP_PRIVATE | MAP_ANONYMOUS | (huge ? MAP_HUGETLB : 0);   // 2M pages if huge
    auto* a = static_cast<std::uint64_t*>(
        mmap(nullptr, BYTES, PROT_READ | PROT_WRITE, flags, -1, 0));
    if (a == MAP_FAILED) { std::perror("mmap"); return 1; }
    // Keep the 4K baseline on 4K pages: with /sys/kernel/mm/transparent_hugepage/enabled = always
    // a 1 GiB anonymous region would otherwise get 2 MB pages at fault time and the comparison collapses.
    if (!huge && madvise(a, BYTES, MADV_NOHUGEPAGE) != 0) { std::perror("madvise"); return 1; }
    std::memset(a, 1, BYTES);                                // pre-fault every page (Ch. 30, 34)

    std::mt19937_64 rng(12345);
    std::vector<std::uint32_t> idx(PROBES);
    for (auto& v : idx) v = std::uint32_t(rng() % N);        // precompute random indices

    // `zero` is 0 at run time but opaque to the compiler: adding (acc & zero) to each address makes
    // probe k+1 DEPEND on probe k, so the loop measures the LATENCY of one translation + one data
    // access, not the out-of-order core's throughput over ~10 overlapped misses and two page walkers.
    std::uint64_t zero = 0;
    asm volatile("" : "+r"(zero));

    auto t0 = std::chrono::steady_clock::now();
    std::uint64_t acc = 0;
    for (std::size_t k = 0; k < PROBES; ++k) acc += a[idx[k] + (acc & zero)];   // random, DEPENDENT probe
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-4s acc=%llu  %.2f ns/probe\n", huge ? "2M" : "4K",
                (unsigned long long)acc, ns / PROBES);
    munmap(a, BYTES);
    return 0;
}
```

Profile both with the dTLB counters: `perf stat -e cycles,instructions,dtlb_load_misses.miss_causes_a_walk,dtlb_load_misses.walk_active,dtlb_load_misses.walk_completed,L1-dcache-load-misses ./tlb` (run `./tlb` and `./tlb huge`; the `huge` run needs reserved hugepages — `sysctl vm.nr_hugepages=...` or a hugetlbfs mount; see §17.4.1). Confirm the baseline really is on 4 KB pages: while `./tlb` runs, `grep AnonHugePages /proc/$(pidof tlb)/smaps_rollup` must read `0 kB` — the `madvise(MADV_NOHUGEPAGE)` opt-out in the listing is what guarantees that on a box whose `transparent_hugepage/enabled` is `always`. Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, 2.9 GHz), GCC 13 `-O2 -std=c++20 -march=native`, Linux 6.18 (THP = `madvise`), pinned, turbo off (illustrative; the *direction* is the point):

```
                                          4K pages           2M huge pages
ns / probe (dependent)                     ~115 ns            ~92 ns        <- ~23 ns = the walk, ~1.25x
dtlb_load_misses.walk_completed            ~1 per probe       ~0            <- the whole story
dtlb_load_misses.walk_active (cycles)      ~20% of cycles     ~0
L1-dcache-load-misses                      ~same              ~same         <- DATA cost unchanged
instructions                               same               same          <- identical work
```

Read it the Ch. 2 way: **same instructions, same data-cache misses (the data access pattern is identical) — yet ~23 ns less per probe, entirely from collapsed `dtlb_load_misses`.** The 1 GiB region needs ~256K distinct 4 KB pages — wildly beyond the dTLB/STLB reach — so nearly every random probe pays a page-table walk; the data itself was in DRAM either way (~90 ns), so this cost is *pure translation*, invisible in `L1-dcache-load-misses`. The walk is "only" ~23 ns here because the 2 MiB of page-table entries for 1 GiB still live in L3 (the upper levels in the paging-structure caches); on a 32 GiB book the PTEs themselves miss to DRAM and a walk costs a second ~90 ns round-trip — the Appendix E range "~10-100+ ns" is this spread. Switching to 2 MB pages cuts the page count 512× (to ~512 pages, comfortably within STLB reach), and the walks — and the cycles spent in `walk_active` — vanish. (Remove the `+ (acc & zero)` and both rows fall to ~20 and ~10 ns: the core overlaps ~10 misses, but only two page walks, at a time — the 4K row loses *more* MLP, which is how translation cost shows up in a throughput-bound loop.) That is the TLB-bound signature: **high `dtlb_load_misses.walk_*` with unchanged data-cache behavior.** When you see it, huge pages and/or better page-locality (§17.4) are the lever; if those counters are already low, the TLB is not your problem.

---

## 17.4 Techniques

### 17.4.1 Transparent vs explicit huge pages

There are two ways to get 2 MB pages, and the choice is a classic latency trade-off between *convenience* and *determinism*.

**Transparent Huge Pages (THP)** — the kernel *automatically* promotes eligible anonymous mappings to 2 MB pages in the background, with no application change. Controlled via `/sys/kernel/mm/transparent_hugepage/enabled` (`always` / `madvise` / `never`) and steerable per-region with `madvise(MADV_HUGEPAGE)`.

- **Pros:** zero code change, no up-front reservation, works for the common case.
- **Cons — and why HFT is wary:** with `enabled=always` most huge pages are allocated *synchronously, in the page fault* of the touching thread, and `/sys/kernel/mm/transparent_hugepage/defrag` decides whether that fault may run **direct compaction/reclaim** to find 2 MB of contiguous memory — the dominant THP tail-latency source; separately, the `khugepaged` daemon **collapses** runs of 4 KiB pages into huge pages in the background (scan + copy, briefly stalling the owner). Either can land in the middle of the hot path at unpredictable times — a textbook source of tail-latency jitter (Ch. 1) on an otherwise-quiet box. A 2 MB page also faults in all at once (a larger first-touch stall — Ch. 30). THP is great for throughput batch jobs; on a jitter-sensitive hot path it's often set to `madvise` (opt-in per region) or `never`, with explicit pages used instead.

**Explicit huge pages (hugetlbfs / `MAP_HUGETLB`)** — pages **reserved at boot or via sysctl** (`vm.nr_hugepages`, or `hugeadm`), then requested explicitly via `mmap(..., MAP_HUGETLB, ...)`, a hugetlbfs file mapping, or `shmget(SHM_HUGETLB)`. 1 GB pages take `MAP_HUGETLB | MAP_HUGE_1GB`; reserve them at boot (`hugepagesz=1G hugepages=N`) for reliability — runtime allocation through `/sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages` works (Linux 3.16+) but needs 1 GB of physically contiguous memory, which `hugetlb_cma=<size>` (5.7+) sets aside for exactly that.

- **Pros:** **deterministic** — the pages are pre-reserved, pinned (not swappable), never demoted, and faulted on first touch with no background daemon. No `khugepaged` jitter. This is the **HFT default** for the big hot regions: order books, market-data ring buffers, capture journals (Ch. 104), and (via linker/loader options or `hugetlbfs` text remapping) the hot **code** segment to cut iTLB misses (Ch. 14).
- **Cons:** up-front reservation (memory carved out of the general pool at boot, reducing flexibility), allocation can fail if the pool is exhausted or fragmented (reserve early — ideally at boot, before fragmentation), and it requires explicit code/config rather than "just working."

*Verify it, don't assume it.* `/proc/meminfo` shows the pool (`HugePages_Total`/`Free`/`Rsvd`; the per-node pools are under `/sys/devices/system/node/nodeN/hugepages/` — Ch. 18), `grep -E 'AnonHugePages|FilePmdMapped' /proc/<pid>/smaps_rollup` shows how much of the process is actually huge-page-backed, and `/proc/vmstat`'s `thp_fault_alloc`/`thp_fault_fallback`/`thp_collapse_alloc` tell you whether THP faults succeeded or fell back. On Linux 6.1+ `madvise(MADV_COLLAPSE)` collapses an existing region into huge pages synchronously, under your control, at warm-up time rather than whenever `khugepaged` gets to it (Appendix I has the one-liners).

Practical recipe for a low-latency box (ties Appendix C): reserve explicit huge pages at boot, map the large hot data structures and (where possible) the hot text on them, **pre-fault and `mlock`** them during warm-up (Ch. 34, 60) so the first real message never pays a fault, and set THP to `madvise` or `never` to avoid compaction/collapse jitter. Use 1 GB pages for very large, long-lived regions (multi-GB capture buffers) where even the 2 MB page count would pressure the STLB.

### 17.4.2 Reducing TLB pressure via layout

Huge pages multiply *reach*; layout reduces *demand*. The same data-oriented principles that cut cache misses (Ch. 8–11) cut TLB misses, one level up — fewer distinct pages touched, touched more predictably:

- **Compact, contiguous structures touch fewer pages.** A `std::vector`/`flat_map` (Ch. 32) packs data into a few contiguous pages; a node-based `std::map`/`unordered_map` or `vector<unique_ptr<T>>` (Ch. 16) scatters nodes across *many* pages, so a traversal that visits N nodes can touch up to N distinct pages — a TLB miss per node. Choosing flat, contiguous containers is a TLB optimization as much as a cache one.
- **Hot/cold splitting and SoA (Ch. 10) shrink the live page set.** Keeping only hot fields contiguous means the working set spans fewer pages, fitting the TLB reach without huge pages at all. Packing the hot path's data into the fewest pages is the layout-side complement to huge pages.
- **Sequential beats random at the page level.** The hardware translates sequential access well (page-walk prefetching, predictable STLB hits); random/pointer-chasing access over the same footprint thrashes the TLB. Where you control order — batch by key, sort, arena-allocate related objects adjacently (Ch. 31) — you turn page-random into page-sequential.
- **Arena/pool allocation co-locates related objects.** Allocating the objects touched together out of the same arena (Ch. 31) packs them onto shared pages, so traversing them stays within a few TLB entries instead of one-per-object scattered across the heap.
- **Mind the iTLB too (Ch. 14).** A sprawling, over-inlined code footprint spread across many pages misses the iTLB; lean, clustered hot code (Ch. 14) and huge pages for `.text` keep instruction translation cheap.

The unifying point: **TLB pressure is a function of how many pages you touch and how predictably** — exactly the cache-locality discipline of Part II, scaled to page granularity. Huge pages and good layout are complementary: layout cuts the number of pages you need; huge pages make each page cover more, so the TLB covers what's left.

---

## 17.5 Pitfalls & anti-patterns: THP stalls, fragmentation

- **THP compaction/collapse jitter.** Leaving THP on `always` (with `defrag` permitting direct compaction) on a jitter-sensitive box invites unpredictable fault-time compaction and `khugepaged` collapse stalls in the middle of the hot path (§17.4.1) — great average, ugly p99.9 (Ch. 1). Prefer explicit huge pages + THP `madvise`/`never` on a latency box; measure jitter with and without (Ch. 6, 57).
- **Allocating huge pages late / fragmentation failure.** Explicit huge pages need contiguous physical memory; request them **early** (boot-time reservation), because after the system has run a while, physical memory fragments and the *reservation* can silently come up short — writing N to `vm.nr_hugepages` may allocate fewer than N with no error. Read the pool back (`HugePages_Total` in `/proc/meminfo`); a later `mmap(MAP_HUGETLB)` then fails with `ENOMEM` (or a `MAP_NORESERVE` fault `SIGBUS`es) — explicit pages never degrade to 4 KB, only THP does. Reserve at boot, verify the pool, fail loudly if unavailable.
- **Forgetting to pre-fault / `mlock`.** Mapping huge pages doesn't touch them; the **first** access still faults (a big fault for a 2 MB page — Ch. 30). On the hot path that fault is a latency spike on the first real message. Pre-fault and `mlock` during warm-up (Ch. 34, 60).
- **Assuming the data cost moved.** Huge pages cut *translation*, not *data* latency. A random access to cold DRAM still costs the DRAM latency (Ch. 8); huge pages remove the *extra* page-walk on top. If you're DRAM-bandwidth/latency-bound, fix locality (Ch. 8–10), not just page size.
- **Optimizing the TLB when you're not TLB-bound.** If the working set fits the 4 KB TLB reach (a few MB) or `dtlb_load_misses.walk_*` is low, huge pages buy nothing — and reserving them wastes memory. Measure the TLB counters first (§17.3); don't cargo-cult huge pages.
- **1 GB pages used indiscriminately.** 1 GB pages want boot reservation (§17.4.1) and are coarse — internal fragmentation and inflexible. Reserve them only for genuinely huge, long-lived regions; 2 MB is the right granularity for most hot structures.
- **TLB shootdowns from the housekeeping thread.** The TLB is per core, so when any thread of the process changes a mapping — `munmap`, `mprotect`, `madvise(MADV_DONTNEED)`, `mremap`, including `malloc` trimming a freed arena back to the kernel — the kernel sends IPIs that flush the TLB on *every* core currently running that process, pinned hot thread included: a microsecond-scale stall it did nothing to cause. Watch the `TLB` row of `/proc/interrupts` on the hot core, or `perf stat -e tlb:tlb_flush`, while a housekeeping thread frees memory. Remedies: never unmap or re-protect on the hot path, pre-allocate and never return memory (Ch. 31 — `mallopt(M_TRIM_THRESHOLD, -1)`/`M_MMAP_THRESHOLD` or a custom allocator), and keep the hot process's memory map static after warm-up. (Which flushes are cheap and which are not also depends on PCID and on KPTI — Ch. 6 §6.2.3.)
- **NUMA-blind huge-page placement (Ch. 18).** A huge page is faulted on the node of the *first-touching* thread. Pre-faulting from the wrong thread/node places the whole 2 MB (or 1 GB!) region remote — a large, coarse NUMA mistake. Pre-fault from the thread that will use it, on the right node (Ch. 18).
- **THP defeating `MADV_DONTNEED`/page-level control.** Transparent promotion can interfere with fine-grained `madvise` patterns and make memory accounting coarser; on a box where you manage pages deliberately (Ch. 34), explicit control is more predictable.
- **TLB shootdowns you sent yourself.** A housekeeping thread's `munmap`/`mprotect`/`MADV_DONTNEED` (or a large `free` that `malloc` turns into one) flushes the hot core's TLB by IPI, because the threads share page tables — measured, with the fix (map once, churn in another process), in Ch. 34 §34.5.

---

## 17.6 Exercises & checklist

**Exercises**

1. **Measure the translation tax.** Build `tlb.cpp`, run 4 KB vs 2 MB (reserve hugepages via `sysctl vm.nr_hugepages`), and `perf stat -e dtlb_load_misses.walk_completed,dtlb_load_misses.walk_active,L1-dcache-load-misses`. Confirm: same data-cache misses, but walks collapse and ns/probe drops on 2 MB. What fraction of cycles was `walk_active` on 4 KB?
2. **Find the TLB cliff.** Sweep the region size from 64 KB up to 4 GiB with random probes on 4 KB pages; plot ns/probe and `dtlb_load_misses.walk_completed`. Identify the knees at the dTLB reach (~256 KB) and STLB reach (~8 MB). Repeat on 2 MB pages — where do the knees move?
3. **Sequential vs random at page scale.** Run the §17.3 probe sequentially vs randomly over the same 1 GiB region (4 KB pages). How much does the *page-walk* count differ for identical bytes touched? Explain via translation prefetching (§17.2.2).
4. **Container layout and the TLB.** Build the same key set as a contiguous `std::vector`/ `flat_map` (Ch. 32) and as a node-based `std::map`. Traverse each; compare `dtlb_load_misses.walk_completed`. Quantify the "one TLB miss per scattered node" effect (§17.4.2).
5. **THP jitter.** With THP `always`, run a long hot loop over freshly-allocated memory and record the latency distribution (HdrHistogram, Ch. 3); look for spikes correlated with direct compaction and `khugepaged` collapse (`perf record -e 'compaction:*' -e 'huge_memory:*'`). Repeat with explicit huge pages + THP `never`. Compare the *tails* (Ch. 1).

**Checklist — virtual memory & the TLB**

- [ ] I measured **`dtlb_load_misses.walk_*`** (not just cache misses) and confirmed the hot path is **TLB-bound** before reaching for huge pages.
- [ ] Large hot regions (books, ring buffers, capture buffers) use **explicit huge pages** (`MAP_HUGETLB`/hugetlbfs), reserved **early/at boot**, not late.
- [ ] THP is set to **`madvise`/`never`** on the latency box (or measured to add no jitter), so neither fault-time compaction nor `khugepaged` collapse can stall the hot path.
- [ ] Huge-page regions are **pre-faulted and `mlock`ed during warm-up** (Ch. 34, 60) — the first real message pays no fault.
- [ ] Pre-faulting happens **on the right NUMA node / by the using thread** (Ch. 18) — no accidental remote placement of a coarse 2 MB/1 GB page.
- [ ] I cut **page demand** via layout (contiguous/flat containers, hot/cold split, arena co-location — Ch. 10–11, 31–32), not page size alone.
- [ ] Hot **code** TLB (iTLB) pressure is addressed (lean code, huge-page `.text` where it helps — Ch. 14) for large hot footprints.
- [ ] I didn't reserve huge pages I don't need (working set fits 4 KB reach), and **1 GB pages** are used only for genuinely huge, long-lived regions.

---

## 17.7 References

- U. Drepper, *What Every Programmer Should Know About Memory* — the canonical treatment of virtual memory, the TLB, page-table walks, and huge pages, with measurement methodology that directly informs §17.2–§17.3.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* and *SDM Vol. 3* (paging) — the page-table structure, page-walk caches, dTLB/STLB organization, and the `dtlb_load_misses.*` performance events.
- The Linux kernel documentation — *Transparent Hugepage Support*, *HugeTLB Pages* (`Documentation/admin-guide/mm/`), `madvise(2)`, `mmap(2)` (`MAP_HUGETLB`), and `vm.nr_hugepages` sysctls (the controls of §17.4.1).
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture TLB sizes, associativity and page-walk behavior (the reach numbers behind §17.2.2).
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — where TLB/translation stalls surface in the back-end memory-bound breakdown.

## 17.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical TLB-miss analysis, huge-page experiments, and reading `dtlb_*` counters.
- Kernel and `libhugetlbfs` documentation, and writeups on remapping a program's text/BSS onto huge pages (for the iTLB side, Ch. 14).
- Ch. 18 (*NUMA*) — *where* the translated physical page lives, and first-touch placement of huge pages; Ch. 30 (*Memory Management Fundamentals*) — page faults and first-touch cost; Ch. 34 (*Memory Mapping*) — `mmap`/`mlock`/pre-faulting the pages this chapter measures; Ch. 60 (*Keeping the Hot Path Warm*) — pre-faulting and warming translations.
- **Appendix C** (System Tuning Checklist) — the boot-time/`sysctl` huge-page and THP settings consolidated; **Appendix E** — the TLB-miss/page-walk latency numbers that frame this chapter.

---

*Next: Ch. 18 — NUMA Architecture, where translation meets topology: once an address is translated to a physical page, *which socket's memory* that page lives on determines whether the access is a fast local hit or a slow cross-interconnect penalty — the first chapter on our dual-socket reference machine.*
