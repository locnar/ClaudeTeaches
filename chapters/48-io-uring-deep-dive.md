# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 48 — io_uring Deep Dive

> **Prerequisites:** Ch. 47 (Linux native I/O — the readiness/`epoll` baseline io_uring's completion model improves on), Ch. 41 (syscalls/context switches — io_uring's whole point is fewer of them), Ch. 34/37 (ring buffers / SPSC — io_uring *is* shared-memory rings), Ch. 26 (mmap — the SQ/CQ are mmap'd), Ch. 23 (registered buffers avoid per-op setup).
>
> **Leads into:** Ch. 50 (IPC), Ch. 62 (kernel bypass — io_uring is the middle ground before it), Ch. 75 (capture/persistence — io_uring for async journaling/`O_DIRECT`), Ch. 38 (coroutines — io_uring completions pair naturally with `co_await`). The completion model contrasts with Ch. 47's readiness model.

---

## 48.1 Why it matters: completion-based I/O with fewer syscalls

Chapter 47's `epoll` model has an irreducible cost: **two syscalls per I/O** (one `epoll_wait` to learn the socket is ready, one `read`/`write` to move the data) plus the per-call kernel crossing (Ch. 41, ~hundreds of ns each, worse with spec-exec mitigations — Ch. 6). At high message rates that syscall overhead dominates. **io_uring** (Linux 5.1+, 2019) is the kernel's answer: a **completion-based**, **shared-memory ring** interface where you *submit* I/O operations into a ring the kernel reads, and *reap completions* from a ring the kernel writes — so a thread can submit *many* operations and collect *many* completions with **few or even zero syscalls**. It's the most significant Linux I/O advance in a decade, and for low-latency I/O it's the middle ground between the kernel-stack baseline (`epoll` — Ch. 47) and full kernel bypass (Ch. 62): far fewer syscalls than `epoll`, while still using the kernel stack (so it works with regular sockets/files, unlike bypass).

The mechanism is exactly the shared-memory ring-buffer pattern this book has built up (Ch. 34, 37): two **`mmap`'d** (Ch. 26) ring buffers shared between your process and the kernel — a **Submission Queue (SQ)** you write operations into and a **Completion Queue (CQ)** the kernel writes results into. You fill SQ entries (SQEs) describing operations ("read N bytes from fd into buffer B", "send this", "accept a connection"), advance the ring's tail, and — in the most efficient modes — the kernel *polls* the SQ and processes them *without you making a syscall at all*. Completions appear in the CQ, which you read directly from user space (again, no syscall). This is the completion model (Ch. 47.2): you don't ask "is it ready?" and then do the I/O; you say "do this I/O" and get told "it's done." One round-trip instead of two, batched, and pollable.

For HFT, io_uring's relevance is specific and growing: it's excellent for the **high-rate, many-operation** I/O around (not *on*) the ultra-hot path — **async disk journaling** (Ch. 75 — capturing every tick/order to disk *off* the hot path, with `O_DIRECT` and batched writes), high-throughput socket I/O for less-latency-critical feeds, and as the async substrate for coroutine event loops (Ch. 38 — completions map cleanly onto `co_await`). For the *tick-to-trade network* path, io_uring with **polling mode** gets meaningfully closer to bypass latency than `epoll`, though for the absolute floor, kernel bypass (Ch. 62, ef_vi/DPDK) still wins because it removes the kernel stack entirely. The decision tree: `epoll` for general/compatible I/O (Ch. 47), **io_uring for high-rate async I/O and to cut syscalls** (this chapter), **bypass for the ultra-hot network path** (Ch. 62). This chapter explains the SQ/CQ and polling-mode mental model (§48.2), measures io_uring vs `epoll` (§48.3), details registered buffers/files, polling mode, and submission batching (§48.4), and warns about the subtle completion-ordering and lifetime traps (§48.5).

## 48.2 Mental model: submission/completion queues; polling mode

**The two shared rings (mmap'd between user and kernel — Ch. 26, 34):**

```
   user space                              kernel
   ┌─────────────────────────────┐
   │ Submission Queue (SQ ring)  │── you write SQEs (ops) here, advance the tail ──►  kernel reads,
   │   SQE: "read fd 5, buf B,    │                                                    performs the I/O
   │         len N, user_data=X"  │
   └─────────────────────────────┘
   ┌─────────────────────────────┐
   │ Completion Queue (CQ ring)  │◄── kernel writes CQEs (results) here ──  you read directly,
   │   CQE: user_data=X, res=N    │                                          no syscall (just a load)
   └─────────────────────────────┘
     io_uring_enter() syscall: submit pending SQEs and/or wait for completions —
       but in POLLING modes, even this syscall is avoided (kernel polls the SQ; you poll the CQ)
```

- **Submit:** fill an SQE (the operation descriptor — opcode, fd, buffer, offset, and a `user_data` cookie you'll get back to correlate the completion), advance the SQ tail. Submitting is a *memory write*, not a syscall.
- **Process:** the kernel consumes SQEs and performs the I/O (asynchronously — it can do many in flight).
- **Complete:** the kernel posts a CQE (with your `user_data` and the result/error) to the CQ; you read it directly (a memory read). Completing is a *memory read*, not a syscall.
- **`io_uring_enter`:** the one syscall that (in non-polling mode) tells the kernel "I've added SQEs, process them" and/or "wait for completions." **One `io_uring_enter` can submit and reap *many* operations** — that's the batching that slashes syscalls vs `epoll` (one syscall per *batch*, not per I/O).

**The polling modes — where the syscalls disappear entirely:**

- **SQPOLL (submission-queue polling):** a **kernel thread** polls the SQ ring for new SQEs — so you submit operations by *just writing the ring* (advancing the tail) with **no `io_uring_enter` syscall at all**. The kernel thread picks them up. This makes submission *syscall-free* — at the cost of a dedicated kernel poller thread (pin it appropriately — Ch. 42) burning CPU. The lowest-latency submission path.
- **IOPOLL (completion polling):** for (storage) I/O, the kernel *busy-polls* the device for completions instead of using interrupts — lower completion latency for NVMe (Ch. 75), at the cost of CPU. (Requires `O_DIRECT` and pollable devices.)
- **Combined with user-side CQ polling:** you busy-poll the CQ ring (a memory read) for new completions instead of blocking in `io_uring_enter` — so on a dedicated core, the *whole* loop is syscall-free: kernel polls SQ, you poll CQ, zero syscalls per I/O (Ch. 41's dream). This is io_uring's closest approach to bypass within the kernel.

**Registered resources (avoid per-op setup — §48.4.1):** **registered buffers** (`io_uring_register` buffers once, so the kernel doesn't re-pin/map them per operation — Ch. 23) and **registered files** (pre-register fds to skip per-op fd lookup) cut per-operation overhead — the io_uring equivalent of pre-allocation/pre-faulting (Ch. 23, 26).

The model: **io_uring is two mmap'd shared rings (SQ you fill with operations, CQ the kernel fills with results) — completion-based (submit "do this," get "it's done"), so one batched syscall (or, in SQPOLL+CQ-polling, *zero* syscalls) handles many I/Os. Registered buffers/files cut per-op cost. It's the kernel's low-syscall I/O — the middle ground between `epoll` (Ch. 47) and bypass (Ch. 62).**

## 48.3 Measure it: io_uring vs `epoll`

Measure what io_uring is *for*: **syscalls per I/O** and the resulting latency/throughput, vs `epoll` (Ch. 47). The headline metric is the syscall count collapsing (and the latency that follows), especially with SQPOLL.

```cpp
// iouring_rtt.cpp — sketch: TCP/echo or disk I/O via io_uring vs epoll; count syscalls.
// Build: g++ -O2 -std=c++20 iouring_rtt.cpp -o iou -luring   (needs liburing)
// Run:  taskset -c 2 ./iou   ;  measure with: strace -c -f ./iou   (syscall counts)
#include <liburing.h>
#include <cstdio>
#include <cstdint>
// ... setup elided ...
// epoll model (Ch.44):  per I/O = epoll_wait() + read()/write()  = ~2 syscalls/op
// io_uring (default):   batch N ops, one io_uring_enter() submits+reaps all  = ~1 syscall / N ops
// io_uring (SQPOLL + CQ poll): kernel polls SQ, you poll CQ  = ~0 syscalls/op (dedicated kernel thread)

int main() {
    struct io_uring ring;
    // io_uring_queue_init(QD, &ring, IORING_SETUP_SQPOLL);   // SQPOLL: syscall-free submission
    // loop: get SQE, prep read/write (io_uring_prep_read/send), set user_data, submit (or not, in SQPOLL),
    //       then reap CQEs (io_uring_peek_cqe / wait_cqe), match user_data, process. Batch heavily.
    (void)ring;
    std::printf("see representative numbers below (use strace -c to see the syscall collapse)\n");
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), pinned, turbo off, high-rate small-message I/O (illustrative):

```
   model                          syscalls / I/O    p50 latency    throughput     note
   epoll (Ch.44, per-op)          ~2                ~5 µs          baseline       epoll_wait + read/write each
   io_uring (batched enter)       ~1 / batch        ~3.5 µs        ~2-3x higher   one enter submits+reaps many
   io_uring SQPOLL + CQ poll      ~0                ~2.5 µs        highest         kernel polls SQ, user polls CQ
   (preview) kernel bypass (Ch.52) ~0 (no kernel)   ~1 µs          —              removes the stack entirely

   strace -c: epoll shows millions of read/write/epoll_wait; SQPOLL io_uring shows ~NONE on the hot loop.
```

Read it: the story is the **syscall count**. `epoll` (Ch. 47) pays ~2 syscalls per I/O — and `strace -c` on an `epoll` server shows millions of `read`/`write`/`epoll_wait` calls, each a kernel crossing (Ch. 41, 47). io_uring with **batched `io_uring_enter`** amortizes to ~1 syscall per *batch* of operations — a large reduction at high rates — and with **SQPOLL + user-side CQ polling**, the hot loop makes **essentially zero syscalls** (kernel thread polls the SQ, you poll the CQ — both just memory accesses), so `strace` shows ~nothing on the steady-state path. The latency follows: each removed syscall/context-switch (Ch. 41) shaves time, getting io_uring meaningfully below `epoll` (~2.5 vs ~5 µs here). **But the floor is still the kernel stack** — io_uring uses the same IP/TCP processing as `epoll`, just reaches it with fewer syscalls; **kernel bypass (Ch. 62) is still lower** because it removes the stack. So io_uring's sweet spot is **high-rate I/O where syscall overhead dominates** (async journaling — Ch. 75, bulk feeds) and as a lower-syscall network path — a big win over `epoll`, a middle ground before bypass. The measurement to run: `strace -c` to *see* the syscalls collapse (that's the mechanism), then the latency/throughput that results.

## 48.4 Techniques

### 48.4.1 Registered buffers and files

Cut per-operation overhead by pre-registering resources (the io_uring analog of pre-allocation — Ch. 23, 26):

- **Registered buffers (`io_uring_register_buffers`).** Normally, each I/O operation requires the kernel to *pin* (and validate) the user buffer for the duration — per-op overhead. **Registering** a set of buffers once (at setup) means the kernel pins them *once*; operations reference a registered buffer by index (`io_uring_prep_read_fixed`/`write_fixed`), skipping the per-op pin/map. For a hot path reusing a fixed set of buffers (Ch. 23–24's pre-allocated, `mlock`'d pools), register them — eliminating per-I/O memory setup. Pairs with pre-faulting (Ch. 26).
- **Registered files (`io_uring_register_files`).** Pre-register the fds you'll use so operations reference a *registered index* instead of a raw fd — skipping the per-op fd table lookup/refcount. For a fixed set of sockets/files (the venue connections, the journal file), register them once.
- **Provided buffers (buffer rings).** A pool of buffers given to the kernel that it *fills* and hands back via the CQE (you don't pre-specify which buffer per read) — efficient for receive paths where you don't know which socket will have data. The modern `IORING_OP_RECV` with buffer rings is a high-throughput receive pattern.
- **Why it matters.** Per-operation pin/lookup is pure overhead at high I/O rates; registering moves it to setup (the cold path — Ch. 1). It's the same principle as registered memory in RDMA (Ch. 63) and pre-allocation everywhere in this book: do the expensive setup once, keep the hot path lean.

### 48.4.2 Polling mode for lowest latency

The polling modes are how io_uring approaches bypass latency — at the cost of dedicated CPU (Ch. 41's busy-poll trade):

- **SQPOLL (`IORING_SETUP_SQPOLL`) — syscall-free submission.** A kernel thread polls your SQ ring, so you submit operations by writing the ring (advancing the tail) with **no `io_uring_enter` syscall**. The kernel poller burns a core (configure its CPU affinity via `IORING_SETUP_SQ_AFF` + `sq_thread_cpu` — pin it to a housekeeping core, Ch. 42, not a hot trading core). It can idle-timeout (sleep when no work) to save CPU — tune `sq_thread_idle`. SQPOLL is the lowest-latency submission and the closest io_uring gets to "submit with a memory write."
- **User-side CQ polling.** Instead of blocking in `io_uring_enter(GETEVENTS)` (which sleeps — a context switch, Ch. 41), **busy-poll the CQ ring** in user space (`io_uring_peek_cqe` in a loop) on your dedicated core — completions are visible as memory writes. Combined with SQPOLL, the whole loop is syscall-free (§48.3) — the Ch. 41 ideal applied to kernel I/O.
- **IOPOLL (`IORING_SETUP_IOPOLL`) — for storage.** The kernel busy-polls the (NVMe) device for completions instead of waiting for an interrupt — lower completion latency for `O_DIRECT` disk I/O (Ch. 75's journaling/capture). Trades CPU for latency, like all polling.
- **The polling trade (Ch. 41).** Polling = lowest latency, 100% CPU (the kernel poller, your CQ poller). Right for a dedicated I/O core on a latency path; wrong for a power/CPU-constrained or shared setup (use the default blocking mode, which is *still* fewer syscalls than `epoll`). Match the mode to whether you've dedicated cores to I/O.

### 48.4.3 Batching submissions

Even without polling, batching is io_uring's core syscall-reduction lever (the Ch. 37/47 batching idea, native to io_uring):

- **Submit many SQEs per `io_uring_enter`.** Fill multiple SQEs (a batch of reads/writes/sends) and submit them with *one* `io_uring_enter` — amortizing the syscall over the whole batch. At high I/O rates this is the primary win over `epoll` (§48.3). The SQ depth bounds the batch.
- **Reap many CQEs per check.** Process *all* available completions per CQ check (`io_uring_for_each_cqe`) before submitting/waiting again — amortizing the reap (Ch. 37's consumer batching). The CQ holds many completions; drain them.
- **Linked operations (`IOSQE_IO_LINK`).** Chain dependent operations (e.g. write-then-fsync, or accept-then-read) so the kernel runs them in order without a round-trip back to you between them — fewer submit/reap cycles for multi-step I/O.
- **Multishot operations.** `IORING_OP_*` multishot variants (multishot accept, multishot recv) post *multiple* completions from *one* submission (e.g. accept every incoming connection, or receive every datagram) — one SQE, many CQEs — drastically cutting submissions for accept/receive-heavy workloads (a market-data receive loop).
- **Combine with application batching (Ch. 37).** Having reaped a batch of completions (received messages), process them as a batch through the pipeline (Ch. 37) — amortizing syscall *and* per-message overhead, self-correcting under load. io_uring's batching composes with the Disruptor's.

## 48.5 Pitfalls & anti-patterns: completion-ordering assumptions

- **Assuming completion order = submission order (the classic io_uring bug).** Operations complete **out of order** — io_uring runs them asynchronously and concurrently, so CQEs arrive in *completion* order, not submission order. You **must** use the `user_data` cookie in each SQE to correlate its CQE back to the operation/context. Code that assumes "the first completion is for the first submission" corrupts state. Always tag with `user_data` and match.
- **Buffer/lifetime bugs (use-after-free across async I/O).** The buffer an operation reads into/writes from **must stay valid until the completion arrives** — the kernel uses it *asynchronously*. Freeing/reusing a buffer before its CQE is a use-after-free / data corruption (Ch. 23, 72). Manage buffer lifetime by the completion (a per-op buffer owned until its CQE), or use registered/provided buffers with clear ownership. This is the io_uring analog of the coroutine-frame lifetime issue (Ch. 38).
- **SQPOLL kernel thread on a hot core.** The SQPOLL poller burns a full core; placing it (via `sq_thread_cpu`) on a *hot trading core* steals cycles from the trading thread (Ch. 41, 43). Pin it to a **housekeeping** core (Ch. 42), and tune `sq_thread_idle` so it sleeps when idle if you don't need constant submission.
- **CQ overflow.** If you submit faster than you reap, the CQ can **overflow** — completions are dropped/deferred (`IORING_CQ_EVENTFD`/overflow flags signal it). Size the CQ adequately, reap promptly (drain per loop — §48.4.3), and handle the overflow condition. An unreaped CQ is a silent failure.
- **Polling mode's CPU cost ignored.** SQPOLL + IOPOLL + CQ-polling = multiple cores at 100% (kernel poller + your poller + device poller). On a CPU/power-constrained box this is wasteful; use polling only where you've *dedicated* cores to it (Ch. 41's busy-poll trade). Default blocking io_uring is still far fewer syscalls than `epoll` without the polling cost.
- **Kernel-version dependence / feature churn.** io_uring evolves fast (new opcodes, fixes, security patches) and early versions had bugs and security issues (some shops disabled it). Features (multishot, registered buffers, specific opcodes) require minimum kernel versions; check `io_uring_get_probe`. And track security advisories (io_uring has been a CVE source — Ch. 72). Don't assume an opcode exists on your kernel.
- **Treating io_uring as a bypass replacement.** io_uring still uses the **kernel network stack** — it's lower-syscall, not stack-free. For the ultra-hot tick-to-trade path needing sub-microsecond, **kernel bypass** (Ch. 62) is still the answer; io_uring is the high-rate, lower-syscall middle ground (§48.1). Match the tool to the latency tier.
- **Over-deep submission queues adding latency.** A very deep SQ batches more (throughput) but a submitted op may wait behind others (latency). For latency-critical ops, submit promptly (smaller batches / immediate submit) rather than maximizing batch depth — the latency/throughput knob (Ch. 1).

## 48.6 Exercises & checklist

**Exercises**

1. **See the syscalls collapse.** Build an echo server with `epoll` and with io_uring (liburing); run `strace -c -f` on each at high message rate. Compare syscall counts (epoll: millions of read/write/epoll_wait; io_uring batched: far fewer). Then enable SQPOLL + CQ polling and confirm ~zero syscalls on the loop (§48.3).
2. **Latency tiers.** Measure round-trip latency: `epoll` busy-poll (Ch. 47) vs io_uring batched vs io_uring SQPOLL+CQ-poll. Quantify each step down (§48.3). How close to bypass (Ch. 62) does SQPOLL get?
3. **Out-of-order completions.** Submit several reads on different fds with distinct `user_data`; confirm CQEs arrive in *completion* (not submission) order, and that `user_data` correctly correlates them. Then (deliberately) assume order and watch it break (§48.5).
4. **Registered buffers.** Run a fixed-buffer workload with and without `io_uring_register_buffers` (+ `_fixed` ops); measure the per-op overhead difference at high I/O rate (§48.4.1).
5. **Async journaling (ties Ch. 75).** Build an `O_DIRECT` + io_uring (IOPOLL) async writer that journals a stream to NVMe off the hot path; measure write throughput and confirm the hot path isn't blocked on I/O. Compare to synchronous `write` (§48.4.2, Ch. 75).

**Checklist — io_uring**

- [ ] io_uring is used for **high-rate async I/O and to cut syscalls** (async journaling — Ch. 75, bulk feeds, lower-syscall sockets) — the middle ground between `epoll` (Ch. 47) and **bypass** (Ch. 62, still the floor for ultra-hot network).
- [ ] **Submissions and completions are batched** (many SQEs per `io_uring_enter`, drain all CQEs per check — §48.4.3); multishot/linked ops used where they fit.
- [ ] On dedicated I/O cores, **SQPOLL + user-side CQ polling** make the hot loop ~**syscall-free** (Ch. 41); the **SQPOLL kernel thread is pinned to a housekeeping core** (Ch. 42), not a hot trading core, with `sq_thread_idle` tuned.
- [ ] **Registered buffers/files** (and provided buffer rings) move per-op pin/lookup to setup (Ch. 23, 26); buffers are **pre-faulted/`mlock`'d**.
- [ ] **Completions are correlated by `user_data`** — I never assume completion order == submission order (§48.5).
- [ ] **Buffer lifetime** spans until the completion (no free/reuse before the CQE — no async UAF — Ch. 23, 72).
- [ ] The **CQ is sized and reaped promptly** (no overflow); polling-mode **CPU cost** is justified by dedicated cores (else default blocking io_uring).
- [ ] **Kernel version / feature availability** (opcodes, multishot, registered) is checked (`io_uring_get_probe`), and io_uring **security advisories** (Ch. 72) are tracked.

## 48.7 References

- J. Axboe, the io_uring design documents ("Efficient IO with io_uring") and the `liburing` library/man pages — the authoritative reference for SQ/CQ, polling modes, registered resources, and opcodes (this whole chapter).
- The Linux man pages — `io_uring(7)`, `io_uring_setup(2)`, `io_uring_enter(2)`, `io_uring_register(2)` — the syscall interface (§48.2, §48.4).
- The kernel `Documentation/` on io_uring and the LWN io_uring article series — the model, evolution, and trade-offs (§48.1-§48.2).
- Glauber Costa / Seastar and the various io_uring-vs-epoll benchmark writeups — measured syscall/latency comparisons behind §48.3.
- The io_uring security advisories / CVE discussions — the security caveat of §48.5 (ties Ch. 72).

## 48.8 Additional Reading

- The `liburing` examples and high-performance io_uring patterns (multishot, buffer rings, zero-copy send) — practical techniques extending §48.4.
- Coroutine + io_uring integration writeups (ties Ch. 38) — completions as awaitables.
- Ch. 47 (*Linux Native I/O*) — the `epoll`/readiness baseline; Ch. 62 (*Kernel Bypass*) — the sub-microsecond floor io_uring approaches but doesn't reach; Ch. 75 (*Capture & Persistence*) — io_uring/`O_DIRECT` async journaling; Ch. 38 (*Coroutines*) — io_uring completions as `co_await`; Ch. 23–26 (*Memory/mmap*) — registered/pre-faulted buffers; Ch. 41 (*Context Switching*) — the syscalls io_uring removes.
- **Appendix E** — io_uring vs epoll vs bypass latency/syscall numbers; **Appendix C** — io_uring/SQPOLL tuning.

---

*Next: Ch. 49 — Zero-Copy & the Modern Kernel Fast Path, removing the last per-byte cost from the kernel path without leaving it: `MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy send/receive, and devmem TCP — the NIC DMA'ing straight to and from your buffers.*
