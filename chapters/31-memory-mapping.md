# Part IV — Memory Management

# Chapter 31 — Memory Mapping

> **Prerequisites:** Ch. 16 (virtual memory, the TLB & huge pages — `mmap` is how you *get* huge-page-backed memory; faults are page faults), Ch. 28 (page faults & pre-faulting — the cost `mmap` regions incur on first touch), Ch. 29 (custom allocators — `mmap` is where their slabs come from), Ch. 17 (NUMA — first-touch placement of mapped pages), Ch. 8 (caches).
>
> **Leads into:** Ch. 60 (IPC — shared-memory queues built on `mmap`), Ch. 39/42 (lock-free queues / Disruptor — often in shared memory), Ch. 92 (process topology — shared-memory data planes between isolated processes), Ch. 97 (capture/persistence — `mmap`/`O_DIRECT` journals), Ch. 56 (warming — pre-faulting as warm-up). Closes **Part IV**.

---

## 31.1 Why it matters: shared memory and zero-copy I/O

`mmap` is the system call that maps a region of memory — anonymous, a file, or a shared segment — directly into your process's address space, and it underpins three things a low-latency trading system can't do without: **huge-page-backed memory** for the allocators and books of Part IV (Ch. 16, 29), **shared memory** for the fastest possible inter-process communication (Ch. 60, 92), and **zero-copy file I/O** for capture and replay (Ch. 97). It's the lowest-level memory primitive in the toolkit, and the one that makes the difference between two processes copying messages through the kernel (a syscall and a copy each way) and two processes reading and writing *the same physical pages* with nothing but a memory access between them.

The shared-memory case is the headline for HFT. A modern trading system is decomposed into cooperating processes — feed handler, strategy, order gateway, risk (Ch. 92) — for fault isolation, and they must exchange market data and orders at nanosecond latencies. Routing that through sockets or pipes means a kernel round-trip per message (Ch. 48, 57); routing it through a **shared-memory ring buffer** (Ch. 39, 42) means the producer writes to a page and the consumer reads from the *same* page — no syscall, no copy, just a cache-coherence transfer between cores (Ch. 8). `mmap` with `MAP_SHARED` (or a POSIX `shm_open` segment) is how you create that shared region; it's the substrate for the lock-free IPC that ties the process topology together.

But `mmap` inherits — and concentrates — the page-fault hazard of Ch. 16/28, and that's the trap this chapter exists to close. `mmap` returns *address space*, not memory: the pages are unbacked until first touch, when each one **faults** into the kernel (Ch. 28) — so a freshly-mapped region, used naively on the hot path, faults *per page* on the very first messages, exactly when latency matters most. And a file-backed mapping can fault all the way to **disk** (a major fault, milliseconds — catastrophic). The discipline is the same as Ch. 28.4.2, applied at the `mmap` level: **pre-fault** (touch every page, or `MAP_POPULATE`), **`mlock`** (pin resident, no swap, no reclaim), place on the right huge pages (Ch. 16) and NUMA node (Ch. 17) — all on the cold/setup path — so the hot path runs on memory that is mapped, backed, resident, local, and warm. This chapter is the syscall-level mechanics and the warming discipline that give every allocator, book, and IPC queue in the system its zero-fault backing.

## 31.2 Mental model: `mmap`, file-backed vs anonymous, page faulting

**What `mmap` does.** `mmap(addr, len, prot, flags, fd, offset)` creates a new mapping in the process's virtual address space — a range of pages with permissions (`PROT_READ`/`WRITE`/`EXEC`) backed by *something* determined by `flags`/`fd`. It returns a pointer; thereafter that range is ordinary memory you read/write. `munmap` removes it. The variants that matter:

- **Anonymous (`MAP_ANONYMOUS`)** — not backed by a file; zero-filled on first touch. This is "raw memory from the kernel" — what a custom allocator's slab (Ch. 29) or a private huge-page region uses. `malloc` itself uses anonymous `mmap` for large allocations.
- **File-backed (`fd` of an open file)** — the file's contents appear as memory; reads fault pages in from the file, writes (if `MAP_SHARED`) go back to the file. The basis of zero-copy file I/O (read a capture by mapping it — Ch. 97) and demand-paged data.
- **`MAP_PRIVATE` vs `MAP_SHARED`** — `PRIVATE` gives copy-on-write (your writes are private, not seen by others or written back); `SHARED` means writes are visible to *other mappings of the same object* — **the foundation of shared memory** (two processes mapping the same file/segment `MAP_SHARED` share physical pages).
- **`MAP_HUGETLB` / `MAP_POPULATE` / `MAP_LOCKED`** — request huge pages (Ch. 16), pre-fault at map time, and *try* to lock-resident respectively (the warming knobs of §31.4.2; `MAP_LOCKED` is weaker than `mlock` — see there).

