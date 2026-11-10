# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 67 — Inter-Process Communication

> **Prerequisites:** Ch. 34 (mmap / shared memory — the substrate), Ch. 45 and 48 (the lock-free SPSC ring and the Disruptor — the structures that go *in* shared memory), Ch. 46 (seqlock publication), Ch. 44 (false sharing — cross-process head/tail), Ch. 41 (memory ordering across processes), Ch. 42 (shared-nothing — IPC is the channel between shared-nothing processes).
>
> **Leads into:** Ch. 103 (process topology — *why* you split into processes; the data planes between them), Ch. 101 (the untrusted-input discipline, applied here to shared segments), Ch. 109 (capture across processes). This is the on-host complement to the network I/O of Ch. 63–65.

---

## 67.1 Why it matters: splitting work across processes cheaply

A serious trading system is not one monolithic process — it's **multiple cooperating processes**: a feed handler, a strategy engine, an order gateway, a risk checker, a journaler (the topology of Ch. 103). They're split into separate processes for **fault isolation** (a crash in one doesn't take down the others — Ch. 103), independent deployment/restart, and the single-writer discipline (Ch. 42, 103). But splitting into processes creates a problem: they must **exchange data** — market-data messages, orders, fills — at the *same nanosecond latencies* the in-process pipeline (Ch. 48) achieves. Naive IPC (pipes, sockets, message queues) routes every message through the **kernel** — a syscall and a copy each way (Ch. 54, 63), costing *microseconds* per message — which would make the inter-process boundaries dominate the tick-to-trade budget. **Inter-process communication on the hot path must be as fast as in-process communication**, and the only way to achieve that is **shared memory**.

The answer is the lock-free **shared-memory ring buffer** (Ch. 45, 48) placed in a `mmap`'d shared segment (Ch. 34): the producer process writes a message into a slot in the shared region, advances a shared index, and the consumer process reads it from the *same physical pages* — with **no kernel involvement per message**, no syscall, no copy, just a cache-coherence transfer between cores (Ch. 8) exactly as if the two processes were two threads. This is the on-host equivalent of the Disruptor pipeline (Ch. 48), spanning a process boundary instead of a thread boundary — and it's *just as fast*, for the reason §67.2 spells out: the two processes share physical pages, so they share cache lines. The feed-handler process publishes into a shared ring; the strategy process consumes from it; the kernel is in the path only at setup (creating the segment), never per message.

The catch — and the reason IPC needs its own chapter beyond Ch. 34/45 — is that crossing the process boundary breaks one assumption everything in-process relied on: **a shared-memory segment maps at *different virtual addresses* in each process, so you cannot store pointers in it.** A pointer written by the producer is a meaningless address in the consumer (Ch. 34). Everything in the shared region — the ring indices, the message contents, any internal links — must use **offsets or indices**, not absolute pointers. Combined with the cross-process versions of the lock-free correctness concerns (memory ordering across processes — Ch. 41, false sharing of the shared head/tail — Ch. 44, the single-writer discipline — Ch. 42, 103) and the lifecycle/security concerns of a shared segment (cleanup after a crash, a hostile process writing garbage — Ch. 101), IPC is the same lock-free ring you know, with the address-space and process-lifecycle wrinkles layered on. This chapter covers the shm-vs-pipe mental model (§67.2), measures the shared-memory ring against a pipe (§67.3), details cross-process lock-free rings and offset-based addressing (§67.4), and warns hard about the pointers-across-address-spaces trap (§67.5).

## 67.2 Mental model: shared-memory queues vs pipes

**The IPC spectrum, by whether the kernel is in the per-message path:**

```
   KERNEL-MEDIATED (a syscall + copy per message — microseconds, Ch.54,63):
     pipes / FIFOs        - byte stream through a kernel buffer; simple, slow
     Unix domain sockets  - like TCP but on-host; faster than TCP, still kernel + copy
     POSIX/SysV msg queues - kernel-managed message queues; kernel + copy
     signals/eventfd       - notification only, not bulk data

   SHARED MEMORY (NO kernel per message — nanoseconds, the hot-path IPC):
     mmap'd shared segment (Ch.34) + a lock-free ring (Ch.45) inside it
       → producer writes a slot, advances a shared index; consumer reads the SAME pages
       → kernel involved ONLY at setup (shm_open/mmap); zero syscalls per message
```

- **Pipes/sockets/msg-queues** are *easy* (the kernel handles synchronization, framing, lifecycle) but *slow* — every message is a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user), with the per-syscall cost (Ch. 54) and a context switch if the reader blocked. Fine for *control-plane* / low-rate / non-latency-critical IPC (config, commands, logging hand-off). **Not** for the hot data path.
- **Shared memory + lock-free ring** is the *hot-path* IPC: the two processes share physical pages (Ch. 34), and communication is the lock-free ring of Ch. 45/48 operating across the process boundary — *no kernel per message*. This is the only IPC fast enough for tick-to-trade between processes.

