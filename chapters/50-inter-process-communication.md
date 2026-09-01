# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 50 — Inter-Process Communication

> **Prerequisites:** Ch. 26 (mmap / shared memory — the substrate), Ch. 34 (lock-free SPSC ring — the structure that goes *in* shared memory), Ch. 35 (seqlock publication), Ch. 33 (false sharing — cross-process head/tail), Ch. 30 (memory ordering across processes), Ch. 31 (shared-nothing — IPC is the channel between shared-nothing processes).
>
> **Leads into:** Ch. 74 (process topology — *why* you split into processes; the data planes between them), Ch. 34/37 (the rings this uses), Ch. 72 (security — shared memory as an attack surface), Ch. 75 (capture across processes). This is the on-host complement to the network I/O of Ch. 47–48.

---

## 50.1 Why it matters: splitting work across processes cheaply

A serious trading system is not one monolithic process — it's **multiple cooperating processes**: a feed handler, a strategy engine, an order gateway, a risk checker, a journaler (the topology of Ch. 74). They're split into separate processes for **fault isolation** (a crash in one doesn't take down the others — Ch. 74), independent deployment/restart, and the single-writer discipline (Ch. 31, 74). But splitting into processes creates a problem: they must **exchange data** — market-data messages, orders, fills — at the *same nanosecond latencies* the in-process pipeline (Ch. 37) achieves. Naive IPC (pipes, sockets, message queues) routes every message through the **kernel** — a syscall and a copy each way (Ch. 41, 47), costing *microseconds* per message — which would make the inter-process boundaries dominate the tick-to-trade budget. **Inter-process communication on the hot path must be as fast as in-process communication**, and the only way to achieve that is **shared memory**.

The answer is the lock-free **shared-memory ring buffer** (Ch. 34, 37) placed in a `mmap`'d shared segment (Ch. 26): the producer process writes a message into a slot in the shared region, advances a shared index, and the consumer process reads it from the *same physical pages* — with **no kernel involvement per message**, no syscall, no copy, just a cache-coherence transfer between cores (Ch. 7) exactly as if the two processes were two threads. This is the on-host equivalent of the Disruptor pipeline (Ch. 37), spanning a process boundary instead of a thread boundary — and it's *just as fast*, because the process boundary is a virtual-address-space distinction, not a physical-memory one: two processes mapping the same shared segment share the same cache lines. The feed-handler process publishes into a shared ring; the strategy process consumes from it; the kernel is in the path only at setup (creating the segment), never per message.

The catch — and the reason IPC needs its own chapter beyond Ch. 26/34 — is that crossing the process boundary breaks one assumption everything in-process relied on: **a shared-memory segment maps at *different virtual addresses* in each process, so you cannot store pointers in it.** A pointer written by the producer is a meaningless address in the consumer (Ch. 26). Everything in the shared region — the ring indices, the message contents, any internal links — must use **offsets or indices**, not absolute pointers. Combined with the cross-process versions of the lock-free correctness concerns (memory ordering across processes — Ch. 30, false sharing of the shared head/tail — Ch. 33, the single-writer discipline — Ch. 31, 74) and the lifecycle/security concerns of a shared segment (cleanup after a crash, a hostile process writing garbage — Ch. 72), IPC is the same lock-free ring you know, with the address-space and process-lifecycle wrinkles layered on. This chapter covers the shm-vs-pipe mental model (§50.2), measures the shared-memory ring against a pipe (§50.3), details cross-process lock-free rings and offset-based addressing (§50.4), and warns hard about the pointers-across-address-spaces trap (§50.5).

## 50.2 Mental model: shared-memory queues vs pipes

**The IPC spectrum, by whether the kernel is in the per-message path:**

```
   KERNEL-MEDIATED (a syscall + copy per message — microseconds, Ch.39,44):
     pipes / FIFOs        - byte stream through a kernel buffer; simple, slow
     Unix domain sockets  - like TCP but on-host; faster than TCP, still kernel + copy
     POSIX/SysV msg queues - kernel-managed message queues; kernel + copy
     signals/eventfd       - notification only, not bulk data

   SHARED MEMORY (NO kernel per message — nanoseconds, the hot-path IPC):
     mmap'd shared segment (Ch.25) + a lock-free ring (Ch.33) inside it
       → producer writes a slot, advances a shared index; consumer reads the SAME pages
       → kernel involved ONLY at setup (shm_open/mmap); zero syscalls per message
```

- **Pipes/sockets/msg-queues** are *easy* (the kernel handles synchronization, framing, lifecycle) but *slow* — every message is a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user), with the per-syscall cost (Ch. 41) and a context switch if the reader blocked. Fine for *control-plane* / low-rate / non-latency-critical IPC (config, commands, logging hand-off). **Not** for the hot data path.
- **Shared memory + lock-free ring** is the *hot-path* IPC: the two processes share physical pages (Ch. 26), and communication is the lock-free ring of Ch. 34/37 operating across the process boundary — *no kernel per message*. This is the only IPC fast enough for tick-to-trade between processes.

