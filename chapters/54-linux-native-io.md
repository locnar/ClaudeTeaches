# Part VIII — Kernel I/O, Sockets & Zero-Copy

# Chapter 54 — Linux Native I/O

> **Prerequisites:** Ch. 46 (syscalls & context switches — I/O is where they happen; blocking I/O sleeps), Ch. 1 (latency vs throughput), Ch. 3 (measuring round-trip), Ch. 6 (the box), Ch. 58-preview (socket tuning). The busy-poll vs block decision (Ch. 46.4.2) is central here.
>
> **Leads into:** Ch. 55 (io_uring — the completion-based successor), Ch. 57 (IPC), Ch. 58 (socket/TCP tuning), Ch. 63 (NIC offloads / busy-poll), Ch. 69–70 (XDP / kernel bypass — what you graduate to when the kernel stack is too slow). Opens **Part VIII**: this is the *baseline* kernel I/O path that the rest of the Part improves on or bypasses.

---

## 54.1 Why it matters: the baseline before bypass

Part VII built a quiet, warm, isolated place to *compute*; Part VIII is about getting market data *in* and orders *out* — the network I/O that bookends the tick-to-trade path. This chapter is the **baseline**: the standard Linux kernel networking stack — `epoll`, non-blocking sockets, the readiness model — which is what the *vast majority* of networked software uses and what you must understand before reaching for io_uring (Ch. 55) or kernel bypass (Ch. 70). It's important to be clear up front: **for the absolute lowest-latency tick-to-trade path, the kernel stack is too slow** — every packet traverses the driver, the network stack (IP/TCP/UDP processing), socket buffers, and a syscall boundary, costing *microseconds* per packet — which is why HFT graduates to kernel bypass (Ch. 70, Solarflare/Onload, DPDK). But the kernel stack is the foundation: it's where you start, what most of the system's *non-ultra-hot* I/O uses (control connections, drop copies, less-latency-critical feeds), and the model everything else builds on or escapes from.

The core concept this chapter teaches is **readiness-based, event-driven I/O** — the `epoll` model — and *why* it replaced thread-per-connection (Ch. 43). Instead of one thread blocking per socket (thousands of threads, constant context switching — Ch. 46), one thread uses `epoll` to wait on *many* sockets at once and is told *which* are ready, then does non-blocking reads/writes on those. This scales to tens of thousands of connections on one thread — the standard high-performance server architecture (nginx, Redis, every event loop). For a trading system it's the right tool for the *many-connection, I/O-bound* work (the same niche as coroutines — Ch. 43, which often wrap `epoll`): venue sessions, market-data subscriptions, control plane. The key mental distinction — **readiness** (`epoll` tells you "the socket is readable," then *you* call `read`) vs **completion** (io_uring/Windows IOCP: "your read of N bytes is *done*, here's the data") — defines this whole Part and the io_uring contrast of Ch. 55.

The latency reality, and the through-line to the rest of the book, is that **the kernel I/O path's cost is dominated by syscalls and the readiness round-trip** (Ch. 46): `epoll_wait` is a syscall, each `read`/`write` is a syscall, and waking from a blocked `epoll_wait` is a context switch + the cold restart (Ch. 46, 53). So the techniques here are about *minimizing* that cost within the kernel model — batching syscalls (handle all ready events per `epoll_wait`, write with `writev`/`sendmmsg`), edge-triggered mode to avoid redundant wakeups, and **busy-polling** (the kernel's NAPI busy-poll — `net.core.busy_poll` for `epoll_wait`, `SO_BUSY_POLL` for blocking receives — Ch. 46) to avoid the block/wake context switch on a latency path. These shrink the kernel stack's cost but can't eliminate it; that's what io_uring (Ch. 55, fewer syscalls) and bypass (Ch. 70, *no* kernel) are for. This chapter covers the readiness/completion and blocking/non-blocking mental model (§54.2), measures `epoll` round-trip latency (§54.3), details `epoll` event loops and syscall batching (§54.4), and warns about the classic traps — thundering herd and edge/level-triggered confusion (§54.5) — establishing the baseline the rest of Part VIII measures against.

