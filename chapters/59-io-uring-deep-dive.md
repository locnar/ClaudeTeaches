# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 59 — io_uring Deep Dive

> **Prerequisites:** Ch. 58 (the kernel datapath's stage costs — io_uring amortises its syscall and copy stages), Ch. 57 (Linux native I/O — the readiness/`epoll` baseline io_uring's completion model improves on), Ch. 48 (syscalls/context switches — io_uring's whole point is fewer of them), Ch. 39/42 (ring buffers / SPSC — io_uring *is* shared-memory rings), Ch. 31 (mmap — the SQ/CQ are mmap'd), Ch. 28 (registered buffers avoid per-op setup).
>
> **Leads into:** Ch. 61 (IPC), Ch. 74 (kernel bypass — io_uring is the middle ground before it), Ch. 98 (capture/persistence — io_uring for async journaling/`O_DIRECT`), Ch. 45 (coroutines — io_uring completions pair naturally with `co_await`). The completion model contrasts with Ch. 57's readiness model.

---

## 59.1 Why it matters: completion-based I/O with fewer syscalls

Chapter 57's `epoll` model has an irreducible cost: **at least one syscall per data movement** (a `read`/`write` per message — fewer per datagram with `recvmmsg`, §57.4.2) plus an `epoll_wait` amortized over the ready batch — still a kernel crossing per read/write — and the per-call kernel crossing (Ch. 48, ~hundreds of ns each, worse with spec-exec mitigations — Ch. 6). At high message rates that syscall overhead dominates. **io_uring** (Linux 5.1+, 2019) is the kernel's answer: a **completion-based**, **shared-memory ring** interface where you *submit* I/O operations into a ring the kernel reads, and *reap completions* from a ring the kernel writes — so a thread can submit *many* operations and collect *many* completions with **few or even zero syscalls**. It's the most significant Linux I/O advance in a decade, and for low-latency I/O it's the middle ground between the kernel-stack baseline (`epoll` — Ch. 57) and full kernel bypass (Ch. 74): far fewer syscalls than `epoll`, while still using the kernel stack (so it works with regular sockets/files, unlike bypass).

The mechanism is exactly the shared-memory ring-buffer pattern this book has built up (Ch. 39, 42): two **`mmap`'d** (Ch. 31) ring buffers shared between your process and the kernel — a **Submission Queue (SQ)** you write operations into and a **Completion Queue (CQ)** the kernel writes results into. You fill SQ entries (SQEs) describing operations ("read N bytes from fd into buffer B", "send this", "accept a connection"), advance the ring's tail, and — in the most efficient modes — the kernel *polls* the SQ and processes them *without you making a syscall at all*. Completions appear in the CQ, which you read directly from user space (again, no syscall). This is the completion model (Ch. 57.2): you don't ask "is it ready?" and then do the I/O; you say "do this I/O" and get told "it's done." One round-trip instead of two, batched, and pollable.

For HFT, io_uring's relevance is specific and growing: it's excellent for the **high-rate, many-operation** I/O around (not *on*) the ultra-hot path — **async disk journaling** (Ch. 98 — capturing every tick/order to disk *off* the hot path, with `O_DIRECT` and batched writes), high-throughput socket I/O for less-latency-critical feeds, and as the async substrate for coroutine event loops (Ch. 45 — completions map cleanly onto `co_await`). For the *tick-to-trade network* path, io_uring with **polling mode** gets meaningfully closer to bypass latency than `epoll`, though for the absolute floor, kernel bypass (Ch. 74, ef_vi/DPDK) still wins because it removes the kernel stack entirely. The decision tree: `epoll` for general/compatible I/O (Ch. 57), **io_uring for high-rate async I/O and to cut syscalls** (this chapter), **bypass for the ultra-hot network path** (Ch. 74). This chapter explains the SQ/CQ and polling-mode mental model (§59.2), measures io_uring vs `epoll` (§59.3), details registered buffers/files, polling mode, and submission batching (§59.4), and warns about the subtle completion-ordering and lifetime traps (§59.5).

## 59.2 Mental model: submission/completion queues; polling mode

