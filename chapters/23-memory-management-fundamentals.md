# Part IV — Memory Management

# Chapter 23 — Memory Management Fundamentals

> **Prerequisites:** Ch. 15 (virtual memory & page faults — the page-fault mechanics this chapter measures as allocation cost), Ch. 7 (caches — a fresh allocation is cold memory), Ch. 1 (tail latency — `malloc` is a tail event), Ch. 3 (benchmarking distributions — allocation cost is a *distribution*, not a mean), Ch. 20 (`noexcept` moves — why container growth copies or moves).
>
> **Leads into:** Ch. 24 (custom allocators — arenas/pools that make the steady state zero-allocation), Ch. 25 (cache-friendly containers — which STL containers allocate and when), Ch. 26 (`mmap`/`mlock`/pre-faulting — the syscall-level controls), Ch. 46 (warming — pre-faulting and pre-touching as part of warm-up). Opens **Part IV**.

---

## 23.1 Why it matters: `malloc` can stall on the hot path

`malloc` is the most dangerous function on the tick-to-trade path, and the danger is not its *average* cost — a warm `malloc` is often ~20-100 ns, annoying but survivable. The danger is its **tail**: a single allocation can, with no warning visible in your source, take a lock another thread holds, walk free-lists looking for a fit, call into the kernel (`mmap`/`brk`) to grow the heap, and — worst of all — trigger a **page fault** that traps into the kernel to wire up physical memory, costing *microseconds*. On a path budgeted in hundreds of nanoseconds, a single page-faulting allocation is a 10-100× blowout, and it lands on exactly the messages you can't predict. The whole north-star of this book — p99/p99.9 (Ch. 1) — is precisely where unmanaged allocation does its damage: the mean looks fine, the tail is a cliff.

What makes this insidious is how *invisible* allocations are in idiomatic C++. A `std::string` that outgrows its small-buffer, a `std::vector::push_back` that reallocates, a `std::function` capturing too much (Ch. 14), a `std::map`/`unordered_map` insert allocating a node (Ch. 25), a `shared_ptr` control block, a lambda returned by value, a `std::stringstream` formatting a log line (Ch. 71) — every one of these can call `operator new` on the hot path without a single literal `new` in sight. The hot path that *looks* allocation-free routinely isn't, and the only way to know is to instrument it (§23.3). The HFT discipline that follows is stark: **the steady-state hot path must allocate zero times.** All allocation happens at startup/setup (the cold path, Ch. 1's hot/cold split); the trading loop runs on memory already obtained, already faulted in, already warm.

This chapter is the foundation for that discipline. It separates the two costs that `malloc` conflates — the **allocator's bookkeeping** (finding/marking a block, possibly under a lock) and the **kernel's page fault** (first touch of a virtual page, Ch. 15) — because they have different magnitudes, different cures, and different measurement (§23.3). It covers the cheap, deterministic alternatives that belong on the hot path (stack buffers, pre-allocated and reused storage — §23.4.1) and the technique that removes the page-fault surprise (pre-faulting — §23.4.2, the bridge to Ch. 26/46). Custom allocators (arenas, pools) — the production answer to "I need dynamic-shaped data without `malloc`" — are Ch. 24; this chapter is *why* you need them and *what* you're avoiding.

## 23.2 Mental model: stack vs heap; page faults; allocator internals

**Stack vs heap — two very different costs.**

- **The stack** is nearly free: allocating a local is *decrementing the stack pointer* (one instruction, no bookkeeping, no syscall), the memory is already mapped and almost certainly hot in cache (Ch. 7), and deallocation is automatic (incrementing `rsp` on return). Stack allocation has *no tail* — it can't fault (the stack pages are resident after first use), can't lock, can't call the kernel. This is why "put it on the stack" is the first answer to hot-path allocation.
- **The heap** (`malloc`/`operator new`) is a *managed pool* with real bookkeeping: find a free block of the right size (size-class free-lists, best-fit search), split/coalesce, mark it used — often *under a lock or per-thread arena lock* — and, when the pool is exhausted, ask the **kernel** to grow it. The cost is variable and has a long tail.

**The two costs `malloc` conflates.** This is the key distinction of the chapter:

```
   malloc(n):
     ┌─ allocator bookkeeping  (userspace): find block in free-list / arena, maybe lock,
     │                                       split/coalesce, return pointer    ~tens of ns
     │
     └─ IF the heap must grow OR the page is untouched:
          kernel involvement:  mmap/brk syscall (grow the mapping)       ~hundreds of ns
          PAGE FAULT on first touch of each new page (Ch. 14):
             minor fault:  map a zero page / existing page        ~hundreds of ns - ~1 µs
             major fault:  bring page from disk/swap              ~ms  (catastrophic; avoid entirely)
```

- **Lazy allocation / first-touch (Ch. 15).** `malloc`/`mmap` return *virtual* address space; the kernel doesn't back it with physical memory until you **first touch** (write) each page — at which point a **page fault** traps into the kernel to wire up a physical frame. So the cost of "allocating" a big buffer is split: a little at `malloc`, then a fault *per page* spread across your first writes. A buffer you allocated but never warmed will fault *on the hot path* the first time you use it (§23.4.2).
- **Minor vs major faults.** A **minor fault** (map a fresh zero page or an already-resident page) is ~hundreds of ns to ~1 µs. A **major fault** (page must be read from disk/swap) is *milliseconds* — catastrophic; on a latency box you `mlockall` and disable swap (Ch. 26, Appendix C) so majors never happen.
- **Allocator internals (glibc `malloc`/tcmalloc/jemalloc).** Modern allocators use **per-thread caches/arenas** (so the common path is lock-free or low-contention), **size classes** (rounding requests to bucket sizes — internal fragmentation), and free-list management. They're good at the common case but still have tails (cache miss in the allocator's own metadata, arena lock contention under multi-threaded load, slow-path refill from the OS). `free` has cost too (coalescing, returning to the right list). The allocator is fast *on average* and unpredictable *at the tail* — exactly wrong for the hot path.

**Why `delete`/`free` also matters.** Deallocation isn't free either (coalescing, lock, cache effects), and a hot path that allocates *and frees* per message pays both. The steady-state-zero-allocation rule covers both directions.

The unifying model: **stack allocation is one instruction with no tail; heap allocation is userspace bookkeeping (tens of ns, possibly locked) plus — on growth or first touch — a kernel page fault (hundreds of ns to ms). The hot path must touch neither: allocate and fault everything in at startup, then reuse.**

## 23.3 Measure it: allocation-cost and page-fault distribution

Allocation cost is a **distribution** (Ch. 1, 3), so measuring the mean is exactly the wrong thing — the story is in the tail and in the page faults. Two measurements: (1) the per-call cost distribution of `new`/`delete` vs a stack/reused buffer, and (2) the page-fault count, which reveals the hidden kernel cost. Use `perf stat`'s fault counters and an HdrHistogram-style tail capture (Ch. 3).

```cpp
// alloccost.cpp — distribution of new/delete vs reused buffer; watch page faults.
// Build: g++ -O2 -std=c++20 -march=native alloccost.cpp -o alloccost
// Run pinned:  taskset -c 2 ./alloccost heap   |   ./alloccost reuse
//   page faults:  perf stat -e minor-faults,major-faults,page-faults ./alloccost heap
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <algorithm>
#include <chrono>
#include <new>

static inline std::uint64_t ns_now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
    bool heap = (argc < 2) || std::strcmp(argv[1], "heap") == 0;
    constexpr int N = 1'000'000;
    constexpr std::size_t SZ = 4096;                 // one page worth
    std::vector<std::uint32_t> samples; samples.reserve(N);

    alignas(64) static unsigned char reuse_buf[SZ];   // pre-allocated, reused every iter
    std::uint64_t sink = 0;

    for (int i = 0; i < N; ++i) {
        std::uint64_t t0 = ns_now();
        if (heap) {
            auto* p = new unsigned char[SZ];          // ALLOCATE on the "hot path"
            std::memset(p, i & 0xFF, SZ);             // first-touch → may PAGE FAULT
            sink += p[i % SZ];
            delete[] p;                               // ... and free
        } else {
            std::memset(reuse_buf, i & 0xFF, SZ);     // reuse: no alloc, already faulted
            sink += reuse_buf[i % SZ];
        }
        samples.push_back(std::uint32_t(ns_now() - t0));
    }
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double p){ return samples[std::size_t(p * (N - 1))]; };
    std::printf("%-6s sink=%llu  p50=%u  p99=%u  p99.9=%u  max=%u ns\n",
                heap ? "heap" : "reuse", (unsigned long long)sink,
                pct(0.50), pct(0.99), pct(0.999), samples.back());
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), `-O2`, pinned, turbo off (illustrative; the *shape of the distribution* is the point):

```
                  p50        p99        p99.9       max        page-faults (perf)