## 54.2 Mental model: readiness vs completion; blocking vs non-blocking

**Blocking vs non-blocking — what a `read` does when there's no data:**

- **Blocking (default):** `read()` on a socket with no data **sleeps** the thread (a context switch — Ch. 46) until data arrives, then returns it. Simple, but one thread per socket and a switch per wait — the thread-per-connection model that doesn't scale (Ch. 43, 46).
- **Non-blocking (`O_NONBLOCK`):** `read()` returns *immediately* — with data if available, or `EAGAIN`/`EWOULDBLOCK` if not. The thread never sleeps in `read`; instead it asks an event mechanism (`epoll`) *which* sockets have data, then does non-blocking reads only on those. This is the basis of the event loop.

**Readiness vs completion — the defining I/O-model distinction:**

```
   READINESS model (epoll, select, poll — the kernel "readiness" notification):
     1. epoll_wait() → "socket 5 is READABLE"        (the kernel tells you it's ready)
     2. YOU call read(5, ...) → get the data          (you do the actual I/O, non-blocking)
     → two steps: notification THEN the syscall to move data; you manage the buffers

   COMPLETION model (io_uring — Ch.55, Windows IOCP):
     1. you SUBMIT "read N bytes from socket 5 into buffer B"
     2. completion → "your read is DONE, B has the data"  (the kernel did the I/O for you)
     → one round-trip; the kernel does the data movement; fewer syscalls (Ch.55)
```

This chapter is the **readiness** model (`epoll`); Ch. 55 is **completion** (io_uring). Readiness is the classic Linux model; completion is newer, with fewer syscalls and better for high I/O rates (Ch. 55).

**`epoll` — the scalable readiness mechanism:**

- **`epoll_create`** makes an epoll instance; **`epoll_ctl`** adds/removes/modifies the sockets (file descriptors) you're interested in and which events (readable/writable); **`epoll_wait`** blocks until one or more are ready, returning the *list* of ready fds. Unlike `select`/`poll` (which scan *all* fds every call — O(n)), `epoll` is **O(ready)** — it returns only the ready ones, scaling to tens of thousands of connections. This is why `epoll` (not `select`/`poll`) is the Linux event mechanism.
- **Level-triggered (LT, default) vs edge-triggered (ET):** **LT** — `epoll_wait` keeps reporting a socket as ready *as long as* there's data (even if you read only some); simpler, more forgiving. **ET** — reports ready only on the *transition* (new data arrived); you *must* drain the socket fully (read until `EAGAIN`) or you'll miss the rest until the next edge. ET means *fewer* `epoll_wait` wakeups (lower overhead) but requires careful draining (§54.5). For low latency, ET + full drain is common; LT is safer.

**The latency cost (Ch. 46) — why the kernel stack is the baseline, not the floor:**

```
   per packet through the kernel stack:  NIC IRQ → driver → IP/TCP processing → socket buffer
                                          → epoll_wait wakes (context switch — Ch.46) → read() syscall → copy to user
   → MICROSECONDS per packet, plus a context switch if epoll_wait was blocked.
   the techniques (§54.4): batch syscalls, edge-trigger, busy-poll (avoid the block/wake switch)
   the escapes (later): io_uring (fewer syscalls — Ch.55), kernel bypass (no kernel — Ch.70)
```

The model: **non-blocking sockets + `epoll` give event-driven, readiness-based I/O that scales to many connections on one thread (the alternative to thread-per-connection). `epoll` notifies you *which* sockets are ready (O(ready)); you do non-blocking reads/writes. The cost is syscalls + the readiness round-trip + a context switch when `epoll_wait` blocks — minimized by batching/edge-triggering/busy-polling, and ultimately escaped by completion-based I/O (io_uring — Ch. 55) or bypass (Ch. 70).**

