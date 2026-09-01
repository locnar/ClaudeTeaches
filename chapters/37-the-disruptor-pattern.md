# Part VI — Concurrency

# Chapter 37 — The Disruptor Pattern

> **Prerequisites:** Ch. 34 (the SPSC/SPMC ring buffer — the Disruptor's core), Ch. 35 (sequence publication — the Disruptor publishes via sequences), Ch. 33 (false sharing — padding the sequences is non-negotiable), Ch. 31 (shared-nothing pipeline — the Disruptor *is* the channel), Ch. 32 (busy-spin waiting), Ch. 23–24 (pre-allocated, no hot-path allocation).
>
> **Leads into:** Ch. 46 (warming — the ring stays warm), Ch. 50 (IPC — the Disruptor across processes via shared memory — Ch. 26), Ch. 71 (logging — async logger as a Disruptor consumer), Ch. 75–76 (capture/replay through the pipeline). Capstone of **Part VI** — it synthesizes Ch. 30–36.

---

## 37.1 Why it matters: a proven low-latency pipeline

The Disruptor is the synthesis of everything in Part VI into one battle-tested architecture: a **pre-allocated ring buffer** (Ch. 23, 34) that producers and consumers coordinate on through **sequence numbers** (Ch. 35), with **no locks** (Ch. 32), **no allocation on the hot path** (Ch. 23–24), **no reclamation problem** (the ring slots are reused, not freed — Ch. 36), **cache-line-padded** coordination state (Ch. 33), and **mechanical sympathy** — a design that works *with* the cache, the prefetcher, and the branch predictor rather than against them. Created by LMAX for their trading exchange, it processes millions of messages per second at single-digit-microsecond (and lower) latency on a single thread per stage, and it has become *the* reference pattern for low-latency message pipelines — in trading, logging (Ch. 71), and event-driven systems generally.

What makes the Disruptor more than "a ring buffer" is that it solves the problems a naive queue creates. A traditional queue (even a lock-free one) has each consumer *contend* with producers on shared head/tail state, allocates per message, and — for multi-stage pipelines — forces messages through a *chain* of queues, each a handoff with its own contention and cache miss. The Disruptor instead has **all stages share one ring**: the producer writes entries once into pre-allocated slots, and multiple consumers (and stages of consumers) read from the *same* ring at their own pace, coordinated by sequence numbers, with a **dependency graph** expressing "stage B can't process entry N until stage A has." This eliminates the inter-stage queues, the per-message allocation, and most of the contention — a producer publishes by advancing *its* sequence, a consumer waits for that sequence, and the only cross-core traffic is the sequence counters (padded — Ch. 33) and the data slots themselves. One data structure replaces a web of queues.

And it's built for **batching**, which is the secret to its throughput *and* its latency. When a consumer falls slightly behind (a burst — market open), it discovers on its next check that *many* entries are now available, and processes them all in one batch — amortizing the per-message coordination cost and, crucially, *catching up without falling further behind*, so latency recovers rather than spiraling. The same mechanism keeps the ring cache-warm (Ch. 46) and lets a slow downstream stage be the natural backpressure. For HFT the Disruptor is often the backbone of the tick-to-trade path: the feed handler publishes decoded messages into the ring, and the strategy, risk, and journaling stages consume from it — each single-threaded, pinned (Ch. 42), shared-nothing (Ch. 31), communicating only through sequences. This chapter explains the ring + sequence-barrier mental model (§37.2), measures the Disruptor against a queue (§37.3), details the sequence barriers / dependency graphs and consumer batching (§37.4), and — fittingly for the Part VI capstone — warns that its performance lives or dies on **sequence false sharing** (§37.5), the Ch. 33 bug in the structure built to avoid it.

## 37.2 Mental model: ring buffer + sequence barriers; mechanical sympathy

**The structure.** One pre-allocated ring of fixed slots (power-of-two — Ch. 28, 34), shared by a producer and one or more consumers, each tracking its position with a **sequence number** (a monotonically-increasing counter; the slot is `sequence & (size-1)`):

```
   ring (pre-allocated slots, reused — no alloc, no free):
        slot0  slot1  slot2  ...  slotN-1   (size = power of two)

   producer cursor ──► seq 1004 (last published entry)
   consumer A     ──► seq 1001 (has processed up to 1001; may process 1002..1004 next — a BATCH)
   consumer B     ──► seq  998 (slower; A doesn't wait for B unless the graph says so)

   a producer may write slot S only when S - size > slowest-consumer-sequence (don't overwrite unread)
   a consumer may read up to the producer cursor (and up to its dependencies' sequences)
```

- **Producer publishes by advancing its cursor.** It claims the next sequence, writes the entry into that slot, then publishes by **releasing** its cursor to that sequence (Ch. 30's release — like seqlock publication, Ch. 35). Before claiming, it checks it won't overwrite a slot the slowest consumer hasn't read (backpressure / wrap protection).
- **Consumers wait on sequences, not locks.** A consumer waits until the producer cursor (and any upstream dependency — §37.4.1) reaches the sequence it wants, then processes everything available up to that point — a **batch**. It advances *its own* sequence (release) so downstream stages/the producer can see its progress. No lock, no head/tail contention between consumers — each consumer owns its sequence.
- **Sequence barriers = the coordination.** A consumer's **sequence barrier** is "wait until these sequences (the producer's cursor, my dependencies') reach my target." This is acquire/release sequence publication (Ch. 35) generalized into a dependency graph (§37.4.1) — the only cross-core coordination, and it's a handful of atomic loads.

**Why it's fast — mechanical sympathy:**

- **Pre-allocated, reused slots → no allocation (Ch. 23), no reclamation (Ch. 36).** The ring's entries are constructed *once* at startup; the producer overwrites slots in place; nothing is allocated or freed on the hot path. This sidesteps the entire SMR problem (Ch. 36) — slots are reused by sequence, not freed.
- **Single-writer per sequence (Ch. 31, 35).** Each sequence has exactly one writer (the producer's cursor; each consumer's own sequence) — no write-write contention, tractable ordering.
- **Cache-friendly and prefetchable (Ch. 7–10).** Entries are contiguous in the ring; a consumer processing a batch streams through adjacent slots (sequential access, prefetcher-friendly — Ch. 10). The ring stays warm (Ch. 46).
- **Batching amortizes coordination and recovers latency (§37.4.2).** The killer property: coordination cost is paid per *batch*, not per *message*, and a consumer that falls behind catches up in bigger batches.
- **Busy-spin waiting (Ch. 32).** On dedicated cores, consumers **busy-spin** (with `pause`) waiting for their sequence — lowest latency, no blocking, no syscall (Ch. 32, 41). (Alternative wait strategies — blocking, yielding — trade latency for CPU when cores aren't dedicated.)

The model: **the Disruptor is one pre-allocated ring shared by single-writer producers and sequence-coordinated consumers; publication and waiting are acquire/release sequence operations (Ch. 35), slots are reused (no alloc/reclaim), coordination is batched, and the whole thing is laid out for the cache. It replaces a web of contended, allocating queues with one mechanically-sympathetic data structure.**

## 37.3 Measure it: Disruptor vs queue latency

Compare a Disruptor-style ring against a lock-free queue (Ch. 34) and a mutex queue for a producer→consumer handoff, focusing on **latency under load** and **batching's effect when the consumer falls behind**.

```cpp
// disruptor.cpp — minimal single-producer/single-consumer Disruptor-style ring with batching.
// Build: g++ -O2 -std=c++20 -march=native disruptor.cpp -o disruptor -pthread
// Run:  ./disruptor   (producer + consumer pinned to two cores)
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <vector>
#include <chrono>
#include <new>
#include <immintrin.h>

template <class T, std::size_t SIZE>
class Disruptor {
    static_assert((SIZE & (SIZE - 1)) == 0);
    std::vector<T> ring_{SIZE};
    alignas(std::hardware_destructive_interference_size) std::atomic<std::int64_t> cursor_{-1};   // producer
    alignas(std::hardware_destructive_interference_size) std::atomic<std::int64_t> consumed_{-1};  // consumer
public:
    void publish(const T& v) {
        std::int64_t next = cursor_.load(std::memory_order_relaxed) + 1;
        while (next - consumed_.load(std::memory_order_acquire) > (std::int64_t)SIZE)  // wrap protection
            _mm_pause();
        ring_[next & (SIZE - 1)] = v;
        cursor_.store(next, std::memory_order_release);                                // publish
    }
    // Consumer: process ALL available entries in one batch, return how many.
    template <class F> std::int64_t consume_batch(F&& fn) {
        std::int64_t last = consumed_.load(std::memory_order_relaxed);
        std::int64_t avail = cursor_.load(std::memory_order_acquire);                  // how far producer got
        for (std::int64_t s = last + 1; s <= avail; ++s) fn(ring_[s & (SIZE - 1)]);    // BATCH
        if (avail > last) consumed_.store(avail, std::memory_order_release);
        return avail - last;
    }
};

Disruptor<std::uint64_t, 4096> d;

int main() {
    constexpr long N = 200'000'000;
    auto t0 = std::chrono::steady_clock::now();
    std::thread prod([&]{ for (long i = 0; i < N; ++i) d.publish(i); });                  // (pin core A)
    std::uint64_t sink = 0; long got = 0; long maxbatch = 0;
    std::thread cons([&]{ while (got < N) { long n = d.consume_batch([&](std::uint64_t v){ sink += v; ++got; });
                                            if (n > maxbatch) maxbatch = n;
                                            if (n == 0) _mm_pause(); } });                 // (pin core B)
    prod.join(); cons.join();
    auto t1 = std::chrono::steady_clock::now();
    double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1-t0).count();
    std::printf("Disruptor: %.1f Mmsg/s  (%.2f ns/msg)  max-batch=%ld  sink=%llu\n",
                N / (ns/1e3), ns/N, maxbatch, (unsigned long long)sink);
    return 0;
}
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP), producer/consumer pinned to two cores, turbo off (illustrative):

```
   structure                       throughput        ns/msg     note
   Disruptor (batched, padded)     ~250-400 Mmsg/s   ~3-5 ns    batching amortizes coordination
   SPSC ring (Ch. 33, per-msg)     ~80-150 Mmsg/s    ~7-12 ns   one coordination per message
   std::mutex queue                ~10-20 Mmsg/s     ~60 ns     lock + contention (Ch. 31)

   under a burst (consumer briefly stalls, then catches up):
   Disruptor max-batch observed:   hundreds-thousands of entries in one batch  ← latency RECOVERS
   per-message queue:              processes one at a time → falls further behind
```

Read it: the **Disruptor's batching makes it markedly faster than even a good per-message SPSC ring** — because the cross-core coordination (the acquire load of the producer cursor) is paid once per *batch* of available entries, not once per message, and the consumer streams through contiguous slots (Ch. 10). Under a **burst**, the difference is qualitative: when the consumer falls behind, it finds a large batch available and processes it all at once — *catching up*, so latency recovers — whereas a strictly per-message structure processes one at a time and can fall further behind (latency spirals). That self-correcting batching is the Disruptor's defining latency property and exactly what you want at market open. The other lessons are the Part VI synthesis in one structure: **padded sequences (Ch. 33 — without which it's 3-5× slower, §37.5), power-of-two ring (Ch. 28), acquire/release publication (Ch. 30, 35), pre-allocated slots (Ch. 23), busy-spin wait (Ch. 32).** Against a mutex queue it's an order of magnitude faster; against a naive ring, batching wins.

## 37.4 Techniques

### 37.4.1 Sequence barriers and dependency graphs

The Disruptor's power for *multi-stage* pipelines is the **dependency graph** of sequence barriers — expressing "stage B processes entry N only after stage A has," without inter-stage queues:

- **One ring, multiple consumers at their own sequences.** Each consumer (stage) tracks its own sequence on the *same* ring. The producer publishes once; every consumer reads the same slots at its own pace. No copying between stages, no per-stage queue — just sequences.
- **Sequence barriers express dependencies.** A consumer's barrier waits on the sequences it depends on. Examples:
  - **Pipeline** (A → B → C): B waits on A's sequence (not just the producer's), C waits on B's. Each stage processes an entry only after its predecessor finished it. The producer can't overwrite a slot until the *last* stage has consumed it.
  - **Parallel fan-out** (A and B both consume independently): both wait only on the producer cursor — they process in parallel, no ordering between them.
  - **Diamond** (A → {B, C} → D): D waits on *both* B and C. Arbitrary DAGs of stages are expressible.
- **In trading.** A natural pipeline: feed-decode (producer) → {risk-check, strategy} (parallel) → order-publish (waits on both) → journal (Ch. 75). Each stage single-threaded, pinned (Ch. 42), shared-nothing (Ch. 31); the dependency graph encodes the required ordering with sequences alone — no locks, no queues between stages.
- **Wrap protection via the slowest consumer.** The producer must not overwrite a slot still unread by the *slowest* consumer in the graph (the "gating" sequence). It tracks the minimum consumer sequence and waits if it would lap them — natural backpressure: a slow stage throttles the producer rather than dropping data or allocating.
- **The single-writer-per-sequence discipline (Ch. 31, 35).** Every sequence has one writer; barriers are acquire-loads of others' sequences. This is what keeps the whole graph lock-free and the ordering tractable.

### 37.4.2 Batching at the consumer

Batching is the Disruptor's throughput-and-latency engine, and it's *automatic* — the consumer just processes everything available:

- **Process all available entries per check.** When a consumer checks its barrier, it gets the *highest* available sequence and processes *every* entry from its last position up to there in one go (the `consume_batch` of §37.3) — not one message per coordination. The cross-core sequence read, the wait-loop overhead, and any per-batch setup are amortized over the whole batch.
- **Self-correcting under load.** The batch size *grows* exactly when the consumer is behind (a burst): more entries have accumulated, so it catches up in one big batch instead of falling further behind (§37.3). At low load, batches are size-1 (lowest latency per message); at high load, batches grow (highest throughput) — the structure auto-tunes between latency and throughput. This is why the Disruptor's tail (Ch. 1) holds up under bursts where a per-message queue's spirals.
- **Batch at the producer too.** A producer can claim a *range* of sequences at once (publish many entries, then advance the cursor once) — amortizing the publish-side coordination. Useful when the producer has many entries ready (e.g. a UDP packet containing several market-data messages — Ch. 53).
- **Amortize downstream work.** A consumer can also batch its *own* work (e.g. one I/O write for a batch of journal entries — Ch. 75, one formatted flush for a batch of log lines — Ch. 71), not just the coordination. Batching composes through the pipeline.
- **Wait strategies (the latency/CPU knob).** How a consumer waits when no entries are available: **busy-spin** (`pause` — lowest latency, burns a core, for dedicated cores — Ch. 32, 41); **yielding**; **blocking** (a condition variable — saves CPU, higher latency, for non-dedicated cores). Choose per deployment: dedicated hot-path cores busy-spin; background stages may block. The wait strategy is the one knob that trades the Disruptor's latency against CPU use.

## 37.5 Pitfalls & anti-patterns: false sharing on sequences

- **Sequence false sharing (the defining Disruptor bug).** The producer cursor and each consumer sequence are hot atomics written by *different* cores; if any two share a cache line, they ping-pong (Ch. 33) and the Disruptor loses 3-5× (§37.3) — in the very structure built to be cache-friendly. **Pad every sequence to its own cache line** (`alignas(hardware_destructive_interference_size)`). The classic, and the original LMAX implementation pads obsessively for exactly this reason.
- **Allocating in the entry / on the hot path.** The whole point is pre-allocated, reused slots (Ch. 23). Storing a `std::string`/`std::vector`/pointer-to-heap *in* an entry (that gets allocated/freed per message) reintroduces the allocation tail. Entries should be fixed-size POD (or pre-allocated buffers reused by slot).
- **Wrong wait strategy for the deployment.** Busy-spinning on a *shared* (non-dedicated) core wastes CPU and starves other work (Ch. 32, 41); blocking on a *dedicated* hot-path core adds latency for no reason. Match the wait strategy to whether the core is dedicated.
- **Forgetting wrap protection / overwriting unread slots.** The producer must gate on the slowest consumer; skipping that overwrites entries a consumer hasn't read (data loss/corruption). The gating sequence and backpressure are not optional.
- **Mismatched ring size.** Too small → the producer constantly waits on consumers (no headroom for bursts); too large → wasted memory and worse cache behavior (the working set exceeds cache — Ch. 7). Size to the burst headroom you need, power-of-two (Ch. 28), and keep the *active* working set cache-resident.
- **Memory-ordering errors in the sequence protocol (Ch. 30).** Publishing the entry data with the wrong ordering relative to the cursor release (or reading with the wrong acquire) is a race that works on x86 and fails on ARM (Ch. 30, 35). Use a vetted Disruptor implementation; validate ordering on ARM / model-check (Ch. 40).
- **Reinventing it / over-engineering.** The Disruptor is subtle (sequences, barriers, wrap, ordering, padding); for most needs use a vetted implementation (the LMAX Disruptor, `disruptor-cpp`, or the equivalent in your stack), or — if a simple SPSC ring (Ch. 34) suffices (single producer, single consumer, no multi-stage graph) — use *that*, simpler. Reach for the full Disruptor when you need the **multi-stage dependency graph** and **batching** it uniquely provides.
- **Treating it as a general queue.** The Disruptor shines for a *known, fixed* pipeline of stages on a shared ring; it's not a drop-in for arbitrary dynamic many-to-many messaging. Match it to the pipeline shape it's designed for.

## 37.6 Exercises & checklist

**Exercises**

1. **Disruptor vs ring vs mutex.** Build `disruptor.cpp` (pin producer/consumer); compare throughput/latency against the SPSC ring (Ch. 34) and a `std::mutex` queue. Confirm the Disruptor's batching advantage. Log the max batch size — when does it grow?
2. **Burst recovery.** Make the consumer stall periodically (simulate a slow downstream); observe the batch size spike and latency *recover* as it catches up. Compare to a per-message queue that processes one at a time — does it fall behind? (§37.3-§37.4.2)
3. **Sequence false sharing.** Put the producer cursor and consumer sequence on the *same* cache line (remove `alignas`); measure the slowdown and HITM (`perf c2c` — Ch. 33). Re-pad; confirm recovery. Quantify the §37.5 bug.
4. **Multi-stage graph.** Extend to a pipeline (producer → A → B) where B's barrier waits on A's sequence, and a parallel fan-out (A and B both consume the producer independently). Verify the ordering guarantees and wrap protection (§37.4.1).
5. **Wait strategies.** Implement busy-spin, yield, and blocking wait strategies; measure latency *and* CPU use for each on a dedicated vs shared core. Confirm the latency/CPU trade-off (§37.4.2, Ch. 32).

**Checklist — the Disruptor pattern**

- [ ] Every **sequence** (producer cursor, each consumer) is **padded to its own cache line** (Ch. 33) — the defining performance requirement (§37.5).
- [ ] The ring is **pre-allocated**, **power-of-two** sized (Ch. 28, `& (size-1)`), entries are **fixed-size POD reused in place** — **no hot-path allocation** (Ch. 23) and **no reclamation** (Ch. 36).
- [ ] Publication/waiting use correct **acquire/release sequence ordering** (Ch. 30, 35), **validated on ARM / model-checked** (Ch. 40) — single-writer per sequence (Ch. 31).
- [ ] **Wrap protection** gates the producer on the **slowest consumer** (natural backpressure); the ring is sized for **burst headroom** while keeping the working set cache-resident.
- [ ] Consumers **batch** (process all available entries per check) — coordination amortized, latency self-corrects under bursts (§37.4.2); producer batches where it has many ready.
- [ ] Multi-stage pipelines use the **sequence-barrier dependency graph** (no inter-stage queues); each stage is **single-threaded, pinned** (Ch. 42), shared-nothing (Ch. 31).
- [ ] The **wait strategy** matches the deployment — **busy-spin** on dedicated cores (Ch. 32, 41), blocking/yield on shared cores.
- [ ] I used a **vetted implementation** (or a simple SPSC ring — Ch. 34 — if no multi-stage graph is needed), not a hand-rolled Disruptor, for production.

## 37.7 References

- M. Thompson, D. Farley, M. Barker, P. Gee, A. Stewart, *"Disruptor: High performance alternative to bounded queues for exchanging data between concurrent threads"* (the LMAX technical paper) — the definitive description (the basis of this whole chapter).
- The LMAX Disruptor project (and `disruptor-cpp` ports) — the reference implementation, padding discipline, sequence barriers, and wait strategies (§37.4-§37.5).
- M. Thompson, *"Mechanical Sympathy"* blog and talks — the cache-aware design philosophy behind the Disruptor (ties Ch. 7–10, 33).
- Ch. 34 references (Vyukov, Michael-Scott) — the ring-buffer/queue foundations the Disruptor builds on and surpasses.
- The Aeron messaging system (Thompson et al.) — the Disruptor's ideas extended to networked/IPC messaging (ties Ch. 26, 50).

## 37.8 Additional Reading

- M. Fowler, *"The LMAX Architecture"* — a clear architectural overview of the Disruptor in a trading system context.
- Talks/posts comparing the Disruptor to lock-free queues, and the LMAX exchange case study — real-world latency results.
- Ch. 34 (*Lock-Free*) — the ring core; Ch. 35 (*Seqlocks*) — sequence publication; Ch. 33 (*False Sharing*) — sequence padding; Ch. 31 (*Foundations*) — the shared-nothing pipeline; Ch. 32/41 (*Spinlocks/Context Switching*) — busy-spin waiting; Ch. 46 (*Warming*) — keeping the ring warm; Ch. 71 (*Logging*) — async logger as a Disruptor; Ch. 75 (*Capture*) — journaling stage; Ch. 50 (*IPC*) — Disruptor across processes.
- **Appendix E** — cross-core handoff latency; **Appendix F** — Disruptor/sequence-barrier/mechanical-sympathy glossary.

---

*Next: Ch. 38 — Coroutines & Async Models, opening the back half of Part VI's concurrency story from a different angle: C++20 coroutines, stackful vs stackless, event loops, and async I/O without thread-per-connection — and where they fit (and don't) on the latency hot path.*
