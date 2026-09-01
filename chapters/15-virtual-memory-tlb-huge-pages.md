# Part II — CPU Microarchitecture

# Chapter 15 — Virtual Memory, the TLB & Huge Pages

> **Prerequisites:** Ch. 7 (the memory hierarchy — the TLB is a cache, and a page-table walk is *itself* a sequence of memory accesses that hit or miss those same caches), Ch. 8–9 (data layout — footprint and access pattern drive TLB pressure), Ch. 12 (the I-TLB side, for hot *code*), Ch. 2–3 (top-down and benchmarking — §15.3).
>
> **Leads into:** Ch. 16 (NUMA — page placement is *where* a translated physical page lives, on which node), Ch. 23 (memory-management fundamentals — page faults, first-touch, the cost of growing the working set), Ch. 26 (`mmap`/`mlock`/pre-faulting — the syscall-level controls for the pages this chapter measures). Explicit huge pages and `mlockall` reappear in the system-tuning checklist (Appendix C) and hot-path warming (Ch. 46).

---

## 15.1 Why it matters: TLB misses tax every memory access

Every load and store in your program uses a **virtual** address that the hardware must translate to a **physical** one before it can touch a cache or DRAM. That translation is not free: it's a lookup in a small per-core cache called the **TLB** (Translation Lookaside Buffer), and when the address you need isn't in the TLB, the CPU runs a **page-table walk** — a multi-level pointer chase through in-memory page tables (four levels on x86-64, five with LA57) that can itself miss in the data caches. A TLB miss that walks all the way to DRAM can cost *hundreds* of cycles, and it happens *in addition to* whatever cache/DRAM cost the actual data access incurs. Translation is a tax levied on every memory reference; most of the time the TLB hides it, but when your working set is large or scattered, the tax becomes visible — and, worse, *variable*, landing in the tail (Ch. 1).

This matters for low-latency code because the standard 4 KB page is *small* relative to modern working sets. A core's L1 data-TLB holds on the order of **64 entries**; at 4 KB/page that covers just **256 KB** of memory before you start missing — far smaller than the multi-megabyte order books, market-data ring buffers, and tick caches a trading system keeps hot. The second-level (L2/STLB) TLB extends the reach to a few thousand entries (a handful of MB), but a feed handler streaming a large book, or a strategy walking a big hash map (Ch. 25), routinely exceeds it. The result is a steady drip of TLB misses that don't show up as *cache* misses (the data may be in L2/L3) yet still cost you — an invisible bottleneck unless you measure the TLB counters specifically (§15.3).

The fix is **huge pages**: instead of 4 KB pages, map memory in **2 MB** (or **1 GB**) chunks, so one TLB entry covers 512× (or 256K×) more memory. The same 64-entry TLB that reached 256 KB with 4 KB pages reaches **128 MB** with 2 MB pages — enough to cover a large book, a capture buffer, or the hot code segment (Ch. 12) with near-zero TLB misses. Huge pages are one of the highest-leverage, lowest-effort wins for a large-footprint hot path, and they come in two flavors — *transparent* (automatic, convenient, with a jitter caveat) and *explicit* (reserved, deterministic, the HFT default) — whose trade-offs §15.4 makes precise. As always, measure first (Ch. 2): if your hot working set fits comfortably in the TLB's reach, this chapter's techniques buy nothing; if `dtlb_load_misses` is elevated, they can be transformative.

---

## 15.2 Mental model

### 15.2.1 Address translation and the page-table walk

Virtual memory gives each process its own flat address space; the hardware (the MMU) maps virtual pages to physical frames through **page tables** the OS maintains. On x86-64 with 4 KB pages, a virtual address is split into a page offset (low 12 bits) and four 9-bit indices, one per page-table level:

```
   virtual addr:  [ 9 bits | 9 bits | 9 bits | 9 bits | 12-bit offset ]
                     PML4      PDPT      PD       PT
                       │         │        │        │
   CR3 ─► PML4[idx] ─► PDPT[idx] ─► PD[idx] ─► PT[idx] ─► physical frame  + offset
          (a load)     (a load)    (a load)   (a load)
```

The page-table **walk** is a dependent chain of **four memory loads** (each level's entry gives the physical address of the next level's table), ending in the physical frame number. Two things follow that matter enormously for latency:

- **A walk is itself memory traffic.** Each of the four levels is a load that hits or misses the *data caches* (Ch. 7). A "warm" walk where the upper levels are cached costs maybe a handful of cycles per level; a "cold" walk that misses to DRAM at multiple levels costs *hundreds* of cycles. The hardware caches page-table entries (page-walk caches / paging-structure caches) precisely because walks are expensive.
- **Huge pages short-circuit the walk.** A **2 MB page** stops the walk at the PD level (the PD entry points directly at a 2 MB frame — three levels, not four) and, more importantly, *one TLB entry now covers 2 MB*. A **1 GB page** stops at the PDPT level (two levels) and one entry covers 1 GB. Fewer levels *and* vastly more coverage per entry.

The translation result — the virtual→physical mapping plus permission bits — is what gets cached in the TLB so the walk can be skipped next time. (Caches are physically tagged, so translation logically precedes the cache lookup; in practice the L1 overlaps them, but a TLB miss still stalls the access.)

### 15.2.2 TLB structure and miss cost

The TLB is a cache *of translations*, with the same structure ideas as the data cache (Ch. 7) — entries, associativity, levels — but indexed by virtual page number:

- **It's hierarchical and split.** A typical modern core has a small, fast **L1 TLB** split into **iTLB** (instruction, Ch. 12) and **dTLB** (data) — tens of entries each, often with separate sub-pools for 4 KB and 2 MB pages — backed by a larger unified **L2 TLB (STLB)** of ~1.5–2K entries. Representative Ice Lake-class numbers: dL1-TLB ~64 entries (4 KB), STLB ~2048 entries.
- **Reach, not just count, is the metric.** What you care about is **coverage** = entries × page size. The same TLB reaches 512× further with 2 MB pages:

  ```
                       4 KB pages              2 MB pages
   dTLB (64 entries)   64 × 4 KB  = 256 KB     64 × 2 MB  = 128 MB
   STLB (2048)         2048 × 4 KB = 8 MB       2048 × 2 MB = 4 GB
  ```

  A working set that overflows the 4 KB reach (8 MB at the STLB) but fits the 2 MB reach (4 GB) goes from "TLB-miss-bound" to "TLB-resident" with no code change.
- **Miss cost is the walk.** An L1-TLB miss that hits the STLB is cheap (a few cycles). An STLB miss triggers the full page-table walk (§15.2.1): cheap if the page-walk caches are warm, *hundreds of cycles* if the walk misses to DRAM. Crucially, a TLB miss is **independent of** whether the *data* is cached — you can have the data hot in L2 and still pay a 100+-cycle translation miss, which is why TLB problems hide from cache-miss counters and need their own (§15.3).
- **What thrashes the TLB.** *Large* working sets (more pages than reach) and *scattered* access (touching many distinct pages with poor locality — pointer-chasing node containers, random hash probes, huge sparse tables). *Sequential* access over a large region still touches many pages but the hardware prefetches translations well; *random* access over the same region is far worse. This is the same locality story as Ch. 7–8, one level up: the TLB rewards dense, contiguous, predictable page access.

The model to carry: **the TLB is a tiny cache whose "lines" are whole pages; its reach is your real budget for hot memory, and huge pages multiply that reach 512×.** When the working set exceeds the reach, translation — not data — can become the bottleneck.

---

## 15.3 Measure it: `dtlb_load_misses` with and without huge pages

The experiment isolates translation cost from data cost: **randomly** probe a large region (so the data access pattern and total bytes touched are identical) under two page regimes — default 4 KB pages vs 2 MB huge pages — and watch the TLB counters move while the cache behavior stays put. Random access defeats the translation prefetcher and maximizes the TLB's visibility.

```cpp
// tlb.cpp — random probes over a large region; compare 4K vs 2M pages.
// Build: g++ -O2 -std=c++20 -march=native tlb.cpp -o tlb
// Run (4K, transparent HP off for this region — see note):  taskset -c 2 ./tlb
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
    std::memset(a, 1, BYTES);                                // pre-fault every page (Ch. 22, 25)

    std::mt19937_64 rng(12345);
    std::vector<std::uint32_t> idx(PROBES);
    for (auto& v : idx) v = std::uint32_t(rng() % N);        // precompute random indices

    auto t0 = std::chrono::steady_clock::now();
    std::uint64_t acc = 0;
    for (std::size_t k = 0; k < PROBES; ++k) acc += a[idx[k]];   // random, dependent-ish probe
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-4s acc=%llu  %.2f ns/probe\n", huge ? "2M" : "4K",
                (unsigned long long)acc, ns / PROBES);
    munmap(a, BYTES);
    return 0;
}
```