## 54.3 Measure it: `epoll` round-trip latency

Measure the **round-trip latency** of the kernel TCP stack — send a small message, get a reply (a ping-pong / echo) — which captures the full path cost (syscalls, stack, context switches) that the kernel I/O baseline imposes. Compare **blocking** (thread-per-connection style), **`epoll` (blocking wait)**, and **busy-poll** (no block/wake switch).

```cpp
// epoll_rtt.cpp — TCP echo round-trip latency over loopback: blocking read vs epoll (blocking wait) vs spin.
// Build: g++ -O2 -std=c++20 epoll_rtt.cpp -o epoll_rtt
// Run:   ./epoll_rtt server &                      # echo server on 127.0.0.1:9000
//        taskset -c 2 ./epoll_rtt block            # client, blocking read()
//        taskset -c 2 ./epoll_rtt epoll            # client, epoll_wait(-1) then read()
//        taskset -c 2 ./epoll_rtt spin             # client, non-blocking read() spin (no sleep/wake)
//   Loopback isolates the SOFTWARE cost: no NIC, no wire (Ch. 63 for real RTTs). NAPI busy polling
//   (net.core.busy_poll / EPIOCSPARAMS — §54.4.1) is INERT over lo: packets arrive via the per-CPU
//   backlog, which carries no NAPI id, so the only way to avoid the sleep/wake here is to spin.
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cerrno>
#include <cstring>
#include <vector>
#include <algorithm>
#include <sys/socket.h>
#include <sys/epoll.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <x86intrin.h>

static constexpr int kPort = 9000;
static constexpr long kIters = 200'000;
static constexpr double kTscGHz = 2.9;             // Appendix L.3: 2.9 GHz, turbo off, invariant TSC (Ch. 17)

static void die(const char* what) { std::perror(what); std::exit(1); }
static void nodelay(int fd) { int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one); }  // Ch. 58 — mandatory
static sockaddr_in addr() { sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons(kPort); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK); return a; }

static int server() {                                // echo: read 8 bytes, write them back, forever
    int ls = socket(AF_INET, SOCK_STREAM, 0); int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in a = addr(); if (bind(ls, (sockaddr*)&a, sizeof a) < 0) die("bind"); listen(ls, 1);
    for (;;) {
        int fd = accept(ls, nullptr, nullptr); if (fd < 0) die("accept"); nodelay(fd);
        std::uint64_t v; while (read(fd, &v, 8) == 8) if (write(fd, &v, 8) != 8) break;
        close(fd);
    }
}

static int client(const char* mode) {
    int fd = socket(AF_INET, SOCK_STREAM, 0); sockaddr_in a = addr();
    if (connect(fd, (sockaddr*)&a, sizeof a) < 0) die("connect"); nodelay(fd);
    bool use_epoll = !std::strcmp(mode, "epoll"), spin = !std::strcmp(mode, "spin");
    int ep = -1;
    if (use_epoll) { ep = epoll_create1(0); epoll_event ev{EPOLLIN, {}}; ev.data.fd = fd; epoll_ctl(ep, EPOLL_CTL_ADD, fd, &ev); }
    if (spin) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);

    std::vector<std::uint32_t> samples(kIters);
    for (long i = -2000; i < kIters; ++i) {          // 2000 warm-up round trips (Ch. 53), then measured
        std::uint64_t req = i, rep = 0;
        std::uint64_t t0 = __rdtsc();
        if (write(fd, &req, 8) != 8) die("write");
        if (use_epoll) { epoll_event ev; while (epoll_wait(ep, &ev, 1, -1) != 1) {} }       // SLEEP until readable
        ssize_t n;
        do { n = read(fd, &rep, 8); } while (n < 0 && errno == EAGAIN);                       // spin: EAGAIN until data
        if (n != 8) die("read");
        std::uint64_t t1 = __rdtsc();
        if (i >= 0) samples[i] = std::uint32_t(t1 - t0);
    }
    std::sort(samples.begin(), samples.end());
    auto pct = [&](double p) { return samples[std::size_t(p * (kIters - 1))] / kTscGHz; };
    std::printf("%-6s loopback TCP echo RTT: p50=%.2f us  p99=%.2f us  p99.9=%.2f us\n", mode, pct(0.5)/1e3, pct(0.99)/1e3, pct(0.999)/1e3);
    return 0;
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "block";
    return !std::strcmp(mode, "server") ? server() : client(mode);
}
```

