# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 47 — Linux Native I/O

> **Prerequisites:** Ch. 41 (syscalls & context switches — I/O is where they happen; blocking I/O sleeps), Ch. 1 (latency vs throughput), Ch. 3 (measuring round-trip), Ch. 6 (the box), Ch. 51-preview (socket tuning). The busy-poll vs block decision (Ch. 41.4.2) is central here.
>
> **Leads into:** Ch. 48 (io_uring — the completion-based successor), Ch. 50 (IPC), Ch. 51 (socket/TCP tuning), Ch. 55 (NIC offloads / busy-poll), Ch. 61–62 (XDP / kernel bypass — what you graduate to when the kernel stack is too slow). Opens **Part VIII**: this is the *baseline* kernel I/O path that the rest of the Part improves on or bypasses.

---

## 47.1 Why it matters: the baseline before bypass

Parts VII built a quiet, warm, isolated place to *compute*; Part VIII is about getting market data *in* and orders *out* — the network I/O that bookends the tick-to-trade path. This chapter is the **baseline**: the standard Linux kernel networking stack — `epoll`, non-blocking sockets, the readiness model — which is what the *vast majority* of networked software uses and what you must understand before reaching for io_uring (Ch. 48) or kernel bypass (Ch. 62). It's important to be clear up front: **for the absolute lowest-latency tick-to-trade path, the kernel stack is too slow** — every packet traverses the driver, the network stack (IP/TCP/UDP processing), socket buffers, and a syscall boundary, costing *microseconds* per packet — which is why HFT graduates to kernel bypass (Ch. 62, Solarflare/Onload, DPDK). But the kernel stack is the foundation: it's where you start, what most of the system's *non-ultra-hot* I/O uses (control connections, drop copies, less-latency-critical feeds), and the model everything else builds on or escapes from.

The core concept this chapter teaches is **readiness-based, event-driven I/O** — the `epoll` model — and *why* it replaced thread-per-connection (Ch. 38). Instead of one thread blocking per socket (thousands of threads, constant context switching — Ch. 41), one thread uses `epoll` to wait on *many* sockets at once and is told *which* are ready, then does non-blocking reads/writes on those. This scales to tens of thousands of connections on one thread — the standard high-performance server architecture (nginx, Redis, every event loop). For a trading system it's the right tool for the *many-connection, I/O-bound* work (the same niche as coroutines — Ch. 38, which often wrap `epoll`): venue sessions, market-data subscriptions, control plane. The key mental distinction — **readiness** (`epoll` tells you "the socket is readable," then *you* call `read`) vs **completion** (io_uring/Windows IOCP: "your read of N bytes is *done*, here's the data") — defines this whole Part and the io_uring contrast of Ch. 48.

The latency reality, and the through-line to the rest of the book, is that **the kernel I/O path's cost is dominated by syscalls and the readiness round-trip** (Ch. 41): `epoll_wait` is a syscall, each `read`/`write` is a syscall, and waking from a blocked `epoll_wait` is a context switch + the cold restart (Ch. 41, 46). So the techniques here are about *minimizing* that cost within the kernel model — batching syscalls (handle all ready events per `epoll_wait`, write with `writev`/`sendmmsg`), edge-triggered mode to avoid redundant wakeups, and **busy-polling** (`SO_BUSY_POLL` / the kernel's busy-poll — Ch. 41) to avoid the block/wake context switch on a latency path. These shrink the kernel stack's cost but can't eliminate it; that's what io_uring (Ch. 48, fewer syscalls) and bypass (Ch. 62, *no* kernel) are for. This chapter covers the readiness/completion and blocking/non-blocking mental model (§47.2), measures `epoll` round-trip latency (§47.3), details `epoll` event loops and syscall batching (§47.4), and warns about the classic traps — thundering herd and edge/level-triggered confusion (§47.5) — establishing the baseline the rest of Part VIII measures against.

## 47.2 Mental model: readiness vs completion; blocking vs non-blocking