**Why shared memory is as fast as threads.** A process boundary is a **virtual-address-space** boundary, not a physical-memory one. When two processes `mmap` the same `MAP_SHARED` segment (Ch. 34), the segment's *physical pages* are shared — each process has its own virtual mapping, but they point at the **same cache lines**. So a producer-process write and a consumer-process read of the same ring slot is *identical* at the hardware level to two threads doing it: one cache-line coherence transfer between cores (Ch. 8). The kernel set up the mapping once; thereafter the processes communicate purely through shared memory and atomics — exactly the Ch. 45/48 ring, just with the producer and consumer in different processes.

**What changes vs in-process (the wrinkles this chapter adds):**

- **No pointers — offsets/indices only (§67.4.2, §67.5).** The segment maps at *different* virtual addresses in each process, so an absolute pointer is invalid across the boundary. Everything is offset-from-base or integer index.
- **Cross-process memory ordering (Ch. 41).** *Lock-free* atomics in shared memory work across processes (the coherence protocol is hardware, process-agnostic) — but only lock-free ones: a `std::atomic<T>` that is *not* lock-free (a 16-byte price/qty pair without `cmpxchg16b`, anything that falls back to libatomic) is implemented with a per-process lock table, so each process takes its *own* lock and the "atomic" silently races across the boundary (the standard requires lock-free atomics to be *address-free* for exactly this use). Rule: `static_assert(std::atomic<T>::is_always_lock_free)` for every atomic placed in the segment, and the same check for any `std::atomic_ref` (C++20) you lay over a plain POD field of the fixed-layout shared struct. Beyond that the same acquire/release discipline applies, and you must place the atomics *in* the shared segment. Two lines of discipline for every atomic placed in a segment: `static_assert(std::atomic<Seq>::is_always_lock_free)` (a lock-using atomic in shm is a mutex shared with the other process, with no owner to clean it up after a crash — Ch. 41 §41.4.3) and `alignas(16)` on any 16-byte one; read every plain field a foreign writer can touch through `std::atomic_ref` (Ch. 41 §41.4.3).
- **Lifecycle and trust (§67.5).** A *named* segment (`shm_open`, a file on the `/dev/shm` tmpfs) outlives a crashed process — orphaned until someone `shm_unlink`s it — so cleanup/re-attach must be designed (Ch. 103 §103.4.8); an anonymous `MAP_SHARED|MAP_ANONYMOUS` or `memfd_create` segment dies with its last reference and can never be orphaned (or re-attached). And a process with the segment mapped can write *anything* into it — a buggy or hostile producer can corrupt the consumer, so the consumer treats the segment with Ch. 101's untrusted-input discipline. The single-writer discipline (Ch. 42, 103) and validation contain this.

**Notification — the one place you might touch the kernel.** A busy-polling consumer (Ch. 54) polls the shared ring's index and needs *no* notification (it sees new data immediately). But if the consumer *blocks* when idle (to save CPU on a non-dedicated core), it needs a wakeup — a **`futex`** on a word in the shared memory (cross-process futex), or an **`eventfd`** the producer signals. On the hot path, **busy-poll** (no notification, no kernel — Ch. 54); use futex/eventfd only for cold/background consumers.

The model: **kernel-mediated IPC (pipes/sockets/msg-queues) costs a syscall + copy per message (microseconds) — fine for control-plane; the hot data path uses a lock-free ring (Ch. 45) in a shared `mmap` segment (Ch. 34), which is as fast as inter-thread communication (shared physical pages, no kernel per message). The wrinkles: offsets-not-pointers, cross-process atomics/ordering, and segment lifecycle/trust.**

## 67.3 Measure it: shm ring vs pipe latency