**Why shared memory is as fast as threads.** A process boundary is a **virtual-address-space** boundary, not a physical-memory one. When two processes `mmap` the same `MAP_SHARED` segment (Ch. 26), the segment's *physical pages* are shared — each process has its own virtual mapping, but they point at the **same cache lines**. So a producer-process write and a consumer-process read of the same ring slot is *identical* at the hardware level to two threads doing it: one cache-line coherence transfer between cores (Ch. 7). The kernel set up the mapping once; thereafter the processes communicate purely through shared memory and atomics — exactly the Ch. 34/37 ring, just with the producer and consumer in different processes.

**What changes vs in-process (the wrinkles this chapter adds):**

- **No pointers — offsets/indices only (§50.4.2, §50.5).** The segment maps at *different* virtual addresses in each process, so an absolute pointer is invalid across the boundary. Everything is offset-from-base or integer index.
- **Cross-process memory ordering (Ch. 30).** Atomics in shared memory work across processes (the coherence protocol is hardware, process-agnostic) — but the same acquire/release discipline applies, and you must place the atomics *in* the shared segment.
- **Lifecycle and trust (§50.5, Ch. 72).** A shared segment outlives a crashed process (orphaned in `/dev/shm`); cleanup/re-attach must be designed (Ch. 74). And a process with the segment mapped can write *anything* into it — a buggy or hostile producer can corrupt the consumer (a trust/security boundary — Ch. 72). The single-writer discipline (Ch. 31, 74) and validation contain this.

**Notification — the one place you might touch the kernel.** A busy-polling consumer (Ch. 41) polls the shared ring's index and needs *no* notification (it sees new data immediately). But if the consumer *blocks* when idle (to save CPU on a non-dedicated core), it needs a wakeup — a **`futex`** on a word in the shared memory (cross-process futex), or an **`eventfd`** the producer signals. On the hot path, **busy-poll** (no notification, no kernel — Ch. 41); use futex/eventfd only for cold/background consumers.

The model: **kernel-mediated IPC (pipes/sockets/msg-queues) costs a syscall + copy per message (microseconds) — fine for control-plane; the hot data path uses a lock-free ring (Ch. 34) in a shared `mmap` segment (Ch. 26), which is as fast as inter-thread communication (shared physical pages, no kernel per message). The wrinkles: offsets-not-pointers, cross-process atomics/ordering, and segment lifecycle/trust.**

## 50.3 Measure it: shm ring vs pipe latency

Measure the gap that justifies shared memory: producer→consumer message latency over a **pipe** (kernel-mediated) vs a **shared-memory lock-free ring** (kernel-free per message), between two processes.