Representative results (toolchain and machine: Appendix L.3 — Linux 6.18, GCC 13; the kernel version is load-bearing here, epoll busy-poll behaviour changed across 5.x/6.x) — reference machine **Xeon Gold 6326** (Ice Lake-SP), loopback TCP, client pinned, server on another core, `TCP_NODELAY` on, turbo off (illustrative; **real NIC RTTs are higher** — Ch. 63 — loopback isolates the *software* cost). One request in flight at a time, so this is a latency ladder, not a throughput one:

```
   model (loopback TCP echo round-trip)             p50        p99        notes
   blocking read (thread-per-conn)                  ~5 µs      ~20 µs     blocks → context switch each way (Ch.46)
   epoll (blocking epoll_wait) + read               ~5.5 µs    ~21 µs     the same sleep/wake path plus one more syscall (Ch.46,53)
   non-blocking read() spin (no sleep/wake)         ~3 µs      ~7 µs      the spin removes the block/wake switch — NAPI busy-poll is INERT over lo
   (preview, ONE-WAY wire→app, Ch. 55) io_uring polled   ~2.5 µs  ~5 µs   fewer syscalls; a different quantity from the RTT rows above
   (preview, ONE-WAY wire→app, Ch. 70) kernel bypass     ~0.5-1 µs ~1-2 µs no kernel stack — the real HFT path (≈2 µs as a round trip)

   even spun/busy-polled, the kernel stack is MICROSECONDS — the floor is bypass (Ch.70).
```

