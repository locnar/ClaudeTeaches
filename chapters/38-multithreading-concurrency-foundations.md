# Part VI — Concurrency

# Chapter 38 — Multithreading & Concurrency Foundations

> **Prerequisites:** Ch. 37 (atomics & the memory model — the cost of sharing this chapter designs *around*), Ch. 8 & 33-preview (cache coherence / false sharing — the physical cost of contention), Ch. 17 (NUMA — cross-socket sharing), Ch. 1 (tail latency — contention spikes the tail), Ch. 3 (measuring scalability curves).
>
> **Leads into:** Ch. 39 (spinlocks/contention control — when you *must* share), Ch. 40 (false sharing), Ch. 41–44 (lock-free structures, seqlocks, Disruptor — the shared-nothing-friendly communication primitives), Ch. 50–58 (scheduling/pinning — the OS side of keeping threads on cores), Ch. 95 (process topology — shared-nothing across processes).

---

## 38.1 Why it matters: scaling without contention

The naive mental model of multithreading is "more threads = more throughput." For latency-critical systems this model is not just wrong, it's *backwards*: adding threads that **share mutable state** can make a system *slower* and *more jittery*, because the cost of coordinating shared data — cache-line bouncing (Ch. 8, 40), lock contention (Ch. 39), atomic barriers (Ch. 37) — grows faster than the work the extra threads do. Amdahl's law (the serial fraction caps speedup) is the optimistic version; the pessimistic reality is the **Universal Scalability Law**, which adds a *coherence/crosstalk* term that makes throughput *peak and then decline* as you add cores past the point where contention dominates. A poorly-designed concurrent system has a scalability curve that goes up, flattens, and comes *back down*.

For HFT the lesson is sharper still, because the goal is **latency**, not aggregate throughput, and contention attacks latency's tail (Ch. 1) most viciously. A contended cache line or lock doesn't just slow the average — it injects unpredictable stalls — tens to a hundred ns per HITM coherence transfer (Ch. 8), ~150 ns cross-socket (the NUMA cliff of Ch. 17), and microseconds when a lock wait falls into the kernel (Ch. 39, 50) — at exactly the moments of high activity (market open, a burst) when you most need determinism. So the foundational concurrency decision for a trading system isn't "how many threads" but **"how do I structure the work so threads barely interact at all"** — because the fastest coordination is no coordination.

That structuring principle is **shared-nothing**: partition the work so each thread (or process — Ch. 95) owns its data outright and communicates with others only through explicit, bounded, lock-free channels (Ch. 41, 44) rather than shared mutable state. A feed handler per symbol-range, a strategy per book, an order gateway per venue — each pinned to its own core (Ch. 52), each touching its own cache lines, each NUMA-local (Ch. 17), exchanging messages over ring buffers rather than sharing a hash map under a lock. This turns the concurrency problem from "make sharing fast" (hard, contention-bound, jittery) into "make sharing rare" (the design that actually scales and stays latency-flat). This chapter builds the mental model of contention and scalability limits (§38.2), measures the scalability curve that reveals where a design falls off (§38.3), and lays out shared-nothing partitioning and shared-state minimization (§38.4) — the architectural foundation that makes the atomics of Ch. 37 and the lock-free structures of Ch. 41–44 *occasional* rather than *central*.

## 38.2 Mental model: contention, scalability limits, shared-nothing

**Why sharing doesn't scale — the physical cost.** When two threads on different cores access the same cache line and at least one writes, the line must move between the cores' caches (Ch. 8's MESI: the writer takes exclusive ownership, invalidating the other's copy; the reader pulls it back — a **HITM** transfer, ~tens of ns, and across sockets the NUMA cliff of ~150 ns, Ch. 17). The more cores contend for a line, the more it ping-pongs, and the *coherence traffic* — not the useful work — comes to dominate. A lock is the same story plus serialization: only one thread holds it, the rest wait, and the lock's own cache line bounces among the waiters (Ch. 39). Contention is fundamentally a *physics* problem (data moving between cores), and you can't optimize your way out of it at the instruction level — you have to *not contend*.

**The scalability laws.** Two models frame how concurrent systems scale:

- **Amdahl's Law** — if a fraction *s* of the work is inherently serial, speedup is capped at `1/s` no matter how many cores. The optimistic bound: even a small serial section limits you.
- **Universal Scalability Law (USL)** — adds a **coherence** term *κ* (crosstalk/contention cost that grows with the *square* of concurrency): throughput `= N / (1 + α(N-1) + κN(N-1))`. The α term (contention/serialization) flattens the curve; the κ term (coherence) makes it **peak and then decline** — past some core count, *adding threads reduces throughput*. Real contended systems exhibit exactly this retrograde curve (§38.3).

