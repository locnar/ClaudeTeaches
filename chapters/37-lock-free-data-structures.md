# Part VI — Concurrency

# Chapter 37 — Lock-Free Data Structures

> **Prerequisites:** Ch. 33 (atomics & memory ordering — the acquire/release this is built on), Ch. 34 (shared-nothing — lock-free queues are the *channels* between shared-nothing stages), Ch. 35 (spinlocks — the alternative; CAS loops spin), Ch. 36 (false sharing — head/tail must be padded), Ch. 7 (coherence — the cost of contended CAS), Ch. 26–27 (no hot-path allocation — the ring is pre-allocated).
>
> **Leads into:** Ch. 38 (seqlocks — single-writer publication), Ch. 39 (safe reclamation — the hard part of *dynamic* lock-free structures), Ch. 40 (the Disruptor — the productionized ring buffer), Ch. 57 (IPC — these queues in shared memory — Ch. 29). The SPSC ring is the single most important structure in the low-latency data plane.

---

## 37.1 Why it matters: progress without locks

A lock-free data structure lets multiple threads operate on shared data **without a lock** — no thread can be blocked by another thread holding a lock (because there is no lock to hold), so there's no lock contention (Ch. 35), no lock convoy, no priority inversion (Ch. 51), and — crucially for latency — **no thread can stall every other thread by being descheduled mid-critical-section** (the spinlock-preemption disaster of §35.5). For the low-latency data plane this is the decisive property: the channels between the shared-nothing stages of Ch. 34 (feed-handler → strategy → gateway) must hand off messages at nanosecond latency with a *bounded, predictable* cost on every operation, and a lock — with its tail-spiking contention and its dependence on the scheduler — can't deliver that. A lock-free **ring buffer** can: a push and a pop are each a handful of atomic operations with a fixed cost, no blocking, no syscall, no allocation (Ch. 26).

The single most important lock-free structure in this domain is the **SPSC (single-producer, single-consumer) ring buffer** — and it's important precisely *because* it's the simplest. The shared-nothing pipeline architecture (Ch. 34, 40) is built from one-way handoffs between single-threaded stages, and a one-way handoff is *exactly* SPSC: one producer thread, one consumer thread, a fixed-size pre-allocated ring between them. SPSC is the workhorse because it needs **no CAS, no ABA handling, no reclamation** — just two atomic indices with acquire/release ordering (Ch. 33) — making it fast, simple, and *easy to get right*. Most of the lock-free structures a trading system actually runs on the hot path are SPSC rings; the more general (and more dangerous) MPMC queues, CAS loops, and dynamic lock-free structures are used *sparingly*, where the design genuinely needs many producers/consumers.