Read it: the **blocking** and **blocking-`epoll`** models cost ~5-6 µs round-trip on *loopback* (no real network!) — dominated by the **syscalls** (`read`/`write`/`epoll_wait`) and the **context switch** each time `epoll_wait`/`read` sleeps and wakes (Ch. 46), plus the cold restart (Ch. 53); for one connection, `epoll_wait` + `read` is a strict superset of a blocking `read`, so it is slightly *slower*, not faster. **Spinning** (non-blocking reads in a loop on a dedicated core — Ch. 46) removes the block/wake switch and nearly halves the latency (~3 µs) — the biggest win available *within* the kernel model, and why a latency-sensitive `epoll` loop on a dedicated core polls rather than blocks. Over a *real NIC* the kernel's **NAPI busy-poll** (`net.core.busy_poll`/the epoll ioctl for `epoll_wait`, `SO_BUSY_POLL` on a blocking receive — §54.4.1) buys the same thing *and* pulls packets off the NIC queue before the interrupt fires; over loopback it is inert (packets arrive via the per-CPU backlog, which has no NAPI id), which is why this table spins instead — move the busy-poll comparison to a two-host run (Ch. 63 §63.3). But the headline lesson is the *floor*: **even perfectly busy-polled, the kernel TCP stack is microseconds** — the per-packet stack traversal (driver → IP/TCP → socket buffer → copy) is irreducible within the kernel. io_uring (Ch. 55) shaves the syscalls; **kernel bypass (Ch. 70) — removing the kernel from the path entirely — is what gets to ~1 µs and below**, which is why it's *the* HFT data path. The kernel native I/O of this chapter is the baseline you measure the rest of Part VIII against: optimize it (busy-poll, batch — §54.4) for the *non-ultra-hot* I/O, and bypass it (Ch. 70) for the tick-to-trade path. (And note `TCP_NODELAY` — Ch. 58 — is non-negotiable: without it, Nagle's algorithm batches small sends and adds *milliseconds*.)

## 54.4 Techniques

### 54.4.1 `epoll` event loops

The standard scalable readiness loop — and how to make it low-latency:

- **The structure.** `epoll_create1` → `epoll_ctl(ADD)` each non-blocking socket → loop: `epoll_wait` returns the ready fds → for each, do non-blocking `read`/`write` (handling `EAGAIN`) → process. One thread, many connections, O(ready). This is the nginx/Redis/event-loop architecture, and what coroutine runtimes (Ch. 43) wrap.
- **Edge-triggered + full drain (lower overhead).** Use ET (`EPOLLET`) to get *one* wakeup per data arrival (not repeated LT wakeups), and on each ready socket **read until `EAGAIN`** (drain it fully) — otherwise you miss data until the next edge (§54.5). ET reduces `epoll_wait` overhead but demands the drain discipline; LT is safer if you can't guarantee full draining.
- **Busy-poll on the latency path (Ch. 46).** For latency-sensitive sockets on a dedicated core, don't let `epoll_wait` *block* (that's a context switch — Ch. 46, §54.3). Options: a **zero-timeout `epoll_wait`** (or a non-blocking `read` loop) in a busy loop — which avoids the sleep/wake switch but does **not** busy-poll the NIC (`ep_poll()` returns on a zero timeout before ever reaching `ep_busy_loop()`, so packets still arrive through the IRQ → softirq → `ep_poll_callback` path, interrupt latency and jitter included) — or the kernel's **NAPI busy-poll**, in which the waiting thread itself pulls packets from the NIC queue (Ch. 63). For `epoll_wait` the latter means the global `net.core.busy_poll=<µs>` sysctl or, on Linux 6.9+, the per-instance `ioctl(epfd, EPIOCSPARAMS, &epoll_params{busy_poll_usecs, busy_poll_budget, prefer_busy_poll})`; the per-socket **`SO_BUSY_POLL`** option (default `net.core.busy_read`) governs only a *blocking* `recv` on that socket and is never consulted by `epoll_wait` (`fs/eventpoll.c`, `ep_busy_loop_on()`). Epoll busy-polls a single NAPI ID — the queue of the socket it most recently saw — so keep the latency sockets on one RX queue (Ch. 65). On a dedicated core, busy-polling is the right model (§46.4.2); block only on cold/background connections. The knobs, in one place:

  | knob | scope | what it does | typical |
  |---|---|---|---|
  | `net.core.busy_read` | sysctl, default for `SO_BUSY_POLL` | µs a *blocking* `recv`/`read` spins on the socket's NAPI queue before sleeping | 50–200 |
  | `net.core.busy_poll` | sysctl | µs `select`/`poll`/`epoll_wait` spin on the NAPI queue before sleeping (global) | 50–200 |
  | `SO_BUSY_POLL` / `SO_PREFER_BUSY_POLL` / `SO_BUSY_POLL_BUDGET` | per socket | per-socket override of `busy_read`; prefer polling over IRQ (defers the IRQ while polling succeeds); packets per poll | 50–200 / 1 / 8–64 |
  | `ioctl(epfd, EPIOCSPARAMS, &epoll_params{busy_poll_usecs, busy_poll_budget, prefer_busy_poll})` | per epoll instance (Linux 6.9+) | per-instance version of `net.core.busy_poll`, no global side effect | 50–200 µs |
  | `SO_INCOMING_NAPI_ID` | per socket (read) | the NAPI id a socket's packets arrive on — epoll busy-polls **one** NAPI id, so every latency socket in the set must share a queue (Ch. 65 flow steering) | — |
  | `/sys/class/net/<dev>/napi_defer_hard_irqs`, `gro_flush_timeout` | per device | keep the NIC in polled mode between packets (defer N IRQs, flush GRO after T ns) — the pair that makes `prefer_busy_poll` effective | 2 / 50000–200000 |