```
   process A addr space          physical memory            process B addr space
   ┌───────────────┐          ┌──────────────────┐        ┌───────────────┐
   │  mmap region  │─────────►│  shared pages    │◄───────│  mmap region  │
   │  (MAP_SHARED) │          │  (one copy!)     │        │  (MAP_SHARED) │
   └───────────────┘          └──────────────────┘        └───────────────┘
         write here  ───────────────►  visible here, no syscall, no copy ── read
```

**Shared memory, concretely.** Two ways to get a `MAP_SHARED` region both processes can map:

- **POSIX shared memory:** `shm_open("/name", ...)` returns an fd for a named segment (in `/dev/shm`, a tmpfs — RAM-backed), `ftruncate` sizes it, both processes `mmap(..., MAP_SHARED, fd, ...)`. Clean, named, RAM-backed. (`/dev/shm` can be hugepage-backed.)
- **A `MAP_SHARED` file** (or `memfd_create`): map a regular file or an anonymous in-memory fd shared between related processes.

Either way, the shared region holds the IPC data structures — a lock-free ring buffer (Ch. 39, 42), a seqlock-published snapshot (Ch. 40) — with the **single-writer-per-segment** discipline (Ch. 92) for correctness. Note: pointers stored in shared memory must be **offsets/indices**, not absolute addresses (the segment may map at different addresses in each process — Ch. 30's index-handle idiom is mandatory here).

**Page faulting — the cost, restated for `mmap` (Ch. 16, 28).** A new mapping is *lazy*: no physical page exists until first touch, which **faults** into the kernel to wire one up. Minor fault (anonymous zero page, or file page already in the page cache) ~0.3–1 µs for 4 KiB, several µs for a zeroed 2 MiB THP page; **major fault** (file page must be read from storage) tens to hundreds of µs from NVMe, *milliseconds* from spinning disk or a loaded swap device. So:

- A fresh `mmap` region faults *per page* across your first writes — on the hot path, a stream of stalls (§31.3).
- A file-backed mapping can **major-fault to disk** mid-hot-path — unacceptable; pre-fault and `mlock`, or don't map cold files on the hot path.
- `mlock` pins pages resident so they can't be reclaimed/swapped back out — guaranteeing no *major* fault recurs (`MAP_LOCKED` only *tries*: `mmap(2)` documents that it does not fail if the lock fails, so pages may be missing and fault later).

The model: **`mmap` maps address space (anonymous/file, private/shared) into your process; shared `MAP_SHARED` segments are the substrate for zero-copy IPC; but every mapping is lazily faulted, so the hot-path discipline is to pre-fault, lock, and place (huge pages/NUMA) the region during setup so it's resident and warm before first use.**

## 31.3 Measure it: cost of the first touch vs pre-faulted

The defining `mmap` cost is the **first-touch fault**, and the measurement is to time the first write to each page of a fresh mapping versus a mapping that was pre-faulted (`MAP_POPULATE` or a warm-up pass) during setup. Same region, same writes; only *when* the faults happen differs.

```cpp
// mmapfault.cpp — first-touch fault cost: lazy mmap vs MAP_POPULATE/pre-faulted.
// Build: g++ -O2 -std=c++20 mmapfault.cpp -o mmapfault
// Run pinned:  taskset -c 2 ./mmapfault lazy   |   ./mmapfault prefault
//   faults:  perf stat -e minor-faults,major-faults ./mmapfault lazy   (whole process: BOTH modes fault
//            every page once -- prefault just does it before the loop; the program prints the IN-LOOP delta)
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <algorithm>
#include <chrono>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>

static long minflt() { rusage ru{}; getrusage(RUSAGE_SELF, &ru); return ru.ru_minflt; }

static inline std::uint64_t ns_now() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
    bool prefault = (argc > 1) && std::strcmp(argv[1], "prefault") == 0;
    const long PAGE = sysconf(_SC_PAGESIZE);
    constexpr std::size_t BYTES = std::size_t(256) << 20;      // 256 MiB
    const std::size_t NPAGES = BYTES / PAGE;

    int flags = MAP_PRIVATE | MAP_ANONYMOUS | (prefault ? MAP_POPULATE : 0);  // pre-fault at map?
    auto* p = static_cast<unsigned char*>(
        mmap(nullptr, BYTES, PROT_READ | PROT_WRITE, flags, -1, 0));
    if (p == MAP_FAILED) { std::perror("mmap"); return 1; }
    // Keep THP out of the experiment: with enabled=always a 2 MiB-aligned region faults in 2 MiB
    // pages (one ~20-60 us fault per 512 "first touches"), which is a different -- also useful --
    // measurement. Print the box's setting alongside the results.
    madvise(p, BYTES, MADV_NOHUGEPAGE);
    if (FILE* f = std::fopen("/sys/kernel/mm/transparent_hugepage/enabled", "r")) {
        char thp[96] = {}; if (std::fgets(thp, sizeof thp, f)) std::printf("THP: %s", thp); std::fclose(f);
    }
    if (prefault) std::memset(p, 0, BYTES);                    // belt-and-suspenders warm-up

    // Time the FIRST write to each page (the hot-path "first message" scenario).
    std::vector<std::uint32_t> samples(NPAGES);                // sized, not reserved: its pages are
    const long flt0 = minflt();                                //   faulted BEFORE the timed loop
    for (std::size_t pg = 0; pg < NPAGES; ++pg) {
        std::uint64_t t0 = ns_now();
        p[pg * PAGE] = 1;                                       // touch one byte per page
        samples[pg] = std::uint32_t(ns_now() - t0);
    }
    const long flt_loop = minflt() - flt0;                     // minor faults taken IN the loop
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double q){ return samples[std::size_t(q * (NPAGES - 1))]; };
    std::printf("%-9s pages=%zu  p50=%u p99=%u p99.9=%u max=%u ns  minor-faults in loop=%ld\n",
                prefault ? "prefault" : "lazy", NPAGES,
                pct(0.50), pct(0.99), pct(0.999), samples.back(), flt_loop);
    munmap(p, BYTES);
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), pinned, turbo off (illustrative; the *shape* is the point):

```
                       p50        p99        p99.9      max        minor-faults IN the loop (ru_minflt delta)
lazy (first touch)     ~400 ns    ~900 ns    ~1,500 ns  ~6,000 ns  ~NPAGES (one per page)  <- faults on the path
prefault (POPULATE)    ~30 ns     ~35 ns     ~60 ns     ~300 ns    0                       <- already resident: the ~30 ns is
                                                                                              the timer's own cost + a dTLB miss per
                                                                                              page; the store itself is ~1 ns
(THP: always [madvise] never — the program prints the box's mode; the listing opts out with MADV_NOHUGEPAGE)
```

Read it the Ch. 28 way: the lazy mapping's first-touch loop is a **page fault per page** — each write traps into the kernel (~0.3–1 µs to wire up and zero a frame), so the "first message to touch fresh memory" pays a fault, and `perf` shows ~one minor-fault per page. The pre-faulted version did all that faulting at `mmap`/`memset` time (setup, the cold path), so the same writes are now plain stores into resident memory — the ~30 ns the harness reports is almost entirely the two `steady_clock::now()` calls plus a dTLB miss per page; the store itself is ~1 ns — flat, no tail, no faults. This is the entire argument for §31.4.2 in one chart: **the fault cost is unavoidable, but pre-faulting moves it off the hot path.** The fingerprint to watch for in production: non-zero `minor-faults` (or, worse, `major-faults`) on the hot path means un-warmed mapped memory — pre-fault and `mlock` it. (Major faults — file-backed to disk — would show millisecond maxes here; never let those touch the hot path.)

## 31.4 Techniques

### 31.4.1 Shared-memory regions

Building the zero-copy IPC substrate (Ch. 60, 92) on `mmap`:

- **Create a named, RAM-backed segment.** `shm_open("/feed", O_CREAT|O_RDWR, 0600)` → `ftruncate(fd, size)` → `mmap(nullptr, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0)` in each process. The segment lives in `/dev/shm` (tmpfs, RAM); both processes now share the same physical pages. (`memfd_create` + fd-passing is an alternative for related processes.)
- **Put a lock-free structure in it.** The shared region holds the IPC data plane — an SPSC/MPSC **ring buffer** (Ch. 39, 42) or a **seqlock-published** snapshot (Ch. 40) — so producer and consumer communicate with atomics and memory ordering (Ch. 35), no syscall per message. This is the fast tick-to-trade IPC path.
- **Offsets, not pointers.** A `MAP_SHARED` segment may map at *different virtual addresses* in each process, so **never store absolute pointers** in it — store **offsets from the segment base** or **indices** (Ch. 30.4.3). A pointer written by process A is meaningless in process B. This is the single most common shared-memory bug.
- **Single-writer-per-segment discipline (Ch. 92).** Assign each shared region exactly one writer process; many readers. This makes the memory model tractable (seqlock/SPSC patterns) and contains faults — the foundation of the fault-isolated process topology (Ch. 92).
- **Layout and false sharing (Ch. 38).** The shared structure's hot indices (head/tail of a ring) are written by different cores/processes; pad them to separate cache lines (`hardware_destructive_interference_size`) or they ping-pong across the interconnect (Ch. 17, 38) — the most expensive memory event, now across processes.
- **Lifecycle.** `shm_unlink` removes the name (the segment persists until all maps close); design startup/shutdown/crash-recovery so a crashed writer's segment can be re-attached or recreated cleanly (Ch. 92's crash-only design; the reattach protocol — header checks, owner liveness, epochs and torn-write recovery — is Ch. 92 §92.4.8). Back the segment with **explicit huge pages** for TLB friendliness (Ch. 16): a file on a hugetlbfs mount (e.g. `/dev/hugepages/feed`) mapped `MAP_SHARED`, `memfd_create(name, MFD_HUGETLB)` (Linux ≥ 4.14) with the fd passed between processes, or SysV `shmget(..., SHM_HUGETLB)`. `MAP_HUGETLB` on a `shm_open` (tmpfs) fd fails with `EINVAL` — the flag is accepted only for anonymous mappings and hugetlbfs/`MFD_HUGETLB` descriptors — and remounting `/dev/shm` as hugetlbfs breaks `shm_open`/`ftruncate` for everything else on the box.

### 31.4.2 Pre-faulting and `mlock`

The warming discipline that makes any mapping — allocator slab, book, shared ring — hot-path-safe (Ch. 28.4.2, 56):

- **Pre-fault the whole region at setup.** `MAP_POPULATE` faults the mapping in at `mmap` time; or do a warm-up pass writing one byte per page (stride by page/huge-page size). Either way, every page is backed *before* the hot path runs (§31.3). For file-backed maps, `MAP_POPULATE`/`readahead`/`madvise(MADV_WILLNEED)` pull pages from disk during setup so no *major* fault hits later.
- **`mlock`/`mlockall` to pin resident.** Faulting maps a page; `mlock(addr, len)` (or `mlockall(MCL_CURRENT|MCL_FUTURE)`) **pins** it so the kernel can't reclaim or swap it out — guaranteeing the page stays resident and no future fault (especially no major fault) recurs. `MAP_LOCKED` is *not* equivalent: `mmap(2)` documents that it tries to populate and lock the range but does not fail if that fails, so pages can be missing and major-fault later — "use mmap() plus mlock(2) when major faults are not acceptable." Standard on a latency box, together with **swap disabled** (Appendix C). Mind the `RLIMIT_MEMLOCK` limit.
- **Place on the right huge pages and NUMA node (Ch. 16, 17).** Map with `MAP_HUGETLB` (reserve hugepages at boot — Ch. 16) for fewer TLB misses on a large region; pre-fault **from the thread that will use it, pinned to its NUMA node** (Ch. 17's first-touch), so the pages land local. A coarse mistake here strands a 2 MB/1 GB page remote.
- **`madvise` to tune kernel behavior.** `MADV_WILLNEED` (prefetch), `MADV_HUGEPAGE` (encourage THP — but prefer explicit hugetlb on a jitter box, Ch. 16), `MADV_DONTNEED` (drop pages — a cold-path tool), `MADV_RANDOM`/`SEQUENTIAL` (readahead hints). Use deliberately; the defaults aren't tuned for latency.
- **This is warm-up (Ch. 56).** Pre-faulting + `mlock` is the memory half of keeping the hot path warm — alongside cache/TLB/branch warming (Ch. 56). The first real message must hit memory that's mapped, faulted, resident, local, and huge-page-backed; all of that happens during startup, never on the path.

## 31.5 Pitfalls & anti-patterns: lazy faults on the hot path

- **Using a fresh `mmap` region on the hot path un-warmed.** The headline trap: map a buffer/ring/slab and start using it on the hot path — every first-touched page **faults** (§31.3), a stream of stalls on the first messages. **Pre-fault + `mlock` during setup** (§31.4.2). A "pre-allocated" but un-faulted region is not ready.
- **Major faults from file-backed maps.** A file mapping faulting to **storage** mid-hot-path is tens-to-hundreds of µs from NVMe, *milliseconds* from disk or swap (Ch. 28) — session-ruining either way. Pre-fault/`MAP_POPULATE`/`mlock` the mapped file during setup, ensure it fits the page cache, and disable swap. Never demand-page a cold file on the hot path.
- **Storing absolute pointers in shared memory.** A `MAP_SHARED` segment maps at different addresses per process, so a stored pointer is garbage in the other process — corruption/crash. Use **offsets/indices** (§31.4.1). The most common shared-memory bug.
- **Forgetting the single-writer discipline / racing the shared region.** Multiple writers to one shared segment without a correct lock-free protocol (Ch. 39–40) is a cross-process data race — UB across the worst possible boundary. Single-writer-per-segment (Ch. 92), or a proven MPSC/lock-free structure with correct memory ordering (Ch. 35).
- **False sharing across processes (Ch. 38).** The ring's head/tail (written by different processes/cores) on the same cache line ping-pongs over the interconnect — expensive (Ch. 17). Pad hot shared indices to separate cache lines.
- **`mlock` without raising `RLIMIT_MEMLOCK` / running out of locked memory.** `mlock` fails or partially locks if the limit is too low; verify the limit and that the lock succeeded. Locking *too much* (or `mlockall(MCL_FUTURE)` with a leaky process) can pressure the system — lock the hot regions deliberately.
- **NUMA-blind pre-faulting of a mapped region (Ch. 17).** First-touch places the (possibly huge) page on the toucher's node; pre-faulting a shared/huge region from the wrong thread strands it remote. Fault from the using thread on the right node.
- **Leaking shared segments / unclean lifecycle.** A crashed process can leave a `/dev/shm` segment behind; without `shm_unlink`/recovery logic, restarts accumulate or re-attach to stale state. Design crash-only re-attach/recreate (Ch. 92 §92.4.8).
- **Assuming `munmap`/`MADV_DONTNEED` is free or instant on the hot path.** Unmapping and re-mapping per use reintroduces faults (and TLB shootdowns across cores). Map once at setup, keep it; tear down on the cold path.

## 31.6 Exercises & checklist

**Exercises**

1. **Measure the first-touch tail.** Build `mmapfault.cpp`; run `lazy` vs `prefault` pinned with `perf stat -e minor-faults,major-faults`. Confirm: lazy faults ~once per page with a long tail; prefault is flat with 0 faults *in the loop* (the program prints the in-loop `ru_minflt` delta — `perf stat`'s whole-process total is ~NPAGES in *both* runs, because `MAP_POPULATE`+`memset` fault every page before the loop). Where did the fault cost go?
2. **Major-fault a file.** `mmap` a large file `MAP_PRIVATE` *without* `MAP_POPULATE`, drop the page cache (`echo 1 > /proc/sys/vm/drop_caches`, root, test box), and time first-touch — observe millisecond **major** faults. Then `MAP_POPULATE`/`madvise(WILLNEED)` + `mlock` and re-measure.
3. **Shared-memory ring.** Build a `shm_open`/`MAP_SHARED` segment holding an SPSC ring (Ch. 39); have a producer process write and a consumer process read. Measure cross-process message latency vs a pipe/socket (Ch. 57). Confirm offsets (not pointers) are used.
4. **Pointer-vs-offset bug.** Deliberately store an absolute pointer in the shared segment and read it from the other process (force different map addresses by passing distinct non-NULL `addr` hints *without* `MAP_FIXED`, or with `MAP_FIXED_NOREPLACE` (Linux ≥ 4.17), which fails with `EEXIST` rather than overwriting — bare `MAP_FIXED` is not a hint: it silently unmaps whatever already lives at that address, libc and heap included). Watch it break; fix with offsets.
5. **Huge-page + NUMA placement.** Map a large region `MAP_HUGETLB` (reserve hugepages — Ch. 16), pre-fault from a thread pinned to node 0 vs node 1, and measure access latency from node 0 (Ch. 17). Confirm first-touch placed the huge pages on the faulting thread's node.

**Checklist — memory mapping**

- [ ] Hot-path mapped regions (allocator slabs, books, rings) are **pre-faulted** (`MAP_POPULATE`/warm-up pass) and **`mlock`ed** during setup — no first-touch fault on the path (§31.4.2).
- [ ] **No major faults** possible on the hot path: file maps pre-faulted/resident, swap disabled, working set fits RAM/page-cache (Ch. 28, Appendix C).
- [ ] Shared memory uses **`MAP_SHARED`** (`shm_open`/`memfd`) with **offsets/indices, never absolute pointers** (§31.4.1).
- [ ] Each shared segment has a **single writer** (Ch. 92) or a proven lock-free protocol (Ch. 39–40) with correct memory ordering (Ch. 35).
- [ ] Hot shared indices (ring head/tail) are **cache-line-padded** to avoid cross-process false sharing (Ch. 38).
- [ ] Large regions are **huge-page-backed** (`MAP_HUGETLB` — Ch. 16) and **pre-faulted from the using thread on the right NUMA node** (Ch. 17).
- [ ] `RLIMIT_MEMLOCK` is sufficient and `mlock` success is **verified**; mappings are created **once at setup** and torn down on the cold path (no per-use map/unmap).
- [ ] Shared-segment **lifecycle/crash-recovery** is designed (`shm_unlink`, re-attach/recreate — Ch. 92 §92.4.8), and pre-faulting is part of **warm-up** (Ch. 56).

## 31.7 References

- The Linux man pages — `mmap(2)` (`MAP_SHARED`/`PRIVATE`/`ANONYMOUS`/`POPULATE`/`HUGETLB`/`LOCKED`), `munmap(2)`, `mlock(2)`/`mlockall(2)`, `madvise(2)`, `shm_open(3)`, `memfd_create(2)`, `ftruncate(2)` — the authoritative interface for this chapter.
- U. Drepper, *What Every Programmer Should Know About Memory* — virtual memory, mapping, and the page-fault/`mlock` mechanics underpinning §31.2-§31.4.
- W. R. Stevens & S. Rago, *Advanced Programming in the UNIX Environment*, and Stevens, *UNIX Network Programming, Vol. 2 (IPC)* — POSIX shared memory and memory-mapped I/O patterns (§31.4.1).
- The Linux `Documentation/admin-guide/mm/` (hugetlbpage, transparent_hugepage) and `vm` sysctl docs — huge-page-backed mappings and overcommit/locking (ties Ch. 16).
- The kernel `Documentation` on `O_DIRECT`/page cache and the `tmpfs`/`/dev/shm` docs — file-backed and RAM-backed mapping behavior (ties Ch. 97).

## 31.8 Additional Reading

- M. Thompson / Mechanical Sympathy writings and the Aeron messaging system — shared-memory ring buffers and zero-copy IPC in practice (ties Ch. 42, 60).
- jemalloc/tcmalloc and kernel `mmap`-allocation writeups — how general allocators use anonymous `mmap`, for contrast with the custom slabs of Ch. 29.
- Ch. 16 (*TLB & Huge Pages*) — huge-page backing for mappings; Ch. 28–29 (*Memory Fundamentals / Allocators*) — the faults and slabs `mmap` provides; Ch. 60 (*IPC*) — shared-memory queues; Ch. 92 (*Process Topology*) — shared-memory data planes and single-writer discipline; Ch. 97 (*Capture & Replay*) — `mmap`/`O_DIRECT` journals; Ch. 56 (*Warming*) — pre-faulting as warm-up.
- **Appendix C** (System Tuning Checklist) — `mlockall`, swap, hugepage, and `RLIMIT_MEMLOCK` settings; **Appendix E** — the page-fault latency numbers framing §31.3.

---

*Next: Ch. 32 — Fixed-Point & Floating-Point Arithmetic, opening Part V. With memory mastered, we turn to numbers themselves: representing prices safely with scaled integers vs decimal vs binary float, tick-size handling, and the floating-point hazards — denormals, FTZ/DAZ, determinism — that lurk on the hot path.*
