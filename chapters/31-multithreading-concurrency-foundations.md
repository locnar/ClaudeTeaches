# Part VI — Concurrency

# Chapter 31 — Multithreading & Concurrency Foundations

> **Prerequisites:** Ch. 30 (atomics & the memory model — the cost of sharing this chapter designs *around*), Ch. 7 & 32-preview (cache coherence / false sharing — the physical cost of contention), Ch. 16 (NUMA — cross-socket sharing), Ch. 1 (tail latency — contention spikes the tail), Ch. 3 (measuring scalability curves).
>
> **Leads into:** Ch. 32 (spinlocks/contention control — when you *must* share), Ch. 33 (false sharing), Ch. 34–37 (lock-free structures, seqlocks, Disruptor — the shared-nothing-friendly communication primitives), Ch. 41–46 (scheduling/pinning — the OS side of keeping threads on cores), Ch. 74 (process topology — shared-nothing across processes).

---

## 31.1 Why it matters: scaling without contention

The naive mental model of multithreading is "more threads = more throughput." For latency-critical systems this model is not just wrong, it's *backwards*: adding threads that **share mutable state** can make a system *slower* and *more jittery*, because the cost of coordinating shared data — cache-line bouncing (Ch. 7, 33), lock contention (Ch. 32), atomic barriers (Ch. 30) — grows faster than the work the extra threads do. Amdahl's law (the serial fraction caps speedup) is the optimistic version; the pessimistic reality is the **Universal Scalability Law**, which adds a *coherence/crosstalk* term that makes throughput *peak and then decline* as you add cores past the point where contention dominates. A poorly-designed concurrent system has a scalability curve that goes up, flattens, and comes *back down*.

For HFT the lesson is sharper still, because the goal is **latency**, not aggregate throughput, and contention attacks latency's tail (Ch. 1) most viciously. A contended cache line or lock doesn't just slow the average — it injects unpredictable, multi-microsecond stalls (the HITM coherence transfer of Ch. 7, the lock wait of Ch. 32, the NUMA cliff of Ch. 16) at exactly the moments of high activity (market open, a burst) when you most need determinism. So the foundational concurrency decision for a trading system isn't "how many threads" but **"how do I structure the work so threads barely interact at all"** — because the fastest coordination is no coordination.

That structuring principle is **shared-nothing**: partition the work so each thread (or process — Ch. 74) owns its data outright and communicates with others only through explicit, bounded, lock-free channels (Ch. 34, 37) rather than shared mutable state. A feed handler per symbol-range, a strategy per book, an order gateway per venue — each pinned to its own core (Ch. 42), each touching its own cache lines, each NUMA-local (Ch. 16), exchanging messages over ring buffers rather than sharing a hash map under a lock. This turns the concurrency problem from "make sharing fast" (hard, contention-bound, jittery) into "make sharing rare" (the design that actually scales and stays latency-flat). This chapter builds the mental model of contention and scalability limits (§31.2), measures the scalability curve that reveals where a design falls off (§31.3), and lays out shared-nothing partitioning and shared-state minimization (§31.4) — the architectural foundation that makes the atomics of Ch. 30 and the lock-free structures of Ch. 34–37 *occasional* rather than *central*.

## 31.2 Mental model: contention, scalability limits, shared-nothing

**Why sharing doesn't scale — the physical cost.** When two threads on different cores access the same cache line and at least one writes, the line must move between the cores' caches (Ch. 7's MESI: the writer takes exclusive ownership, invalidating the other's copy; the reader pulls it back — a **HITM** transfer, ~tens of ns, and across sockets the NUMA cliff of ~150 ns, Ch. 16). The more cores contend for a line, the more it ping-pongs, and the *coherence traffic* — not the useful work — comes to dominate. A lock is the same story plus serialization: only one thread holds it, the rest wait, and the lock's own cache line bounces among the waiters (Ch. 32). Contention is fundamentally a *physics* problem (data moving between cores), and you can't optimize your way out of it at the instruction level — you have to *not contend*.

**The scalability laws.** Two models frame how concurrent systems scale:

- **Amdahl's Law** — if a fraction *s* of the work is inherently serial, speedup is capped at `1/s` no matter how many cores. The optimistic bound: even a small serial section limits you.
- **Universal Scalability Law (USL)** — adds a **coherence** term *κ* (crosstalk/contention cost that grows with the *square* of concurrency): throughput `= N / (1 + α(N-1) + κN(N-1))`. The α term (contention/serialization) flattens the curve; the κ term (coherence) makes it **peak and then decline** — past some core count, *adding threads reduces throughput*. Real contended systems exhibit exactly this retrograde curve (§31.3).

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

**Shared-nothing — the design that beats the laws.** The way to keep α and κ near zero is to eliminate sharing: **each thread owns its data; threads communicate only through explicit message channels** (lock-free queues — Ch. 34, ring buffers — Ch. 37), never through shared mutable state. With nothing shared, there's no coherence traffic, no lock, no contention — each thread runs as if single-threaded on its own data, and the system scales linearly (limited only by the channel bandwidth and the genuinely-serial handoffs). This is the architecture of the fastest trading systems and the LMAX Disruptor (Ch. 37): a pipeline of single-threaded stages, each pinned to a core, passing messages one-way. The trading-domain version is **sharding by symbol/venue** (Ch. 16) — partition the instruments so each shard's feed/book/strategy is independent.

**The corollary: design for *one writer*.** Most coordination cost comes from *multiple writers* to the same data. A **single-writer** discipline — one thread/process owns each piece of mutable state and is the only one that writes it; others read via lock-free publication (seqlock — Ch. 35, or a ring) — eliminates write-write contention entirely and makes the memory model tractable (Ch. 30). Single-writer-per-datum is the practical heart of shared-nothing (and reappears in shared-memory IPC — Ch. 26, 74).

The model: **sharing mutable state across cores costs coherence traffic that grows super-linearly (USL) and spikes the tail; the fix isn't faster sharing but *less* sharing — partition into shared-nothing, single-writer-owned data communicating through explicit lock-free channels, so threads barely interact and the system scales and stays latency-flat.**

## 31.3 Measure it: scalability curve vs thread count

The diagnostic for a concurrency design is its **scalability curve**: throughput (and latency) as a function of thread count. A shared-nothing design scales ~linearly; a contended design peaks and declines (the USL retrograde curve). Measure both designs doing the *same* work — increment a counter N times total, split across M threads — sharing one atomic vs each thread owning a padded counter.

```cpp
// scale.cpp — shared atomic vs per-thread (shared-nothing) counters; sweep thread count.
// Build: g++ -O2 -std=c++20 -march=native scale.cpp -o scale -pthread
// Run:  ./scale shared 1 | ./scale shared 8 | ./scale sharded 8   (threads pinned to distinct cores)
#include <atomic>
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>
#include <new>

struct alignas(std::hardware_destructive_interference_size) Padded {  // own cache line (Ch. 32)
    std::uint64_t v = 0;
};

int main(int argc, char** argv) {
    bool sharded = (argc > 1) && std::strcmp(argv[1], "sharded") == 0;
    int M = argc > 2 ? std::atoi(argv[2]) : 1;
    constexpr long TOTAL = 800'000'000;
    long per = TOTAL / M;

    std::atomic<std::uint64_t> shared{0};
    std::vector<Padded> local(M);                          // one padded counter per thread

    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::thread> ts;
    for (int t = 0; t < M; ++t) ts.emplace_back([&, t]{
        // (pin to core t here — sched_setaffinity / Ch. 40)
        if (sharded) { std::uint64_t s = 0; for (long i=0;i<per;++i) s++; local[t].v = s; }  // no sharing
        else for (long i = 0; i < per; ++i) shared.fetch_add(1, std::memory_order_relaxed);  // shared line
    });
    for (auto& th : ts) th.join();
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("%-8s M=%d  total-throughput %.1f Mops/s  (%.2f ns/op)\n",
                sharded ? "sharded" : "shared", M, TOTAL / (ns/1e3), ns/TOTAL);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP, single socket), threads pinned to distinct cores, turbo off (illustrative; the *curve shape* is the point):

```
   M (cores)   shared atomic (one line)      sharded (per-thread padded)
   1           ~120 Mops/s                    ~900 Mops/s     <- even at M=1, atomic RMW costs
   2           ~50 Mops/s  (line bounces)      ~1800 Mops/s    <- shared: WORSE than M=1
   4           ~30 Mops/s                       ~3500 Mops/s    <- shared declines; sharded ~linear
   8           ~20 Mops/s  (retrograde!)        ~6800 Mops/s    <- shared: 6x SLOWER than M=1