- **One event loop per thread, sharded.** For multiple latency threads, give each its own `epoll` instance and a *shard* of the connections (shared-nothing — Ch. 34), each thread pinned (Ch. 48). Don't share one `epoll` across threads if you can shard — sharing invites the thundering herd (§54.5) and contention.
- **Handle writability correctly.** Register `EPOLLOUT` only when a write would block (the socket buffer is full); a perpetually-registered `EPOLLOUT` floods you with ready events (the socket is *usually* writable). Add `EPOLLOUT` on `EAGAIN` from `write`, remove it once drained.

### 54.4.2 Syscall batching

Each syscall is a kernel crossing (~hundreds of ns — Ch. 46); **batching amortizes** the crossing over more work — the main kernel-model latency lever after busy-polling:

- **Process all ready events per `epoll_wait`.** `epoll_wait` returns *many* ready fds in one call; handle *all* of them before calling `epoll_wait` again — amortizing the `epoll_wait` syscall over the whole batch (the Disruptor batching idea — Ch. 40 — at the syscall level). Don't call `epoll_wait` after each single event.
- **Scatter/gather and multi-message syscalls.** `readv`/`writev` (scatter/gather — one syscall moves data to/from *multiple* buffers, e.g. header + body without a copy — Ch. 60), and **`recvmmsg`/`sendmmsg`** (receive/send *multiple* datagrams in one syscall — huge for UDP market-data feeds, Ch. 60: one syscall drains many packets). These cut the syscall count per message dramatically on high-rate feeds.
- **Batch the application work too.** Having received a batch of messages (via `recvmmsg` or draining an ET socket), process them as a batch through the pipeline (Ch. 40) — amortizing not just the syscall but the per-message overhead. The batch grows under load (a burst), self-correcting (Ch. 40.4.2).
- **Coalesce writes.** Buffer small outgoing messages and send them with one `writev`/`sendmmsg` where latency allows — fewer syscalls. (On the *order* path, latency usually trumps batching — send immediately; on bulk/non-critical paths, batch.)
- **The limit — batching is amortization, not elimination.** Batching reduces *syscalls per message* but doesn't remove the per-syscall cost or the stack traversal. It's the right optimization within the kernel model (and io_uring — Ch. 55 — batches *submissions* even better), but the kernel stack floor remains (§54.3) — bypass (Ch. 70) is the elimination.

## 54.5 Pitfalls & anti-patterns: thundering herd, edge/level confusion