Profile both with the dTLB counters: `perf stat -e cycles,instructions, dtlb_load_misses.miss_causes_a_walk,dtlb_load_misses.walk_active, dtlb_load_misses.walk_completed,L1-dcache-load-misses ./tlb` (run `./tlb` and `./tlb huge`; the `huge` run needs reserved hugepages — `sysctl vm.nr_hugepages=...` or a hugetlbfs mount; see §15.4.1). Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), pinned, turbo off (illustrative; the *direction* is the point):

```
                                          4K pages           2M huge pages
ns / probe                                 ~75 ns             ~45 ns        <- ~1.6x faster
dtlb_load_misses.walk_completed            HIGH               ~10–100x fewer <- the whole story
dtlb_load_misses.walk_active (cycles)      large fraction     small
L1-dcache-load-misses                      ~same              ~same         <- DATA cost unchanged
instructions                               same               same          <- identical work
```

Read it the Ch. 2 way: **same instructions, same data-cache misses (the data access pattern is identical) — yet ~1.6× faster, entirely from collapsed `dtlb_load_misses`.** The 1 GiB region needs ~256K distinct 4 KB pages — wildly beyond the dTLB/STLB reach — so nearly every random probe pays a page-table walk; the data itself was in DRAM either way, so this cost is *pure translation*, invisible in `L1-dcache-load-misses`. Switching to 2 MB pages cuts the page count 512× (to ~512 pages, comfortably within STLB reach), and the walks — and the cycles spent in `walk_active` — largely vanish. That is the TLB-bound signature: **high `dtlb_load_misses.walk_*` with unchanged data-cache behavior.** When you see it, huge pages and/or better page-locality (§15.4) are the lever; if those counters are already low, the TLB is not your problem.

---

## 15.4 Techniques

### 15.4.1 Transparent vs explicit huge pages

There are two ways to get 2 MB pages, and the choice is a classic latency trade-off between *convenience* and *determinism*.

**Transparent Huge Pages (THP)** — the kernel *automatically* promotes eligible anonymous mappings to 2 MB pages in the background, with no application change. Controlled via `/sys/kernel/mm/transparent_hugepage/enabled` (`always` / `madvise` / `never`) and steerable per-region with `madvise(MADV_HUGEPAGE)`.

- **Pros:** zero code change, no up-front reservation, works for the common case.
- **Cons — and why HFT is wary:** promotion happens *lazily* and via a kernel daemon (`khugepaged`) that scans and compacts memory; the **compaction and promotion can stall your thread** at unpredictable times — a textbook source of tail-latency jitter (Ch. 1) on an otherwise-quiet box. A 2 MB page also faults in all at once (a larger first-touch stall — Ch. 23). THP is great for throughput batch jobs; on a jitter-sensitive hot path it's often set to `madvise` (opt-in per region) or `never`, with explicit pages used instead.

**Explicit huge pages (hugetlbfs / `MAP_HUGETLB`)** — pages **reserved at boot or via sysctl** (`vm.nr_hugepages`, or `hugeadm`), then requested explicitly via `mmap(..., MAP_HUGETLB, ...)`, a hugetlbfs file mapping, or `shmget(SHM_HUGETLB)`. 1 GB pages need `MAP_HUGETLB | MAP_HUGE_1GB` and boot-time reservation (`hugepagesz=1G hugepages=N`).

- **Pros:** **deterministic** — the pages are pre-reserved, pinned (not swappable), never demoted, and faulted on first touch with no background daemon. No `khugepaged` jitter. This is the **HFT default** for the big hot regions: order books, market-data ring buffers, capture journals (Ch. 75), and (via linker/loader options or `hugetlbfs` text remapping) the hot **code** segment to cut iTLB misses (Ch. 12).
- **Cons:** up-front reservation (memory carved out of the general pool at boot, reducing flexibility), allocation can fail if the pool is exhausted or fragmented (reserve early — ideally at boot, before fragmentation), and it requires explicit code/config rather than "just working."

Practical recipe for a low-latency box (ties Appendix C): reserve explicit huge pages at boot, map the large hot data structures and (where possible) the hot text on them, **pre-fault and `mlock`** them during warm-up (Ch. 26, 46) so the first real message never pays a fault, and set THP to `madvise` or `never` to avoid `khugepaged` jitter. Use 1 GB pages for very large, long-lived regions (multi-GB capture buffers) where even the 2 MB page count would pressure the STLB.

### 15.4.2 Reducing TLB pressure via layout

Huge pages multiply *reach*; layout reduces *demand*. The same data-oriented principles that cut cache misses (Ch. 7–9) cut TLB misses, one level up — fewer distinct pages touched, touched more predictably:

- **Compact, contiguous structures touch fewer pages.** A `std::vector`/`flat_map` (Ch. 25) packs data into a few contiguous pages; a node-based `std::map`/`unordered_map` or `vector<unique_ptr<T>>` (Ch. 14) scatters nodes across *many* pages, so a traversal that visits N nodes can touch up to N distinct pages — a TLB miss per node. Choosing flat, contiguous containers is a TLB optimization as much as a cache one.
- **Hot/cold splitting and SoA (Ch. 8) shrink the live page set.** Keeping only hot fields contiguous means the working set spans fewer pages, fitting the TLB reach without huge pages at all. Packing the hot path's data into the fewest pages is the layout-side complement to huge pages.
- **Sequential beats random at the page level.** The hardware translates sequential access well (page-walk prefetching, predictable STLB hits); random/pointer-chasing access over the same footprint thrashes the TLB. Where you control order — batch by key, sort, arena-allocate related objects adjacently (Ch. 24) — you turn page-random into page-sequential.
- **Arena/pool allocation co-locates related objects.** Allocating the objects touched together out of the same arena (Ch. 24) packs them onto shared pages, so traversing them stays within a few TLB entries instead of one-per-object scattered across the heap.
- **Mind the iTLB too (Ch. 12).** A sprawling, over-inlined code footprint spread across many pages misses the iTLB; lean, clustered hot code (Ch. 12) and huge pages for `.text` keep instruction translation cheap.

The unifying point: **TLB pressure is a function of how many pages you touch and how predictably** — exactly the cache-locality discipline of Part II, scaled to page granularity. Huge pages and good layout are complementary: layout cuts the number of pages you need; huge pages make each page cover more, so the TLB covers what's left.

---

## 15.5 Pitfalls & anti-patterns: THP stalls, fragmentation

- **`khugepaged`/THP compaction jitter.** Leaving THP on `always` on a jitter-sensitive box invites unpredictable promotion/compaction stalls in the middle of the hot path (§15.4.1) — great average, ugly p99.9 (Ch. 1). Prefer explicit huge pages + THP `madvise`/`never` on a latency box; measure jitter with and without (Ch. 6, 45).
- **Allocating huge pages late / fragmentation failure.** Explicit huge pages need contiguous physical memory; request them **early** (boot-time reservation), because after the system has run a while, physical memory fragments and a large 2 MB/1 GB reservation can *fail* or fall back to 4 KB silently. Reserve at boot, verify the pool, fail loudly if unavailable.
- **Forgetting to pre-fault / `mlock`.** Mapping huge pages doesn't touch them; the **first** access still faults (a big fault for a 2 MB page — Ch. 23). On the hot path that fault is a latency spike on the first real message. Pre-fault and `mlock` during warm-up (Ch. 26, 46).
- **Assuming the data cost moved.** Huge pages cut *translation*, not *data* latency. A random access to cold DRAM still costs the DRAM latency (Ch. 7); huge pages remove the *extra* page-walk on top. If you're DRAM-bandwidth/latency-bound, fix locality (Ch. 7–8), not just page size.
- **Optimizing the TLB when you're not TLB-bound.** If the working set fits the 4 KB TLB reach (a few MB) or `dtlb_load_misses.walk_*` is low, huge pages buy nothing — and reserving them wastes memory. Measure the TLB counters first (§15.3); don't cargo-cult huge pages.
- **1 GB pages used indiscriminately.** 1 GB pages need boot reservation and are coarse — internal fragmentation and inflexible. Reserve them only for genuinely huge, long-lived regions; 2 MB is the right granularity for most hot structures.
- **NUMA-blind huge-page placement (Ch. 16).** A huge page is faulted on the node of the *first-touching* thread. Pre-faulting from the wrong thread/node places the whole 2 MB (or 1 GB!) region remote — a large, coarse NUMA mistake. Pre-fault from the thread that will use it, on the right node (Ch. 16).
- **THP defeating `MADV_DONTNEED`/page-level control.** Transparent promotion can interfere with fine-grained `madvise` patterns and make memory accounting coarser; on a box where you manage pages deliberately (Ch. 26), explicit control is more predictable.

---

## 15.6 Exercises & checklist

**Exercises**