```

Read it via the USL: the **shared** design exhibits the retrograde curve — throughput *peaks at one thread and declines* as cores are added, because every `fetch_add` fights for the one cache line and the coherence traffic (HITM — Ch. 7) swamps the work; eight cores deliver *less* than one. The **sharded** (shared-nothing) design scales ~linearly — each thread hits only its own padded cache line (Ch. 33), no coherence traffic, so 8 cores do ~8× the work. *Same total operations, opposite scaling*, purely from whether the state is shared. This is the foundational measurement of Part VI: **a concurrency design's quality is its scalability curve, and contention turns "more cores" into "less throughput."** The fix is structural (don't share — §31.4), not a faster atomic. (And note the latency story behind it: the shared version's per-op cost isn't just higher, it's *variable* — the line-bounce time depends on contention, spiking the tail, Ch. 1.)

## 31.4 Techniques

### 31.4.1 Shared-nothing partitioning

The primary technique: structure the system so each thread owns its data and rarely touches another's.

- **Shard by a natural key.** Partition the workload so each shard is independent and assign one thread/core per shard. In trading: **shard by symbol or venue** — feed handler, order book, and strategy for symbols A-M on core 0 (NUMA node 0 — Ch. 16), N-Z on core 1, etc. Each tick is processed entirely within one shard; no cross-shard sharing on the hot path. (Choose the shard key so load is balanced and cross-shard interactions — e.g. a strategy trading correlated symbols — are rare or handled off the hot path.)
- **Pipeline of single-threaded stages.** Decompose by *stage* rather than (or as well as) by data: feed-decode → book-build → strategy → risk → order-out, each a single-threaded stage pinned to a core, passing messages one-way through lock-free queues (Ch. 34) or a Disruptor ring (Ch. 37). Each stage is single-threaded (no internal locking), and the only "sharing" is the bounded handoff channel. This is the LMAX/mechanical-sympathy architecture.
- **Communicate through explicit channels, not shared state.** The cardinal rule: threads exchange *messages* over lock-free SPSC/MPSC queues or ring buffers (Ch. 34, 37), never reach into each other's data structures. A message handoff touches one producer line and one consumer line (and you can batch to amortize even that); a shared hash map under a lock touches everything. Make the channels the *only* contention point, and make them efficient (Ch. 34).
- **Pin everything (Ch. 42) and go NUMA-local (Ch. 16).** Shared-nothing only holds if threads stay put: pin each shard/stage thread to its core so its data stays cache-warm and its first-touched memory stays NUMA-local. Affinity + partitioning are a pair (as placement + pinning were in Ch. 16).
- **Replicate read-mostly data per thread/node (Ch. 16).** Reference data many threads read (symbol tables, config, risk limits) is *copied* per thread/node so reads are local and never contend, updated via lock-free publication (seqlock/RCU — Ch. 35–36, hot-reload — Ch. 73). Read-mostly sharing becomes no sharing.

### 31.4.2 Minimizing shared mutable state

Where some sharing is unavoidable, shrink and shape it to minimize contention:

- **Single-writer per datum.** Make exactly one thread the writer of each shared piece; others only read (via lock-free publication — Ch. 35). Single-writer eliminates write-write contention (the worst kind) and makes the memory ordering tractable (Ch. 30). This is the most important shaping rule when you can't fully shard.
- **Per-thread state aggregated off the hot path.** Counters, statistics, accumulators: give each thread its *own* (padded — Ch. 33) copy that it updates with no synchronization, and aggregate them lazily off the hot path (a monitoring thread sums them periodically). The §31.3 sharded counter — turns a contended hot atomic into contention-free local updates. Applies to metrics, P&L accumulation, histograms (Ch. 3).
- **Pad shared data to cache lines (Ch. 33).** When data *must* be shared, ensure independent items don't share a cache line (false sharing — Ch. 33): `alignas(hardware_destructive_interference_size)`. A queue's head and tail, two unrelated atomics — separate lines so they don't bounce together.
- **Batch to amortize coordination.** If a handoff/lock is unavoidable, do it less often: batch N items per queue push, per lock acquisition, per atomic update (Ch. 37's batching). Amortizing the coordination cost over many items shrinks its per-item impact — often turning a contention bottleneck into a non-issue.
- **Prefer immutable / copy-on-write for shared reads.** Shared data that's *read* by many and *changed* rarely can be published as immutable snapshots (atomic pointer swap to a new version, old version reclaimed safely — Ch. 36, 73), so readers never lock and never contend with the writer (seqlock — Ch. 35 for small data, RCU — Ch. 36 / pointer-swap for larger).
- **Make the shared channel efficient, not the shared data structure.** The right place for shared mutable state is a *purpose-built lock-free channel* (SPSC/MPSC queue, ring — Ch. 34, 37) designed for exactly one contention pattern, not a general-purpose container under a lock. Concentrate sharing into the channel and engineer that channel well.

## 31.5 Pitfalls & anti-patterns: false scaling, oversubscription

- **Assuming more threads = more throughput.** The foundational error: adding threads that share mutable state can *reduce* throughput (the USL retrograde curve — §31.3) and *spike latency*. Measure the scalability curve; design for shared-nothing, not thread count.
- **A shared container under a lock as the "concurrent data structure."** A `std::unordered_map` (Ch. 25) behind a `std::mutex` shared by all threads is the canonical contention bottleneck — every access serializes and bounces the lock line. Shard it (per-thread maps), make it single-writer + lock-free-read, or use a purpose-built channel (§31.4). 
- **Oversubscription (more threads than cores).** Running more busy threads than physical cores forces the scheduler to context-switch them (Ch. 41), destroying cache warmth and adding jitter — catastrophic for a latency system. Pin a bounded set of threads to dedicated cores (Ch. 42, 45); don't spawn a thread pool larger than the core budget on the hot path.
- **False sharing masquerading as necessary contention (Ch. 33).** Two *logically independent* variables on the same cache line contend invisibly — looks like a concurrency cost, is actually a layout bug. Pad to cache lines; suspect false sharing when "independent" per-thread work doesn't scale (§31.3, Ch. 33).
- **"It scaled on my 4-core laptop."** Contention costs grow with core count and are worse across NUMA nodes (Ch. 16); a design that looks fine on a few cores can fall off a cliff on a 32-core dual-socket server. Test at the real core count and topology (§31.3).
- **Sharing read-mostly reference data by reference under a lock.** Locking to read config/symbol data that changes rarely serializes all readers for no reason. Replicate per thread/node or publish lock-free (seqlock/RCU — Ch. 35–36); reads should never lock.
- **Ignoring the tail (Ch. 1).** A design can have acceptable *average* throughput while contention spikes the *tail* (the line-bounce time is variable). For a latency system the tail is the product; measure the latency distribution under concurrency, not just throughput.
- **Multiple writers where single-writer would do.** Allowing many threads to write the same datum (when one owner could) creates the worst contention and the hardest memory-ordering reasoning (Ch. 30). Default to single-writer-per-datum; it's simpler *and* faster.
- **Threads for work that's inherently serial.** Parallelizing a fundamentally serial dependency chain (each step needs the previous result) just adds coordination cost for no parallelism (Amdahl). Some hot paths (the single-message tick-to-trade path) are best *single-threaded* on a pinned core — don't parallelize what can't parallelize.

## 31.6 Exercises & checklist

**Exercises**

1. **Draw the curve.** Build `scale.cpp` (pin threads to distinct cores); run `shared` and `sharded` for M=1,2,4,8,16. Plot total throughput vs cores for each. Confirm `shared` is retrograde (peaks at 1, declines) and `sharded` is ~linear (§31.3). Where's the `shared` peak?
2. **NUMA makes it worse.** Run the `shared` version with threads on *one* socket vs split across *two* sockets (Ch. 16). How much worse is cross-socket contention? Relate to the NUMA cliff (Ch. 16) and the κ term (USL).
3. **Shard a contended map.** Take a single `mutex`-guarded `unordered_map` hammered by 8 threads (lookups + inserts); measure throughput/latency. Reshard into 8 per-thread maps (or a single-writer + lock-free-read design). Measure the improvement and the latency-tail change (Ch. 1).
4. **Find false sharing.** Make the `sharded` counters *unpadded* (remove `alignas`) so adjacent threads' counters share a line. Confirm it stops scaling (it now behaves like `shared` — Ch. 33). Re-pad and confirm. Why did "no logical sharing" still contend?
5. **Oversubscribe.** Run 32 busy threads on 8 cores vs 8 threads on 8 cores (pinned). Compare throughput, latency distribution, and context-switch counts (`perf stat -e context-switches` — Ch. 41). Quantify the oversubscription tax.

**Checklist — concurrency foundations**

- [ ] The design is **shared-nothing**: each thread/shard **owns its data**, communicating only through **explicit lock-free channels** (Ch. 34, 37) — not shared mutable state.
- [ ] Work is **sharded by a natural key** (symbol/venue) and/or structured as a **pipeline of single-threaded pinned stages**; threads are **pinned** (Ch. 42) and **NUMA-local** (Ch. 16).
- [ ] Unavoidable shared state is **single-writer-per-datum**, read via lock-free publication (Ch. 35–36); per-thread state (counters/stats) is **local + padded + aggregated off the hot path**.
- [ ] Shared data is **cache-line padded** (no false sharing — Ch. 33) and coordination is **batched** to amortize its cost (Ch. 37).
- [ ] I **measured the scalability curve** (throughput *and* latency distribution vs core count) at the **real core count / NUMA topology** — and confirmed it scales, not peaks-and-declines (§31.3).
- [ ] Threads are **not oversubscribed** (bounded set pinned to dedicated cores — Ch. 41, 42, 43, 45); inherently-serial hot paths are kept **single-threaded** on a pinned core.
- [ ] Read-mostly reference data is **replicated per thread/node** or published lock-free — reads never lock (Ch. 35–36, 73).
- [ ] Contention is attacked **structurally** (don't share) before micro-tuning atomics/locks (Ch. 30, 32).

## 31.7 References

- N. Gunther, *Guerrilla Capacity Planning* — the Universal Scalability Law (the α/κ model behind §31.2-§31.3) and how to fit it to measured data.
- G. Amdahl, *"Validity of the Single Processor Approach…"* — Amdahl's Law; and the contrast with USL's coherence term.
- M. Thompson et al., the **LMAX Disruptor** technical paper — single-threaded stages, mechanical sympathy, and shared-nothing pipeline design (leads into Ch. 37).
- P. McKenney, *Is Parallel Programming Hard, And, If So, What Can You Do About It?* — partitioning, shared-nothing, single-writer, and the cost of synchronization, from first principles.
- A. Williams, *C++ Concurrency in Action* (2e) — designing concurrent systems, contention, and thread management (the practical companion).

## 31.8 Additional Reading

- The "Mechanical Sympathy" blog/community (Martin Thompson) — practical shared-nothing/single-writer design for low-latency systems.
- D. Bakhvalov, *Performance Analysis and Tuning on Modern CPUs* — measuring scalability and contention with `perf`.
- Ch. 30 (*Atomics*) — the cost of the sharing this chapter avoids; Ch. 32 (*Spinlocks*) — when you must share; Ch. 33 (*False Sharing*) — the layout cost; Ch. 34/37 (*Lock-Free / Disruptor*) — the channels shared-nothing communicates through; Ch. 16 (*NUMA*) — sharding by node; Ch. 42/45 (*Pinning / RT scheduling*) — keeping threads on cores; Ch. 74 (*Process Topology*) — shared-nothing across processes.
- **Appendix E** — coherence/HITM and NUMA latency numbers that drive the contention cost; **Appendix F** — USL/Amdahl/shared-nothing glossary.

---

*Next: Ch. 32 — Spinlocks, Backoff & Contention Control, for the cases where sharing is unavoidable: spin vs block on a hot core, test-and-test-and-set, exponential backoff, `pause`/`tpause`, queue locks, and when a well-tuned spinlock beats lock-free complexity.*