Measure the gap that justifies shared memory: producer→consumer handoff over a **pipe** (kernel-mediated) vs a **shared-memory lock-free ring** (kernel-free per message), between two processes. The ring is deliberately *not* a new implementation: it is `SpscRing<T, CAP>` from Ch. 45 §45.3, verbatim, lifted into a header and **placement-new'd into a `MAP_SHARED` segment**. That is the whole point of the measurement — the same code, the same protocol (relaxed/acquire/release, `& (CAP-1)` indexing, padded head/tail), moved across a process boundary:

```cpp
// ipc.cpp — process-to-process handoff: pipe vs the Ch. 45 SPSC ring placed in a MAP_SHARED segment.
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
#include "spsc_ring.hpp"            // SpscRing<T, CAP> verbatim from Ch. 45 §45.3 — NOT a second implementation

using Ring = SpscRing<std::uint64_t, 1024>;   // indices are integers, slots are fixed-size PODs: no pointers (§67.4.2)
static_assert(std::is_trivially_destructible_v<Ring>);

static void pin(int core) { cpu_set_t s; CPU_ZERO(&s); CPU_SET(core, &s); sched_setaffinity(0, sizeof s, &s); }

int main(int argc, char** argv) {
    const bool shm = argc < 2 || std::strcmp(argv[1], "shm") == 0;
    const int coreA = argc > 2 ? std::atoi(argv[2]) : 2, coreB = argc > 3 ? std::atoi(argv[3]) : 3;
    constexpr long N = 5'000'000;

    // One MAP_SHARED|MAP_ANONYMOUS region (Ch. 34): it survives fork() and is the same physical pages in both
    // processes. MAP_POPULATE pre-faults it (Ch. 30/34) BEFORE the ring is constructed in it.
    void* mem = mmap(nullptr, sizeof(Ring), PROT_READ | PROT_WRITE,
                     MAP_SHARED | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
    if (mem == MAP_FAILED) { std::perror("mmap"); return 1; }
    Ring* r = new (mem) Ring();                 // placement-new the Ch. 45 ring into the shared segment

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

Representative results (toolchain and machine: Appendix L.3; this listing was last rebuilt and run with GCC 13.3) — producer and consumer in separate processes pinned to two cores in one L3 domain (Ch. 19 — the pair you pick sets the handoff floor), turbo off (illustrative). The figures are **inverse throughput** (N pushes timed, divided by N), not a per-message one-way latency — the timestamp-per-slot variant is exercise 1:

```
   IPC mechanism                              throughput        ns/msg (inverse)   note
   pipe (kernel-mediated)                     ~3-8 Mmsg/s       ~125-330 ns        write+read syscalls + copies (Ch. 54, 63)
   Unix domain socket (socketpair; not in ipc.cpp) ~2-6 Mmsg/s  ~170-500 ns        comparable to a pipe, often SLOWER for tiny messages (skb alloc + socket lock)
   Ch. 45 SpscRing in a MAP_SHARED segment    ~80-150 Mmsg/s    ~7-12 ns           no kernel per message — the §45.3 figure, unchanged

   the shared-memory ring is ~20x faster than the pipe — and IS the in-process SPSC ring of §45.3, byte for byte.
