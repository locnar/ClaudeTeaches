# Part IV — Memory Management

# Chapter 34 — Memory Mapping

> **Prerequisites:** Ch. 17 (virtual memory, the TLB & huge pages — `mmap` is how you *get* huge-page-backed memory; faults are page faults), Ch. 30 (page faults & pre-faulting — the cost `mmap` regions incur on first touch), Ch. 31 (custom allocators — `mmap` is where their slabs come from), Ch. 18 (NUMA — first-touch placement of mapped pages), Ch. 8 (caches).
>
> **Leads into:** Ch. 67 (IPC — shared-memory queues built on `mmap`), Ch. 45/48 (lock-free queues / Disruptor — often in shared memory), Ch. 103 (process topology — shared-memory data planes between isolated processes), Ch. 109 (capture/persistence — `mmap`/`O_DIRECT` journals), Ch. 62 (warming — pre-faulting as warm-up). Closes **Part IV**.

---

## 34.1 Why it matters: shared memory and zero-copy I/O

`mmap` is the system call that maps a region of memory — anonymous, a file, or a shared segment — directly into your process's address space, and it underpins three things a low-latency trading system can't do without: **huge-page-backed memory** for the allocators and books of Part IV (Ch. 17, 31), **shared memory** for the fastest possible inter-process communication (Ch. 67, 103), and **zero-copy file I/O** for capture and replay (Ch. 109). It's the lowest-level memory primitive in the toolkit, and the one that makes the difference between two processes copying messages through the kernel (a syscall and a copy each way) and two processes reading and writing *the same physical pages* with nothing but a memory access between them.

The shared-memory case is the headline for HFT. A modern trading system is decomposed into cooperating processes — feed handler, strategy, order gateway, risk (Ch. 103) — for fault isolation, and they must exchange market data and orders at nanosecond latencies. Routing that through sockets or pipes means a kernel round-trip per message (Ch. 54, 63); routing it through a **shared-memory ring buffer** (Ch. 45, 48) means the producer writes to a page and the consumer reads from the *same* page — no syscall, no copy, just a cache-coherence transfer between cores (Ch. 8). `mmap` with `MAP_SHARED` (or a POSIX `shm_open` segment) is how you create that shared region; it's the substrate for the lock-free IPC that ties the process topology together.

But `mmap` inherits — and concentrates — the page-fault hazard of Ch. 17/30, and that's the trap this chapter exists to close. `mmap` returns *address space*, not memory: the pages are unbacked until first touch, when each one **faults** into the kernel (Ch. 30) — so a freshly-mapped region, used naively on the hot path, faults *per page* on the very first messages, exactly when latency matters most. And a file-backed mapping can fault all the way to **disk** (a major fault, milliseconds — catastrophic). The discipline is the same as §30.4.2, applied at the `mmap` level: **pre-fault** (touch every page, or `MAP_POPULATE`), **`mlock`** (pin resident, no swap, no reclaim), place on the right huge pages (Ch. 17) and NUMA node (Ch. 18) — all on the cold/setup path — so the hot path runs on memory that is mapped, backed, resident, local, and warm. This chapter is the syscall-level mechanics and the warming discipline that give every allocator, book, and IPC queue in the system its zero-fault backing.

## 34.2 Mental model: `mmap`, file-backed vs anonymous, page faulting

**What `mmap` does.** `mmap(addr, len, prot, flags, fd, offset)` creates a new mapping in the process's virtual address space — a range of pages with permissions (`PROT_READ`/`WRITE`/`EXEC`) backed by *something* determined by `flags`/`fd`. It returns a pointer; thereafter that range is ordinary memory you read/write. `munmap` removes it. The variants that matter:

- **Anonymous (`MAP_ANONYMOUS`)** — not backed by a file; zero-filled on first touch. This is "raw memory from the kernel" — what a custom allocator's slab (Ch. 31) or a private huge-page region uses. `malloc` itself uses anonymous `mmap` for large allocations.
- **File-backed (`fd` of an open file)** — the file's contents appear as memory; reads fault pages in from the file, writes (if `MAP_SHARED`) go back to the file. The basis of zero-copy file I/O (read a capture by mapping it — Ch. 109) and demand-paged data.
- **`MAP_PRIVATE` vs `MAP_SHARED`** — `PRIVATE` gives copy-on-write (your writes are private, not seen by others or written back); `SHARED` means writes are visible to *other mappings of the same object* — **the foundation of shared memory** (two processes mapping the same file/segment `MAP_SHARED` share physical pages).
- **`MAP_HUGETLB` / `MAP_POPULATE` / `MAP_LOCKED`** — request huge pages (Ch. 17), pre-fault at map time, and *try* to lock-resident respectively (the warming knobs of §34.4.2; `MAP_LOCKED` is weaker than `mlock` — see there).

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

Either way, the shared region holds the IPC data structures — a lock-free ring buffer (Ch. 45, 48), a seqlock-published snapshot (Ch. 46) — with the **single-writer-per-segment** discipline (Ch. 103) for correctness. Note: pointers stored in shared memory must be **offsets/indices**, not absolute addresses (the segment may map at different addresses in each process — Ch. 32's index-handle idiom is mandatory here).

**Page faulting — the cost, restated for `mmap` (Ch. 17, 30).** A new mapping is *lazy*: no physical page exists until first touch, which **faults** into the kernel to wire one up. Minor fault (anonymous zero page, or file page already in the page cache) ~0.3–1 µs for 4 KiB, several µs for a zeroed 2 MiB THP page; **major fault** (file page must be read from storage) tens to hundreds of µs from NVMe, *milliseconds* from spinning disk or a loaded swap device. So:

- A fresh `mmap` region faults *per page* across your first writes — on the hot path, a stream of stalls (§34.3).
- A file-backed mapping can **major-fault to disk** mid-hot-path — unacceptable; pre-fault and `mlock`, or don't map cold files on the hot path.
- `mlock` pins pages resident so they can't be reclaimed/swapped back out — guaranteeing no *major* fault recurs (`MAP_LOCKED` only *tries*: `mmap(2)` documents that it does not fail if the lock fails, so pages may be missing and fault later).

The model: **`mmap` maps address space (anonymous/file, private/shared) into your process; shared `MAP_SHARED` segments are the substrate for zero-copy IPC; but every mapping is lazily faulted, so the hot-path discipline is to pre-fault, lock, and place (huge pages/NUMA) the region during setup so it's resident and warm before first use.**

## 34.3 Measure it: cost of the first touch vs pre-faulted

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

Read it the Ch. 30 way: the lazy mapping's first-touch loop is a **page fault per page** — each write traps into the kernel (~0.3–1 µs to wire up and zero a frame), so the "first message to touch fresh memory" pays a fault, and `perf` shows ~one minor-fault per page. The pre-faulted version did all that faulting at `mmap`/`memset` time (setup, the cold path), so the same writes are now plain stores into resident memory — the ~30 ns the harness reports is almost entirely the two `steady_clock::now()` calls plus a dTLB miss per page; the store itself is ~1 ns — flat, no tail, no faults. This is the entire argument for §34.4.2 in one chart: **the fault cost is unavoidable, but pre-faulting moves it off the hot path.** The fingerprint to watch for in production: non-zero `minor-faults` (or, worse, `major-faults`) on the hot path means un-warmed mapped memory — pre-fault and `mlock` it. (Major faults — file-backed to disk — would show millisecond maxes here; never let those touch the hot path.)

## 34.4 Techniques

### 34.4.1 Shared-memory regions

Building the zero-copy IPC substrate (Ch. 67, 103) on `mmap`:

- **Create a named, RAM-backed segment.** `shm_open("/feed", O_CREAT|O_RDWR, 0600)` → `ftruncate(fd, size)` → `mmap(nullptr, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0)` in each process. The segment lives in `/dev/shm` (tmpfs, RAM); both processes now share the same physical pages. (`memfd_create` + fd-passing is an alternative for related processes.)
- **Put a lock-free structure in it.** The shared region holds the IPC data plane — an SPSC/MPSC **ring buffer** (Ch. 45, 48) or a **seqlock-published** snapshot (Ch. 46) — so producer and consumer communicate with atomics and memory ordering (Ch. 41), no syscall per message. This is the fast tick-to-trade IPC path.
- **Offsets, not pointers.** A `MAP_SHARED` segment may map at *different virtual addresses* in each process, so **never store absolute pointers** in it — store **offsets from the segment base** or **indices** (§32.4.3). A pointer written by process A is meaningless in process B. This is the single most common shared-memory bug.
- **Single-writer-per-segment discipline (Ch. 103).** Assign each shared region exactly one writer process; many readers. This makes the memory model tractable (seqlock/SPSC patterns) and contains faults — the foundation of the fault-isolated process topology (Ch. 103).
- **Layout and false sharing (Ch. 44).** The shared structure's hot indices (head/tail of a ring) are written by different cores/processes; pad them to separate cache lines (`hardware_destructive_interference_size`) or they ping-pong across the interconnect (Ch. 18, 44) — the most expensive memory event, now across processes.
- **Lifecycle.** `shm_unlink` removes the name (the segment persists until all maps close); design startup/shutdown/crash-recovery so a crashed writer's segment can be re-attached or recreated cleanly (Ch. 103's crash-only design; the reattach protocol — header checks, owner liveness, epochs and torn-write recovery — is Ch. 103 §103.4.8). Back the segment with **explicit huge pages** for TLB friendliness (Ch. 17): a file on a hugetlbfs mount (e.g. `/dev/hugepages/feed`) mapped `MAP_SHARED`, `memfd_create(name, MFD_HUGETLB)` (Linux ≥ 4.14) with the fd passed between processes, or SysV `shmget(..., SHM_HUGETLB)`. `MAP_HUGETLB` on a `shm_open` (tmpfs) fd fails with `EINVAL` — the flag is accepted only for anonymous mappings and hugetlbfs/`MFD_HUGETLB` descriptors — and remounting `/dev/shm` as hugetlbfs breaks `shm_open`/`ftruncate` for everything else on the box.

