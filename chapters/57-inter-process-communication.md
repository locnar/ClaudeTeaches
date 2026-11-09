# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 57 — Inter-Process Communication

> **Prerequisites:** Ch. 29 (mmap / shared memory — the substrate), Ch. 37 and 40 (the lock-free SPSC ring and the Disruptor — the structures that go *in* shared memory), Ch. 38 (seqlock publication), Ch. 36 (false sharing — cross-process head/tail), Ch. 33 (memory ordering across processes), Ch. 34 (shared-nothing — IPC is the channel between shared-nothing processes).
>
> **Leads into:** Ch. 89 (process topology — *why* you split into processes; the data planes between them), Ch. 87 (the untrusted-input discipline, applied here to shared segments), Ch. 93 (capture across processes). This is the on-host complement to the network I/O of Ch. 54–55.

---

## 57.1 Why it matters: splitting work across processes cheaply

A serious trading system is not one monolithic process — it's **multiple cooperating processes**: a feed handler, a strategy engine, an order gateway, a risk checker, a journaler (the topology of Ch. 89). They're split into separate processes for **fault isolation** (a crash in one doesn't take down the others — Ch. 89), independent deployment/restart, and the single-writer discipline (Ch. 34, 89). But splitting into processes creates a problem: they must **exchange data** — market-data messages, orders, fills — at the *same nanosecond latencies* the in-process pipeline (Ch. 40) achieves. Naive IPC (pipes, sockets, message queues) routes every message through the **kernel** — a syscall and a copy each way (Ch. 46, 54), costing *microseconds* per message — which would make the inter-process boundaries dominate the tick-to-trade budget. **Inter-process communication on the hot path must be as fast as in-process communication**, and the only way to achieve that is **shared memory**.

The answer is the lock-free **shared-memory ring buffer** (Ch. 37, 40) placed in a `mmap`'d shared segment (Ch. 29): the producer process writes a message into a slot in the shared region, advances a shared index, and the consumer process reads it from the *same physical pages* — with **no kernel involvement per message**, no syscall, no copy, just a cache-coherence transfer between cores (Ch. 7) exactly as if the two processes were two threads. This is the on-host equivalent of the Disruptor pipeline (Ch. 40), spanning a process boundary instead of a thread boundary — and it's *just as fast*, for the reason §57.2 spells out: the two processes share physical pages, so they share cache lines. The feed-handler process publishes into a shared ring; the strategy process consumes from it; the kernel is in the path only at setup (creating the segment), never per message.

The catch — and the reason IPC needs its own chapter beyond Ch. 29/37 — is that crossing the process boundary breaks one assumption everything in-process relied on: **a shared-memory segment maps at *different virtual addresses* in each process, so you cannot store pointers in it.** A pointer written by the producer is a meaningless address in the consumer (Ch. 29). Everything in the shared region — the ring indices, the message contents, any internal links — must use **offsets or indices**, not absolute pointers. Combined with the cross-process versions of the lock-free correctness concerns (memory ordering across processes — Ch. 33, false sharing of the shared head/tail — Ch. 36, the single-writer discipline — Ch. 34, 89) and the lifecycle/security concerns of a shared segment (cleanup after a crash, a hostile process writing garbage — Ch. 87), IPC is the same lock-free ring you know, with the address-space and process-lifecycle wrinkles layered on. This chapter covers the shm-vs-pipe mental model (§57.2), measures the shared-memory ring against a pipe (§57.3), details cross-process lock-free rings and offset-based addressing (§57.4), and warns hard about the pointers-across-address-spaces trap (§57.5).

## 57.2 Mental model: shared-memory queues vs pipes

**The IPC spectrum, by whether the kernel is in the per-message path:**

```
   KERNEL-MEDIATED (a syscall + copy per message — microseconds, Ch.46,54):
     pipes / FIFOs        - byte stream through a kernel buffer; simple, slow
     Unix domain sockets  - like TCP but on-host; faster than TCP, still kernel + copy
     POSIX/SysV msg queues - kernel-managed message queues; kernel + copy
     signals/eventfd       - notification only, not bulk data

   SHARED MEMORY (NO kernel per message — nanoseconds, the hot-path IPC):
     mmap'd shared segment (Ch.29) + a lock-free ring (Ch.37) inside it
       → producer writes a slot, advances a shared index; consumer reads the SAME pages
       → kernel involved ONLY at setup (shm_open/mmap); zero syscalls per message
```

- **Pipes/sockets/msg-queues** are *easy* (the kernel handles synchronization, framing, lifecycle) but *slow* — every message is a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user), with the per-syscall cost (Ch. 46) and a context switch if the reader blocked. Fine for *control-plane* / low-rate / non-latency-critical IPC (config, commands, logging hand-off). **Not** for the hot data path.
- **Shared memory + lock-free ring** is the *hot-path* IPC: the two processes share physical pages (Ch. 29), and communication is the lock-free ring of Ch. 37/40 operating across the process boundary — *no kernel per message*. This is the only IPC fast enough for tick-to-trade between processes.