```
   throughput
     │            ___________  ideal (linear)
     │          /
     │        /  ____________  Amdahl (caps at 1/s)
     │      / _/
     │    /_/        ‾‾‾‾----___   USL (peaks, then DECLINES — coherence cost)
     │  //                       ‾‾‾---___
     └──┴──┴──┴──┴──┴──┴──┴──► cores
        the peak is where contention starts costing more than the added core gives
```

**Shared-nothing — the design that beats the laws.** The way to keep α and κ near zero is to eliminate sharing: **each thread owns its data; threads communicate only through explicit message channels** (lock-free queues — Ch. 41, ring buffers — Ch. 44), never through shared mutable state. With nothing shared, there's no coherence traffic, no lock, no contention — each thread runs as if single-threaded on its own data, and the system scales linearly (limited only by the channel bandwidth and the genuinely-serial handoffs). This is the architecture of the fastest trading systems and the LMAX Disruptor (Ch. 44): a pipeline of single-threaded stages, each pinned to a core, passing messages one-way. The trading-domain version is **sharding by symbol/venue** (Ch. 17) — partition the instruments so each shard's feed/book/strategy is independent.

**The corollary: design for *one writer*.** Most coordination cost comes from *multiple writers* to the same data. A **single-writer** discipline — one thread/process owns each piece of mutable state and is the only one that writes it; others read via lock-free publication (seqlock — Ch. 42, or a ring) — eliminates write-write contention entirely and makes the memory model tractable (Ch. 37). Single-writer-per-datum is the practical heart of shared-nothing (and reappears in shared-memory IPC — Ch. 33, 95).

The model: **sharing mutable state across cores costs coherence traffic that grows super-linearly (USL) and spikes the tail; the fix isn't faster sharing but *less* sharing — partition into shared-nothing, single-writer-owned data communicating through explicit lock-free channels, so threads barely interact and the system scales and stays latency-flat.**

## 38.3 Measure it: scalability curve vs thread count

The diagnostic for a concurrency design is its **scalability curve**: throughput (and latency) as a function of thread count. A shared-nothing design scales ~linearly; a contended design peaks and declines (the USL retrograde curve). Measure both designs doing the *same* work — increment a counter N times total, split across M threads — sharing one atomic vs each thread owning a padded counter.

```cpp
// scale.cpp — shared atomic vs per-thread (shared-nothing) counters; sweep thread count.
// Build: g++ -O2 -std=c++20 -march=native scale.cpp -o scale -pthread
// Run:  ./scale shared 1 | ./scale shared 8 | ./scale sharded 8   (each thread pins itself to core t)
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>
#include <new>
#include <pthread.h>
#include <sched.h>

#ifdef __cpp_lib_hardware_interference_size            // GCC 12+/libstdc++; Clang 19+ (Appendix H)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif
struct alignas(kLine) Padded {                          // own cache line (Ch. 40)
    std::atomic<std::uint64_t> v{0};
};

static void pin_to(int core) {                           // Ch. 52
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(core, &s);
    if (pthread_setaffinity_np(pthread_self(), sizeof s, &s) != 0) std::perror("pthread_setaffinity_np");
}

int main(int argc, char** argv) {
    bool sharded = (argc > 1) && std::strcmp(argv[1], "sharded") == 0;
    int M = argc > 2 ? std::atoi(argv[2]) : 1;
    constexpr long TOTAL = 800'000'000;
    long per = TOTAL / M;

    std::atomic<std::uint64_t> shared{0};
    std::vector<Padded> local(M);                          // one padded counter per thread

    // Both designs do the SAME per-op work — one relaxed fetch_add (a locked RMW in memory) — and
    // differ only in whether the target line is shared. (A plain `s++` on a local would be folded
    // by the compiler into one store of `per`, measuring nothing.)
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::thread> ts;
    for (int t = 0; t < M; ++t) ts.emplace_back([&, t]{
        pin_to(t);
        std::atomic<std::uint64_t>& ctr = sharded ? local[t].v : shared;    // own line vs the one line
        for (long i = 0; i < per; ++i) ctr.fetch_add(1, std::memory_order_relaxed);
    });
    for (auto& th : ts) th.join();
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-8s M=%d  total-throughput %.1f Mops/s  (%.2f ns/op)\n",
                sharded ? "sharded" : "shared", M, TOTAL / (ns/1e3), ns/TOTAL);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, single socket, 2.9 GHz), GCC 13 `-O2 -std=c++20 -march=native`, Linux 6.18, each thread pinned to its own core by the listing, turbo off (illustrative; the *curve shape* is the point). Both columns execute the identical `lock xadd` per operation — an earlier draft's sharded loop incremented a register and was folded by the compiler into one store, so its column measured thread start-up:

```
   M (cores)   shared atomic (one line)      sharded (per-thread padded line)
   1           ~160 Mops/s  (~6 ns/op)        ~160 Mops/s     <- identical at M=1: one uncontended locked RMW (~18 cycles)
   2           ~50 Mops/s   (line bounces)    ~320 Mops/s     <- shared: WORSE than M=1
   4           ~30 Mops/s                     ~640 Mops/s     <- shared declines; sharded ~linear
   8           ~20 Mops/s   (retrograde!)     ~1280 Mops/s    <- shared: 8x SLOWER than M=1; sharded 8x FASTER