```cpp
// ipc.cpp — process-to-process latency: pipe vs shared-memory SPSC ring.
// Build: g++ -O2 -std=c++20 ipc.cpp -o ipc -pthread
// Run:  ./ipc pipe   |   ./ipc shm    (forks producer+consumer, pins to two cores)
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include <algorithm>
#include <chrono>
#include <sys/mman.h>
#include <unistd.h>

// Shared-memory SPSC ring: head/tail (padded — Ch.32) and slots, all in ONE mmap'd region.
// Indices are integers; slots hold fixed-size PODs; NO pointers (Ch.46.5).
struct Ring {
    static constexpr std::size_t CAP = 1024;
    alignas(64) std::atomic<std::uint64_t> head{0};   // producer writes (own cache line — Ch.32)
    alignas(64) std::atomic<std::uint64_t> tail{0};   // consumer writes
    alignas(64) std::uint64_t slot[CAP];              // fixed-size message slots
};

int main(int argc, char** argv) {
    bool shm = (argc < 2) || std::strcmp(argv[1], "shm") == 0;
    constexpr long N = 5'000'000;

    // MAP_SHARED|MAP_ANONYMOUS region survives fork() and is shared by parent+child (Ch.25).
    void* mem = mmap(nullptr, sizeof(Ring), PROT_READ|PROT_WRITE,
                     MAP_SHARED|MAP_ANONYMOUS, -1, 0);
    Ring* r = new (mem) Ring();
    std::memset(mem, 0, sizeof(Ring));                 // pre-fault (Ch.22,25)

    int pfd[2]; if (!shm) pipe(pfd);

    if (fork() == 0) {                                 // CHILD = consumer (pin to core B)
        std::uint64_t v;
        for (long i = 0; i < N; ) {
            if (shm) { std::uint64_t t = r->tail.load(std::memory_order_relaxed);
                       if (t != r->head.load(std::memory_order_acquire)) {
                           v = r->slot[t & (Ring::CAP-1)]; r->tail.store(t+1, std::memory_order_release); ++i; } }
            else { if (read(pfd[0], &v, 8) == 8) ++i; }   // pipe: a syscall per message
        }
        _exit(0);
    }
    // PARENT = producer (pin to core A), measures round-of-pushes throughput
    auto t0 = std::chrono::steady_clock::now();
    for (long i = 0; i < N; ) {
        if (shm) { std::uint64_t h = r->head.load(std::memory_order_relaxed);
                   if (h - r->tail.load(std::memory_order_acquire) < Ring::CAP) {
                       r->slot[h & (Ring::CAP-1)] = i; r->head.store(h+1, std::memory_order_release); ++i; } }
        else { write(pfd[1], &i, 8); ++i; }               // pipe: a syscall per message
    }
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-4s  %.1f Mmsg/s  (%.1f ns/msg)\n", shm?"shm":"pipe", N/(ns/1e3), ns/N);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), producer/consumer in separate processes pinned to two cores, turbo off (illustrative):

```
   IPC mechanism                         throughput        ns/msg     note
   pipe (kernel-mediated)                ~3-8 Mmsg/s       ~150-300 ns  write+read syscalls + copies (Ch.39,44)
   Unix domain socket                    ~5-10 Mmsg/s      ~120-250 ns  faster than pipe, still kernel+copy
   shared-memory SPSC ring (busy-poll)   ~80-150 Mmsg/s    ~7-15 ns     NO kernel per message — like threads (Ch.33)

   the shared-memory ring is ~20x faster — same as the in-PROCESS SPSC ring (Ch.33.3).