**Blocking vs non-blocking — what a `read` does when there's no data:**

- **Blocking (default):** `read()` on a socket with no data **sleeps** the thread (a context switch — Ch. 41) until data arrives, then returns it. Simple, but one thread per socket and a switch per wait — the thread-per-connection model that doesn't scale (Ch. 38, 41).
- **Non-blocking (`O_NONBLOCK`):** `read()` returns *immediately* — with data if available, or `EAGAIN`/`EWOULDBLOCK` if not. The thread never sleeps in `read`; instead it asks an event mechanism (`epoll`) *which* sockets have data, then does non-blocking reads only on those. This is the basis of the event loop.

**Readiness vs completion — the defining I/O-model distinction:**

```
   READINESS model (epoll, select, poll — the kernel "readiness" notification):
     1. epoll_wait() → "socket 5 is READABLE"        (the kernel tells you it's ready)
     2. YOU call read(5, ...) → get the data          (you do the actual I/O, non-blocking)
     → two steps: notification THEN the syscall to move data; you manage the buffers

   COMPLETION model (io_uring — Ch.45, Windows IOCP):
     1. you SUBMIT "read N bytes from socket 5 into buffer B"
     2. completion → "your read is DONE, B has the data"  (the kernel did the I/O for you)
     → one round-trip; the kernel does the data movement; fewer syscalls (Ch.45)
```

This chapter is the **readiness** model (`epoll`); Ch. 48 is **completion** (io_uring). Readiness is the classic Linux model; completion is newer, with fewer syscalls and better for high I/O rates (Ch. 48).

**`epoll` — the scalable readiness mechanism:**

- **`epoll_create`** makes an epoll instance; **`epoll_ctl`** adds/removes/modifies the sockets (file descriptors) you're interested in and which events (readable/writable); **`epoll_wait`** blocks until one or more are ready, returning the *list* of ready fds. Unlike `select`/`poll` (which scan *all* fds every call — O(n)), `epoll` is **O(ready)** — it returns only the ready ones, scaling to tens of thousands of connections. This is why `epoll` (not `select`/`poll`) is the Linux event mechanism.
- **Level-triggered (LT, default) vs edge-triggered (ET):** **LT** — `epoll_wait` keeps reporting a socket as ready *as long as* there's data (even if you read only some); simpler, more forgiving. **ET** — reports ready only on the *transition* (new data arrived); you *must* drain the socket fully (read until `EAGAIN`) or you'll miss the rest until the next edge. ET means *fewer* `epoll_wait` wakeups (lower overhead) but requires careful draining (§47.5). For low latency, ET + full drain is common; LT is safer.

**The latency cost (Ch. 41) — why the kernel stack is the baseline, not the floor:**

```
   per packet through the kernel stack:  NIC IRQ → driver → IP/TCP processing → socket buffer
                                          → epoll_wait wakes (context switch — Ch.39) → read() syscall → copy to user
   → MICROSECONDS per packet, plus a context switch if epoll_wait was blocked.
   the techniques (§44.4): batch syscalls, edge-trigger, busy-poll (avoid the block/wake switch)
   the escapes (later): io_uring (fewer syscalls — Ch.45), kernel bypass (no kernel — Ch.52)
```

The model: **non-blocking sockets + `epoll` give event-driven, readiness-based I/O that scales to many connections on one thread (the alternative to thread-per-connection). `epoll` notifies you *which* sockets are ready (O(ready)); you do non-blocking reads/writes. The cost is syscalls + the readiness round-trip + a context switch when `epoll_wait` blocks — minimized by batching/edge-triggering/busy-polling, and ultimately escaped by completion-based I/O (io_uring — Ch. 48) or bypass (Ch. 62).**

## 47.3 Measure it: `epoll` round-trip latency

Measure the **round-trip latency** of the kernel TCP stack — send a small message, get a reply (a ping-pong / echo) — which captures the full path cost (syscalls, stack, context switches) that the kernel I/O baseline imposes. Compare **blocking** (thread-per-connection style), **`epoll` (blocking wait)**, and **busy-poll** (no block/wake switch).