- **Edge-triggered without full drain (the classic ET bug).** With `EPOLLET`, you get *one* wakeup per data arrival; if you don't **read until `EAGAIN`**, the un-read data sits there and `epoll_wait` *won't* tell you about it again until the *next* arrival — so you stall/lose throughput (and latency spikes when the rest finally comes). ET *requires* draining each ready socket fully. If you can't guarantee that, use LT (§54.4.1).
- **Thundering herd.** Several threads, each with its *own* `epoll` instance watching the same listening socket, all wake when *one* connection arrives → they contend, and all but one find nothing to do (wasted wakeups + a context-switch storm — Ch. 46). (Threads blocked on *one shared* epoll instance already get single-waiter wake-ups.) Use `EPOLLEXCLUSIVE` (Linux 4.5+; wakes *one or more* of the instances instead of all — not guaranteed to be exactly one), `SO_REUSEPORT` (each thread its own listening socket — the kernel load-balances), or shard connections per-thread-epoll (§54.4.1, Ch. 34). Don't share one epoll/accept across a thread pool naively.
- **Blocking on the latency path (Ch. 46).** Letting `epoll_wait`/`read` *block* on a latency-critical socket means a context switch + cold restart (Ch. 46, 53, §54.3) per message. **Busy-poll** on dedicated cores (zero-timeout `epoll_wait` / `net.core.busy_poll` — §54.4.1); block only on cold connections.
- **Forgetting `TCP_NODELAY` (Ch. 58).** Nagle's algorithm (on by default) *delays* small sends to coalesce them — adding **milliseconds** to a small order/request. **`TCP_NODELAY` is mandatory** on latency sockets (§54.3, Ch. 58). The single most common catastrophic networking latency bug.
- **`select`/`poll` instead of `epoll`.** `select`/`poll` scan *all* fds every call (O(n)) — and `select` additionally caps fds at `FD_SETSIZE` (1024) — so they don't scale and add per-call cost. Use `epoll` (O(ready)) on Linux (§54.2).
- **One syscall per event.** Calling `epoll_wait` after each single event, `read`-ing one message at a time, `write`-ing each small message separately — paying the syscall crossing per message instead of batching (§54.4.2). Process the whole ready batch; use `recvmmsg`/`writev`.
- **Perpetual `EPOLLOUT`.** Registering for writable events permanently floods the loop (the socket is usually writable) — burning CPU. Register `EPOLLOUT` only when a write blocked, deregister when drained (§54.4.1).
- **Ignoring partial reads/writes.** TCP is a *stream* — a `read` may return *part* of a message, a `write` may accept only *part*. Non-blocking I/O code must handle partial I/O (buffer and resume) — assuming whole-message reads is a classic bug. (Framing/length-prefixing — Ch. 60.)
- **Using the kernel stack for the ultra-hot path.** The kernel TCP/UDP stack is microseconds (§54.3); for the tick-to-trade path that needs sub-microsecond, the kernel model — even optimized — is too slow. That's what io_uring (Ch. 55) and **kernel bypass** (Ch. 70) are for. Don't try to optimize the kernel stack into being a bypass; know when to graduate.

## 54.6 Exercises & checklist

**Exercises**

1. **Round-trip, three ways.** Build `epoll_rtt.cpp`; measure loopback round-trip latency with `block`, `epoll` and `spin`. Confirm the spin roughly halves it (§54.3) and that blocking `epoll` is no faster than a blocking `read` for one connection. Add `perf stat -e context-switches` — which models switch? Then, on two hosts over a real NIC, add a fourth mode with `net.core.busy_poll=100` (or the `EPIOCSPARAMS` ioctl) and compare it with the spin — on the wire, NAPI busy-poll also beats the interrupt.
2. **`TCP_NODELAY`.** Nagle only withholds a small segment while earlier data is *unacknowledged*, so the strict one-in-flight ping-pong of `epoll_rtt.cpp` never triggers it — every ACK rides the reply. Change the client to send each request as *two* small `write()` calls (a 4-byte header, then a 4-byte body) with the server reading all 8 bytes before replying: now the second write waits for the ACK of the first, which the server delays (~40 ms, delayed ACK) because it has nothing to send yet — the classic write-write-read stall. Watch the RTT jump to ~40 ms without `TCP_NODELAY`; add it and confirm the fix. This is the chapter's mandatory knob (Ch. 58 has the mechanism).
3. **ET drain bug.** Build an ET `epoll` loop that reads only *once* per ready event (not draining); send a burst and watch messages get stuck until the next arrival. Fix with read-until-`EAGAIN`; confirm (§54.5).
4. **Batching.** Receive a high-rate UDP stream with `recv` (one syscall per packet) vs `recvmmsg` (many packets per syscall). Measure syscalls/sec (`strace -c`) and throughput/latency. Quantify the batching win (§54.4.2, ties Ch. 60).
5. **Thundering herd.** Have N threads share one listening `epoll` (naive) vs `SO_REUSEPORT` (per-thread). Measure wakeups and CPU under connection load; confirm the herd and its fix (§54.5).

**Checklist — Linux native I/O**