1. **Measure the translation tax.** Build `tlb.cpp`, run 4 KB vs 2 MB (reserve hugepages via `sysctl vm.nr_hugepages`), and `perf stat -e dtlb_load_misses.walk_completed, dtlb_load_misses.walk_active,L1-dcache-load-misses`. Confirm: same data-cache misses, but walks collapse and ns/probe drops on 2 MB. What fraction of cycles was `walk_active` on 4 KB?
2. **Find the TLB cliff.** Sweep the region size from 64 KB up to 4 GiB with random probes on 4 KB pages; plot ns/probe and `dtlb_load_misses.walk_completed`. Identify the knees at the dTLB reach (~256 KB) and STLB reach (~8 MB). Repeat on 2 MB pages — where do the knees move?
3. **Sequential vs random at page scale.** Run the §15.3 probe sequentially vs randomly over the same 1 GiB region (4 KB pages). How much does the *page-walk* count differ for identical bytes touched? Explain via translation prefetching (§15.2.2).
4. **Container layout and the TLB.** Build the same key set as a contiguous `std::vector`/ `flat_map` (Ch. 25) and as a node-based `std::map`. Traverse each; compare `dtlb_load_misses.walk_completed`. Quantify the "one TLB miss per scattered node" effect (§15.4.2).
5. **THP jitter.** With THP `always`, run a long hot loop over freshly-allocated memory and record the latency distribution (HdrHistogram, Ch. 3); look for `khugepaged`-correlated spikes (`perf record` on the compaction events). Repeat with explicit huge pages + THP `never`. Compare the *tails* (Ch. 1).

**Checklist — virtual memory & the TLB**

- [ ] I measured **`dtlb_load_misses.walk_*`** (not just cache misses) and confirmed the hot path is **TLB-bound** before reaching for huge pages.
- [ ] Large hot regions (books, ring buffers, capture buffers) use **explicit huge pages** (`MAP_HUGETLB`/hugetlbfs), reserved **early/at boot**, not late.
- [ ] THP is set to **`madvise`/`never`** on the latency box (or measured to add no jitter), so `khugepaged` compaction can't stall the hot path.
- [ ] Huge-page regions are **pre-faulted and `mlock`ed during warm-up** (Ch. 26, 46) — the first real message pays no fault.
- [ ] Pre-faulting happens **on the right NUMA node / by the using thread** (Ch. 16) — no accidental remote placement of a coarse 2 MB/1 GB page.
- [ ] I cut **page demand** via layout (contiguous/flat containers, hot/cold split, arena co-location — Ch. 8–9, 24–25), not page size alone.
- [ ] Hot **code** TLB (iTLB) pressure is addressed (lean code, huge-page `.text` where it helps — Ch. 12) for large hot footprints.
- [ ] I didn't reserve huge pages I don't need (working set fits 4 KB reach), and **1 GB pages** are used only for genuinely huge, long-lived regions.

---

## 15.7 References

- U. Drepper, *What Every Programmer Should Know About Memory* — the canonical treatment of virtual memory, the TLB, page-table walks, and huge pages, with measurement methodology that directly informs §15.2–§15.3.
- Intel, *64 and IA-32 Architectures Optimization Reference Manual* and *SDM Vol. 3* (paging) — the page-table structure, page-walk caches, dTLB/STLB organization, and the `dtlb_load_misses.*` performance events.
- The Linux kernel documentation — *Transparent Hugepage Support*, *HugeTLB Pages* (`Documentation/admin-guide/mm/`), `madvise(2)`, `mmap(2)` (`MAP_HUGETLB`), and `vm.nr_hugepages` sysctls (the controls of §15.4.1).
- A. Fog, *The Microarchitecture of Intel, AMD and VIA CPUs* — per-microarchitecture TLB sizes, associativity and page-walk behavior (the reach numbers behind §15.2.2).
- A. Yasin, *"A Top-Down Method for Performance Analysis"* (Ch. 2) — where TLB/translation stalls surface in the back-end memory-bound breakdown.

## 15.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — practical TLB-miss analysis, huge-page experiments, and reading `dtlb_*` counters.
- Kernel and `libhugetlbfs` documentation, and writeups on remapping a program's text/BSS onto huge pages (for the iTLB side, Ch. 12).
- Ch. 16 (*NUMA*) — *where* the translated physical page lives, and first-touch placement of huge pages; Ch. 23 (*Memory Management Fundamentals*) — page faults and first-touch cost; Ch. 26 (*Memory Mapping*) — `mmap`/`mlock`/pre-faulting the pages this chapter measures; Ch. 46 (*Keeping the Hot Path Warm*) — pre-faulting and warming translations.
- **Appendix C** (System Tuning Checklist) — the boot-time/`sysctl` huge-page and THP settings consolidated; **Appendix E** — the TLB-miss/page-walk latency numbers that frame this chapter.

---

*Next: Ch. 16 — NUMA Architecture, where translation meets topology: once an address is translated to a physical page, *which socket's memory* that page lives on determines whether the access is a fast local hit or a slow cross-interconnect penalty — the first chapter on our dual-socket reference machine.*