```

Read it: the **shared-memory ring is ~20× faster than a pipe** and lands at *the same ~7-12 ns/msg as the in-process SPSC ring* (§45.3) — not approximately, but because it *is* that ring: the only things that changed are where its bytes live (a `MAP_SHARED` mapping instead of a global) and which process runs `push` and which runs `pop`. At the hardware level a lock-free ring over shared cache lines doesn't know about processes (§67.2). The **pipe** pays a `write` syscall (copy user→kernel) and a `read` syscall (copy kernel→user) **per message** — the per-syscall cost (Ch. 54, 63) plus copies dominate, capping it at single-digit Mmsg/s. The lesson is unambiguous: **for hot-path IPC, use a shared-memory lock-free ring — it makes the process boundary as cheap as a thread boundary; reserve pipes/sockets/msg-queues for control-plane/low-rate IPC** where the convenience (kernel handles framing/lifecycle) outweighs the latency. Everything Ch. 45 taught about the ring — padded head/tail (Ch. 44), power-of-two capacity (Ch. 37), acquire/release (Ch. 41), the cached-opposite-index batching (§45.4.2) — carries over untouched; what this chapter adds is the segment around it (§67.4).

## 67.4 Techniques

### 67.4.1 Lock-free cross-process ring buffers

The hot-path IPC primitive — the Ch. 45/48 ring, placed in shared memory and made process-safe:

- **Reuse the Ch. 45 ring; its layout rules come with it.** Put the whole object — atomic head/tail indices *and* the message slots — in the `MAP_SHARED` region (Ch. 34) by placement-new, as §67.3 does, so both processes see the same indices and data; the atomics work across processes because coherence is hardware (§67.2), and the acquire/release protocol is exactly the in-process one (Ch. 41, 45). Everything §45.4.2 prescribes — producer-written `head` and consumer-written `tail` on separate cache lines (cross-process false sharing is the same ping-pong, Ch. 44), power-of-two capacity, caching the opposite index, pre-allocated storage — applies unchanged. Do *not* write a second ring for IPC: one implementation, tested once (Ch. 53), used in both places.
- **SPSC across processes is the default** (Ch. 45). One producer process, one consumer process — no CAS, no ABA, wait-free, simple. The shared-nothing process topology (Ch. 103) maps each one-way data flow (feed→strategy, strategy→gateway) to an SPSC shared-memory ring. MPSC (many producers → one consumer, e.g. several strategies → one gateway) where needed, with the cross-process CAS care of Ch. 45.
- **Slots are a wire format.** Messages are fixed-size plain-old-data copied into slots — no pointers (§67.4.2, §67.5), no heap (Ch. 30); variable-size messages use a fixed-slot ring with an overflow scheme, or a separate byte-ring with offset+length descriptors. Unlike the in-process ring, the slot layout crosses a *build* boundary too: the producer and consumer may be compiled separately, so version the layout in the segment header (§67.4.2, Ch. 70).
- **The segment is set up once, like any hot mapping (Ch. 34 §34.4.2).** Pre-faulted (`MAP_POPULATE`, as in §67.3, or a touch pass — Ch. 30), `mlock`'d, ideally huge-page-backed (Ch. 17) and on the NUMA node shared by the producer and consumer cores (Ch. 18 — or accept one cross-node hop if they're on different nodes).
- **Busy-poll the consumer (Ch. 54).** The consumer process busy-polls the ring's index on its dedicated core — instant, no kernel, no notification. Only a cold/background consumer blocks (futex/eventfd on a shared word — §67.2).

### 67.4.2 Shared-memory layout and addressing

The discipline that makes a shared region correct across address spaces (the generational slot map of Ch. 32 §32.4.4 is the relocatable, validated handle these rules ask for):

- **Offsets/indices, never pointers (the cardinal rule — §67.5).** The segment maps at different base addresses per process, so store **offsets from the segment base** (compute the real address as `base + offset` in each process) or **integer indices** (into the ring's slot array). A `T*` written by one process is garbage in another. This applies to the ring indices (already integers), *and* to any internal structure you put in shared memory (a free-list, a map) — index-based designs (Ch. 32's index handles) are mandatory here, not optional.
- **Define a fixed, versioned layout (a wire format — Ch. 70).** The shared region's structure (header, ring offsets, slot format) is a *contract* between processes that may be compiled separately and deployed at different versions. Define it as a fixed-layout struct (explicit sizes, no implementation-defined padding — Ch. 11), put a **version/magic** field in a header, and check it on attach (so a version mismatch fails loudly, not silently corrupts). Treat layout changes like protocol changes (Ch. 70).
- **A header with metadata.** The segment typically starts with a header: magic/version, the offsets/sizes of the rings within it, producer/consumer PIDs (for liveness — Ch. 103 §103.4.8: pid + start time, an epoch and a heartbeat slot), and any config. Both processes read the header on attach to locate the rings.
- **Single-writer per region (Ch. 42, 103).** Each shared region (or each ring within it) has exactly one writer process — the foundation of correctness (tractable ordering — Ch. 41) and fault containment (Ch. 103). Many readers are fine (seqlock publication — Ch. 46 — for shared *snapshots* like a published BBO across processes).
- **Alignment for atomics and SIMD (Ch. 11).** The shared atomics must be naturally aligned (for lock-free atomicity — Ch. 41) and on their own cache lines (Ch. 44); SIMD-accessed data over-aligned (Ch. 11). Lay out the shared struct deliberately (it's a binary contract).

### 67.4.3 Segment setup and cross-process synchronization

The parts of IPC that are *not* the ring — how two unrelated processes come to share the same pages, and how they wait on each other when they must:

- **Creating and sharing the segment.** Three routes. `shm_open("/feed-ring", O_CREAT|O_RDWR, 0600)` + `ftruncate` + `mmap(MAP_SHARED)`: a *named* tmpfs object any process with the name and permissions can attach — convenient, persistent across crashes (§67.5), and discoverable by an attacker who can read `/dev/shm`. `memfd_create("feed-ring", MFD_ALLOW_SEALING)` + `mmap`: an *anonymous* file with no name — pass the fd to the peer over a Unix socket with **`SCM_RIGHTS`** (`sendmsg` with a `SOL_SOCKET`/`SCM_RIGHTS` control message), and apply **seals** (`fcntl(F_ADD_SEALS, F_SEAL_SHRINK|F_SEAL_GROW)`) so neither side can resize it under the other — the modern default when a supervisor (Ch. 103) spawns the processes. For huge pages (Ch. 17), `MFD_HUGETLB` or a `hugetlbfs` file; `MAP_POPULATE` or a touch pass pre-faults either (Ch. 30/34). The §67.3 listing uses the fourth, simplest route — `MAP_SHARED|MAP_ANONYMOUS` inherited across `fork()` — which only works between parent and child.
- **Cross-process futex (the wake-up when a consumer may sleep).** A `futex` word *in the segment* works across processes as long as the futex is **shared** (no `FUTEX_PRIVATE_FLAG` — `FUTEX_WAIT`/`FUTEX_WAKE`, not the `_PRIVATE` variants libc uses for intra-process locks). The protocol is Ch. 54's: the consumer loads the sequence it saw, re-checks the ring, then `FUTEX_WAIT(word, seen)` — the kernel sleeps it only if the word still equals `seen`, so a producer that bumps the word and `FUTEX_WAKE`s between the check and the wait cannot lose the wake-up; the producer wakes only when it observes a "consumer sleeping" flag, to keep the syscall off the common path. `eventfd` is the simpler, fd-based alternative when the consumer sits in an `epoll` loop (Ch. 63). Neither belongs on a busy-polling hot path (Ch. 54).
- **Process-shared and robust mutexes (when a lock is unavoidable).** For control-plane state in the segment (not the data ring), `pthread_mutexattr_setpshared(PTHREAD_PROCESS_SHARED)` makes a `pthread_mutex_t` in shared memory work across processes, and `pthread_mutexattr_setrobust(PTHREAD_MUTEX_ROBUST)` is the standard answer to this chapter's "producer died mid-write": the next locker gets `EOWNERDEAD`, repairs the protected state, calls `pthread_mutex_consistent()`, and continues — instead of every process hanging forever on a lock whose owner no longer exists. `PTHREAD_PRIO_INHERIT` (Ch. 59) composes with both. Same for `pthread_cond_t`/`pthread_rwlock_t` with the `pshared` attribute.
- **Trust the fd, not the name.** Whoever can open the name can write the segment; an fd passed over `SCM_RIGHTS` from a supervisor you trust is a capability the recipient could not have forged. Prefer memfd + `SCM_RIGHTS` for anything that crosses a trust boundary (Ch. 101), and mount `/dev/shm` with restrictive permissions where names are unavoidable.


### 67.4.4 Schema evolution: versioning, compatibility windows and rolling upgrades

§67.4.2 said *version the layout and fail loudly on a mismatch*; that is the right policy for a lockstep upgrade (every process on the segment restarted together, pre-open) and the wrong one for the independently restartable roles Ch. 103 §103.2.1 promises and the intraday cutover Ch. 119 §119.4.3 performs. Evolving a layout while old and new processes share a ring needs four mechanisms:

- **Three places a version can live, and what each buys.** A **segment-level version** in the header (checked once at attach) says "every record in this ring has layout *V*" — cheap, and it means a layout change is a new segment. A **per-message version** (a type/version byte or an SBE-style header per slot) lets one ring carry mixed versions, so a writer can be upgraded while readers still read the old records queued ahead — the price is a check per message. **Per-field presence** (a bitmap, or a length that implies which trailing fields exist) is the finest grain and what makes *additive* evolution free. Use all three: segment version for the ring's structure (offsets, slot size, header), per-message version + length for the records, presence-by-length for fields.
- **Additive-only evolution with length-driven skipping.** The only intraday-safe change is *append a field at the end of a record and bump the version*: a reader built for version *V* reads the first `blockLength_V` bytes and **skips the rest by the writer's length**, never by its own; a reader built for *V+1* reading a *V* record sees `blockLength_V < blockLength_{V+1}` and treats the missing trailing fields as absent (a default, or "not present"). Reserve padding in the slot (a 64-byte slot with a 40-byte record has 24 bytes of runway) so an addition fits without changing the ring's geometry. Renaming, reordering, widening or removing a field is a *new message type* with its own id, not a new version of the old one — the old type keeps flowing until no reader needs it.
- **Why layouts drift without a source change.** Two processes built from the same header can disagree on the layout: a different compiler or `-m` flag (Ch. 11 §11.2 — `alignof(long double)`, `-mno-ms-bitfields`, packing pragmas), a `[[no_unique_address]]` or bit-field interpretation that differs between GCC and Clang, a re-padding after someone added `alignas(64)` for Ch. 44's false-sharing fix. The defence is a **layout fingerprint**: `static_assert(sizeof/alignof/offsetof)` tables pin the layout at compile time (Ch. 11 §11.3, Ch. 29 §29.4.3), and a *hash of the layout* — `pahole`'s output for the record types, or a C++26 reflection-derived digest of `(name, offset, size)` per member — is computed at build time, embedded in the binary, written into the segment header at create and into the capture header (Ch. 109 §109.4.2), and compared at attach: a fingerprint mismatch under the *same* version number is the "rebuilt struct" refusal of Ch. 119 §119.4.2, and it catches the drift no version bump would.
- **The rolling-upgrade sequence.** For an additive change: (1) deploy readers that accept `[V, V+1]` (they still see only *V*); (2) deploy the writer that writes *V+1*; (3) once every reader is on the new binary, retire *V* from the window. For a change that *removes* a field: the reverse — writer first stops *depending* on the field (still writes it), readers stop reading it, then the writer stops writing it. Which order is right is a property of the change, and it is recorded with the change (the release manifest, Ch. 119 §119.4.1). The **compatibility window** — which `(reader version, writer version)` pairs must interoperate — is declared in code (Ch. 119 §119.4.2's `LayoutWindow`) and enforced at attach, and the readiness gate (Appendix M.4) checks every process's window against every live segment before arming.
- **The mixed-version test.** CI runs the previous release's reader against the new writer and vice versa on one shared ring (a Ch. 116 table-driven case per declared pair), asserting that every record decodes to the same fields both agree on and that the skipped trailing bytes are the ones the writer appended; the test fails the build when a change breaks a pair inside the window.
- **What the per-message check costs.** `vercheck.cpp` on the 4-vCPU KVM guest (GCC 13.3, `-O2 -march=native`): an unchecked fixed-16-byte decode is **1.65 cycles/message**; the same decode behind an SBE-style header — `schemaId`, `version ∈ [1, 2]`, `blockLength ≥ 16` as one predicated compare, then a `blockLength`-driven read of the optional trailing field and a `blockLength`-driven advance — is **6.5 cycles/message**. The *check* is one predictable branch (~1 cycle); the rest is the **length-driven advance**: the next message's offset now depends on a load from this one, a dependency chain the fixed-stride loop did not have. Any length-prefixed framing pays it (the wire already does — Ch. 70 §70.2.2); a shared-memory ring with fixed-size slots can keep the fixed stride and put the version and length *inside* the slot, which gets the check for ~1 cycle and the skipping for free. Verify in asm (Ch. 4) that the version compare compiled to a `cmp`/`setcc` chain and not a `switch`.

## 67.5 Pitfalls & anti-patterns: pointers across address spaces

- **Storing pointers in shared memory (the #1 IPC bug).** A `T*` written by the producer is a *different process's* virtual address — meaningless (and a likely crash/corruption) in the consumer, which mapped the segment at a *different* base. §67.4.2 has the rule and the layout discipline (offsets-from-base or integer indices for *everything*); this is the defining shared-memory trap, so it heads the list.
- **Cross-process false sharing (Ch. 44).** The ring's head/tail (written by different processes/cores) on the same cache line ping-pongs across the process boundary — the Ch. 44 bug, identical cost. **Pad** the shared atomics to separate cache lines.
- **Wrong/missing memory ordering across processes (Ch. 41).** The same acquire/release discipline applies across processes as across threads — and the same x86-hides-ARM trap (Ch. 41). Publish data with release, observe the index with acquire; validate on ARM / model-check (Ch. 53). Relaxed everywhere "works on x86" and races.
- **Orphaned segments / lifecycle leaks.** A *named* `shm_open()`/tmpfs segment outlives a crashed process (orphaned in `/dev/shm`) — design cleanup/re-attach; an anonymous `MAP_SHARED|MAP_ANONYMOUS` or `memfd` segment dies with its last reference (pass the memfd over `SCM_RIGHTS` for automatic cleanup; use a name only when a late joiner must find the segment on its own — §67.4.3). Either way a producer that died mid-write leaves the ring in an indeterminate state. Design **cleanup and re-attach** (Ch. 103's crash-only design): `shm_unlink` on clean shutdown, detect-and-recreate (or reconcile) on restart, a robust mutex (`EOWNERDEAD` — §67.4.3) for any lock-protected control state, and a way to tell a stale segment from a live one (the header version/PID + liveness — Ch. 103).
- **Trusting the shared region (Ch. 101's untrusted-input discipline, at a process boundary).** Any process with the segment mapped can write *anything* into it — a buggy or compromised producer can corrupt the consumer (out-of-range indices, garbage data, a malicious wire format). The consumer must **validate** what it reads (bounds-check indices, sanity-check messages — Ch. 70, 101) and not blindly trust the shared region. The single-writer discipline (Ch. 42, 103) limits *who* can write, but the reader still validates.
- **Using a pipe/socket on the hot path.** Reaching for the *easy* IPC (pipe, Unix socket, msg queue) on the data path pays the per-message syscall + copy (§67.3) — microseconds where nanoseconds are needed. Shared-memory ring for hot data; kernel IPC only for control-plane.
- **Blocking the consumer with a slow notification.** If the consumer blocks on a futex/eventfd (instead of busy-polling), a slow or lost wakeup (Ch. 45's lost-wakeup) adds latency. On the hot path **busy-poll** (no notification — Ch. 54); use futex/eventfd only for cold consumers, with a correct wakeup protocol.
- **A lockstep-only version policy on roles meant to restart independently.** "Fail loudly on mismatch" with no compatibility window means every layout change is a restart of every process on the segment; additive evolution with length-driven skipping, a declared window and a fingerprint (§67.4.4) is what makes a rolling upgrade possible.
- **Non-fixed / unversioned layout.** A shared struct with implementation-defined padding (Ch. 11) or no version field breaks when the two processes are compiled differently or deployed at different versions — silent corruption. Fixed layout + version check (§67.4.2).
- **Forgetting to pre-fault/`mlock` the segment (Ch. 30, 34).** A shared segment that faults on first touch (or gets swapped) spikes the hot-path latency (Ch. 30). Pre-fault + `mlock` at setup, like all hot-path memory.

## 67.6 Exercises & checklist

**Exercises**

1. **shm vs pipe.** Build `ipc.cpp` against Ch. 45's `spsc_ring.hpp`; run `pipe` vs `shm` on two pinned cores. Confirm the shared-memory ring is ~20× faster and matches the in-process SPSC number (§45.3). Then convert the measurement from inverse throughput to one-way latency: stamp `rdtsc` (Ch. 20) into each slot, histogram `now - stamp` on the consumer. Add `strace -c` — the pipe shows millions of read/write; the shm ring shows ~none on the loop (§67.3).
2. **The pointer trap.** Store a pointer (to a slot, or to heap) in the shared region and read it from the other process. Note that `ipc.cpp`'s `fork()` child inherits the parent's address space, so the segment sits at the *same* virtual address in both and the bug stays hidden; to expose it, either `exec` two separate programs that attach via `shm_open()`/`memfd_create()` + `mmap(nullptr, …)` (ASLR then gives different bases), or have the child `munmap` and re-map the segment at a different address (`MAP_FIXED_NOREPLACE`, Linux 4.17 — never plain `MAP_FIXED`, which silently clobbers whatever is already mapped there). Watch it crash/corrupt. Fix with an offset/index (§67.4.2).
3. **Cross-process false sharing.** Put the ring's head and tail on the *same* cache line; measure the throughput drop and HITM (`perf c2c` — Ch. 44) across the process boundary. Re-pad; confirm recovery.
4. **Ordering on ARM.** Weaken the cross-process ring to `relaxed`; show it "works" on x86 and races on ARM (Appendix A) / model-check (Ch. 53). Restore acquire/release (§67.5).
5. **Lifecycle + validation.** Kill the producer process mid-run; show the orphaned segment and design re-attach (header version/PID check — Ch. 103 §103.4.8). Then have a "buggy" producer write out-of-range indices; add consumer-side validation that detects and rejects it (§67.5, Ch. 101).

**Checklist — inter-process communication**

- [ ] Hot-path IPC uses a **lock-free ring in a shared `mmap` segment** (Ch. 34, 45) — **not** pipes/sockets/msg-queues (kernel + copy per message — §67.3); kernel IPC is **control-plane only**.
- [ ] **No pointers in shared memory** — offsets-from-base / integer indices for *everything* (ring indices, internal links — §67.4.2, §67.5).
- [ ] Shared atomic head/tail are **padded to separate cache lines** (no cross-process false sharing — Ch. 44); **acquire/release ordering** is correct and **validated on ARM/model-checked** (Ch. 41, 53).
- [ ] The segment has a **fixed, versioned, fixed-layout header** (magic/version + ring offsets/sizes + PIDs) checked on attach — treated as a **wire format** (Ch. 11, 70).
- [ ] Each region/ring has a **single writer** (Ch. 42, 103); shared *snapshots* (e.g. a published BBO) use a **seqlock** (Ch. 46).
- [ ] The segment is **pre-faulted, `mlock`'d, huge-page-backed, NUMA-placed** (Ch. 17, 18, 30, 34); the consumer **busy-polls** on a dedicated core (Ch. 54), blocking only on cold paths.
- [ ] **Lifecycle** is designed: clean `shm_unlink`, crash detection / re-attach / recreate (Ch. 103 §103.4.8) — no orphaned/stale segments silently reused.
- [ ] The consumer **validates** what it reads (bounds-check indices, sanity-check messages — Ch. 70, 101) — it doesn't blindly trust the shared region (a trust/security boundary).

## 67.7 References

- W. R. Stevens & S. Rago, *Advanced Programming in the UNIX Environment*, and Stevens, *UNIX Network Programming, Vol. 2 (IPC)* — pipes, sockets, message queues, and shared memory (the spectrum of §67.2).
- The Linux man pages — `shm_open(3)`, `mmap(2)` (`MAP_SHARED`), `memfd_create(2)`, `pipe(2)`, `unix(7)` (domain sockets), `mq_overview(7)`, `futex(2)`, `eventfd(2)` — the IPC mechanisms.
- The LMAX Disruptor paper and the **Aeron** messaging system (Thompson et al.) — shared-memory ring buffers for IPC (Aeron is essentially this chapter productionized), and cross-process publication (§67.4).
- Ch. 45/46 references (Vyukov, the seqlock/RCU literature) — the lock-free rings and single-writer publication placed in shared memory.
- The `/dev/shm` (tmpfs) and shared-memory security documentation — segment lifecycle and the trust boundary of §67.5 (ties Ch. 101).

## 67.8 Additional Reading

- The Aeron and Chronicle Queue (Java, but the design transfers) documentation — production shared-memory IPC/messaging with offset-based addressing and versioned layouts.
- "Mechanical Sympathy" posts on shared-memory IPC and single-writer cross-process design.
- Ch. 34 (*Memory Mapping*) — the shared-segment substrate; Ch. 45 (*Lock-Free*) — the ring; Ch. 46 (*Seqlocks*) — cross-process snapshot publication; Ch. 44 (*False Sharing*) / Ch. 41 (*Ordering*) — the cross-process correctness; Ch. 103 (*Process Topology*) — why split into processes and the data-plane design; Ch. 70 (*Wire Decoding*) — the slot/message format; Ch. 101 (*Security*) — the shared-memory trust boundary.
- Ch. 45 §45.3 and Ch. 54 §54.3 — the ring-handoff and syscall/context-switch figures the §67.3 rows decompose into (Appendix E has no pipe-vs-shm row, and its "IPC" entry is instructions per cycle).

---

*Next: Ch. 68 — Socket Optimization & TCP/Protocol Tuning, back to the network: `TCP_NODELAY`/Nagle (the millisecond bug), buffer sizing, congestion control, and multicast for market data — the socket-level knobs that turn the kernel stack of Ch. 63 from default-slow into as-fast-as-the-kernel-gets.*