- [ ] Latency sockets have **`TCP_NODELAY`** (no Nagle — Ch. 58) — the non-negotiable knob; I verified it (§54.5).
- [ ] Many-connection I/O uses **non-blocking sockets + `epoll`** (O(ready)) — **not** `select`/`poll` or thread-per-connection (§54.2).
- [ ] Latency-critical sockets **busy-poll** on a dedicated core (zero-timeout `epoll_wait` / `net.core.busy_poll`; `SO_BUSY_POLL` only for blocking receives) — they don't **block** (no context switch — Ch. 46); cold/background connections block (§54.4.1).
- [ ] Edge-triggered loops **drain each ready socket to `EAGAIN`**; otherwise LT is used (no ET-without-drain bug — §54.5).
- [ ] Syscalls are **batched**: all ready events per `epoll_wait`, `recvmmsg`/`sendmmsg` for high-rate UDP (Ch. 60), `readv`/`writev` for scatter/gather, batched application processing (Ch. 40) (§54.4.2).
- [ ] **Thundering herd** is avoided (`SO_REUSEPORT`/`EPOLLEXCLUSIVE`/per-thread-epoll sharding — Ch. 34); `EPOLLOUT` is registered only when needed.
- [ ] **Partial reads/writes** are handled (TCP is a stream — buffer and resume; framing — Ch. 60).
- [ ] For the **ultra-hot tick-to-trade path**, I recognize the kernel stack is the **baseline, not the floor** — graduating to **io_uring** (Ch. 55) and **kernel bypass** (Ch. 70) where sub-microsecond is required.

## 54.7 References

- The Linux man pages — `epoll(7)`, `epoll_ctl(2)`/`epoll_wait(2)`, `socket(7)`, `tcp(7)` (`TCP_NODELAY`), `recvmmsg(2)`/`sendmmsg(2)`, `readv(2)`/`writev(2)`, `SO_BUSY_POLL`/`SO_REUSEPORT` — the mechanisms of this chapter.
- W. R. Stevens, *UNIX Network Programming, Vol. 1* — the definitive treatment of sockets, blocking/non-blocking, and the I/O models (readiness vs completion) of §54.2.
- The "C10K problem" writeups (Dan Kegel) and the nginx/libevent/libev architecture docs — event-driven readiness-based servers at scale (§54.4.1).
- The Linux kernel networking and `SO_BUSY_POLL`/NAPI busy-poll documentation — busy-polling the kernel stack for lower latency (§54.4.1, ties Ch. 63).
- Cloudflare / netdev talks on epoll, busy-polling, and kernel networking latency — practical measurements behind §54.3.

## 54.8 Additional Reading

- M. Kerrisk, *The Linux Programming Interface* — comprehensive `epoll`/sockets/non-blocking I/O coverage.
- The io_uring vs epoll comparison writeups (ties Ch. 55) — readiness vs completion, measured.
- Ch. 55 (*io_uring*) — the completion-based successor with fewer syscalls; Ch. 57 (*IPC*) — shared-memory alternatives to sockets; Ch. 58 (*Socket/TCP Tuning*) — `TCP_NODELAY`, buffers, congestion control; Ch. 60 (*Wire Decoding*) — `recvmmsg` and parsing the received data; Ch. 63 (*NIC*) — busy-poll/offloads; Ch. 70 (*Kernel Bypass*) — escaping the kernel stack; Ch. 43 (*Coroutines*) — the async layer over epoll.
- Ch. 56 (*Zero-Copy & the Modern Kernel Fast Path*) — removing the per-byte copy from this kernel path (`MSG_ZEROCOPY`, `TCP_ZEROCOPY_RECEIVE`, io_uring zero-copy) without full bypass.
- **Appendix E** — the kernel-stack (~5-15 µs) and kernel-bypass (~1-2 µs) NIC RX rows (the busy-polled kernel-stack figure is Ch. 63 §63.3's); **Appendix C** — busy-poll/`SO_BUSY_POLL` tuning.

---

*Next: Ch. 55 — io_uring Deep Dive, the completion-based evolution of Linux I/O: submission/completion queues that batch operations and slash syscalls, registered buffers, and polling mode — the kernel's answer to "fewer syscalls per I/O," and the middle ground between epoll and full bypass.*