**The two shared rings (mmap'd between user and kernel — Ch. 31, 39):**

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

- **SQPOLL (submission-queue polling):** a **kernel thread** polls the SQ ring for new SQEs — so you submit operations by *just writing the ring* (advancing the tail) with **no `io_uring_enter` syscall** *while the poller is awake*. After `sq_thread_idle` ms without work it sleeps and sets `IORING_SQ_NEED_WAKEUP` in `sq_ring->flags`; you must then issue one `io_uring_enter(fd, 0, 0, IORING_ENTER_SQ_WAKEUP)` or your SQEs sit unprocessed — liburing's `io_uring_submit` checks the flag and does this for you. (Before 5.11 SQPOLL also required registered files; `IORING_FEAT_SQPOLL_NONFIXED` marks the fix.) This makes steady-state submission *syscall-free* — at the cost of a dedicated kernel poller thread (pin it appropriately — Ch. 50) burning CPU. The lowest-latency submission path.
- **IOPOLL (completion polling):** for (storage) I/O, the kernel *busy-polls* the device for completions instead of using interrupts — lower completion latency for NVMe (Ch. 98), at the cost of CPU. (Requires `O_DIRECT` and pollable devices.) There is no separate "device poller": the device is polled by whichever thread enters the kernel to reap — your thread in `io_uring_enter(GETEVENTS)`, or the SQPOLL thread when both flags are set.
- **Combined with user-side CQ polling:** you busy-poll the CQ ring (a memory read) for new completions instead of blocking in `io_uring_enter` — so on a dedicated core, the *whole* loop is syscall-free: kernel polls SQ, you poll CQ, zero syscalls per I/O (Ch. 48's dream). This is io_uring's closest approach to bypass within the kernel.

**Registered resources (avoid per-op setup — §59.4.1):** **registered buffers** (`io_uring_register` buffers once, so the kernel doesn't re-pin/map them per operation — Ch. 28) and **registered files** (pre-register fds to skip per-op fd lookup) cut per-operation overhead — the io_uring equivalent of pre-allocation/pre-faulting (Ch. 28, 31).

The model: **io_uring is two mmap'd shared rings (SQ you fill with operations, CQ the kernel fills with results) — completion-based (submit "do this," get "it's done"), so one batched syscall (or, in SQPOLL+CQ-polling, *zero* syscalls) handles many I/Os. Registered buffers/files cut per-op cost. It's the kernel's low-syscall I/O — the middle ground between `epoll` (Ch. 57) and bypass (Ch. 74).**

## 59.3 Measure it: io_uring vs `epoll`

Measure what io_uring is *for*: **syscalls per I/O** and the resulting latency/throughput, vs `epoll` (Ch. 57). The headline metric is the syscall count collapsing (and the latency that follows), especially with SQPOLL.

```cpp
// iouring_rtt.cpp — TCP echo client over io_uring: one request in flight, same wire format as the
// epoll_rtt.cpp echo server of Ch. 57 (the epoll baseline; 8-byte request, 8-byte echo), so the numbers are comparable.
// Build:  g++ -O2 -std=c++20 iouring_rtt.cpp -o iou -luring              (liburing 2.5, Linux 6.x)
//         g++ -O2 -std=c++20 -DSQPOLL iouring_rtt.cpp -o iou_sqpoll -luring   (kernel SQ poller + user-side CQ spin)
// Run:    ./epoll_rtt server &  (Ch. 57)  ;  taskset -c 2 ./iou  ;  taskset -c 2 ./iou_sqpoll
// Count syscalls on the steady-state loop: perf stat -e raw_syscalls:sys_enter -- ./iou   (or perf trace -s)
#include <liburing.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <algorithm>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <x86intrin.h>

static constexpr long kIters = 200'000;
static constexpr double kTscGHz = 2.9;                       // Appendix L.3

static void die(const char* what, int err = 0) { std::fprintf(stderr, "%s: %s\n", what, std::strerror(err ? -err : errno)); std::exit(1); }

int main() {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(9000); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (sockaddr*)&a, sizeof a) < 0) die("connect");
    int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);   // Ch. 62

    io_uring ring; io_uring_params p{};
#ifdef SQPOLL
    p.flags = IORING_SETUP_SQPOLL | IORING_SETUP_SQ_AFF;      // kernel thread polls the SQ ...
    p.sq_thread_cpu  = 1;                                      // ... pinned to a HOUSEKEEPING core (Ch. 50), never the hot one
    p.sq_thread_idle = 2000;                                   // ms before it sleeps and sets IORING_SQ_NEED_WAKEUP
#else
    p.flags = IORING_SETUP_SINGLE_ISSUER | IORING_SETUP_DEFER_TASKRUN;  // 6.1+: no task_work IPIs into this thread
#endif
    if (int r = io_uring_queue_init_params(16, &ring, &p); r < 0) die("io_uring_queue_init", r);
    int files[1] = { fd };
    if (int r = io_uring_register_files(&ring, files, 1); r < 0) die("register_files", r);   // fixed file 0: no per-op fd lookup

    std::uint64_t req = 0, rep = 0;
    std::vector<std::uint32_t> samples(kIters);
    for (long i = -2000; i < kIters; ++i) {                   // 2000 warm-up round trips (Ch. 56), then measured
        req = i;
        std::uint64_t t0 = __rdtsc();
        // Two linked SQEs, one submission: send, then recv — the kernel runs the recv when the send completes
        // (IOSQE_IO_LINK), so there is no round trip back to us between them.
        io_uring_sqe* s = io_uring_get_sqe(&ring);
        io_uring_prep_send(s, 0, &req, 8, 0); s->flags |= IOSQE_FIXED_FILE | IOSQE_IO_LINK; io_uring_sqe_set_data64(s, 1);
        io_uring_sqe* r = io_uring_get_sqe(&ring);
        io_uring_prep_recv(r, 0, &rep, 8, 0); r->flags |= IOSQE_FIXED_FILE;                 io_uring_sqe_set_data64(r, 2);
#ifdef SQPOLL
        io_uring_submit(&ring);                               // a ring write — liburing issues io_uring_enter only if the poller
                                                              // has gone idle (IORING_SQ_NEED_WAKEUP), otherwise NO syscall
        io_uring_cqe* cqe; int got = 0;
        while (got < 2) {                                     // user-side CQ SPIN: completions appear as memory writes
            unsigned head, n = 0;
            io_uring_for_each_cqe(&ring, head, cqe) { if (cqe->res < 0) die("op", cqe->res); ++n; }
            io_uring_cq_advance(&ring, n); got += n;
        }
#else
        if (int r2 = io_uring_submit_and_wait(&ring, 2); r2 < 0) die("submit_and_wait", r2);   // ONE syscall: submit both, sleep for both
        io_uring_cqe* cqe; unsigned head, n = 0;
        io_uring_for_each_cqe(&ring, head, cqe) { if (cqe->res < 0) die("op", cqe->res); ++n; }
        io_uring_cq_advance(&ring, n);                        // reap the batch
#endif
        std::uint64_t t1 = __rdtsc();
        if (i >= 0) samples[i] = std::uint32_t(t1 - t0);
    }
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double q) { return samples[std::size_t(q * (kIters - 1))] / kTscGHz / 1e3; };
    std::printf("io_uring%s loopback TCP echo RTT: p50=%.2f us  p99=%.2f us  p99.9=%.2f us  (rep=%llu)\n",
#ifdef SQPOLL
                " SQPOLL+CQspin",
#else
                " submit_and_wait",
#endif
                pct(0.5), pct(0.99), pct(0.999), (unsigned long long)rep);
    io_uring_queue_exit(&ring);
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3 — Linux 6.18, liburing 2.5, GCC 13; kernel version matters: SQPOLL semantics, multishot and buffer rings all changed across 5.x/6.x) — reference machine **Xeon Gold 6326** (Ice Lake-SP), client pinned, turbo off, **loopback TCP echo with one request in flight** (Ch. 57's server and protocol, so the rows are comparable with §57.3). Latency is round-trip; the throughput column is what the same code does when you keep the ring *full* (many requests in flight), which is where batching actually amortizes:

```
   model                                     syscalls / RTT    p50 RTT    p99 RTT    pipelined throughput   note
   epoll, blocking epoll_wait (§57.3)        2                 ~5.5 µs    ~21 µs     baseline               epoll_wait + read/write each
   epoll, spin / NAPI busy-poll (§57.3)      1 (the read)      ~3 µs      ~7 µs      ~1.5x                  no sleep/wake
   io_uring submit_and_wait (linked send+recv) 1               ~4.5 µs    ~18 µs     ~2-3x (one enter per BATCH) one syscall replaces two; still sleeps/wakes
   io_uring SQPOLL + user CQ spin            ~0                ~2.5 µs    ~5 µs      highest                kernel polls SQ, user polls CQ
   (preview, ONE-WAY wire→app) kernel bypass (Ch.74) 0        ~0.5-1 µs  ~1-2 µs    —                      removes the stack entirely (≈2 µs RTT)

   perf stat -e raw_syscalls:sys_enter: epoll shows two per round trip; SQPOLL io_uring shows ~NONE on the hot loop.
```

Read it: the story is the **syscall count**. `epoll` (Ch. 57) pays two syscalls per round trip — and `perf stat -e raw_syscalls:sys_enter` on an `epoll` server shows millions of `read`/`write`/`epoll_wait` calls, each a kernel crossing — a syscall (Ch. 48, 57). io_uring's **`submit_and_wait`** replaces them with one — but for a *single* request in flight that saves only one syscall crossing (~100-400 ns of syscall cost, Ch. 48) and still sleeps and wakes, so its RTT is ~4.5 µs, barely below blocking epoll; the "one `enter` per *batch*" win is a **throughput** win that appears only when many operations are in flight (the right-hand column). With **SQPOLL + user-side CQ polling**, the hot loop makes **essentially zero syscalls** (kernel thread polls the SQ, you poll the CQ — both just memory accesses) *and* never sleeps, so the steady-state path shows ~no syscalls and the RTT drops to ~2.5 µs. The like-for-like latency comparison is therefore **polled io_uring ~2.5 µs vs polled epoll ~3 µs vs blocking epoll ~5 µs**: against the busy-polled epoll of §57.3 io_uring's edge is the removed `read` syscall, not a different stack. **But the floor is still the kernel stack** — io_uring uses the same IP/TCP processing as `epoll`, just reaches it with fewer syscalls; **kernel bypass (Ch. 74) is still lower** because it removes the stack. So io_uring's sweet spot is **high-rate I/O where syscall overhead dominates** (async journaling — Ch. 98, bulk feeds) and as a lower-syscall network path — a big win over `epoll`, a middle ground before bypass. The measurement to run: `strace -c` to *see* the syscalls collapse (that's the mechanism), then the latency/throughput that results.

## 59.4 Techniques

### 59.4.1 Registered buffers and files

Cut per-operation overhead by pre-registering resources (the io_uring analog of pre-allocation — Ch. 28, 31):

- **Registered buffers (`io_uring_register_buffers`).** Be precise about what they save: per-operation page *pinning* (`get_user_pages` plus iov setup) happens for **`O_DIRECT`** reads/writes and for **zero-copy sends** — ordinary buffered-file and socket `recv`/`send` go through the `iov_iter` copy path and pin nothing, so for copying socket I/O (the venue connections) `READ_FIXED`/`WRITE_FIXED` buy little. **Registering** a set of buffers once (at setup) means the kernel pins them *once*; operations reference a registered buffer by index (`io_uring_prep_read_fixed`/`write_fixed`, `SEND_ZC` with `IORING_RECVSEND_FIXED_BUF`), skipping the per-op pin — which is exactly the `O_DIRECT` journal of Ch. 98 and the zero-copy paths of Ch. 60. For those, register the fixed, `mlock`'d pool of Ch. 28–29; pairs with pre-faulting (Ch. 31).
- **Registered files (`io_uring_register_files`).** Pre-register the fds you'll use so operations reference a *registered index* instead of a raw fd — skipping the per-op fd table lookup/refcount. For a fixed set of sockets/files (the venue connections, the journal file), register them once.
- **Provided buffers (buffer rings).** A pool of buffers given to the kernel that it *fills* and hands back via the CQE (you don't pre-specify which buffer per read) — efficient for receive paths where you don't know which socket will have data. The modern `IORING_OP_RECV` with buffer rings is a high-throughput receive pattern.
- **Why it matters.** Per-operation pin/lookup is pure overhead at high I/O rates; registering moves it to setup (the cold path — Ch. 1). It's the same principle as registered memory in RDMA (Ch. 75) and pre-allocation everywhere in this book: do the expensive setup once, keep the hot path lean.

### 59.4.2 Polling mode for lowest latency

The polling modes are how io_uring approaches bypass latency — at the cost of dedicated CPU (Ch. 48's busy-poll trade):

- **SQPOLL (`IORING_SETUP_SQPOLL`) — syscall-free submission.** A kernel thread polls your SQ ring, so you submit operations by writing the ring (advancing the tail) with **no `io_uring_enter` syscall while it is awake**. The kernel poller burns a core (configure its CPU affinity via `IORING_SETUP_SQ_AFF` + `sq_thread_cpu` — pin it to a housekeeping core, Ch. 50, not a hot trading core). After `sq_thread_idle` ms idle it sleeps and raises `IORING_SQ_NEED_WAKEUP`; the next submission must then pay one `io_uring_enter(IORING_ENTER_SQ_WAKEUP)` (liburing does it inside `io_uring_submit`) — on a latency path set the idle long enough that the poller never sleeps between messages, or accept that the first message after a quiet spell pays a syscall plus the poller's wake-up (the Ch. 56 cold-start, in kernel-thread form). SQPOLL is the lowest-latency submission and the closest io_uring gets to "submit with a memory write."
- **io-wq — the worker threads you didn't ask for.** Any operation that cannot complete without blocking (a buffered file read that misses the page cache, a blocking socket op on a non-pollable fd, anything marked `IOSQE_ASYNC`) is *punted* to **io-wq**: kernel worker threads io_uring spawns on demand, which inherit the issuing thread's affinity — on a pinned hot thread that means they appear **on your isolated core** (Ch. 50, 53), a classic source of mystery jitter. Pin them away with `io_uring_register_iowq_aff()` (`IORING_REGISTER_IOWQ_AFF`, 5.14) to the housekeeping mask and cap them with `io_uring_register_iowq_max_workers()` (`IORING_REGISTER_IOWQ_MAX_WORKERS`, 5.15); better still, give the hot ring only pollable, non-blocking operations so nothing is ever punted.
- **Keep task_work off the hot thread (`IORING_SETUP_SINGLE_ISSUER` + `IORING_SETUP_DEFER_TASKRUN`, 6.0/6.1).** By default completions arriving from another CPU are delivered via *task_work*, which interrupts the issuing thread with an IPI and runs completion code at an arbitrary point in your loop. With these two flags (one submitting thread; completion work deferred until *you* call `io_uring_enter`/`get_cqe`) the kernel never interrupts the hot thread — the io_uring analogue of routing IRQs away. `IORING_SETUP_COOP_TASKRUN` (5.19) is the weaker, multi-issuer form.
- **User-side CQ polling.** Instead of blocking in `io_uring_enter(GETEVENTS)` (which sleeps — a context switch, Ch. 48), **busy-poll the CQ ring** in user space (`io_uring_peek_cqe` in a loop) on your dedicated core — completions are visible as memory writes. Combined with SQPOLL, the whole loop is syscall-free (§59.3) — the Ch. 48 ideal applied to kernel I/O.
- **IOPOLL (`IORING_SETUP_IOPOLL`) — for storage.** The kernel busy-polls the (NVMe) device for completions instead of waiting for an interrupt — lower completion latency for `O_DIRECT` disk I/O (Ch. 98's journaling/capture). Completions are harvested only when a thread enters the kernel to poll (`io_uring_enter(GETEVENTS)`, or the SQPOLL thread if set) — there is no third "device poller" core. Trades CPU for latency, like all polling. The *network* analogue is **io_uring NAPI busy-poll** (`io_uring_register_napi()`, Linux 6.9): the ring's waiting thread polls the NAPI queues of its sockets instead of sleeping on the IRQ — §57.4.1's busy-poll, done by io_uring.
- **The polling trade (Ch. 48).** Polling = lowest latency, 100% CPU (the kernel poller, your CQ poller). Right for a dedicated I/O core on a latency path; wrong for a power/CPU-constrained or shared setup (use the default blocking mode, which is *still* fewer syscalls than `epoll`). Match the mode to whether you've dedicated cores to I/O.

### 59.4.3 Batching submissions

Even without polling, batching is io_uring's core syscall-reduction lever (the Ch. 42/57 batching idea, native to io_uring):

- **Submit many SQEs per `io_uring_enter`.** Fill multiple SQEs (a batch of reads/writes/sends) and submit them with *one* `io_uring_enter` — amortizing the syscall over the whole batch. At high I/O rates this is the primary win over `epoll` (§59.3). The SQ depth bounds the batch.
- **Reap many CQEs per check.** Process *all* available completions per CQ check (`io_uring_for_each_cqe`) before submitting/waiting again — amortizing the reap (Ch. 42's consumer batching). The CQ holds many completions; drain them.
- **Linked operations (`IOSQE_IO_LINK`).** Chain dependent operations (e.g. write-then-fsync, or accept-then-read) so the kernel runs them in order without a round-trip back to you between them — fewer submit/reap cycles for multi-step I/O.
- **Multishot operations.** `IORING_OP_*` multishot variants (multishot accept, multishot recv) post *multiple* completions from *one* submission (e.g. accept every incoming connection, or receive every datagram) — one SQE, many CQEs — drastically cutting submissions for accept/receive-heavy workloads (a market-data receive loop).
- **Combine with application batching (Ch. 42).** Having reaped a batch of completions (received messages), process them as a batch through the pipeline (Ch. 42) — amortizing syscall *and* per-message overhead, self-correcting under load. io_uring's batching composes with the Disruptor's.

## 59.5 Pitfalls & anti-patterns: completion-ordering assumptions

- **Assuming completion order = submission order (the classic io_uring bug).** Operations complete **out of order** — io_uring runs them asynchronously and concurrently, so CQEs arrive in *completion* order, not submission order. You **must** use the `user_data` cookie in each SQE to correlate its CQE back to the operation/context. Code that assumes "the first completion is for the first submission" corrupts state. Always tag with `user_data` and match.
- **Buffer/lifetime bugs (use-after-free across async I/O).** The buffer an operation reads into/writes from **must stay valid until the completion arrives** — the kernel uses it *asynchronously*. Freeing/reusing a buffer before its CQE is a use-after-free / data corruption (Ch. 29 §29.5, 72). Manage buffer lifetime by the completion (a per-op buffer owned until its CQE), or use registered/provided buffers with clear ownership. This is the io_uring analog of the coroutine-frame lifetime issue (Ch. 45).
- **SQPOLL kernel thread on a hot core.** The SQPOLL poller burns a full core; placing it (via `sq_thread_cpu`) on a *hot trading core* steals cycles from the trading thread (Ch. 48, 51). Pin it to a **housekeeping** core (Ch. 50), and tune `sq_thread_idle` so it sleeps when idle if you don't need constant submission.
- **CQ overflow.** If you submit faster than you reap, the CQ **overflows**: the kernel sets `IORING_SQ_CQ_OVERFLOW` in `sq_ring->flags` (liburing: `io_uring_cq_has_overflow()`) and bumps the `overflow` counter, and — on 5.5+ (`IORING_FEAT_NODROP`) — parks the extra CQEs in an overflow list that is flushed into the ring by the next `io_uring_enter`: a **latency cliff** (the completions are late and cost a syscall to recover), not a loss; pre-5.5 kernels simply dropped them. Size the CQ (`IORING_SETUP_CQSIZE`, typically ≥ 2× the SQ), reap promptly (drain per loop — §59.4.3), and treat the overflow flag as a bug in your loop, never as normal operation.
- **Polling mode's CPU cost ignored.** SQPOLL + IOPOLL + CQ-polling = two cores at 100%: the SQPOLL kernel thread (which also does the device polling in IOPOLL mode) and your reaping thread. On a CPU/power-constrained box this is wasteful; use polling only where you've *dedicated* cores to it (Ch. 48's busy-poll trade). Default blocking io_uring is still far fewer syscalls than `epoll` without the polling cost.
- **Kernel-version dependence / feature churn.** io_uring evolves fast (new opcodes, fixes, security patches) and early versions had bugs and security issues (some shops disabled it; since 6.6 `kernel.io_uring_disabled` lets an admin turn it off box-wide — check it first). Features require minimum kernel versions; check `io_uring_get_probe` (or the `IORING_FEAT_*` bits from `io_uring_queue_init_params`) rather than assuming. The versions that matter for this chapter:

  | kernel | feature |
  |---|---|
  | 5.1 | io_uring (SQ/CQ, `io_uring_enter`), SQPOLL, IOPOLL |
  | 5.5 | `IORING_FEAT_NODROP` — CQ overflow buffered, not dropped |
  | 5.7 | provided buffers (`IORING_OP_PROVIDE_BUFFERS`) |
  | 5.11 / 5.13 | SQPOLL without registered files (`IORING_FEAT_SQPOLL_NONFIXED`); SQPOLL no longer needs `CAP_SYS_NICE` |
  | 5.14 / 5.15 | `IORING_REGISTER_IOWQ_AFF` / `IORING_REGISTER_IOWQ_MAX_WORKERS` |
  | 5.19 | provided-buffer rings (`io_uring_setup_buf_ring`), `IORING_SETUP_COOP_TASKRUN` |
  | 6.0 | `IORING_OP_SEND_ZC` (Ch. 60), multishot `recv`, `IORING_SETUP_SINGLE_ISSUER` |
  | 6.1 | `IORING_SETUP_DEFER_TASKRUN` |
  | 6.6 | `kernel.io_uring_disabled` sysctl |
  | 6.9 | io_uring NAPI busy-poll (`io_uring_register_napi`), epoll `EPIOCSPARAMS` (Ch. 57) |
  | 6.15 | io_uring zero-copy receive (Ch. 60) |

  And track security advisories (io_uring has been a CVE source — Ch. 91). Don't assume an opcode exists on your kernel.
- **Treating io_uring as a bypass replacement.** io_uring still uses the **kernel network stack** — it's lower-syscall, not stack-free. For the ultra-hot tick-to-trade path needing sub-microsecond, **kernel bypass** (Ch. 74) is still the answer; io_uring is the high-rate, lower-syscall middle ground (§59.1). Match the tool to the latency tier.
- **Over-deep submission queues adding latency.** A very deep SQ batches more (throughput) but a submitted op may wait behind others (latency). For latency-critical ops, submit promptly (smaller batches / immediate submit) rather than maximizing batch depth — the latency/throughput knob (Ch. 1).

## 59.6 Exercises & checklist

**Exercises**

1. **See the syscalls collapse.** Build an echo client with `epoll` (Ch. 57) and with io_uring (`iouring_rtt.cpp`); run `perf stat -e raw_syscalls:sys_enter` (or `perf trace -s`; `strace -c` works but perturbs timing) on each at high message rate. Compare syscall counts (epoll: millions of read/write/epoll_wait; io_uring batched: far fewer). Then enable SQPOLL + CQ polling and confirm ~zero syscalls on the loop (§59.3).
2. **Latency tiers.** Measure round-trip latency with `iouring_rtt.cpp` against Ch. 57's server: blocking `epoll` vs spun `epoll` (Ch. 57) vs io_uring `submit_and_wait` vs io_uring SQPOLL+CQ-spin. Expect the batched-enter build to land *near* — not below — spun epoll (one request in flight amortizes nothing; it still sleeps), and SQPOLL+spin to be the only io_uring mode that beats it (§59.3). Then pipeline 32 requests and watch the batched build's *throughput* pull ahead. How close to bypass (Ch. 74) does SQPOLL get?
3. **Out-of-order completions.** Submit several reads on different fds with distinct `user_data`; confirm CQEs arrive in *completion* (not submission) order, and that `user_data` correctly correlates them. Then (deliberately) assume order and watch it break (§59.5).
4. **Registered buffers.** Run a fixed-buffer workload with and without `io_uring_register_buffers` (+ `_fixed` ops); measure the per-op overhead difference at high I/O rate (§59.4.1).
5. **Async journaling (ties Ch. 98).** Build an `O_DIRECT` + io_uring (IOPOLL) async writer that journals a stream to NVMe off the hot path; measure write throughput and confirm the hot path isn't blocked on I/O. Compare to synchronous `write` (§59.4.2, Ch. 98).

**Checklist — io_uring**

- [ ] io_uring is used for **high-rate async I/O and to cut syscalls** (async journaling — Ch. 98, bulk feeds, lower-syscall sockets) — the middle ground between `epoll` (Ch. 57) and **bypass** (Ch. 74, still the floor for ultra-hot network).
- [ ] **Submissions and completions are batched** (many SQEs per `io_uring_enter`, drain all CQEs per check — §59.4.3); multishot/linked ops used where they fit.
- [ ] On dedicated I/O cores, **SQPOLL + user-side CQ polling** make the hot loop ~**syscall-free** (Ch. 48); the **SQPOLL kernel thread is pinned to a housekeeping core** (Ch. 50), not a hot trading core, with `sq_thread_idle` tuned; **io-wq workers are pinned away and capped**, and the ring runs `SINGLE_ISSUER` + `DEFER_TASKRUN` so no task_work IPI lands on the hot thread.
- [ ] **Registered buffers/files** (and provided buffer rings) move per-op pin/lookup to setup (Ch. 28, 31); buffers are **pre-faulted/`mlock`'d**.
- [ ] **Completions are correlated by `user_data`** — I never assume completion order == submission order (§59.5).
- [ ] **Buffer lifetime** spans until the completion (no free/reuse before the CQE — no async UAF — Ch. 29 §29.5, 72).
- [ ] The **CQ is sized and reaped promptly** (no overflow); polling-mode **CPU cost** is justified by dedicated cores (else default blocking io_uring).
- [ ] **Kernel version / feature availability** (opcodes, multishot, registered) is checked (`io_uring_get_probe`), and io_uring **security advisories** (Ch. 91) are tracked.

## 59.7 References

- J. Axboe, the io_uring design documents ("Efficient IO with io_uring") and the `liburing` library/man pages — the authoritative reference for SQ/CQ, polling modes, registered resources, and opcodes (this whole chapter).
- The Linux man pages — `io_uring(7)`, `io_uring_setup(2)`, `io_uring_enter(2)`, `io_uring_register(2)` — the syscall interface (§59.2, §59.4).
- The kernel `Documentation/` on io_uring and the LWN io_uring article series — the model, evolution, and trade-offs (§59.1-§59.2).
- Glauber Costa / Seastar and the various io_uring-vs-epoll benchmark writeups — measured syscall/latency comparisons behind §59.3.
- The io_uring security advisories / CVE discussions — the security caveat of §59.5 (ties Ch. 91).

## 59.8 Additional Reading

- The `liburing` examples and high-performance io_uring patterns (multishot, buffer rings, zero-copy send) — practical techniques extending §59.4.
- Coroutine + io_uring integration writeups (ties Ch. 45) — completions as awaitables.
- Ch. 57 (*Linux Native I/O*) — the `epoll`/readiness baseline; Ch. 74 (*Kernel Bypass*) — the sub-microsecond floor io_uring approaches but doesn't reach; Ch. 98 (*Capture & Persistence*) — io_uring/`O_DIRECT` async journaling; Ch. 45 (*Coroutines*) — io_uring completions as `co_await`; Ch. 28–31 (*Memory/mmap*) — registered/pre-faulted buffers; Ch. 48 (*Context Switching*) — the syscalls io_uring removes.
- Ch. 57 §57.3 and Ch. 74 §74.3 — the epoll and bypass rows this chapter's table sits between; **Appendix E** — the null-syscall and kernel-stack RX rows that bound it.

---

*Next: Ch. 60 — Zero-Copy & the Modern Kernel Fast Path, removing the last per-byte cost from the kernel path without leaving it: `MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy send/receive, and devmem TCP — the NIC DMA'ing straight to and from your buffers.*