```cpp
// epoll_rtt.cpp — TCP echo round-trip latency: blocking vs epoll vs busy-poll (sketch).
// Build: g++ -O2 -std=c++20 epoll_rtt.cpp -o epoll_rtt
// Run:  ./epoll_rtt server &   ;   taskset -c 2 ./epoll_rtt client 127.0.0.1
//   (over loopback here; real latency is over the NIC — Ch.49 — and far higher than loopback)
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include <algorithm>
#include <chrono>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <unistd.h>

// ... setup elided: socket(), TCP_NODELAY (Ch.47!), connect/accept ...
// Client measures: write(req); read(reply); record the round-trip; repeat.
// Server: read(req); write(reply) as fast as possible (echo).

int main(int argc, char** argv) {
    // (illustrative — the point is the measured numbers below; full socket setup omitted)
    // Key knobs that matter for the measurement:
    //   - setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, 1)   // disable Nagle (Ch.47) — critical!
    //   - O_NONBLOCK + epoll, vs blocking read, vs SO_BUSY_POLL
    // Client loop (warm — Ch.43 — then measured):
    //   for N iters: t0=rdtsc; write(fd,&req,8); read(fd,&rep,8); t1=rdtsc; samples.push_back(t1-t0);
    std::printf("see representative numbers below\n");
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), loopback TCP, pinned, `TCP_NODELAY` on, turbo off (illustrative; **real NIC RTTs are higher** — Ch. 55 — loopback isolates the *software* cost):

```
   model (loopback TCP echo round-trip)     p50        p99        notes
   blocking read (thread-per-conn)          ~6 µs      ~25 µs     blocks → context switch each way (Ch.39)
   epoll (blocking epoll_wait)              ~5 µs      ~20 µs     epoll_wait sleeps → switch + cold (Ch.39,43)
   epoll + SO_BUSY_POLL (no block)          ~3 µs      ~7 µs      busy-poll avoids the block/wake switch
   (preview) io_uring polled (Ch.45)        ~2.5 µs    ~5 µs      fewer syscalls
   (preview) kernel bypass (Ch.52)          ~1 µs      ~2 µs      no kernel stack — the real HFT path

   even busy-polled, the kernel stack is MICROSECONDS — the floor is bypass (Ch.52).