heap (new+touch)  ~120 ns    ~480 ns    ~2,800 ns   ~9,000 ns  ~1 per iter (minor)  <- the TAIL + faults
reuse (buffer)    ~80 ns     ~95 ns     ~110 ns     ~250 ns    ~0 (warmed once)     <- flat, no tail
```

Read it the Ch. 1 way: **the means are not far apart, but the tails are a different universe.** The reuse path is *flat* — same work every iteration, no allocation, the buffer faulted in once and stays resident — so p99.9 ≈ p50. The heap path has a p99.9 ~25× its median and a max in the *microseconds*, because some iterations hit the allocator's slow path and, critically, the `memset` first-touches fresh pages that **page-fault** (the `~1 minor-fault per iter` in `perf` is the smoking gun: the cost isn't in `new`'s bookkeeping, it's in the *kernel* wiring up memory). This is the entire argument for steady-state-zero-allocation in one chart: allocation doesn't slow your average much, it *destroys your tail* and drags the kernel onto the hot path. The fingerprint to recognize: **a hot path with non-zero `minor-faults`/`page-faults` in `perf stat`, and a p99.9 far above p50** — that's hidden allocation (or un-warmed memory), and §23.4 is the cure.

## 23.4 Techniques

### 23.4.1 Stack and pre-allocated buffers

The two hot-path-safe ways to get memory, in order of preference:

- **Put it on the stack.** Fixed-size, bounded-lifetime data belongs in locals / fixed-size arrays / `std::array` — one-instruction allocation, no tail, cache-hot, auto-freed (§23.2). A decode scratch buffer, a fixed working set, a small bounded collection: stack-allocate it. For *bounded-but-variable* sizes, a stack buffer sized to the maximum (plus a fallback for the rare overflow) keeps the common case allocation-free. Beware *unbounded* stack growth and very large frames (stack overflow, and the guard-page fault) — the stack is for bounded data.
- **Pre-allocate at startup and reuse.** For data that must outlive a function or is too large/variable for the stack, allocate it **once during setup** (the cold path) and **reuse** it every iteration — the `reuse_buf` of §23.3, a pre-sized `std::vector` you `clear()` (not free) and refill, a fixed ring buffer (Ch. 34, 37), an object pool (Ch. 24). The steady state never calls `new`; it writes into memory it already owns.
- **`reserve()` / size containers up front.** A `std::vector` you `reserve(max)` once never reallocates on `push_back` (no per-append `new`, no move/copy of elements — Ch. 20); a hash map (Ch. 25) sized to its working set up front avoids rehash-time allocation. `clear()` keeps the capacity; `shrink_to_fit`/reassignment throws it away — on the hot path you want to *keep* capacity and reuse it.
- **Prefer move and `noexcept` (Ch. 20).** Where objects must transfer, `std::move` avoids a copy *and* often an allocation; a `noexcept` move ctor is what lets `std::vector` growth *move* instead of copy (Ch. 20). Returning by value (NRVO/move) is cheap; copying a heap-owning object is an allocation.
- **Small-buffer optimization (SBO), used deliberately.** `std::string` and many small-vector types store small payloads inline (no allocation) and only heap-allocate when they outgrow the buffer. Know your types' SBO thresholds (Ch. 25) and keep hot-path payloads under them; a `string` that just exceeds SBO allocates silently.

### 23.4.2 Pre-faulting pages

Allocating memory isn't enough — until each page is *touched*, the first hot-path access **page-faults** (§23.2, Ch. 15). Pre-faulting moves that fault to startup:

- **Touch every page during warm-up.** After allocating (or `mmap`ing) a region, write one byte per page (stride by page size, 4 KB or the huge-page size — Ch. 15) *before* the hot path runs, so the kernel wires up all the physical frames during setup. This is the difference between the §23.3 heap tail and the flat reuse line: the reuse buffer was faulted once and never again.
- **`mlock`/`mlockall` to keep pages resident (Ch. 26).** Touching a page maps it; `mlock`/`mlockall(MCL_CURRENT|MCL_FUTURE)` *pins* it so the kernel can't reclaim/swap it back out — guaranteeing no **major fault** (the millisecond catastrophe) ever hits the hot path. On a latency box this is standard (disable swap too — Appendix C).
- **`MAP_POPULATE` / pre-faulting `mmap` (Ch. 26).** `mmap(..., MAP_POPULATE, ...)` pre-faults the mapping at map time; combined with huge pages (Ch. 15) and `mlock`, you get a region that's fully resident and TLB-friendly before the first message.
- **First-touch on the right thread/NUMA node (Ch. 16).** Pre-faulting decides *placement* — the page lands on the node of the touching thread (Ch. 16's first-touch). Pre-fault each thread's hot memory *from that thread*, pinned to its node, so warming and NUMA-locality happen together. A coarse mistake here strands a whole region remote.
- **This is part of warming (Ch. 46).** Pre-faulting sits alongside cache/TLB/branch-predictor warming: the first real message must hit memory that's allocated, faulted, resident, and local. Pre-faulting handles the "faulted + resident" part; Ch. 46 is the full warm-up discipline.

## 23.5 Pitfalls & anti-patterns: hidden allocations; minor/major faults

- **Hidden allocations on the "allocation-free" hot path.** The headline trap: `std::string` growth, `vector::push_back` reallocation, `std::function`/lambda capture (Ch. 14), `map`/`unordered_map` node inserts (Ch. 25), `shared_ptr` control blocks, `stringstream` formatting (Ch. 71), `to_string`, returning containers by value. None show a literal `new`. **Instrument `operator new`** on the hot path (override it to count/abort) to prove zero allocation — don't eyeball it (§23.3).
- **Un-warmed memory faulting on the hot path.** Allocating at startup but never *touching* the pages means the first hot-path access page-faults (§23.4.2). Pre-fault during warm-up; a buffer that's "pre-allocated" but cold is not actually ready.
- **Major faults / swapping.** A major fault (disk/swap) is *milliseconds* — total latency destruction. `mlockall`, disable swap (`swapoff`), and ensure the working set fits RAM on the latency box (Ch. 26, Appendix C). A single major fault ruins a session's tail.
- **Allocator lock contention under threads.** Multi-threaded `malloc`/`free` can contend on arena locks; even per-thread-cache allocators have slow-path refills. The cure isn't a "faster malloc" on the hot path — it's *not allocating* there (Ch. 24). (For off-hot-path allocation, tcmalloc/jemalloc reduce contention vs glibc.)
- **Freeing and reallocating per message.** Allocating *and* freeing each iteration pays both costs and churns the allocator/cache. Reuse the buffer (`clear()` keeps capacity) instead of `free`+`new` (§23.4.1).
- **`shrink_to_fit`/reassignment dropping capacity.** Throwing away a container's capacity (then regrowing it next message) reintroduces allocation. On the hot path, *keep* capacity and reuse; only release it on the cold path if memory pressure demands.
- **Giant stack frames / unbounded stack use.** The stack is great but finite; a huge `std::array` local or deep recursion overflows it (guard-page fault → crash). Use the stack for *bounded* data; large/variable data is pre-allocated heap reused (§23.4.1).
- **Benchmarking allocation by the mean.** Reporting average `malloc` cost hides the tail that actually matters (§23.3, Ch. 1). Always measure the *distribution* (p99/p99.9) and the page-fault count, not the average.
- **Assuming `reserve` once is enough across reuse.** It is — *if* you `clear()` (which keeps capacity) and don't reassign/`shrink`. Accidentally replacing the container (`v = {}`, `v = other`) can drop capacity; verify the hot loop reuses the same storage.

## 23.6 Exercises & checklist

**Exercises**

1. **Measure the tail and the faults.** Build `alloccost.cpp`; run `heap` vs `reuse` pinned, and `perf stat -e minor-faults,major-faults,page-faults` on each. Confirm: similar p50, wildly different p99.9, and ~1 minor-fault/iter on `heap`, ~0 on `reuse`. Where does the heap tail come from — `new`'s bookkeeping or the page fault?
2. **Catch hidden allocations.** Override global `operator new`/`delete` to count calls (or `abort()`); run a "hot loop" using `std::string`, `vector::push_back` without `reserve`, and a capturing `std::function`. How many allocations does the "allocation-free" loop actually do? Now fix each (SBO/`reserve`/concrete callable) to reach zero.
3. **Pre-faulting.** Allocate a large buffer, then time the first write to each page *without* pre-faulting vs *with* a warm-up pass that touches every page first. Show the un-warmed first-touch faults (the §23.4.2 effect); confirm with `perf` minor-faults.
4. **`mlockall` and majors.** Under artificial memory pressure (allocate most of RAM), measure hot-path tails with and without `mlockall`/swap. Reproduce a major fault and its millisecond cost; show `mlockall` prevents it.
5. **Stack vs heap scratch.** Replace a per-call heap scratch buffer with a stack `std::array` (or pre-allocated reused buffer). Compare ns/op distributions. Quantify the tail you removed.

**Checklist — memory-management fundamentals**

- [ ] The steady-state hot path allocates **zero times** — verified by **instrumenting `operator new`** (counting/aborting), not by inspection (§23.5).
- [ ] Fixed/bounded data is **stack-allocated** (`std::array`/locals); larger or variable data is **pre-allocated at startup and reused** (`reserve` + `clear`, pools — Ch. 24).
- [ ] Containers are **`reserve()`d to their working set** up front; the hot loop **keeps capacity** (`clear()`), never `shrink`/reassigns it away.
- [ ] All hot-path memory is **pre-faulted during warm-up** (touch every page) and **`mlock`ed** so no minor/major fault hits the hot path (§23.4.2, Ch. 26, 46).
- [ ] **Swap is off / working set fits RAM**; major faults are impossible on the latency box (Appendix C).
- [ ] Pre-faulting happens on the **right thread/NUMA node** (Ch. 16) so warming and locality coincide.
- [ ] Move/`noexcept` are used so transfers don't copy-allocate (Ch. 20); SBO thresholds are respected for hot-path `string`/small types (Ch. 25).
- [ ] Allocation cost is judged by the **distribution (p99/p99.9) and page-fault count** (Ch. 1, 3), never the mean.

## 23.7 References

- U. Drepper, *What Every Programmer Should Know About Memory* — virtual memory, page faults, and allocation cost; the foundation for §23.2 and the page-fault half of §23.3.
- The glibc `malloc` internals documentation, and the tcmalloc/jemalloc papers and docs — per-thread arenas/caches, size classes, and the slow-path/lock behavior behind the allocator tail (§23.2).
- The Linux man pages — `mmap(2)` (`MAP_POPULATE`/`MAP_ANONYMOUS`), `mlock(2)`/`mlockall(2)`, `brk(2)`, and `Documentation` on page faults and overcommit — the kernel mechanisms of §23.2/§23.4.2.
- ISO C++ / cppreference — `operator new`/`delete` replacement, `std::array`, container `reserve`/`capacity`/`clear` semantics, and the small-buffer optimization notes (§23.4.1).
- Carl Cook, *"When a Microsecond Is an Eternity"* (CppCon) — the HFT case for zero-allocation hot paths and measuring the tail, motivating this chapter.

## 23.8 Additional Reading

- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — page-fault and allocation analysis with `perf`, and the cost of cold memory.
- The jemalloc and mimalloc design writeups — modern allocator architecture, for when off-hot-path allocation contention matters.
- Ch. 24 (*Custom Allocators*) — arenas/pools/`std::pmr` that provide dynamic-shaped data with no hot-path `malloc`; Ch. 25 (*Cache-Friendly Containers*) — which STL containers allocate and the flat alternatives; Ch. 26 (*Memory Mapping*) — `mmap`/`mlock`/pre-faulting in depth; Ch. 46 (*Keeping the Hot Path Warm*) — pre-faulting as part of warm-up; Ch. 16 (*NUMA*) — first-touch placement.
- **Appendix C** (System Tuning Checklist) — `mlockall`, swap, and overcommit settings; **Appendix E** — the page-fault and allocation latency numbers that frame this chapter.

---

*Next: Ch. 24 — Custom Allocators, the production answer to this chapter's problem: arena, pool, and slab allocators and `std::pmr` that give you dynamic-shaped, variable-lifetime data with the steady-state-zero-allocation guarantee the hot path demands.*