### 34.4.2 Pre-faulting and `mlock`

The warming discipline that makes any mapping — allocator slab, book, shared ring — hot-path-safe (§30.4.2, Ch. 62):

- **Pre-fault the whole region at setup.** `MAP_POPULATE` faults the mapping in at `mmap` time; or do a warm-up pass writing one byte per page (stride by page/huge-page size). Either way, every page is backed *before* the hot path runs (§34.3). For file-backed maps, `MAP_POPULATE`/`readahead`/`madvise(MADV_WILLNEED)` pull pages from disk during setup so no *major* fault hits later.
- **`mlock`/`mlockall` to pin resident.** Faulting maps a page; `mlock(addr, len)` (or `mlockall(MCL_CURRENT|MCL_FUTURE)`) **pins** it so the kernel can't reclaim or swap it out — guaranteeing the page stays resident and no future fault (especially no major fault) recurs. `MAP_LOCKED` is *not* equivalent: `mmap(2)` documents that it tries to populate and lock the range but does not fail if that fails, so pages can be missing and major-fault later — "use mmap() plus mlock(2) when major faults are not acceptable." Standard on a latency box, together with **swap disabled** (Appendix C). Mind the `RLIMIT_MEMLOCK` limit.
- **Place on the right huge pages and NUMA node (Ch. 17, 18).** Map with `MAP_HUGETLB` (reserve hugepages at boot — Ch. 17) for fewer TLB misses on a large region; pre-fault **from the thread that will use it, pinned to its NUMA node** (Ch. 18's first-touch), so the pages land local. A coarse mistake here strands a 2 MB/1 GB page remote.
- **`madvise` to tune kernel behavior.** `MADV_WILLNEED` (prefetch), `MADV_HUGEPAGE` (encourage THP — but prefer explicit hugetlb on a jitter box, Ch. 17), `MADV_DONTNEED` (drop pages — a cold-path tool), `MADV_RANDOM`/`SEQUENTIAL` (readahead hints). Use deliberately; the defaults aren't tuned for latency.
- **This is warm-up (Ch. 62).** Pre-faulting + `mlock` is the memory half of keeping the hot path warm — alongside cache/TLB/branch warming (Ch. 62). The first real message must hit memory that's mapped, faulted, resident, local, and huge-page-backed; all of that happens during startup, never on the path.

### 34.4.3 Beyond `MAP_POPULATE`: the double-mapped ring, `mremap`, `MCL_ONFAULT`, `MADV_POPULATE_*`, `MADV_COLLAPSE`, `userfaultfd`

*Toolchain and machine: Appendix L.3; the numbers in this section are measured on the 4-vCPU KVM guest (kernel 6.18, g++ 13.3 `-O2`; clang++ 18.1 within 3 % on every row) and are a guest's — nested paging makes every first touch dearer than bare metal — so the ratios are the lesson.*

Five mapping tricks that §34.4.2's pre-fault-and-`mlock` discipline does not cover, each with the kernel version it needs:

**(a) The double-mapped ("magic") ring.** A byte ring that carries variable-length messages (Ch. 45 §45.4.2's "separate byte-ring", Ch. 70 §70.4.4's reorder buffer, Ch. 67's IPC rings) has an end, and a message that straddles it is two pieces. The usual fixes — a bounce-buffer copy, or padding the tail and wasting it — cost a copy or a branch per message. The mapping fix makes the wrap disappear: back the ring with a `memfd` and map the *same* pages **twice, back to back**, so byte `size + k` is byte `k`. A reader at offset `off` with `off + len > size` reads straight through; a writer likewise. The second mapping costs page-table entries, not memory:

```cpp
// magic_ring.cpp — the double-mapped ring: a memfd mapped twice, back to back, so a message that straddles the end
// of the ring is contiguous in virtual memory and needs no split copy.   Build: g++ -O2 -std=c++20 magic_ring.cpp
#include <cstdint>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
struct MagicRing {
    std::uint8_t* base = nullptr; std::size_t size = 0;      // [base, base+size) and [base+size, base+2*size) alias
    bool init(std::size_t sz) {                               // sz: a multiple of the page size (2 MiB for MFD_HUGETLB)
        int fd = memfd_create("ring", MFD_CLOEXEC);           // MFD_HUGETLB (4.14+) for a 2 MiB-backed ring: one TLB entry per 2 MiB
        if (fd < 0 || ftruncate(fd, sz) < 0) return false;
        void* reserve = mmap(nullptr, 2 * sz, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);   // grab 2*sz of VA, contiguous
        if (reserve == MAP_FAILED) return false;
        void* a = mmap(reserve, sz, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED, fd, 0);
        void* b = mmap(static_cast<std::uint8_t*>(reserve) + sz, sz, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED, fd, 0);
        close(fd);                                            // the pages live on while mapped
        if (a == MAP_FAILED || b == MAP_FAILED) return false;
        base = static_cast<std::uint8_t*>(a); size = sz;
        std::memset(base, 0, sz);                             // pre-fault (§34.4.2); mlock(base, 2*sz) on a real box
        return true;
    }
    // a message at `off` of `len` bytes is always `base + off .. base + off + len`, even when off + len > size
};
```

Measured against the split-copy alternative (a plain ring reassembling a straddling message into a bounce buffer before the same 8-byte-stride "decode"), 200,000 messages per row, every message straddling the end:

```
   straddling   64-byte message: split copy + decode   10.7 ns, in-place decode    4.6 ns
   straddling  256-byte message: split copy + decode   27.5 ns, in-place decode   17.6 ns
   straddling 1024-byte message: split copy + decode   64.1 ns, in-place decode   49.7 ns
   straddling 4096-byte message: split copy + decode  200.9 ns, in-place decode  159.2 ns
```

The saving is the copy (~6 ns + 10 ns/KiB here) *and* the bounce buffer's cache lines *and* the branch — and, more valuable than any of them, the decoder (Ch. 70's overlay structs, `std::span`) sees contiguous bytes without a special case. The cost is virtual address space (2×), two TLB entries per page instead of one (use `MFD_HUGETLB` so it is two per 2 MiB), and the `MAP_FIXED` dance, which must reserve first (`PROT_NONE`) or it can clobber a neighbour. The same trick works for a hugetlbfs file and for a `shm_open` segment shared across processes (Ch. 67).

**(b) `mremap` for cold-path growth.** An arena (Ch. 31) that must grow does not have to copy: `mremap(old, old_len, new_len, MREMAP_MAYMOVE)` moves the *page-table entries* to a larger virtual range — 128 → 256 MiB in **41 µs** vs **69 ms** for `mmap(POPULATE)` + `memcpy` of the same 128 MiB. The region may move, so it is for offset-addressed data (§34.4.1) or for control-plane structures on the cold path; `MREMAP_DONTUNMAP` (5.7+, with `MREMAP_MAYMOVE`) leaves the old range mapped and empty — a way to hand a populated region to a new address and keep the old one as a fresh, zero-filled buffer without faulting. Never on the hot path: it takes `mmap_lock` and triggers a TLB flush on every core running the process (§34.5).

**(c) `MCL_ONFAULT`, `MADV_POPULATE_READ/WRITE`, and what to pre-fault with.** Four ways to turn virtual into resident, measured on 256 MiB (65,536 4 KiB pages):

```
   mmap + touch loop (one write per page)          562.8 ms  (8.59 us/page)    <- a page fault per page, each a VM exit in the guest
   mmap(MAP_POPULATE)                               51.1 ms  (0.78 us/page)    <- one syscall, kernel-side fault loop
   mmap + madvise(MADV_POPULATE_WRITE)  [5.14+]     61.8 ms  (0.94 us/page)    <- same, but on an EXISTING mapping, any range
   mmap + mlock (populates and pins)                50.2 ms  (0.77 us/page)    <- populate + lock in one
```

`MAP_POPULATE` only exists at `mmap` time; **`MADV_POPULATE_WRITE`** (kernel 5.14) does the same for a range you already have — a sub-range of a huge arena, the pages a `mremap` just added, a shared segment another process created — and `MADV_POPULATE_READ` maps the zero page / file pages without dirtying them. Both are ~10× cheaper per page than touching from user space even on bare metal (no exception entry per page) and ~11× here. **`mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT)`** (4.4+) is the other half: plain `MCL_FUTURE` *populates* every future mapping at `mmap` time — including the 8 MiB stack of every thread you create and every `dlopen`ed library — which is why `mlockall` makes startup slow and `RLIMIT_MEMLOCK` overflow; `MCL_ONFAULT` locks pages *as they fault* instead, so you pre-fault the regions you choose (with `POPULATE_WRITE`) and the rest locks lazily. The combination for a trading process: `mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT)` at startup, then `MADV_POPULATE_WRITE` on every hot region, then the §34.4.2 verification that `VmLck` in `/proc/self/status` covers what you meant.

**(d) `MADV_COLLAPSE` (6.1+): THP on your terms.** Ch. 17 §17.5 sends `khugepaged` and fault-time compaction off the box (`enabled=never` or `madvise` with `defrag` off). `madvise(range, MADV_COLLAPSE)` collapses a range into huge pages *synchronously, now, on the calling thread* — at setup, where you want the cost — and fails cleanly (`EAGAIN`/`ENOMEM`) if the memory cannot be assembled, instead of a background daemon doing it at 10:00. The recipe for a region that cannot be hugetlbfs-backed (a `malloc` arena, a library's static data): `MADV_HUGEPAGE` on it, `MADV_POPULATE_WRITE`, then `MADV_COLLAPSE`, then check `AnonHugePages` in `/proc/self/smaps` for the range. (On the guest, `MADV_HUGEPAGE` + touch took 672 ms for 256 MiB — *slower* than 4 KiB pages, because the host had no huge pages to give; `MADV_COLLAPSE` would have returned `ENOMEM` and told you so.)

**(e) `userfaultfd`: never on the hot path, useful off it.** A registered range's page faults are delivered to a user-space thread, which supplies the page (`UFFDIO_COPY`/`ZEROPAGE`) — the mechanism behind live migration and lazy snapshot restore (Ch. 103 §103.4.7). Measured: a `userfaultfd`-served first touch is **2.8 µs min, 3.4 µs mean, 65.7 µs max** per page here (two context switches and the copy) — three orders of magnitude over a resident access. It is a cold-path tool for restoring a checkpoint page-on-demand while the warm-up proceeds, with `MADV_POPULATE_WRITE` over the whole range before the readiness gate so no fault survives into the session; `vm.unprivileged_userfaultfd=0` (the default since 5.11) means the trading unit needs `CAP_SYS_PTRACE` or the sysctl to use it at all.

## 34.5 Pitfalls & anti-patterns: lazy faults on the hot path; the self-inflicted TLB shootdown

- **Using a fresh `mmap` region on the hot path un-warmed.** The headline trap: map a buffer/ring/slab and start using it on the hot path — every first-touched page **faults** (§34.3), a stream of stalls on the first messages. **Pre-fault + `mlock` during setup** (§34.4.2). A "pre-allocated" but un-faulted region is not ready.
- **Major faults from file-backed maps.** A file mapping faulting to **storage** mid-hot-path is tens-to-hundreds of µs from NVMe, *milliseconds* from disk or swap (Ch. 30) — session-ruining either way. Pre-fault/`MAP_POPULATE`/`mlock` the mapped file during setup, ensure it fits the page cache, and disable swap. Never demand-page a cold file on the hot path.
- **Storing absolute pointers in shared memory.** A `MAP_SHARED` segment maps at different addresses per process, so a stored pointer is garbage in the other process — corruption/crash. Use **offsets/indices** (§34.4.1). The most common shared-memory bug.
- **Forgetting the single-writer discipline / racing the shared region.** Multiple writers to one shared segment without a correct lock-free protocol (Ch. 45–46) is a cross-process data race — UB across the worst possible boundary. Single-writer-per-segment (Ch. 103), or a proven MPSC/lock-free structure with correct memory ordering (Ch. 41).
- **False sharing across processes (Ch. 44).** The ring's head/tail (written by different processes/cores) on the same cache line ping-pongs over the interconnect — expensive (Ch. 18). Pad hot shared indices to separate cache lines.
- **`mlock` without raising `RLIMIT_MEMLOCK` / running out of locked memory.** `mlock` fails or partially locks if the limit is too low; verify the limit and that the lock succeeded. Locking *too much* (or `mlockall(MCL_FUTURE)` with a leaky process) can pressure the system — lock the hot regions deliberately.
- **NUMA-blind pre-faulting of a mapped region (Ch. 18).** First-touch places the (possibly huge) page on the toucher's node; pre-faulting a shared/huge region from the wrong thread strands it remote. Fault from the using thread on the right node.
- **Leaking shared segments / unclean lifecycle.** A crashed process can leave a `/dev/shm` segment behind; without `shm_unlink`/recovery logic, restarts accumulate or re-attach to stale state. Design crash-only re-attach/recreate (Ch. 103 §103.4.8).
- **The self-inflicted TLB shootdown: a housekeeping thread's `munmap`/`mprotect`/`MADV_DONTNEED` interrupts the hot core.** Every thread of a process shares its page tables, so when *any* thread shrinks or re-protects a mapping the kernel must invalidate the stale TLB entries on **every CPU currently running the process** — by inter-processor interrupt. The hot thread, pinned and isolated, takes the IPI, runs the flush handler, and resumes with a cold TLB. Measured on the guest (`shootdown.cpp`: a hot thread spinning on vCPU 3 counting its own gaps and the `TLB:` row of `/proc/interrupts` for that CPU, a housekeeping thread on vCPU 0 mapping, touching and releasing 4 MiB ~1,000 times a second):

```
   housekeeping does …                      TLB IPIs on the hot cpu in 3 s     (the guest's own steal noise makes the gap
   nothing                                        +0                             statistics useless here — the IPI count is
   mmap + touch + munmap                      +1,472                             the signal; on bare metal each IPI is a
   … + mprotect(PROT_READ) first              +2,910                             2–5 µs gap plus the TLB refill afterwards,
   … + madvise(MADV_DONTNEED) first           +1,451                             Ch. 61 §61.3's CAL/TLB rows)
   the same munmap loop in a SEPARATE process     +1                             <- different mm: no IPI to the hot core
```

  `mprotect` costs two flushes (the protection change and the unmap); `MADV_DONTNEED` one; a **separate process** none, because its page tables are not the hot thread's. The fixes, in order: **map once at setup and never unmap, re-protect or `DONTNEED` in the session** (buffers are pools, Ch. 31; the logger and the control plane recycle, they do not free — `malloc` *does* `munmap`/`madvise` large frees, so §30.5's no-`malloc` rule is also a no-shootdown rule); move memory-churning work into **another process** over shared memory (Ch. 67, 103); and make `mremap`/`MADV_COLLAPSE`/`userfaultfd` (§34.4.3) startup-only. The signature in production is the `TLB` and `CAL` rows for the hot CPU moving in Ch. 114's sampler while nothing the hot thread did explains it.

## 34.6 Exercises & checklist

**Exercises**

1. **Measure the first-touch tail.** Build `mmapfault.cpp`; run `lazy` vs `prefault` pinned with `perf stat -e minor-faults,major-faults`. Confirm: lazy faults ~once per page with a long tail; prefault is flat with 0 faults *in the loop* (the program prints the in-loop `ru_minflt` delta — `perf stat`'s whole-process total is ~NPAGES in *both* runs, because `MAP_POPULATE`+`memset` fault every page before the loop). Where did the fault cost go?
2. **Major-fault a file.** `mmap` a large file `MAP_PRIVATE` *without* `MAP_POPULATE`, drop the page cache (`echo 1 > /proc/sys/vm/drop_caches`, root, test box), and time first-touch — observe millisecond **major** faults. Then `MAP_POPULATE`/`madvise(WILLNEED)` + `mlock` and re-measure.
3. **Shared-memory ring.** Build a `shm_open`/`MAP_SHARED` segment holding an SPSC ring (Ch. 45); have a producer process write and a consumer process read. Measure cross-process message latency vs a pipe/socket (Ch. 63). Confirm offsets (not pointers) are used.
4. **Pointer-vs-offset bug.** Deliberately store an absolute pointer in the shared segment and read it from the other process (force different map addresses by passing distinct non-NULL `addr` hints *without* `MAP_FIXED`, or with `MAP_FIXED_NOREPLACE` (Linux ≥ 4.17), which fails with `EEXIST` rather than overwriting — bare `MAP_FIXED` is not a hint: it silently unmaps whatever already lives at that address, libc and heap included). Watch it break; fix with offsets.
5. **Huge-page + NUMA placement.** Map a large region `MAP_HUGETLB` (reserve hugepages — Ch. 17), pre-fault from a thread pinned to node 0 vs node 1, and measure access latency from node 0 (Ch. 18). Confirm first-touch placed the huge pages on the faulting thread's node.

**Checklist — memory mapping**

- [ ] Hot-path mapped regions (allocator slabs, books, rings) are **pre-faulted** (`MAP_POPULATE`/warm-up pass) and **`mlock`ed** during setup — no first-touch fault on the path (§34.4.2).
- [ ] **No major faults** possible on the hot path: file maps pre-faulted/resident, swap disabled, working set fits RAM/page-cache (Ch. 30, Appendix C).
- [ ] Shared memory uses **`MAP_SHARED`** (`shm_open`/`memfd`) with **offsets/indices, never absolute pointers** (§34.4.1).
- [ ] Each shared segment has a **single writer** (Ch. 103) or a proven lock-free protocol (Ch. 45–46) with correct memory ordering (Ch. 41).
- [ ] Hot shared indices (ring head/tail) are **cache-line-padded** to avoid cross-process false sharing (Ch. 44).
- [ ] Large regions are **huge-page-backed** (`MAP_HUGETLB` — Ch. 17) and **pre-faulted from the using thread on the right NUMA node** (Ch. 18).
- [ ] `RLIMIT_MEMLOCK` is sufficient and `mlock` success is **verified**; mappings are created **once at setup** and torn down on the cold path (no per-use map/unmap).
- [ ] Shared-segment **lifecycle/crash-recovery** is designed (`shm_unlink`, re-attach/recreate — Ch. 103 §103.4.8), and pre-faulting is part of **warm-up** (Ch. 62).
- [ ] Variable-length byte rings are **double-mapped** (`memfd` × 2) so no message is ever split; regions that must grow use **`mremap`** on the cold path; pre-faulting uses **`MADV_POPULATE_WRITE`** + `mlockall(MCL_ONFAULT)`; THP, if any, is collapsed with **`MADV_COLLAPSE`** at setup (§34.4.3).
- [ ] Nothing **unmaps, re-protects or `MADV_DONTNEED`s** in the session from any thread of the hot process — no TLB-shootdown IPIs reach the hot core (`TLB`/`CAL` rows flat — §34.5).

## 34.7 References

- The Linux man pages — `mmap(2)` (`MAP_SHARED`/`PRIVATE`/`ANONYMOUS`/`POPULATE`/`HUGETLB`/`LOCKED`), `munmap(2)`, `mlock(2)`/`mlockall(2)`, `madvise(2)`, `shm_open(3)`, `memfd_create(2)`, `ftruncate(2)` — the authoritative interface for this chapter.
- U. Drepper, *What Every Programmer Should Know About Memory* — virtual memory, mapping, and the page-fault/`mlock` mechanics underpinning §34.2-§34.4.
- W. R. Stevens & S. Rago, *Advanced Programming in the UNIX Environment*, and Stevens, *UNIX Network Programming, Vol. 2 (IPC)* — POSIX shared memory and memory-mapped I/O patterns (§34.4.1).
- The Linux `Documentation/admin-guide/mm/` (hugetlbpage, transparent_hugepage) and `vm` sysctl docs — huge-page-backed mappings and overcommit/locking (ties Ch. 17).
- The kernel `Documentation` on `O_DIRECT`/page cache and the `tmpfs`/`/dev/shm` docs — file-backed and RAM-backed mapping behavior (ties Ch. 109).

## 34.8 Additional Reading

- M. Thompson / Mechanical Sympathy writings and the Aeron messaging system — shared-memory ring buffers and zero-copy IPC in practice (ties Ch. 48, 67).
- jemalloc/tcmalloc and kernel `mmap`-allocation writeups — how general allocators use anonymous `mmap`, for contrast with the custom slabs of Ch. 31.
- Ch. 17 (*TLB & Huge Pages*) — huge-page backing for mappings; Ch. 30–31 (*Memory Fundamentals / Allocators*) — the faults and slabs `mmap` provides; Ch. 67 (*IPC*) — shared-memory queues; Ch. 103 (*Process Topology*) — shared-memory data planes and single-writer discipline; Ch. 109 (*Capture & Replay*) — `mmap`/`O_DIRECT` journals; Ch. 62 (*Warming*) — pre-faulting as warm-up.
- **Appendix C** (System Tuning Checklist) — `mlockall`, swap, hugepage, and `RLIMIT_MEMLOCK` settings; **Appendix E** — the page-fault latency numbers framing §34.3.

---

*Next: Ch. 35 — Fixed-Point & Floating-Point Arithmetic, opening Part V. With memory mastered, we turn to numbers themselves: representing prices safely with scaled integers vs decimal vs binary float, tick-size handling, and the floating-point hazards — denormals, FTZ/DAZ, determinism — that lurk on the hot path.*