```

Read it via the USL: the **shared** design exhibits the retrograde curve — throughput *peaks at one thread and declines* as cores are added, because every `fetch_add` fights for the one cache line and the coherence traffic (HITM — Ch. 8) swamps the work; eight cores deliver *less* than one. The **sharded** (shared-nothing) design scales ~linearly — each thread hits only its own padded cache line (Ch. 40), no coherence traffic, so 8 cores do ~8× the work. *Same instruction, same total operations, opposite scaling*, purely from whether the line is shared. This is the foundational measurement of Part VI: **a concurrency design's quality is its scalability curve, and contention turns "more cores" into "less throughput."** The fix is structural (don't share — §38.4), not a faster atomic. (And note the latency story behind it: the shared version's per-op cost isn't just higher, it's *variable* — the line-bounce time depends on contention, spiking the tail, Ch. 1.)

## 38.4 Techniques

### 38.4.1 Shared-nothing partitioning

The primary technique: structure the system so each thread owns its data and rarely touches another's.

- **Shard by a natural key.** Partition the workload so each shard is independent and assign one thread/core per shard. In trading: **shard by symbol or venue** — feed handler, order book, and strategy for symbols A-M on core 0 (NUMA node 0 — Ch. 17), N-Z on core 1, etc. Each tick is processed entirely within one shard; no cross-shard sharing on the hot path. (Choose the shard key so load is balanced and cross-shard interactions — e.g. a strategy trading correlated symbols — are rare or handled off the hot path.)
- **Pipeline of single-threaded stages.** Decompose by *stage* rather than (or as well as) by data: feed-decode → book-build → strategy → risk → order-out, each a single-threaded stage pinned to a core, passing messages one-way through lock-free queues (Ch. 41) or a Disruptor ring (Ch. 44). Each stage is single-threaded (no internal locking), and the only "sharing" is the bounded handoff channel. This is the LMAX/mechanical-sympathy architecture.
- **Communicate through explicit channels, not shared state.** The cardinal rule: threads exchange *messages* over lock-free SPSC/MPSC queues or ring buffers (Ch. 41, 44), never reach into each other's data structures. A message handoff touches one producer line and one consumer line (and you can batch to amortize even that); a shared hash map under a lock touches everything. Make the channels the *only* contention point, and make them efficient (Ch. 41).
- **Pin everything (Ch. 52) and go NUMA-local (Ch. 17).** Shared-nothing only holds if threads stay put: pin each shard/stage thread to its core so its data stays cache-warm and its first-touched memory stays NUMA-local. Affinity + partitioning are a pair (as placement + pinning were in Ch. 17).
- **Thread lifecycle is setup, not hot path.** Threads are created once, at start-up, and live for the session: creating one costs ~15–30 µs (`clone`, stack `mmap`, scheduler placement) and the first touches of its stack and data are page faults (Ch. 29) — never spawn on a tick. Use the C++20 primitives for the *control* side only: `std::jthread`/`std::stop_token` (libstdc++ 10+, libc++ 20+; `__cpp_lib_jthread`) for housekeeping threads that must shut down cleanly, `std::latch`/`std::barrier` (libstdc++ 11+, libc++ 11+) to release all pinned stages together once each has pinned, pre-faulted and warmed (Ch. 58), and `std::thread::hardware_concurrency()` only as a sanity check — the core budget comes from the topology (`lscpu`, Ch. 52), not from a count. On the hot path a stage thread never blocks on, joins, or signals another thread; it spins on its channel (Ch. 39, 41). Toolchain availability: Appendix H.
- **Replicate read-mostly data per thread/node (Ch. 17).** Reference data many threads read (symbol tables, config, risk limits) is *copied* per thread/node so reads are local and never contend, updated via lock-free publication (seqlock/RCU — Ch. 42–43, hot-reload — Ch. 94). Read-mostly sharing becomes no sharing.

### 38.4.2 Minimizing shared mutable state

Where some sharing is unavoidable, shrink and shape it to minimize contention:

- **Single-writer per datum.** Make exactly one thread the writer of each shared piece; others only read (via lock-free publication — Ch. 42). Single-writer eliminates write-write contention (the worst kind) and makes the memory ordering tractable (Ch. 37). This is the most important shaping rule when you can't fully shard.
- **Per-thread state aggregated off the hot path.** Counters, statistics, accumulators: give each thread its *own* (padded — Ch. 40) copy that it updates with no synchronization, and aggregate them lazily off the hot path (a monitoring thread sums them periodically). The §38.3 sharded counter — turns a contended hot atomic into contention-free local updates. Applies to metrics, P&L accumulation, histograms (Ch. 3) — Ch. 92 builds the production version: per-thread owned lines in a shared-memory segment, merged by an off-core scraper.
- **Pad shared data to cache lines (Ch. 40).** When data *must* be shared, ensure independent items don't share a cache line (false sharing — Ch. 40): `alignas(hardware_destructive_interference_size)`. A queue's head and tail, two unrelated atomics — separate lines so they don't bounce together.
- **Batch to amortize coordination.** If a handoff/lock is unavoidable, do it less often: batch N items per queue push, per lock acquisition, per atomic update (Ch. 44's batching). Amortizing the coordination cost over many items shrinks its per-item impact — often turning a contention bottleneck into a non-issue.
- **Prefer immutable / copy-on-write for shared reads.** Shared data that's *read* by many and *changed* rarely can be published as immutable snapshots (atomic pointer swap to a new version, old version reclaimed safely — Ch. 43, 94), so readers never lock and never contend with the writer (seqlock — Ch. 42 for small data, RCU — Ch. 43 / pointer-swap for larger).
- **Make the shared channel efficient, not the shared data structure.** The right place for shared mutable state is a *purpose-built lock-free channel* (SPSC/MPSC queue, ring — Ch. 41, 44) designed for exactly one contention pattern, not a general-purpose container under a lock. Concentrate sharing into the channel and engineer that channel well.

## 38.5 Pitfalls & anti-patterns: false scaling, oversubscription

- **Assuming more threads = more throughput.** The foundational error: adding threads that share mutable state can *reduce* throughput (the USL retrograde curve — §38.3) and *spike latency*. Measure the scalability curve; design for shared-nothing, not thread count.
- **A shared container under a lock as the "concurrent data structure."** A `std::unordered_map` (Ch. 31) behind a `std::mutex` shared by all threads is the canonical contention bottleneck — every access serializes and bounces the lock line. Shard it (per-thread maps), make it single-writer + lock-free-read, or use a purpose-built channel (§38.4). 
- **Oversubscription (more threads than cores).** Running more busy threads than physical cores forces the scheduler to context-switch them (Ch. 50), destroying cache warmth and adding jitter — catastrophic for a latency system. Pin a bounded set of threads to dedicated cores (Ch. 52, 55); don't spawn a thread pool larger than the core budget on the hot path.
- **False sharing masquerading as necessary contention (Ch. 40).** Two *logically independent* variables on the same cache line contend invisibly — looks like a concurrency cost, is actually a layout bug. Pad to cache lines; suspect false sharing when "independent" per-thread work doesn't scale (§38.3, Ch. 40).
- **"It scaled on my 4-core laptop."** Contention costs grow with core count and are worse across NUMA nodes (Ch. 17); a design that looks fine on a few cores can fall off a cliff on a 32-core dual-socket server. Test at the real core count and topology (§38.3).
- **Sharing read-mostly reference data by reference under a lock.** Locking to read config/symbol data that changes rarely serializes all readers for no reason. Replicate per thread/node or publish lock-free (seqlock/RCU — Ch. 42–43); reads should never lock.
- **Ignoring the tail (Ch. 1).** A design can have acceptable *average* throughput while contention spikes the *tail* (the line-bounce time is variable). For a latency system the tail is the product; measure the latency distribution under concurrency, not just throughput.
- **Multiple writers where single-writer would do.** Allowing many threads to write the same datum (when one owner could) creates the worst contention and the hardest memory-ordering reasoning (Ch. 37). Default to single-writer-per-datum; it's simpler *and* faster.
- **Threads for work that's inherently serial.** Parallelizing a fundamentally serial dependency chain (each step needs the previous result) just adds coordination cost for no parallelism (Amdahl). Some hot paths (the single-message tick-to-trade path) are best *single-threaded* on a pinned core — don't parallelize what can't parallelize.

## 38.6 Exercises & checklist

**Exercises**

1. **Draw the curve.** Build `scale.cpp` (pin threads to distinct cores); run `shared` and `sharded` for M=1,2,4,8,16. Plot total throughput vs cores for each. Confirm `shared` is retrograde (peaks at 1, declines) and `sharded` is ~linear (§38.3). Where's the `shared` peak?
2. **NUMA makes it worse.** Run the `shared` version with threads on *one* socket vs split across *two* sockets (Ch. 17). How much worse is cross-socket contention? Relate to the NUMA cliff (Ch. 17) and the κ term (USL).
3. **Shard a contended map.** Take a single `mutex`-guarded `unordered_map` hammered by 8 threads (lookups + inserts); measure throughput/latency. Reshard into 8 per-thread maps (or a single-writer + lock-free-read design). Measure the improvement and the latency-tail change (Ch. 1).
4. **Find false sharing.** Make the `sharded` counters *unpadded* (remove `alignas`) so adjacent threads' counters share a line. Confirm it stops scaling (it now behaves like `shared` — Ch. 40). Re-pad and confirm. Why did "no logical sharing" still contend?
5. **Oversubscribe.** Run 32 busy threads on 8 cores vs 8 threads on 8 cores (pinned). Compare throughput, latency distribution, and context-switch counts (`perf stat -e context-switches` — Ch. 50). Quantify the oversubscription tax.

**Checklist — concurrency foundations**

- [ ] The design is **shared-nothing**: each thread/shard **owns its data**, communicating only through **explicit lock-free channels** (Ch. 41, 44) — not shared mutable state.
- [ ] Work is **sharded by a natural key** (symbol/venue) and/or structured as a **pipeline of single-threaded pinned stages**; threads are **pinned** (Ch. 52) and **NUMA-local** (Ch. 17).
- [ ] Unavoidable shared state is **single-writer-per-datum**, read via lock-free publication (Ch. 42–43); per-thread state (counters/stats) is **local + padded + aggregated off the hot path**.
- [ ] Shared data is **cache-line padded** (no false sharing — Ch. 40) and coordination is **batched** to amortize its cost (Ch. 44).
- [ ] I **measured the scalability curve** (throughput *and* latency distribution vs core count) at the **real core count / NUMA topology** — and confirmed it scales, not peaks-and-declines (§38.3).
- [ ] Threads are **not oversubscribed** (bounded set pinned to dedicated cores — Ch. 50, 52, 53, 55); inherently-serial hot paths are kept **single-threaded** on a pinned core.
- [ ] Read-mostly reference data is **replicated per thread/node** or published lock-free — reads never lock (Ch. 42–43, 94).
- [ ] Contention is attacked **structurally** (don't share) before micro-tuning atomics/locks (Ch. 37, 39).

## 38.7 References

- N. Gunther, *Guerrilla Capacity Planning* — the Universal Scalability Law (the α/κ model behind §38.2-§38.3) and how to fit it to measured data.
- G. Amdahl, *"Validity of the Single Processor Approach…"* — Amdahl's Law; and the contrast with USL's coherence term.
- M. Thompson et al., the **LMAX Disruptor** technical paper — single-threaded stages, mechanical sympathy, and shared-nothing pipeline design (leads into Ch. 44).
- P. McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* — partitioning, shared-nothing, single-writer, and the cost of synchronization, from first principles.
- A. Williams, *C++ Concurrency in Action* (2e) — designing concurrent systems, contention, and thread management (the practical companion).

## 38.8 Additional Reading

- The "Mechanical Sympathy" blog/community (Martin Thompson) — practical shared-nothing/single-writer design for low-latency systems.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — measuring scalability and contention with `perf`.
- Ch. 37 (*Atomics*) — the cost of the sharing this chapter avoids; Ch. 39 (*Spinlocks*) — when you must share; Ch. 40 (*False Sharing*) — the layout cost; Ch. 41/44 (*Lock-Free / Disruptor*) — the channels shared-nothing communicates through; Ch. 17 (*NUMA*) — sharding by node; Ch. 52/55 (*Pinning / RT scheduling*) — keeping threads on cores; Ch. 95 (*Process Topology*) — shared-nothing across processes.
- **Appendix E** — coherence/HITM and NUMA latency numbers that drive the contention cost; **Appendix F** — USL/Amdahl/shared-nothing glossary.

---

*Next: Ch. 39 — Spinlocks, Backoff & Contention Control, for the cases where sharing is unavoidable: spin vs block on a hot core, test-and-test-and-set, exponential backoff, `pause`/`tpause`, queue locks, and when a well-tuned spinlock beats lock-free complexity.*