**Why shared memory is as fast as threads.** A process boundary is a **virtual-address-space** boundary, not a physical-memory one. When two processes `mmap` the same `MAP_SHARED` segment (Ch. 29), the segment's *physical pages* are shared — each process has its own virtual mapping, but they point at the **same cache lines**. So a producer-process write and a consumer-process read of the same ring slot is *identical* at the hardware level to two threads doing it: one cache-line coherence transfer between cores (Ch. 7). The kernel set up the mapping once; thereafter the processes communicate purely through shared memory and atomics — exactly the Ch. 37/40 ring, just with the producer and consumer in different processes.

**What changes vs in-process (the wrinkles this chapter adds):**

- **No pointers — offsets/indices only (§57.4.2, §57.5).** The segment maps at *different* virtual addresses in each process, so an absolute pointer is invalid across the boundary. Everything is offset-from-base or integer index.
- **Cross-process memory ordering (Ch. 33).** *Lock-free* atomics in shared memory work across processes (the coherence protocol is hardware, process-agnostic) — but only lock-free ones: a `std::atomic<T>` that is *not* lock-free (a 16-byte price/qty pair without `cmpxchg16b`, anything that falls back to libatomic) is implemented with a per-process lock table, so each process takes its *own* lock and the "atomic" silently races across the boundary (the standard requires lock-free atomics to be *address-free* for exactly this use). Rule: `static_assert(std::atomic<T>::is_always_lock_free)` for every atomic placed in the segment, and the same check for any `std::atomic_ref` (C++20) you lay over a plain POD field of the fixed-layout shared struct. Beyond that the same acquire/release discipline applies, and you must place the atomics *in* the shared segment.
- **Lifecycle and trust (§57.5).** A *named* segment (`shm_open`, a file on the `/dev/shm` tmpfs) outlives a crashed process — orphaned until someone `shm_unlink`s it — so cleanup/re-attach must be designed (Ch. 89); an anonymous `MAP_SHARED|MAP_ANONYMOUS` or `memfd_create` segment dies with its last reference and can never be orphaned (or re-attached). And a process with the segment mapped can write *anything* into it — a buggy or hostile producer can corrupt the consumer, so the consumer treats the segment with Ch. 87's untrusted-input discipline. The single-writer discipline (Ch. 34, 89) and validation contain this.

**Notification — the one place you might touch the kernel.** A busy-polling consumer (Ch. 46) polls the shared ring's index and needs *no* notification (it sees new data immediately). But if the consumer *blocks* when idle (to save CPU on a non-dedicated core), it needs a wakeup — a **`futex`** on a word in the shared memory (cross-process futex), or an **`eventfd`** the producer signals. On the hot path, **busy-poll** (no notification, no kernel — Ch. 46); use futex/eventfd only for cold/background consumers.

The model: **kernel-mediated IPC (pipes/sockets/msg-queues) costs a syscall + copy per message (microseconds) — fine for control-plane; the hot data path uses a lock-free ring (Ch. 37) in a shared `mmap` segment (Ch. 29), which is as fast as inter-thread communication (shared physical pages, no kernel per message). The wrinkles: offsets-not-pointers, cross-process atomics/ordering, and segment lifecycle/trust.**

## 57.3 Measure it: shm ring vs pipe latency

Measure the gap that justifies shared memory: producer→consumer handoff over a **pipe** (kernel-mediated) vs a **shared-memory lock-free ring** (kernel-free per message), between two processes. The ring is deliberately *not* a new implementation: it is `SpscRing<T, CAP>` from Ch. 37 §37.3, verbatim, lifted into a header and **placement-new'd into a `MAP_SHARED` segment**. That is the whole point of the measurement — the same code, the same protocol (relaxed/acquire/release, `& (CAP-1)` indexing, padded head/tail), moved across a process boundary:

```cpp
// ipc.cpp — process-to-process handoff: pipe vs the Ch. 37 SPSC ring placed in a MAP_SHARED segment.
// Build: g++ -O2 -std=c++20 ipc.cpp -o ipc
// Run:  ./ipc shm 2 3   |   ./ipc pipe 2 3     (producer pinned to core 2, consumer to core 3)
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <sched.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include "spsc_ring.hpp"            // SpscRing<T, CAP> verbatim from Ch. 37 §37.3 — NOT a second implementation

using Ring = SpscRing<std::uint64_t, 1024>;   // indices are integers, slots are fixed-size PODs: no pointers (§57.4.2)
static_assert(std::is_trivially_destructible_v<Ring>);

static void pin(int core) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(core, &s); sched_setaffinity(0, sizeof s, &s); }

int main(int argc, char** argv) {
    const bool shm = argc < 2 || std::strcmp(argv[1], "shm") == 0;
    const int coreA = argc > 2 ? std::atoi(argv[2]) : 2, coreB = argc > 3 ? std::atoi(argv[3]) : 3;
    constexpr long N = 5'000'000;

    // One MAP_SHARED|MAP_ANONYMOUS region (Ch. 29): it survives fork() and is the same physical pages in both
    // processes. MAP_POPULATE pre-faults it (Ch. 26/29) BEFORE the ring is constructed in it.
    void* mem = mmap(nullptr, sizeof(Ring), PROT_READ | PROT_WRITE,
                     MAP_SHARED | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    if (mem == MAP_FAILED) { std::perror("mmap"); return 1; }
    Ring* r = new (mem) Ring();                 // placement-new the Ch. 37 ring into the shared segment

    int pfd[2]; if (!shm && pipe(pfd)) { std::perror("pipe"); return 1; }

    if (fork() == 0) {                          // CHILD = consumer
        pin(coreB);
        std::uint64_t v;
        for (long i = 0; i < N;) {
            if (shm) { if (r->pop(v)) ++i; }                       // same pop() as the in-process ring
            else     { if (read(pfd[0], &v, 8) == 8) ++i; }        // pipe: a syscall + copy per message
        }
        _exit(0);
    }
    pin(coreA);                                 // PARENT = producer, times N pushes
    auto t0 = std::chrono::steady_clock::now();
    for (long i = 0; i < N;) {
        if (shm) { if (r->push(std::uint64_t(i))) ++i; }           // same push() as the in-process ring
        else     { std::uint64_t v = std::uint64_t(i); if (write(pfd[1], &v, 8) == 8) ++i; }
    }
    wait(nullptr);                              // consumer has drained everything
    auto t1 = std::chrono::steady_clock::now();
    double ns = double(std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    std::printf("%-4s  %.1f Mmsg/s  (%.1f ns/msg, inverse throughput)\n", shm ? "shm" : "pipe", N / (ns / 1e3), ns / N);
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3; this listing was last rebuilt and run with GCC 13.3) — producer and consumer in separate processes pinned to two cores, turbo off (illustrative). The figures are **inverse throughput** (N pushes timed, divided by N), not a per-message one-way latency — the timestamp-per-slot variant is exercise 1:

```
   IPC mechanism                              throughput        ns/msg (inverse)   note
   pipe (kernel-mediated)                     ~3-8 Mmsg/s       ~125-330 ns        write+read syscalls + copies (Ch. 46, 54)
   Unix domain socket (socketpair; not in ipc.cpp) ~2-6 Mmsg/s  ~170-500 ns        comparable to a pipe, often SLOWER for tiny messages (skb alloc + socket lock)
   Ch. 37 SpscRing in a MAP_SHARED segment    ~80-150 Mmsg/s    ~7-12 ns           no kernel per message — the §37.3 figure, unchanged

   the shared-memory ring is ~20x faster than the pipe — and IS the in-process SPSC ring of §37.3, byte for byte.