```

Read it: the **blocking** and **blocking-`epoll`** models cost ~5-6 µs round-trip on *loopback* (no real network!) — dominated by the **syscalls** (`read`/`write`/`epoll_wait`) and the **context switch** each time `epoll_wait`/`read` sleeps and wakes (Ch. 41), plus the cold restart (Ch. 46). **Busy-polling** (`SO_BUSY_POLL`, or busy-looping non-blocking reads on a dedicated core — Ch. 41) removes the block/wake switch and nearly halves the latency (~3 µs) — the biggest win available *within* the kernel model, and why a latency-sensitive `epoll` loop on a dedicated core busy-polls rather than blocks. But the headline lesson is the *floor*: **even perfectly busy-polled, the kernel TCP stack is microseconds** — the per-packet stack traversal (driver → IP/TCP → socket buffer → copy) is irreducible within the kernel. io_uring (Ch. 48) shaves the syscalls; **kernel bypass (Ch. 62) — removing the kernel from the path entirely — is what gets to ~1 µs and below**, which is why it's *the* HFT data path. The kernel native I/O of this chapter is the baseline you measure the rest of Part VIII against: optimize it (busy-poll, batch — §47.4) for the *non-ultra-hot* I/O, and bypass it (Ch. 62) for the tick-to-trade path. (And note `TCP_NODELAY` — Ch. 51 — is non-negotiable: without it, Nagle's algorithm batches small sends and adds *milliseconds*.)

## 47.4 Techniques

### 47.4.1 `epoll` event loops

The standard scalable readiness loop — and how to make it low-latency:

- **The structure.** `epoll_create1` → `epoll_ctl(ADD)` each non-blocking socket → loop: `epoll_wait` returns the ready fds → for each, do non-blocking `read`/`write` (handling `EAGAIN`) → process. One thread, many connections, O(ready). This is the nginx/Redis/event-loop architecture, and what coroutine runtimes (Ch. 38) wrap.
- **Edge-triggered + full drain (lower overhead).** Use ET (`EPOLLET`) to get *one* wakeup per data arrival (not repeated LT wakeups), and on each ready socket **read until `EAGAIN`** (drain it fully) — otherwise you miss data until the next edge (§47.5). ET reduces `epoll_wait` overhead but demands the drain discipline; LT is safer if you can't guarantee full draining.
- **Busy-poll on the latency path (Ch. 41).** For latency-sensitive sockets on a dedicated core, don't let `epoll_wait` *block* (that's a context switch — Ch. 41, §47.3). Options: a **zero-timeout `epoll_wait`** in a busy loop (poll, don't sleep), the kernel's **`SO_BUSY_POLL`** socket option (the kernel busy-polls the NIC for the socket before sleeping — Ch. 55), or `NAPI` busy-polling. On a dedicated core, busy-polling is the right model (Ch. 41.4.2); block only on cold/background connections.
- **One event loop per thread, sharded.** For multiple latency threads, give each its own `epoll` instance and a *shard* of the connections (shared-nothing — Ch. 31), each thread pinned (Ch. 42). Don't share one `epoll` across threads if you can sard — sharing invites the thundering herd (§47.5) and contention.
- **Handle writability correctly.** Register `EPOLLOUT` only when a write would block (the socket buffer is full); a perpetually-registered `EPOLLOUT` floods you with ready events (the socket is *usually* writable). Add `EPOLLOUT` on `EAGAIN` from `write`, remove it once drained.

### 47.4.2 Syscall batching

Each syscall is a kernel crossing (~hundreds of ns — Ch. 41); **batching amortizes** the crossing over more work — the main kernel-model latency lever after busy-polling:

- **Process all ready events per `epoll_wait`.** `epoll_wait` returns *many* ready fds in one call; handle *all* of them before calling `epoll_wait` again — amortizing the `epoll_wait` syscall over the whole batch (the Disruptor batching idea — Ch. 37 — at the syscall level). Don't call `epoll_wait` after each single event.
- **Scatter/gather and multi-message syscalls.** `readv`/`writev` (scatter/gather — one syscall moves data to/from *multiple* buffers, e.g. header + body without a copy — Ch. 53), and **`recvmmsg`/`sendmmsg`** (receive/send *multiple* datagrams in one syscall — huge for UDP market-data feeds, Ch. 53: one syscall drains many packets). These cut the syscall count per message dramatically on high-rate feeds.
- **Batch the application work too.** Having received a batch of messages (via `recvmmsg` or draining an ET socket), process them as a batch through the pipeline (Ch. 37) — amortizing not just the syscall but the per-message overhead. The batch grows under load (a burst), self-correcting (Ch. 37.4.2).
- **Coalesce writes.** Buffer small outgoing messages and send them with one `writev`/`sendmmsg` where latency allows — fewer syscalls. (On the *order* path, latency usually trumps batching — send immediately; on bulk/non-critical paths, batch.)
- **The limit — batching is amortization, not elimination.** Batching reduces *syscalls per message* but doesn't remove the per-syscall cost or the stack traversal. It's the right optimization within the kernel model (and io_uring — Ch. 48 — batches *submissions* even better), but the kernel stack floor remains (§47.3) — bypass (Ch. 62) is the elimination.

## 47.5 Pitfalls & anti-patterns: thundering herd, edge/level confusion

- **Edge-triggered without full drain (the classic ET bug).** With `EPOLLET`, you get *one* wakeup per data arrival; if you don't **read until `EAGAIN`**, the un-read data sits there and `epoll_wait` *won't* tell you about it again until the *next* arrival — so you stall/lose throughput (and latency spikes when the rest finally comes). ET *requires* draining each ready socket fully. If you can't guarantee that, use LT (§47.4.1).
- **Thundering herd.** Multiple threads blocked on the *same* `epoll`/listening socket all wake when *one* event arrives → they contend, and all but one find nothing to do (wasted wakeups + a context-switch storm — Ch. 41). Use `EPOLLEXCLUSIVE` (wake only one waiter), `SO_REUSEPORT` (each thread its own listening socket — the kernel load-balances), or shard connections per-thread-epoll (§47.4.1, Ch. 31). Don't share one epoll/accept across a thread pool naively.
- **Blocking on the latency path (Ch. 41).** Letting `epoll_wait`/`read` *block* on a latency-critical socket means a context switch + cold restart (Ch. 41, 46, §47.3) per message. **Busy-poll** on dedicated cores (zero-timeout `epoll_wait` / `SO_BUSY_POLL`); block only on cold connections.
- **Forgetting `TCP_NODELAY` (Ch. 51).** Nagle's algorithm (on by default) *delays* small sends to coalesce them — adding **milliseconds** to a small order/request. **`TCP_NODELAY` is mandatory** on latency sockets (§47.3, Ch. 51). The single most common catastrophic networking latency bug.
- **`select`/`poll` instead of `epoll`.** `select`/`poll` scan *all* fds every call (O(n)) and have fd limits — they don't scale and add per-call cost. Use `epoll` (O(ready)) on Linux (§47.2).
- **One syscall per event.** Calling `epoll_wait` after each single event, `read`-ing one message at a time, `write`-ing each small message separately — paying the syscall crossing per message instead of batching (§47.4.2). Process the whole ready batch; use `recvmmsg`/`writev`.
- **Perpetual `EPOLLOUT`.** Registering for writable events permanently floods the loop (the socket is usually writable) — burning CPU. Register `EPOLLOUT` only when a write blocked, deregister when drained (§47.4.1).
- **Ignoring partial reads/writes.** TCP is a *stream* — a `read` may return *part* of a message, a `write` may accept only *part*. Non-blocking I/O code must handle partial I/O (buffer and resume) — assuming whole-message reads is a classic bug. (Framing/length-prefixing — Ch. 53.)
- **Using the kernel stack for the ultra-hot path.** The kernel TCP/UDP stack is microseconds (§47.3); for the tick-to-trade path that needs sub-microsecond, the kernel model — even optimized — is too slow. That's what io_uring (Ch. 48) and **kernel bypass** (Ch. 62) are for. Don't try to optimize the kernel stack into being a bypass; know when to graduate.

## 47.6 Exercises & checklist

**Exercises**

1. **Round-trip, three ways.** Build a TCP echo client/server; measure round-trip latency (loopback) with (a) blocking, (b) `epoll` blocking, (c) `epoll` busy-poll (`SO_BUSY_POLL` or zero-timeout). Confirm busy-poll roughly halves it (§47.3). Add `perf stat -e context-switches` — which models switch?
2. **`TCP_NODELAY`.** Run the round-trip *without* `TCP_NODELAY` and watch the latency explode (Nagle — milliseconds for small messages). Add it; confirm the fix. This is the §47.5 mandatory knob.
3. **ET drain bug.** Build an ET `epoll` loop that reads only *once* per ready event (not draining); send a burst and watch messages get stuck until the next arrival. Fix with read-until-`EAGAIN`; confirm (§47.5).
4. **Batching.** Receive a high-rate UDP stream with `recv` (one syscall per packet) vs `recvmmsg` (many packets per syscall). Measure syscalls/sec (`strace -c`) and throughput/latency. Quantify the batching win (§47.4.2, ties Ch. 53).
5. **Thundering herd.** Have N threads share one listening `epoll` (naive) vs `SO_REUSEPORT` (per-thread). Measure wakeups and CPU under connection load; confirm the herd and its fix (§47.5).

**Checklist — Linux native I/O**

- [ ] Latency sockets have **`TCP_NODELAY`** (no Nagle — Ch. 51) — the non-negotiable knob; I verified it (§47.5).
- [ ] Many-connection I/O uses **non-blocking sockets + `epoll`** (O(ready)) — **not** `select`/`poll` or thread-per-connection (§47.2).
- [ ] Latency-critical sockets **busy-poll** on a dedicated core (zero-timeout `epoll_wait` / `SO_BUSY_POLL`) — they don't **block** (no context switch — Ch. 41); cold/background connections block (§47.4.1).
- [ ] Edge-triggered loops **drain each ready socket to `EAGAIN`**; otherwise LT is used (no ET-without-drain bug — §47.5).
- [ ] Syscalls are **batched**: all ready events per `epoll_wait`, `recvmmsg`/`sendmmsg` for high-rate UDP (Ch. 53), `readv`/`writev` for scatter/gather, batched application processing (Ch. 37) (§47.4.2).
- [ ] **Thundering herd** is avoided (`SO_REUSEPORT`/`EPOLLEXCLUSIVE`/per-thread-epoll sharding — Ch. 31); `EPOLLOUT` is registered only when needed.
- [ ] **Partial reads/writes** are handled (TCP is a stream — buffer and resume; framing — Ch. 53).
- [ ] For the **ultra-hot tick-to-trade path**, I recognize the kernel stack is the **baseline, not the floor** — graduating to **io_uring** (Ch. 48) and **kernel bypass** (Ch. 62) where sub-microsecond is required.

## 47.7 References

- The Linux man pages — `epoll(7)`, `epoll_ctl(2)`/`epoll_wait(2)`, `socket(7)`, `tcp(7)` (`TCP_NODELAY`), `recvmmsg(2)`/`sendmmsg(2)`, `readv(2)`/`writev(2)`, `SO_BUSY_POLL`/`SO_REUSEPORT` — the mechanisms of this chapter.
- W. R. Stevens, *UNIX Network Programming, Vol. 1* — the definitive treatment of sockets, blocking/non-blocking, and the I/O models (readiness vs completion) of §47.2.
- The "C10K problem" writeups (Dan Kegel) and the nginx/libevent/libev architecture docs — event-driven readiness-based servers at scale (§47.4.1).
- The Linux kernel networking and `SO_BUSY_POLL`/NAPI busy-poll documentation — busy-polling the kernel stack for lower latency (§47.4.1, ties Ch. 55).
- Cloudflare / netdev talks on epoll, busy-polling, and kernel networking latency — practical measurements behind §47.3.

## 47.8 Additional Reading

- M. Kerrisk, *The Linux Programming Interface* — comprehensive `epoll`/sockets/non-blocking I/O coverage.
- The io_uring vs epoll comparison writeups (ties Ch. 48) — readiness vs completion, measured.
- Ch. 48 (*io_uring*) — the completion-based successor with fewer syscalls; Ch. 50 (*IPC*) — shared-memory alternatives to sockets; Ch. 51 (*Socket/TCP Tuning*) — `TCP_NODELAY`, buffers, congestion control; Ch. 53 (*Wire Decoding*) — `recvmmsg` and parsing the received data; Ch. 55 (*NIC*) — busy-poll/offloads; Ch. 62 (*Kernel Bypass*) — escaping the kernel stack; Ch. 38 (*Coroutines*) — the async layer over epoll.
- Ch. 49 (*Zero-Copy & the Modern Kernel Fast Path*) — removing the per-byte copy from this kernel path (`MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy) without full bypass.
- **Appendix E** — kernel-stack vs busy-poll vs bypass RX latency numbers; **Appendix C** — busy-poll/`SO_BUSY_POLL` tuning.

---

*Next: Ch. 48 — io_uring Deep Dive, the completion-based evolution of Linux I/O: submission/completion queues that batch operations and slash syscalls, registered buffers, and polling mode — the kernel's answer to "fewer syscalls per I/O," and the middle ground between epoll and full bypass.*