That ordering — simple-and-safe to complex-and-perilous — is the spine of this chapter, because lock-free programming is **famously hard to get right**. The bugs are the worst kind: rare, timing-dependent, architecture-dependent (x86 hides what ARM exposes — Ch. 33), and able to corrupt data silently. The **ABA problem** (a value reads the same but the world changed underneath you), **memory reclamation** (you can't free a node another thread might still be reading — Ch. 39, the single hardest part), **livelock** (CAS loops retrying forever under contention), and subtle **memory-ordering** mistakes (Ch. 33) lurk in every non-trivial lock-free structure. So the discipline this chapter teaches is: **prefer the simplest structure that fits — SPSC if you can shard down to it (Ch. 34) — use well-tested library implementations rather than hand-rolling, reserve hand-written CAS-based MPMC/dynamic structures for genuine need, and validate ruthlessly (model-check, run on ARM, stress-test — Ch. 45).** Lock-free is a powerful tool and a sharp one; this chapter covers the mental model (§37.2), measures the SPSC ring that does most of the real work (§37.3), builds up SPSC → MPMC → ABA-avoidance (§37.4), and warns about livelock and lost wakeups (§37.5).

## 37.2 Mental model: CAS loops, ABA, progress guarantees

**The building block: compare-and-swap (CAS).** Lock-free algorithms are built on atomic **read-modify-write** primitives, chiefly `compare_exchange` (CAS): "atomically, if this location still equals the value I expected, set it to the new value; otherwise tell me it changed." The canonical lock-free update is a **CAS loop**: read the current value, compute the new value, CAS it in; if the CAS fails (someone else changed it), retry from the read:

```cpp
T old = x.load(std::memory_order_acquire);
T desired;
do { desired = compute(old); }               // compute new state from observed old
while (!x.compare_exchange_weak(old, desired, std::memory_order_acq_rel,
                                std::memory_order_acquire));   // retry until we win (old is refreshed on failure)
```

This makes progress *without a lock*: a thread that fails its CAS does so *because another thread succeeded* — the system as a whole advanced. That's the essence of lock-freedom.

**Progress guarantees — the hierarchy.** "Lock-free" is one rung of a precise ladder:

- **Obstruction-free** — a thread makes progress if it runs in isolation (no contention). Weakest.
- **Lock-free** — *at least one* thread always makes progress (the system as a whole can't stall); an *individual* thread might retry forever (starve), but someone always advances. This is what most "lock-free" structures provide.
- **Wait-free** — *every* thread makes progress in a bounded number of steps (no starvation, bounded latency per operation). Strongest — and what you ideally want for latency (bounded worst case — Ch. 1) — but harder and often slower in the common case. SPSC rings are naturally wait-free; general MPMC wait-free structures are exotic.

For latency, **wait-free is the gold standard** (bounded per-operation cost = bounded tail), but lock-free with low contention often suffices. The thing to *avoid* is something that looks lock-free but livelocks under contention (§37.5).

**The ABA problem.** CAS checks that the value is *unchanged* — but "unchanged value" doesn't mean "unchanged world." If a location goes A → B → A between your read and your CAS, the CAS **succeeds** (it still sees A) even though the state changed and changed back — and your update, premised on the original A, may now be wrong (e.g. you reuse a freed node that was popped and re-pushed). This is the **ABA problem**, the classic lock-free trap, and it haunts pointer-based structures (a node freed and reallocated to the same address). Cures: **tagged pointers** (pack a version counter with the pointer so A-with-tag-1 ≠ A-with-tag-2 — §37.4.3), double-width CAS (`cmpxchg16b`), or avoiding the reuse entirely (don't free — pool indices, Ch. 27; or safe reclamation — Ch. 39).

**Reclamation — the hard part.** In a *dynamic* lock-free structure (nodes allocated/freed), you can't simply `free` a node you've unlinked: another thread may *still be reading it* (it loaded the pointer before you unlinked). Freeing it is a use-after-free. Solving "when is it safe to reclaim?" without a lock is the single hardest part of lock-free programming — hazard pointers, epochs, RCU — and it's **Chapter 39's** entire subject. The practical escape: **avoid dynamic allocation** in the first place (fixed-size rings, pre-allocated pools indexed by integer — Ch. 27), which sidesteps reclamation *and* ABA. This is why SPSC rings (fixed array, no node freeing) are so much simpler than a lock-free linked list.

The model: **lock-free = build with atomic CAS loops so the system always progresses without locks (wait-free = every thread bounded, the latency ideal). The traps are ABA (value same, world changed — use tags/no-reuse) and reclamation (can't free what others read — Ch. 39, or avoid allocation). The lesson: prefer fixed-size, allocation-free structures (SPSC rings) that dodge both.**

## 37.3 Measure it: SPSC ring throughput/latency

The SPSC ring is the structure that matters most, so measure *it*: producer→consumer handoff throughput and latency, and the effect of the two things that dominate — **head/tail false sharing** (Ch. 36) and **batching the index updates**. A correct SPSC ring with padded indices and cached opposite-end reads is the target.

```cpp
// spsc.cpp — single-producer/single-consumer ring buffer; measure handoff throughput.
// Build: g++ -O2 -std=c++20 -march=native spsc.cpp -o spsc -pthread
// Run:  ./spsc   (producer pins itself to core 2, consumer to core 4 — two full cores, not SMT siblings)
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <chrono>
#include <new>
#include <array>
#include <pthread.h>
#include <sched.h>

#ifdef __cpp_lib_hardware_interference_size            // GCC 12+/libstdc++; Clang 19+ (Ch. 36, Appendix H)
constexpr std::size_t kLine = std::hardware_destructive_interference_size;
#else
constexpr std::size_t kLine = 64;
#endif

static void pin_to(int core) {                           // Ch. 48
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(core, &s);
    if (pthread_setaffinity_np(pthread_self(), sizeof s, &s) != 0) std::perror("pthread_setaffinity_np");
}

template <class T, std::size_t CAP>   // CAP must be a power of two (Ch. 31: index & (CAP-1))
class SpscRing {
    static_assert((CAP & (CAP - 1)) == 0, "power of two");
    std::array<T, CAP> buf_{};   // fixed, pre-allocated storage. NOT std::vector<T> buf_{CAP}: for an integral T
                                 // that brace-init picks the initializer_list ctor -> a ONE-element vector holding CAP
    alignas(kLine) std::atomic<std::size_t> head_{0}; // producer writes — own cache line (Ch. 36)
    alignas(kLine) std::atomic<std::size_t> tail_{0}; // consumer writes — own cache line
public:
    bool push(const T& v) {
        std::size_t h = head_.load(std::memory_order_relaxed);
        if (h - tail_.load(std::memory_order_acquire) == CAP) return false;  // full
        buf_[h & (CAP - 1)] = v;
        head_.store(h + 1, std::memory_order_release);                       // publish (Ch. 33)
        return true;
    }
    bool pop(T& v) {
        std::size_t t = tail_.load(std::memory_order_relaxed);
        if (t == head_.load(std::memory_order_acquire)) return false;        // empty
        v = buf_[t & (CAP - 1)];
        tail_.store(t + 1, std::memory_order_release);
        return true;
    }
};

SpscRing<std::uint64_t, 1024> ring;

int main() {
    constexpr long N = 200'000'000;
    auto t0 = std::chrono::steady_clock::now();
    std::thread prod([&]{ pin_to(2); for (long i = 0; i < N; ) if (ring.push(i)) ++i; });
    std::uint64_t v, last = 0;
    std::thread cons([&]{ pin_to(4); for (long i = 0; i < N; ) if (ring.pop(v)) { last = v; ++i; } });  // two full cores, not SMT siblings
    prod.join(); cons.join();
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("SPSC: %.1f Mmsg/s  (%.2f ns/msg, last=%llu)\n",
                N / (ns/1e3), ns/N, (unsigned long long)last);
    return 0;
}
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), producer/consumer pinned to two cores, turbo off (illustrative):

```
   SPSC ring, padded head/tail:        ~80-150 Mmsg/s   (~7-12 ns/msg)   <- one line each way
   SPSC ring, head/tail SAME line:     ~25 Mmsg/s        (~40 ns/msg)     <- false sharing (Ch. 36): 3-5x worse
   + batching (amortize index loads):  ~300+ Mmsg/s                      <- read opposite index less often

   vs a std::mutex-protected queue:    ~10-20 Mmsg/s     (lock + futex under contention — Ch. 35)
```

Read it: the padded SPSC ring *sustains* ~7-12 ns per message of **throughput** — inverse throughput, not the latency of one handoff. That figure is *below* a single cross-core cache-line transfer (~40-100 ns, Ch. 36 / Appendix E) because the transfers overlap: the producer's stores drain through its store buffer without waiting, the consumer pulls each slot line while the producer is already filling later slots, and the index lines move only when an end re-reads the other's index. The *latency* of one push→pop handoff — what a tick-to-trade path cares about — is a separate quantity, roughly one line transfer each way plus the consumer's polling granularity (~100-200 ns round trip on this box — measure it with a ping-pong variant of this listing under Appendix L's tail-latency harness), and its tail is what Ch. 1 asks you to watch. No lock, no syscall, no allocation either way. **Putting `head` and `tail` on the same cache line (no padding) is 3-5× slower** — the exact false-sharing bug of Ch. 36 in the hottest structure, the most common SPSC mistake. **Batching** (reading the opposite index less frequently — caching it locally and only re-reading when the cached value says full/empty) amortizes the cross-core read and pushes throughput much higher. And the whole thing dwarfs a mutex-protected queue (Ch. 35). The lessons that carry: **pad head/tail (Ch. 36), use power-of-two capacity (Ch. 31: `& (CAP-1)`, no modulo), acquire/release ordering (Ch. 33: publish with release, observe with acquire), pre-allocate the buffer (no hot-path allocation — Ch. 26), and batch to amortize the cross-core index reads.** This SPSC ring — correct, padded, batched — is the data-plane primitive most of the system runs on. (Ch. 80 §80.3 ports this listing to Rust and finds the unbatched ring's *throughput* bistable in both languages — 23 vs 107 Mmsg/s for the same binary depending on whether the consumer falls into lock-step — which is why the ping-pong latency, not throughput, is the number to compare.)

## 37.4 Techniques

### 37.4.1 SPSC and MPMC queues

The queue family, simplest to hardest — and the strong advice to stay as simple as your design allows:

- **SPSC (single-producer, single-consumer) — the default.** One producer, one consumer, a fixed-size ring. Needs **no CAS** (each index has exactly one writer — single-writer, Ch. 34), no ABA, no reclamation — just two acquire/release atomic indices (§37.3). Fast, simple, **wait-free**, easy to verify. **If you can structure the handoff as SPSC (and shared-nothing pipelines usually can — Ch. 34, 40), do.** This is the right answer for the vast majority of hot-path queues.
- **MPSC (multi-producer, single-consumer).** Many producers, one consumer (e.g. many threads logging to one I/O thread — Ch. 84, or feeding one aggregator). Producers contend on the head index via CAS (or a per-producer slot scheme); the consumer is single. More complex than SPSC (the producer side needs CAS + careful ordering) but avoids full MPMC. Common for fan-in.
- **SPMC (single-producer, multi-consumer).** One producer, many consumers (broadcast/work-distribution — the Disruptor's model, Ch. 40). Consumers contend on the tail.
- **MPMC (multi-producer, multi-consumer) — use sparingly.** The general case: CAS on both ends, the hardest to make correct and fast (bounded MPMC rings like Vyukov's, or Michael-Scott unbounded queues). Real contention (Ch. 35), ABA risk, and (if unbounded/dynamic) reclamation (Ch. 39). **Reach for MPMC only when the design genuinely needs many-to-many** — often a sign you should re-shard to SPSC/MPSC (Ch. 34). Use a vetted library (moodycamel, boost.lockfree, folly) — don't hand-roll MPMC.
- **Bounded vs unbounded.** Prefer **bounded** (fixed-size ring) on the hot path: pre-allocated (no allocation — Ch. 26), no reclamation, predictable memory, natural backpressure (push fails when full → caller handles it). Unbounded (linked) queues need allocation and reclamation (Ch. 39) — avoid on the hot path.

### 37.4.2 Ring buffers

The ring buffer is the concrete structure behind bounded queues, and the details matter:

- **Power-of-two capacity (Ch. 31).** Keep the capacity a *compile-time* power of two: with an unsigned index, `idx % CAP` and `idx & (CAP-1)` both compile to a single AND (the compiler does the mask trick for you — §31.4.3); it is a *runtime* capacity (or a signed index needing sign fix-up) that forces a ~15-40-cycle `div` (Ch. 11) into the hot loop. Use *monotonically increasing* indices (never wrap the counters themselves; wrap only when indexing) so `head - tail` gives the count and full/empty are unambiguous (avoids the "is it full or empty?" ambiguity of wrapped indices).
- **Pad head and tail to separate cache lines (Ch. 36).** The single most important layout rule (§37.3): producer-written `head` and consumer-written `tail` on **separate** lines, or they false-share and you lose 3-5×.
- **Cache the opposite index (batching).** The producer reads `tail` only to check fullness; instead of reading it (a cross-core load) every push, **cache** the last-seen `tail` and only re-read when the cache says "full" — most pushes then need no cross-core read. Symmetrically for the consumer and `head`. This batching is the big throughput lever (§37.3) and the Disruptor's core trick (Ch. 40).
- **Pre-allocate the buffer (Ch. 26, 29).** The ring's storage is allocated once at startup, pre-faulted and `mlock`ed (Ch. 29), ideally huge-page-backed (Ch. 15) and NUMA-local (Ch. 16). The hot path never allocates. For IPC, the ring lives in shared memory (Ch. 29, 57) with indices as offsets (Ch. 29).
- **Element layout.** Store fixed-size POD elements inline (no pointers to chase — Ch. 28); for variable-size messages, a ring of fixed slots with an overflow strategy, or a separate byte-ring. Keep the element cache-friendly (Ch. 8).

### 37.4.3 Avoiding ABA (tagged pointers)

For the CAS-based structures that *do* manipulate pointers (MPMC, lock-free stacks/lists), defeat ABA (§37.2) — and note that the index-plus-generation handle Ch. 28 §28.4.4 builds as a slot map is the same mechanism in single-threaded form:

- **Tagged pointers (version counters).** Pack a monotonically-incrementing **version/tag** alongside the pointer (in the unused high bits of a 64-bit pointer, or as a separate counter in a double-width CAS). Every modification bumps the tag, so A-with-tag-5 ≠ A-with-tag-7 even at the same address — the CAS fails as it should when the world changed. Double-width CAS (`cmpxchg16b` on x86-64, `casp` on ARM) swaps pointer+tag atomically. The 16-byte CAS this needs has a toolchain trap: build with `-mcx16`, `alignas(16)` the pair, and `static_assert(std::atomic<Tagged>::is_always_lock_free)` — which **fails on GCC 13 even with `-mcx16`** (GCC routes 16-byte atomics through libatomic, lock-free at runtime via `cmpxchg16b` but not provably so), while Clang inlines `lock cmpxchg16b` (Ch. 33 §33.4.3). Know which you ship.
- **Avoid reuse entirely (the simpler escape).** ABA arises from *address reuse* (a node freed and reallocated to the same address). If you **don't free** — use a fixed pool indexed by integer (Ch. 27), or indices into a ring instead of pointers — there's no reuse and no ABA. This is why allocation-free, index-based designs (rings, slot maps) sidestep the whole problem, and why they're preferred on the hot path.
- **Safe reclamation defers the free (Ch. 39).** Hazard pointers / epochs / RCU (Ch. 39) ensure a node isn't reused until no thread can reference it — solving ABA *and* use-after-free together. The right tool for genuinely dynamic lock-free structures, but heavyweight; covered in Ch. 39.
- **The practical guidance.** On the hot path, prefer **index-based, allocation-free** structures that don't have ABA at all (SPSC/MPMC rings with integer indices). Reach for tagged pointers / DWCAS / reclamation only for genuinely dynamic pointer structures you can't avoid — and use a vetted library.

## 37.5 Pitfalls & anti-patterns: livelock, lost wakeups

- **Livelock under contention.** A CAS loop where threads keep failing and retrying (each invalidating the others' reads) can make *no* useful progress while burning CPU — lock-free in name, stuck in practice. Add **backoff** (§35.4.2) to CAS retries, reduce contention (shard — Ch. 34), or use a structure with less contention (SPSC). Watch for it under high load (Exercise 5's library MPMC queue at 4↔4 shows where the retries begin).
- **Head/tail false sharing (the #1 ring bug).** Producer-written `head` and consumer-written `tail` on the same cache line → 3-5× slowdown (§37.3, Ch. 36). **Pad them.** The most common and most impactful lock-free-queue mistake.
- **Wrong memory ordering.** Publishing the data with `relaxed` instead of `release` (or reading the index with `relaxed` instead of `acquire`) is a race that *works on x86 and fails on ARM* (Ch. 33). The consumer sees the index advance but reads stale/absent slot data. Use acquire/release correctly; test on ARM / model-check (Ch. 45).
- **ABA in pointer-based structures.** A lock-free stack/queue that frees and reuses nodes without tags/reclamation hits ABA — a popped node re-pushed at the same address makes a stale CAS succeed, corrupting the structure (§37.2-§37.4.3). Use tags, DWCAS, reclamation (Ch. 39), or avoid reuse (indices).
- **Use-after-free / reclamation bugs.** Freeing a node another thread still references (it loaded the pointer before you unlinked) is UAF — the hardest lock-free bug (Ch. 39). Don't `free` in a dynamic lock-free structure without a reclamation scheme; prefer allocation-free designs.
- **Lost wakeups (blocking + lock-free mix).** If a consumer *blocks* when the queue is empty (to save CPU) and the producer's "wake" races with the consumer's "sleep", a wakeup can be lost (consumer sleeps forever with data waiting). Mixing lock-free queues with condition-variable/futex blocking needs careful wakeup protocols. On a dedicated core, **busy-spin** instead (Ch. 35, 40) — no blocking, no lost wakeups.
- **Hand-rolling MPMC/dynamic lock-free structures.** These are *extremely* hard to get right (ABA, reclamation, ordering, livelock all at once) and the bugs are silent and rare. **Use vetted libraries** (boost.lockfree, moodycamel, folly, the Disruptor — Ch. 40); reserve hand-rolling for SPSC (simple enough to verify) or genuine research need, and validate ruthlessly (Ch. 45).
- **Assuming lock-free = wait-free = fast.** Lock-free allows individual-thread starvation (only the *system* progresses); under contention a thread's latency can be unbounded (bad for the tail — Ch. 1). For bounded per-op latency you need *wait-free* (SPSC is). And lock-free isn't automatically faster than a good spinlock for low contention (Ch. 35) — measure.
- **Unbounded queues on the hot path.** Linked/growable queues allocate (Ch. 26) and need reclamation (Ch. 39) — both hot-path hazards. Use **bounded rings** with backpressure (push fails when full); handle fullness explicitly.

## 37.6 Exercises & checklist

**Exercises**

1. **SPSC throughput + false sharing.** Build `spsc.cpp` (pin producer/consumer to two cores); measure Mmsg/s. Then put `head`/`tail` on the *same* line (remove `alignas`) and re-measure — confirm the 3-5× drop and HITM (Ch. 36, `perf c2c`). Re-pad; confirm recovery.
2. **Batching.** Add opposite-index caching (producer caches `tail`, only re-reads when "full"). Measure the throughput gain. Why does caching the cross-core index help so much (§37.4.2, Ch. 7)?
3. **Ordering on ARM.** Weaken the ring's `release`/`acquire` to `relaxed`; on x86 it likely still "works." Cross-compile/run on ARM (Graviton — Appendix A) or model-check (Ch. 45) — show the race. Restore acquire/release.
4. **ABA demo.** Build a lock-free stack that frees+reuses nodes; construct an A→B→A sequence that makes a stale CAS succeed and corrupt the stack. Fix it with tagged pointers (DWCAS) or by using a fixed pool of indices (§37.4.3).
5. **SPSC vs mutex vs MPMC.** Compare the SPSC ring, a `std::mutex` queue, and a library MPMC queue (boost.lockfree/moodycamel) for 1↔1 handoff. Quantify the gap; then measure the MPMC queue at 4↔4 — where does its contention show (Ch. 35)?

**Checklist — lock-free data structures**

- [ ] I used the **simplest structure that fits** — **SPSC** wherever the handoff can be 1↔1 (sharded down — Ch. 34, 40); MPSC/SPMC for fan-in/out; **MPMC only when genuinely needed** (and from a vetted library).
- [ ] Rings are **bounded, pre-allocated** (no hot-path allocation — Ch. 26, pre-faulted/`mlock`ed — Ch. 29), **power-of-two** capacity (`& (CAP-1)` — Ch. 31), with **head/tail padded to separate cache lines** (Ch. 36).
- [ ] **Acquire/release ordering** is correct (publish data with `release`, observe index with `acquire` — Ch. 33) and **validated on a weakly-ordered machine / model checker** (Ch. 45) — not x86 alone.
- [ ] The cross-core index read is **batched/cached** (read the opposite index less often) to amortize coherence traffic (§37.4.2, Ch. 40).
- [ ] Pointer-based/dynamic structures handle **ABA** (tagged pointers / DWCAS / no-reuse) and **reclamation** (Ch. 39) — or, preferably, are **avoided** in favor of index-based allocation-free designs.
- [ ] CAS loops have **backoff** to avoid **livelock** under contention (Ch. 35); I don't mix lock-free with blocking carelessly (no **lost wakeups** — busy-spin on dedicated cores instead, Ch. 35, 40).
- [ ] For bounded latency I prefer **wait-free** (SPSC is); I don't assume lock-free = bounded-latency or = faster than a spinlock — **measured** (Ch. 1, 35).
- [ ] Non-trivial (MPMC/dynamic) structures are **vetted-library** implementations, **stress-tested and model-checked** (Ch. 45) — not hand-rolled.

## 37.7 References

- M. Michael & M. Scott, *"Simple, Fast, and Practical Non-Blocking and Blocking Concurrent Queue Algorithms"* — the foundational MS-queue (§37.4.1).
- D. Vyukov's writings (1024cores.net) — bounded MPMC queues, SPSC rings, and lock-free design patterns; an essential practical resource.
- M. Herlihy & N. Shavit, *The Art of Multiprocessor Programming* — progress guarantees (obstruction/lock/wait-free), CAS, ABA, and the theory behind §37.2.
- The LMAX **Disruptor** paper (Thompson et al.) — the productionized SPSC/SPMC ring with batching and cache-aware layout (Ch. 40).
- The boost.lockfree, moodycamel ConcurrentQueue, and folly (`MPMCQueue`, `ProducerConsumerQueue`) documentation — vetted implementations to use instead of hand-rolling (§37.4-§37.5).

## 37.8 Additional Reading

- P. McKenney, *Is Parallel Programming Hard?* — lock-free design, ABA, and the reclamation problem (leads into Ch. 39).
- J. Preshing's blog — lock-free programming, the ABA problem, and memory ordering, with clear examples.
- Ch. 33 (*Atomics*) — the ordering foundation; Ch. 35 (*Spinlocks*) — the locking alternative and CAS backoff; Ch. 36 (*False Sharing*) — padding head/tail; Ch. 38 (*Seqlocks*) — single-writer publication; Ch. 39 (*Safe Reclamation*) — the hard part of dynamic lock-free; Ch. 40 (*Disruptor*) — the ring buffer productionized; Ch. 45 (*Concurrency Tooling*) — validating lock-free code; Ch. 57 (*IPC*) — rings in shared memory.
- **Appendix A** — ARM weak ordering and LSE atomics; **Appendix E** — cross-core (HITM) transfer latency, the floor under any handoff; **Appendix F** — ABA glossary entry.

---

*Next: Ch. 38 — Seqlocks & Single-Writer Publication, a different lock-free pattern for a different need: publishing a frequently-updated snapshot (a price, a book top, reference state) to many readers without locks — versioned reads, torn-read avoidance, and the single-writer/multi-reader discipline.*