```

Read it: the **shared-memory ring is ~20× faster than a pipe** and lands at *the same ~7-12 ns/msg as the in-process SPSC ring* (§37.3) — not approximately, but because it *is* that ring: the only things that changed are where its bytes live (a `MAP_SHARED` mapping instead of a global) and which process runs `push` and which runs `pop`. At the hardware level a lock-free ring over shared cache lines doesn't know about processes (§57.2). The **pipe** pays a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user) **per message** — the per-syscall cost (Ch. 46, 54) plus copies dominate, capping it at single-digit Mmsg/s. The lesson is unambiguous: **for hot-path IPC, use a shared-memory lock-free ring — it makes the process boundary as cheap as a thread boundary; reserve pipes/sockets/msg-queues for control-plane/low-rate IPC** where the convenience (kernel handles framing/lifecycle) outweighs the latency. Everything Ch. 37 taught about the ring — padded head/tail (Ch. 36), power-of-two capacity (Ch. 31), acquire/release (Ch. 33), the cached-opposite-index batching (§37.4.2) — carries over untouched; what this chapter adds is the segment around it (§57.4).

## 57.4 Techniques

### 57.4.1 Lock-free cross-process ring buffers

The hot-path IPC primitive — the Ch. 37/40 ring, placed in shared memory and made process-safe:

- **Reuse the Ch. 37 ring; its layout rules come with it.** Put the whole object — atomic head/tail indices *and* the message slots — in the `MAP_SHARED` region (Ch. 29) by placement-new, as §57.3 does, so both processes see the same indices and data; the atomics work across processes because coherence is hardware (§57.2), and the acquire/release protocol is exactly the in-process one (Ch. 33, 37). Everything §37.4.2 prescribes — producer-written `head` and consumer-written `tail` on separate cache lines (cross-process false sharing is the same ping-pong, Ch. 36), power-of-two capacity, caching the opposite index, pre-allocated storage — applies unchanged. Do *not* write a second ring for IPC: one implementation, tested once (Ch. 45), used in both places.
- **SPSC across processes is the default** (Ch. 37). One producer process, one consumer process — no CAS, no ABA, wait-free, simple. The shared-nothing process topology (Ch. 89) maps each one-way data flow (feed→strategy, strategy→gateway) to an SPSC shared-memory ring. MPSC (many producers → one consumer, e.g. several strategies → one gateway) where needed, with the cross-process CAS care of Ch. 37.
- **Slots are a wire format.** Messages are fixed-size plain-old-data copied into slots — no pointers (§57.4.2, §57.5), no heap (Ch. 26); variable-size messages use a fixed-slot ring with an overflow scheme, or a separate byte-ring with offset+length descriptors. Unlike the in-process ring, the slot layout crosses a *build* boundary too: the producer and consumer may be compiled separately, so version the layout in the segment header (§57.4.2, Ch. 60).
- **The segment is set up once, like any hot mapping (Ch. 29 §29.4.2).** Pre-faulted (`MAP_POPULATE`, as in §57.3, or a touch pass — Ch. 26), `mlock`'d, ideally huge-page-backed (Ch. 15) and on the NUMA node shared by the producer and consumer cores (Ch. 16 — or accept one cross-node hop if they're on different nodes).
- **Busy-poll the consumer (Ch. 46).** The consumer process busy-polls the ring's index on its dedicated core — instant, no kernel, no notification. Only a cold/background consumer blocks (futex/eventfd on a shared word — §57.2).

### 57.4.2 Shared-memory layout and addressing

The discipline that makes a shared region correct across address spaces (the generational slot map of Ch. 28 §28.4.4 is the relocatable, validated handle these rules ask for):

- **Offsets/indices, never pointers (the cardinal rule — §57.5).** The segment maps at different base addresses per process, so store **offsets from the segment base** (compute the real address as `base + offset` in each process) or **integer indices** (into the ring's slot array). A `T*` written by one process is garbage in another. This applies to the ring indices (already integers), *and* to any internal structure you put in shared memory (a free-list, a map) — index-based designs (Ch. 28's index handles) are mandatory here, not optional.
- **Define a fixed, versioned layout (a wire format — Ch. 60).** The shared region's structure (header, ring offsets, slot format) is a *contract* between processes that may be compiled separately and deployed at different versions. Define it as a fixed-layout struct (explicit sizes, no implementation-defined padding — Ch. 9), put a **version/magic** field in a header, and check it on attach (so a version mismatch fails loudly, not silently corrupts). Treat layout changes like protocol changes (Ch. 60).
- **A header with metadata.** The segment typically starts with a header: magic/version, the offsets/sizes of the rings within it, producer/consumer PIDs (for liveness — Ch. 89), and any config. Both processes read the header on attach to locate the rings.
- **Single-writer per region (Ch. 34, 89).** Each shared region (or each ring within it) has exactly one writer process — the foundation of correctness (tractable ordering — Ch. 33) and fault containment (Ch. 89). Many readers are fine (seqlock publication — Ch. 38 — for shared *snapshots* like a published BBO across processes).
- **Alignment for atomics and SIMD (Ch. 9).** The shared atomics must be naturally aligned (for lock-free atomicity — Ch. 33) and on their own cache lines (Ch. 36); SIMD-accessed data over-aligned (Ch. 9). Lay out the shared struct deliberately (it's a binary contract).

### 57.4.3 Segment setup and cross-process synchronization

The parts of IPC that are *not* the ring — how two unrelated processes come to share the same pages, and how they wait on each other when they must:

- **Creating and sharing the segment.** Three routes. `shm_open("/feed-ring", O_CREAT|O_RDWR, 0600)` + `ftruncate` + `mmap(MAP_SHARED)`: a *named* tmpfs object any process with the name and permissions can attach — convenient, persistent across crashes (§57.5), and discoverable by an attacker who can read `/dev/shm`. `memfd_create("feed-ring", MFD_ALLOW_SEALING)` + `mmap`: an *anonymous* file with no name — pass the fd to the peer over a Unix socket with **`SCM_RIGHTS`** (`sendmsg` with a `SOL_SOCKET`/`SCM_RIGHTS` control message), and apply **seals** (`fcntl(F_ADD_SEALS, F_SEAL_SHRINK|F_SEAL_GROW)`) so neither side can resize it under the other — the modern default when a supervisor (Ch. 89) spawns the processes. For huge pages (Ch. 15), `MFD_HUGETLB` or a `hugetlbfs` file; `MAP_POPULATE` or a touch pass pre-faults either (Ch. 26/29). The §57.3 listing uses the fourth, simplest route — `MAP_SHARED|MAP_ANONYMOUS` inherited across `fork()` — which only works between parent and child.
- **Cross-process futex (the wake-up when a consumer may sleep).** A `futex` word *in the segment* works across processes as long as the futex is **shared** (no `FUTEX_PRIVATE_FLAG` — `FUTEX_WAIT`/`FUTEX_WAKE`, not the `_PRIVATE` variants libc uses for intra-process locks). The protocol is Ch. 46's: the consumer loads the sequence it saw, re-checks the ring, then `FUTEX_WAIT(word, seen)` — the kernel sleeps it only if the word still equals `seen`, so a producer that bumps the word and `FUTEX_WAKE`s between the check and the wait cannot lose the wake-up; the producer wakes only when it observes a "consumer sleeping" flag, to keep the syscall off the common path. `eventfd` is the simpler, fd-based alternative when the consumer sits in an `epoll` loop (Ch. 54). Neither belongs on a busy-polling hot path (Ch. 46).
- **Process-shared and robust mutexes (when a lock is unavoidable).** For control-plane state in the segment (not the data ring), `pthread_mutexattr_setpshared(PTHREAD_PROCESS_SHARED)` makes a `pthread_mutex_t` in shared memory work across processes, and `pthread_mutexattr_setrobust(PTHREAD_MUTEX_ROBUST)` is the standard answer to this chapter's "producer died mid-write": the next locker gets `EOWNERDEAD`, repairs the protected state, calls `pthread_mutex_consistent()`, and continues — instead of every process hanging forever on a lock whose owner no longer exists. `PTHREAD_PRIO_INHERIT` (Ch. 51) composes with both. Same for `pthread_cond_t`/`pthread_rwlock_t` with the `pshared` attribute.
- **Trust the fd, not the name.** Whoever can open the name can write the segment; an fd passed over `SCM_RIGHTS` from a supervisor you trust is a capability the recipient could not have forged. Prefer memfd + `SCM_RIGHTS` for anything that crosses a trust boundary (Ch. 87), and mount `/dev/shm` with restrictive permissions where names are unavoidable.

## 57.5 Pitfalls & anti-patterns: pointers across address spaces

- **Storing pointers in shared memory (the #1 IPC bug).** A `T*` written by the producer is a *different process's* virtual address — meaningless (and a likely crash/corruption) in the consumer, which mapped the segment at a *different* base. §57.4.2 has the rule and the layout discipline (offsets-from-base or integer indices for *everything*); this is the defining shared-memory trap, so it heads the list.
- **Cross-process false sharing (Ch. 36).** The ring's head/tail (written by different processes/cores) on the same cache line ping-pongs across the process boundary — the Ch. 36 bug, identical cost. **Pad** the shared atomics to separate cache lines.
- **Wrong/missing memory ordering across processes (Ch. 33).** The same acquire/release discipline applies across processes as across threads — and the same x86-hides-ARM trap (Ch. 33). Publish data with release, observe the index with acquire; validate on ARM / model-check (Ch. 45). Relaxed everywhere "works on x86" and races.
- **Orphaned segments / lifecycle leaks.** A *named* `shm_open()`/tmpfs segment outlives a crashed process (orphaned in `/dev/shm`) — design cleanup/re-attach; an anonymous `MAP_SHARED|MAP_ANONYMOUS` or `memfd` segment dies with its last reference (pass the memfd over `SCM_RIGHTS` for automatic cleanup; use a name only when a late joiner must find the segment on its own — §57.4.3). Either way a producer that died mid-write leaves the ring in an indeterminate state. Design **cleanup and re-attach** (Ch. 89's crash-only design): `shm_unlink` on clean shutdown, detect-and-recreate (or reconcile) on restart, a robust mutex (`EOWNERDEAD` — §57.4.3) for any lock-protected control state, and a way to tell a stale segment from a live one (the header version/PID + liveness — Ch. 89).
- **Trusting the shared region (Ch. 87's untrusted-input discipline, at a process boundary).** Any process with the segment mapped can write *anything* into it — a buggy or compromised producer can corrupt the consumer (out-of-range indices, garbage data, a malicious wire format). The consumer must **validate** what it reads (bounds-check indices, sanity-check messages — Ch. 60, 87) and not blindly trust the shared region. The single-writer discipline (Ch. 34, 89) limits *who* can write, but the reader still validates.
- **Using a pipe/socket on the hot path.** Reaching for the *easy* IPC (pipe, Unix socket, msg queue) on the data path pays the per-message syscall + copy (§57.3) — microseconds where nanoseconds are needed. Shared-memory ring for hot data; kernel IPC only for control-plane.
- **Blocking the consumer with a slow notification.** If the consumer blocks on a futex/eventfd (instead of busy-polling), a slow or lost wakeup (Ch. 37's lost-wakeup) adds latency. On the hot path **busy-poll** (no notification — Ch. 46); use futex/eventfd only for cold consumers, with a correct wakeup protocol.
- **Non-fixed / unversioned layout.** A shared struct with implementation-defined padding (Ch. 9) or no version field breaks when the two processes are compiled differently or deployed at different versions — silent corruption. Fixed layout + version check (§57.4.2).
- **Forgetting to pre-fault/`mlock` the segment (Ch. 26, 29).** A shared segment that faults on first touch (or gets swapped) spikes the hot-path latency (Ch. 26). Pre-fault + `mlock` at setup, like all hot-path memory.

## 57.6 Exercises & checklist

**Exercises**

1. **shm vs pipe.** Build `ipc.cpp` against Ch. 37's `spsc_ring.hpp`; run `pipe` vs `shm` on two pinned cores. Confirm the shared-memory ring is ~20× faster and matches the in-process SPSC number (Ch. 37.3). Then convert the measurement from inverse throughput to one-way latency: stamp `rdtsc` (Ch. 17) into each slot, histogram `now - stamp` on the consumer. Add `strace -c` — the pipe shows millions of read/write; the shm ring shows ~none on the loop (§57.3).
2. **The pointer trap.** Store a pointer (to a slot, or to heap) in the shared region and read it from the other process. Note that `ipc.cpp`'s `fork()` child inherits the parent's address space, so the segment sits at the *same* virtual address in both and the bug stays hidden; to expose it, either `exec` two separate programs that attach via `shm_open()`/`memfd_create()` + `mmap(nullptr, …)` (ASLR then gives different bases), or have the child `munmap` and re-map the segment at a different address (`MAP_FIXED_NOREPLACE`, Linux 4.17 — never plain `MAP_FIXED`, which silently clobbers whatever is already mapped there). Watch it crash/corrupt. Fix with an offset/index (§57.4.2).
3. **Cross-process false sharing.** Put the ring's head and tail on the *same* cache line; measure the throughput drop and HITM (`perf c2c` — Ch. 36) across the process boundary. Re-pad; confirm recovery.
4. **Ordering on ARM.** Weaken the cross-process ring to `relaxed`; show it "works" on x86 and races on ARM (Appendix A) / model-check (Ch. 45). Restore acquire/release (§57.5).
5. **Lifecycle + validation.** Kill the producer process mid-run; show the orphaned segment and design re-attach (header version/PID check — Ch. 89). Then have a "buggy" producer write out-of-range indices; add consumer-side validation that detects and rejects it (§57.5, Ch. 87).

**Checklist — inter-process communication**

- [ ] Hot-path IPC uses a **lock-free ring in a shared `mmap` segment** (Ch. 29, 37) — **not** pipes/sockets/msg-queues (kernel + copy per message — §57.3); kernel IPC is **control-plane only**.
- [ ] **No pointers in shared memory** — offsets-from-base / integer indices for *everything* (ring indices, internal links — §57.4.2, §57.5).
- [ ] Shared atomic head/tail are **padded to separate cache lines** (no cross-process false sharing — Ch. 36); **acquire/release ordering** is correct and **validated on ARM/model-checked** (Ch. 33, 45).
- [ ] The segment has a **fixed, versioned, fixed-layout header** (magic/version + ring offsets/sizes + PIDs) checked on attach — treated as a **wire format** (Ch. 9, 60).
- [ ] Each region/ring has a **single writer** (Ch. 34, 89); shared *snapshots* (e.g. a published BBO) use a **seqlock** (Ch. 38).
- [ ] The segment is **pre-faulted, `mlock`'d, huge-page-backed, NUMA-placed** (Ch. 15, 16, 26, 29); the consumer **busy-polls** on a dedicated core (Ch. 46), blocking only on cold paths.
- [ ] **Lifecycle** is designed: clean `shm_unlink`, crash detection / re-attach / recreate (Ch. 89) — no orphaned/stale segments silently reused.
- [ ] The consumer **validates** what it reads (bounds-check indices, sanity-check messages — Ch. 60, 87) — it doesn't blindly trust the shared region (a trust/security boundary).

## 57.7 References

- W. R. Stevens & S. Rago, *Advanced Programming in the UNIX Environment*, and Stevens, *UNIX Network Programming, Vol. 2 (IPC)* — pipes, sockets, message queues, and shared memory (the spectrum of §57.2).
- The Linux man pages — `shm_open(3)`, `mmap(2)` (`MAP_SHARED`), `memfd_create(2)`, `pipe(2)`, `unix(7)` (domain sockets), `mq_overview(7)`, `futex(2)`, `eventfd(2)` — the IPC mechanisms.
- The LMAX Disruptor paper and the **Aeron** messaging system (Thompson et al.) — shared-memory ring buffers for IPC (Aeron is essentially this chapter productionized), and cross-process publication (§57.4).
- Ch. 37/38 references (Vyukov, the seqlock/RCU literature) — the lock-free rings and single-writer publication placed in shared memory.
- The `/dev/shm` (tmpfs) and shared-memory security documentation — segment lifecycle and the trust boundary of §57.5 (ties Ch. 87).

## 57.8 Additional Reading

- The Aeron and Chronicle Queue (Java, but the design transfers) documentation — production shared-memory IPC/messaging with offset-based addressing and versioned layouts.
- "Mechanical Sympathy" posts on shared-memory IPC and single-writer cross-process design.
- Ch. 29 (*Memory Mapping*) — the shared-segment substrate; Ch. 37 (*Lock-Free*) — the ring; Ch. 38 (*Seqlocks*) — cross-process snapshot publication; Ch. 36 (*False Sharing*) / Ch. 33 (*Ordering*) — the cross-process correctness; Ch. 89 (*Process Topology*) — why split into processes and the data-plane design; Ch. 60 (*Wire Decoding*) — the slot/message format; Ch. 87 (*Security*) — the shared-memory trust boundary.
- Ch. 37 §37.3 and Ch. 46 §46.3 — the ring-handoff and syscall/context-switch figures the §57.3 rows decompose into (Appendix E has no pipe-vs-shm row, and its "IPC" entry is instructions per cycle).

---

*Next: Ch. 58 — Socket Optimization & TCP/Protocol Tuning, back to the network: `TCP_NODELAY`/Nagle (the millisecond bug), buffer sizing, congestion control, and multicast for market data — the socket-level knobs that turn the kernel stack of Ch. 54 from default-slow into as-fast-as-the-kernel-gets.*