```

Read it: the **shared-memory ring is ~20× faster than a pipe** and lands at *the same ~7-15 ns/msg as the in-process SPSC ring* (Ch. 34.3) — because, at the hardware level, *it is the same thing*: a lock-free ring over shared cache lines, just with the producer and consumer in different processes (§50.2). The **pipe** pays a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user) **per message** — the per-syscall cost (Ch. 41, 47) plus copies dominate, capping it at single-digit Mmsg/s. The lesson is unambiguous: **for hot-path IPC, use a shared-memory lock-free ring — it makes the process boundary as cheap as a thread boundary; reserve pipes/sockets/msg-queues for control-plane/low-rate IPC** where the convenience (kernel handles framing/lifecycle) outweighs the latency. (Note the ring's details that carry over from Ch. 34: padded head/tail — Ch. 33, power-of-two — Ch. 28, acquire/release — Ch. 30, pre-faulted — Ch. 23/26, integer indices — §50.5.)

## 50.4 Techniques

### 50.4.1 Lock-free cross-process ring buffers

The hot-path IPC primitive — the Ch. 34/37 ring, placed in shared memory and made process-safe:

- **Put the ring (indices + slots) entirely in the shared segment.** Both the atomic head/tail indices *and* the message slots live in the `MAP_SHARED` region (Ch. 26), so both processes see the same indices and data. The atomics work across processes (coherence is hardware — §50.2); use acquire/release ordering exactly as in-process (Ch. 30, 34).
- **SPSC across processes is the default** (Ch. 34). One producer process, one consumer process — no CAS, no ABA, wait-free, simple. The shared-nothing process topology (Ch. 74) maps each one-way data flow (feed→strategy, strategy→gateway) to an SPSC shared-memory ring. MPSC (many producers → one consumer, e.g. several strategies → one gateway) where needed, with the cross-process CAS care of Ch. 34.
- **Padded head/tail (Ch. 33).** Producer-written `head` and consumer-written `tail` on **separate cache lines** — cross-process false sharing is the same bug as cross-thread (Ch. 33), and the ping-pong now crosses a process boundary but the cost is identical. Pad them.
- **Fixed-size POD slots (no pointers, no allocation).** Messages are fixed-size plain-old-data copied into slots — no pointers (§50.5), no heap (Ch. 23). Variable-size messages use a fixed-slot ring with an overflow scheme, or a separate byte-ring with offset+length descriptors. The slot layout is a *wire format* across processes — version it (Ch. 53).
- **Pre-faulted, `mlock`'d, huge-page, NUMA-placed (Ch. 15, 16, 23, 26).** The shared segment is set up once at startup: pre-faulted (no first-touch fault on the hot path — Ch. 23), `mlock`'d (resident), ideally huge-page-backed (TLB — Ch. 15) and on the NUMA node shared by the producer and consumer cores (Ch. 16 — or accept one cross-node hop if they're on different nodes).
- **Busy-poll the consumer (Ch. 41).** The consumer process busy-polls the ring's index on its dedicated core — instant, no kernel, no notification. Only a cold/background consumer blocks (futex/eventfd on a shared word — §50.2).

### 50.4.2 Shared-memory layout and addressing

The discipline that makes a shared region correct across address spaces:

- **Offsets/indices, never pointers (the cardinal rule — §50.5).** The segment maps at different base addresses per process, so store **offsets from the segment base** (compute the real address as `base + offset` in each process) or **integer indices** (into the ring's slot array). A `T*` written by one process is garbage in another. This applies to the ring indices (already integers), *and* to any internal structure you put in shared memory (a free-list, a map) — index-based designs (Ch. 25's index handles) are mandatory here, not optional.
- **Define a fixed, versioned layout (a wire format — Ch. 53).** The shared region's structure (header, ring offsets, slot format) is a *contract* between processes that may be compiled separately and deployed at different versions. Define it as a fixed-layout struct (explicit sizes, no implementation-defined padding — Ch. 9), put a **version/magic** field in a header, and check it on attach (so a version mismatch fails loudly, not silently corrupts). Treat layout changes like protocol changes (Ch. 53).
- **A header with metadata.** The segment typically starts with a header: magic/version, the offsets/sizes of the rings within it, producer/consumer PIDs (for liveness — Ch. 74), and any config. Both processes read the header on attach to locate the rings.
- **Single-writer per region (Ch. 31, 74).** Each shared region (or each ring within it) has exactly one writer process — the foundation of correctness (tractable ordering — Ch. 30) and fault containment (Ch. 74). Many readers are fine (seqlock publication — Ch. 35 — for shared *snapshots* like a published BBO across processes).
- **Alignment for atomics and SIMD (Ch. 9).** The shared atomics must be naturally aligned (for lock-free atomicity — Ch. 30) and on their own cache lines (Ch. 33); SIMD-accessed data over-aligned (Ch. 9). Lay out the shared struct deliberately (it's a binary contract).

## 50.5 Pitfalls & anti-patterns: pointers across address spaces

- **Storing pointers in shared memory (the #1 IPC bug).** A `T*` written by the producer is a *different process's* virtual address — meaningless (and a likely crash/corruption) in the consumer, which mapped the segment at a *different* base (§50.2). **Use offsets-from-base or integer indices** for *everything* in shared memory — ring indices, internal links, free-lists (§50.4.2). This is the defining shared-memory trap.
- **Cross-process false sharing (Ch. 33).** The ring's head/tail (written by different processes/cores) on the same cache line ping-pongs across the process boundary — the Ch. 33 bug, identical cost. **Pad** the shared atomics to separate cache lines.
- **Wrong/missing memory ordering across processes (Ch. 30).** The same acquire/release discipline applies across processes as across threads — and the same x86-hides-ARM trap (Ch. 30). Publish data with release, observe the index with acquire; validate on ARM / model-check (Ch. 40). Relaxed everywhere "works on x86" and races.
- **Orphaned segments / lifecycle leaks.** A `MAP_SHARED`/`shm_open` segment outlives a crashed process (orphaned in `/dev/shm`), and a producer that died mid-write leaves the ring in an indeterminate state. Design **cleanup and re-attach** (Ch. 74's crash-only design): `shm_unlink` on clean shutdown, detect-and-recreate (or reconcile) on restart, and a way to tell a stale segment from a live one (the header version/PID + liveness — Ch. 74).
- **Trusting the shared region (a security/robustness boundary — Ch. 72).** Any process with the segment mapped can write *anything* into it — a buggy or compromised producer can corrupt the consumer (out-of-range indices, garbage data, a malicious wire format). The consumer must **validate** what it reads (bounds-check indices, sanity-check messages — Ch. 53, 72) and not blindly trust the shared region. The single-writer discipline (Ch. 31, 74) limits *who* can write, but the reader still validates.
- **Using a pipe/socket on the hot path.** Reaching for the *easy* IPC (pipe, Unix socket, msg queue) on the data path pays the per-message syscall + copy (§50.3) — microseconds where nanoseconds are needed. Shared-memory ring for hot data; kernel IPC only for control-plane.
- **Blocking the consumer with a slow notification.** If the consumer blocks on a futex/eventfd (instead of busy-polling), a slow or lost wakeup (Ch. 34's lost-wakeup) adds latency. On the hot path **busy-poll** (no notification — Ch. 41); use futex/eventfd only for cold consumers, with a correct wakeup protocol.
- **Non-fixed / unversioned layout.** A shared struct with implementation-defined padding (Ch. 9) or no version field breaks when the two processes are compiled differently or deployed at different versions — silent corruption. Fixed layout + version check (§50.4.2).
- **Forgetting to pre-fault/`mlock` the segment (Ch. 23, 26).** A shared segment that faults on first touch (or gets swapped) spikes the hot-path latency (Ch. 23). Pre-fault + `mlock` at setup, like all hot-path memory.

## 50.6 Exercises & checklist

**Exercises**

1. **shm vs pipe.** Build `ipc.cpp`; run `pipe` vs `shm` (pin the two processes to two cores). Confirm the shared-memory ring is ~20× faster and matches the in-process SPSC number (Ch. 34.3). Add `strace -c` — the pipe shows millions of read/write; the shm ring shows ~none on the loop (§50.3).
2. **The pointer trap.** Store a pointer (to a slot, or to heap) in the shared region and read it from the other process (force different map bases with `MAP_FIXED` hints or just observe ASLR). Watch it crash/corrupt. Fix with an offset/index (§50.5).
3. **Cross-process false sharing.** Put the ring's head and tail on the *same* cache line; measure the throughput drop and HITM (`perf c2c` — Ch. 33) across the process boundary. Re-pad; confirm recovery.
4. **Ordering on ARM.** Weaken the cross-process ring to `relaxed`; show it "works" on x86 and races on ARM (Appendix A) / model-check (Ch. 40). Restore acquire/release (§50.5).
5. **Lifecycle + validation.** Kill the producer process mid-run; show the orphaned segment and design re-attach (header version/PID check — Ch. 74). Then have a "buggy" producer write out-of-range indices; add consumer-side validation that detects and rejects it (§50.5, Ch. 72).

**Checklist — inter-process communication**

- [ ] Hot-path IPC uses a **lock-free ring in a shared `mmap` segment** (Ch. 26, 34) — **not** pipes/sockets/msg-queues (kernel + copy per message — §50.3); kernel IPC is **control-plane only**.
- [ ] **No pointers in shared memory** — offsets-from-base / integer indices for *everything* (ring indices, internal links — §50.4.2, §50.5).
- [ ] Shared atomic head/tail are **padded to separate cache lines** (no cross-process false sharing — Ch. 33); **acquire/release ordering** is correct and **validated on ARM/model-checked** (Ch. 30, 40).
- [ ] The segment has a **fixed, versioned, fixed-layout header** (magic/version + ring offsets/sizes + PIDs) checked on attach — treated as a **wire format** (Ch. 9, 53).
- [ ] Each region/ring has a **single writer** (Ch. 31, 74); shared *snapshots* (e.g. a published BBO) use a **seqlock** (Ch. 35).
- [ ] The segment is **pre-faulted, `mlock`'d, huge-page-backed, NUMA-placed** (Ch. 15, 16, 23, 26); the consumer **busy-polls** on a dedicated core (Ch. 41), blocking only on cold paths.
- [ ] **Lifecycle** is designed: clean `shm_unlink`, crash detection / re-attach / recreate (Ch. 74) — no orphaned/stale segments silently reused.
- [ ] The consumer **validates** what it reads (bounds-check indices, sanity-check messages — Ch. 53, 72) — it doesn't blindly trust the shared region (a trust/security boundary).

## 50.7 References

- W. R. Stevens & S. Rago, *Advanced Programming in the UNIX Environment*, and Stevens, *UNIX Network Programming, Vol. 2 (IPC)* — pipes, sockets, message queues, and shared memory (the spectrum of §50.2).
- The Linux man pages — `shm_open(3)`, `mmap(2)` (`MAP_SHARED`), `memfd_create(2)`, `pipe(2)`, `unix(7)` (domain sockets), `mq_overview(7)`, `futex(2)`, `eventfd(2)` — the IPC mechanisms.
- The LMAX Disruptor paper and the **Aeron** messaging system (Thompson et al.) — shared-memory ring buffers for IPC (Aeron is essentially this chapter productionized), and cross-process publication (§50.4).
- Ch. 34/35 references (Vyukov, the seqlock/RCU literature) — the lock-free rings and single-writer publication placed in shared memory.
- The `/dev/shm` (tmpfs) and shared-memory security documentation — segment lifecycle and the trust boundary of §50.5 (ties Ch. 72).

## 50.8 Additional Reading

- The Aeron and Chronicle Queue (Java, but the design transfers) documentation — production shared-memory IPC/messaging with offset-based addressing and versioned layouts.
- "Mechanical Sympathy" posts on shared-memory IPC and single-writer cross-process design.
- Ch. 26 (*Memory Mapping*) — the shared-segment substrate; Ch. 34 (*Lock-Free*) — the ring; Ch. 35 (*Seqlocks*) — cross-process snapshot publication; Ch. 33 (*False Sharing*) / Ch. 30 (*Ordering*) — the cross-process correctness; Ch. 74 (*Process Topology*) — why split into processes and the data-plane design; Ch. 53 (*Wire Decoding*) — the slot/message format; Ch. 72 (*Security*) — the shared-memory trust boundary.
- **Appendix E** — IPC latency numbers (pipe vs shm); **Appendix F** — IPC/shared-memory glossary.

---

*Next: Ch. 51 — Socket Optimization & TCP/Protocol Tuning, back to the network: `TCP_NODELAY`/Nagle (the millisecond bug), buffer sizing, congestion control, and multicast for market data — the socket-level knobs that turn the kernel stack of Ch. 47 from default-slow into as-fast-as-the-kernel-gets.*
