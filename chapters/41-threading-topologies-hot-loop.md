# Part VI — Concurrency

# Chapter 41 — Threading Topologies & the Hot Loop

> **Prerequisites:** Ch. 40 (the Disruptor — claim/publish mechanics, sequence barriers and batching, which this chapter generalizes into an ownership protocol), Ch. 37 (the SPSC ring that carries every hop here), Ch. 34 §34.4.1 (shared-nothing partitioning — the two topologies named there as bullets), Ch. 36 (false sharing — the per-message line transfer *is* the hop), Ch. 33 (acquire/release — what a slot handoff is made of), Ch. 17 (`rdtsc` — the loop's clock), Ch. 7 and Appendix E (cache-line transfer latency — the unit of the cost model).
>
> **Leads into:** Ch. 42 (flow control — what the loop does when a consumer is slower than its input), Ch. 47 (timers — the deadline queue as one more polled source of this chapter's loop), Ch. 46 (context switching — why every stage here busy-polls and never blocks), Ch. 48 and Ch. 16 (pinning stages and shards onto cores and NUMA nodes), Ch. 57 (the same slot protocol across processes over shared memory), Ch. 89 (process-per-role topology — this chapter's decision made at the process level), Ch. 96 (the sequencer — a topology with one total order). Ch. 40 showed *how* to hand a message between cores without a lock; this chapter decides *whether* to hand it over at all, how many threads a tick should touch, in what order one thread should look at its inputs, and who owns the bytes at every instant in between.

---

## 41.1 Why it matters: the number of threads a tick touches is the latency floor

Before any micro-optimization in Parts II–V can matter, one design decision has already set the floor under tick-to-trade: **how many threads a message passes through on its way from the wire to the order, and how those threads talk.** Every hand-off between two cores costs at least one cache-line transfer (Ch. 7, 36 — ~50–70 ns on the reference machine, Appendix E's HITM row), usually two (the ring's index and the slot), plus whatever the receiving core was doing when the line arrived; three hops is a quarter of a microsecond on a good day, and a stage that is momentarily slower than its input turns a hop into a queue (Ch. 1 §1.2.1). The book has asserted the right answer four times — "single-threaded run-to-completion on a pinned core" (Ch. 70 §70.4.3), "shard by symbol" and "pipeline of single-threaded stages" (Ch. 34 §34.4.1), "one hot thread per core" (Ch. 46 §46.4.1) — and measured the hop twice (Ch. 44 §44.3, Ch. 65 §65.3), but never developed the *decision*: when does splitting a path across cores pay back its hops, which shape of split (stages in series or shards in parallel), and what does the choice do to the p99.9 rather than the throughput?

Two consequences follow from that decision and are usually left implicit. First, the thread that does the work is a **scheduler**: a hot thread with one input is a toy; the real one polls a feed-A ring, a feed-B ring, the order-acknowledgement channel, a control/config ring (Ch. 88) and a deadline queue (Ch. 47) in every iteration, and the *order* and *batch caps* of that polling decide whether a market-data burst can starve the ack that the strategy is waiting for. Second, a zero-copy pipeline (Ch. 40, 56, 57) has **no copies to delimit ownership**: the bytes of message *N* live in one slot that the producer writes, two stages read and the producer eventually overwrites, so the question "who may touch slot *N* right now" has to be answered by a protocol — a state machine over sequences — or it is answered by a data race that appears once a week at the open. This chapter builds the cost model (§41.2.2), measures the three topologies and the hop itself (§41.3), develops the many-source hot loop (§41.4.2), maps the result onto cores and NUMA nodes (§41.4.3) and writes the slot lifecycle down as a protocol with a test (§41.4.4–§41.4.5). Ch. 42 then takes the one question this chapter defers — what the loop does when a consumer cannot keep up.

## 41.2 Mental model

### 41.2.1 Three topologies

```
   A. run-to-completion            B. staged pipeline                     C. shard fan-out
   ───────────────────────         ─────────────────────────────────      ─────────────────────────────────
   core 1: RX → decode → book →    core 1: RX → decode ─ring─► core 2:    core 1: RX → decode → dispatch(sym)
           strategy → risk →               book → strategy ─ring─►             ─ring─► core 2: shard A (syms 0,2,4…)
           encode → TX                     core 3: risk → encode → TX          ─ring─► core 3: shard B (syms 1,3,5…)
   hops: 0                         hops: 2 (serial)                       hops: 1 (parallel), + a merge if TX is shared
   latency = Σ work                latency = Σ work + 2·hop + queueing     latency = work + hop (+ merge hop)
   throughput = 1 / Σ work         throughput = 1 / max(stage)            throughput = shards / work (if balanced)
   p99.9: one core's noise         p99.9: three cores' noise, compounded  p99.9: one shard's noise (uncorrelated)
```

- **Run-to-completion (A).** One thread takes a message from its source and runs every stage to the TX doorbell before it looks at the next input. No hops, no queues, no cross-core coherence traffic on the message, the whole path in one core's L1/L2 and one branch-predictor history (Ch. 11–13). Latency is the sum of the work; throughput is its reciprocal; the tail is one core's noise (Ch. 52). It is the lowest-latency topology *whenever the work fits on one core*, and most tick-to-trade paths do (Ch. 103 §103.3 budgets ~800 ns of software work for the whole path).
- **Staged pipeline (B).** Stages in series on different cores, each single-threaded, joined by SPSC rings (Ch. 37) or Disruptor barriers (Ch. 40). It raises *throughput* (the path runs at the pace of its slowest stage, not the sum) and isolates *faults and cache footprints* (a stage's working set stays on its core); it costs one hop per boundary on *every* message's latency, and — the part designers forget — it adds queueing whenever any stage is momentarily slower than its input (§41.3's microburst rows).
- **Shard fan-out (C).** Parallel copies of the whole path, partitioned by a key that makes them independent (symbol, venue, port). One hop to dispatch; the shards never share state, so there is no coherence traffic between them (Ch. 34 §34.4.1); throughput scales with shards; the tail is one shard's noise. It is the right answer when the *aggregate* rate exceeds one core, as long as the key really partitions the work (a strategy that trades spreads across shards has just reinvented the pipeline, with a merge).

The hybrid every real system ends up with: **run-to-completion within a shard, shards by symbol, and a pipeline only where a stage is genuinely too heavy or must be a separate fault domain** (Ch. 89's gateway and risk processes). The rest of this chapter is about making that sentence quantitative.

### 41.2.2 The hop-cost model

A hop between two cores is not "a queue operation"; it is **coherence traffic plus a wait**:

```
   producer core P                                 consumer core C
   ────────────────                                ────────────────
   write slot[N]           (line S: P now owns it)
   store head = N+1        (line H: P owns it)      load head …… miss → request line H from P    ~50–70 ns (Appendix E, HITM)
                                                    load slot[N] …… miss → request line S from P ~50–70 ns (the data line; several if the message is large)
   (next push) load tail … miss → line T from C     store tail = N+1 (line T: C now owns it)       — the producer pays this one on ITS next push
```

So the minimum one-way hop is **two line transfers** (index + data) on the consumer's critical path, and a third on the producer's, unless the ring caches indices (Ch. 37) and the consumer batches (Ch. 40 §40.4.2) so the index lines move once per *batch* rather than once per message. On the reference machine that is ~100–150 ns one-way with both cores spinning (Ch. 89 §89.2.1's figure), ~200–300 ns on the VM of §41.3 where a line transfer is ~140 ns, and *microseconds* if the consumer has blocked and must be woken (Ch. 46 §46.3 — a futex wake-up is ~5–10 µs plus the C-state exit). Add the message's own lines (a 64-byte message is one; a 512-byte decoded book update is eight, fetched by the hardware prefetcher if the slot is contiguous — Ch. 10) and the consumer's cold-start on that message (its branch history and L1 do not know this message's path — Ch. 53).

**The rule for when a split pays.** Let *W* be the work per message on the path, *h* the one-way hop cost, *λ* the arrival rate. A staged split of *W* into *W₁ + W₂* improves *latency* only if nothing else changes — it never does: latency goes from *W* to *W + h + q*, where *q* is the queueing delay at the second stage, which is zero only while *W₂ < 1/λ* for *every* inter-arrival, i.e. never during a burst. A split pays for latency in exactly one case: **when a stage is being removed from the critical path** (the journal writer, the logger, telemetry — Ch. 84, 86, 94 — consume a copy and are not waited for). For throughput, the pipeline pays when *max(W₁, W₂) + h_amortized < W*, i.e. when the stages are heavy and the hop is batched; the shard pays when *λ·W > 1* and the key partitions. Everything else is a hop added for architectural comfort, and §41.3 prices it.

### 41.2.3 The hot loop is a scheduler

The hot thread's loop is not `while (true) { msg = ring.pop(); handle(msg); }`. It is a **polling scheduler over heterogeneous sources** with a priority and a fairness policy:

```
   for (;;) {
       n = drain(ack_channel,  cap=∞)     // venue acks/fills: the strategy is WAITING on these → first, unbounded
       n += drain(feed_A, feed_B, cap=K)  // market data: highest volume → bounded batch so it cannot starve the rest
       n += expire(deadline_queue, now)   // timers (Ch. 47): a polled input, fires become events like any other
       n += drain(control_ring, cap=1)    // config/limits/kill (Ch. 88, 90): rare, must not be delayed by a burst
       if (n == 0) idle_hook();           // pause / shadow traffic (Ch. 53) / telemetry flush — NEVER a syscall
   }
```

Three things this shape fixes: **priority** (an ack that lets a strategy re-quote is worth more than the 400th book update of a burst, so it is polled first and never capped), **starvation** (a per-source batch cap *K* bounds how long any one source can hold the loop — K × per-message cost is the worst-case delay a control message or a timer sees), and **determinism** (the polling order is a fixed function of state, so the deterministic core of Ch. 89 §89.2.2 sees the same input sequence on replay as long as the *arrival order* is journaled — Ch. 94, 96). The loop's own cost is the sum of the empty-poll checks, measured in §41.3: an acquire-load of a head index per source, ~0.5 ns each when the line is local — so a loop that polls eight sources spends ~2 ns per iteration finding out that nothing happened, and that is why "poll everything every iteration" is affordable.

### 41.2.4 The slot lifecycle is a protocol

In a zero-copy pipeline the message is never copied, so **ownership of a slot is a function of the sequences** (Ch. 40 §40.2), not of any object. Written down:

```
   slot state      owner / who may touch it                    transition                                   granted by
   ─────────────   ─────────────────────────────────────────   ───────────────────────────────────────────   ─────────────────────────────────────────
   FREE            producer (may claim)                        claim: seq N assigned to this slot            N − size ≤ min(consumer sequences)  (wrap gate)
   CLAIMED         producer only (writing)                     publish: cursor := N  (release store)         producer finished writing
   PUBLISHED       every consumer whose barrier ≤ N (reading)  consumed-by-S: S.sequence := N (release)      stage S finished with the slot
   CONSUMED-by-all last stage's sequence ≥ N                   → FREE (producer may claim N + size)          the gating (slowest) sequence
```

The rights are exactly the Disruptor's rules, but stating them as a protocol makes three things checkable that are usually folklore: (1) **a stage's view of slot *N* is valid only until its own sequence passes *N*** — a pointer or `std::span` into the slot that outlives the stage's sequence advance is a use-after-free with no allocator to catch it; (2) **publishing is the only point at which the producer's writes become visible** — a consumer that reads a slot it has not been granted (because it peeked past its barrier) reads torn data, and (3) **the same table holds across processes** (Ch. 57): the sequences live in the shared segment and the "stage" is a process, so a crashed consumer that never advances its sequence stalls the producer at the wrap gate — which is a flow-control decision (Ch. 42), not a bug in the protocol. §41.4.4 turns the table into types the compiler enforces, and §41.4.5 into a test.

## 41.3 Measure it: the hop, the three topologies, and the loop's own tax

*Toolchain and machine: Appendix L.3. The listings below were built with GCC 13.3 (`-O2 -std=c++20 -march=native -pthread`) and run on a 4-vCPU Intel Xeon VM (TSC 2.70 GHz, no core isolation, visible hypervisor steal — Ch. 52). On this host a bare cross-core cache-line transfer is ~140 ns (the ping-pong row) against the reference machine's ~50–70 ns, so every hop figure below is roughly 2–3× the bare-metal number; the ratios and the shape of the distributions are what transfers. Figures are labelled as VM-measured.*

**The harness.** Four stages of calibrated integer work (decode ≈ 7 ns, book update ≈ 12 ns, risk ≈ 5 ns, encode ≈ 6 ns; ~30 ns in total, standing in for Ch. 103 §103.3's software stages at a tenth of their real cost so that the hop is not drowned), a 64 K-slot slab of 64-byte messages, SPSC rings carrying *slot indices* (the zero-copy handoff of §41.2.4), arrivals paced 1 µs apart or in microbursts of 32 back-to-back messages every 40 µs (the same mean rate), and every latency measured on the **producer's own TSC** by sending the finished slot back through a return ring — so no cross-core TSC comparison is involved (the VM's vCPU TSCs differ by ~100–300 ns, which would otherwise be the whole signal). The run-to-completion variant does the stages inline and stamps the end on the same core; the staged and sharded variants therefore carry one *extra* return hop, which the hop-count columns state.

```cpp
// topo.cpp (abridged — the three runners share the pattern; the full file also has the shm and ping-pong rows).
// Build: g++ -O2 -std=c++20 -march=native -pthread topo.cpp -o topo
struct alignas(64) Msg { std::uint64_t t0; std::uint32_t seq, sym; std::int64_t px; std::int32_t qty; std::uint32_t pad; std::uint64_t out; };
constexpr std::uint32_t SLOTS = 1 << 16;  static Msg* slab;              // the zero-copy slab: rings carry indices into it
static Spsc<SLOTS> r1, r2, back;                                           // Ch. 37 SPSC of 64-bit words, padded head/tail

static void run_staged(bool burst) {                                       // B: [gen+decode] -> r1 -> [book] -> r2 -> [risk+encode] -> back
    std::thread t2([&] { pin(2); std::uint64_t v; for (;;) { if (r1.pop(v)) { if (v == ~0ull) { while (!r2.push(v)) {} break; } bookupd(slab[v]); while (!r2.push(v)) _mm_pause(); } else _mm_pause(); } });
    std::thread t3([&] { pin(3); std::uint64_t v; for (;;) { if (r2.pop(v)) { if (v == ~0ull) break; Msg& m = slab[v]; risk(m); encode(m); while (!back.push(v)) _mm_pause(); } else _mm_pause(); } });
    pin(1); std::uint64_t next = rdtsc() + 1000000;
    for (std::uint32_t i = 0; i < N; ++i) {
        wait_arrival(i, next, burst);                                      // paced 1 µs, or 32-message microbursts every 40 µs
        std::uint64_t t = rdtsc(); gen(i, t); Msg& m = slab[i & (SLOTS - 1)]; decode(m);
        while (!r1.push(i & (SLOTS - 1))) _mm_pause();                     // hand over the SLOT INDEX, not the bytes
        std::uint64_t v; while (back.pop(v)) lat.push_back(rdtsc() - slab[v].t0);   // round trip on the producer's clock
    }
    while (!r1.push(~0ull)) {} t2.join(); t3.join(); /* drain back; print percentiles */
}
// run_rtc(): same loop, stages inline, lat.push_back(rdtsc() - m.t0) on the same core.
// run_shard(): dispatch on (sym & 1) to r1 or r3; each shard does book+risk+encode and has ITS OWN return ring (SPSC = one writer).
```

**Results (VM-measured; ns unless stated).** The hop first, isolated, as the RTT through *k* forwarding stages on their own cores (`hops.cpp`: push an index, each stage pops and re-pushes, the producer measures the round trip):

```
cache-line ping-pong RTT (two spinning threads, one line each way)   p50   286    p99    453    p99.9   564
1 forwarding stage  = 2 ring hops  RTT                                p50   887    p99  1 106    p99.9  1 507
2 forwarding stages = 3 ring hops  RTT                                p50 1 316    p99  1 604    p99.9  7 916
one hop out and back across PROCESSES over a MAP_SHARED ring pair     p50   867    p99    937    p99.9  2 287
⇒ ≈ 430–480 ns per ring hop on this host ≈ 3 line transfers (head, slot, tail) at ~140 ns; the thread/process boundary costs nothing extra
```

Then the topologies at the same mean rate (200 000 messages each):

```
                                                 p50        p90        p99        p99.9      p99.99
A run-to-completion, paced 1 µs                   35         36         47         177        459      (0 hops; 30 ns of work)
B staged, 3 cores, paced                       2 017      2 037      6 799    155 761    237 849      (2 hops + 1 return hop)
C 2 shards, paced                              1 021      1 160      2 262     13 383     30 022      (1 hop + 1 return hop)
A run-to-completion, microbursts of 32            36         46         49         208        453
B staged, microbursts of 32                    3 963     34 993     38 985     45 025     54 863      ← queueing: the burst meets per-message hops
C 2 shards, microbursts of 32                  1 209      2 008     29 968     40 603    107 380
polling k EMPTY sources per loop iteration:  k=1 0.4 ns   k=2 1.0   k=4 1.2   k=8 2.2   k=16 8.8 ns
```

Reading it:

- **The hop is the floor.** 30 ns of work becomes 2 µs through two stages — the work is 1.5 % of the latency; the rest is three ring hops at ~450 ns each on this host (≈ 100–150 ns each on the reference machine, so ~400 ns there). The shard variant pays half as many hops and shows half the latency. Nothing in Parts II–V can buy back a hop that the topology spent.
- **A pipeline turns a burst into a queue.** With arrivals 1 µs apart the staged p50 is "work + hops"; with 32 messages back-to-back the p90 is **35 µs**: each message's hand-off costs the consumer a line transfer, so a stage that could do its 12 ns of work at 80 M msg/s actually drains the burst at ~2 M msg/s and the 32nd message waits for the other 31. This is precisely Ch. 40 §40.4.2's argument for batching at the consumer (one acquire of the cursor, then *all* available slots) and Ch. 37's for cached indices — the SPSC here does neither, deliberately, so the per-message hop cost is visible. Run-to-completion has no such term: its microburst distribution is identical to its paced one.
- **The tail compounds across cores.** The staged p99.9 is 156 µs against run-to-completion's 177 *ns*: on a non-isolated host, three cores each take hypervisor steal and interrupts, and a message's latency is the *sum* of what its three cores suffered. On the reference machine with `isolcpus`/`nohz_full` (Ch. 51–52) the absolute tails collapse, but the compounding rule stands: *k* stages see *k* cores' noise.
- **The many-source loop is free.** Checking eight empty rings costs ~2 ns; sixteen cost ~9 ns (the head lines no longer all sit in L1). The order and caps of §41.2.3 cost nothing measurable; what they buy — bounded delay for acks and control messages — is §41.4.2.

**What transfers to the reference machine.** Divide the hop rows by ~2.5–3 (line transfer ~50–70 ns vs ~140): ~130–160 ns per unbatched ring hop, ~400 ns for the two-hop pipeline's round trip, and run-to-completion unchanged at ~30 ns of work — which is why Ch. 103 §103.3 can budget the whole software path at ~800 ns only in a single-process build and adds "~3 shared-memory hops at ~50–150 ns each" for Ch. 89's process-per-role variant. Re-derive the absolutes on isolated cores before quoting them; the ratios are the lesson.

## 41.4 Techniques

### 41.4.1 Choosing the topology: a decision procedure

1. **Start from run-to-completion on one isolated core per shard.** Measure the path's work *W* with the harness of §41.3 (or Ch. 85's in-band stamps) at the realistic rate *λ* from a replayed open (Ch. 94). If *λ·W* < 0.5 on the heaviest second of the day, stop: one core per shard, zero hops. Headroom matters more than average utilization (Ch. 1 §1.2.1 — queueing delay explodes past ~70 %).
2. **If one core is not enough, shard before you stage.** Find the key that makes shards independent (symbol → book, strategy, orders all keyed by symbol; venue → session). Sharding costs one dispatch hop and scales linearly; staging costs a hop per boundary and scales to the slowest stage. Verify independence by grepping for *every* cross-shard read (a consolidated BBO, a portfolio-level risk limit): each is a merge hop or a Ch. 38 seqlock-published snapshot.
3. **Stage only for the two reasons that pay.** (a) A stage is off the critical path — journal, logger, telemetry, GUI (Ch. 84, 86, 94) — so its hop costs the *producer* one release-store and the consumer's latency is irrelevant; always do this. (b) A stage must be a separate **fault or trust domain** — the order gateway and the pre-trade risk gate (Ch. 89 §89.2.1, Ch. 90) — and you have measured that its hop (~100–150 ns over a shm ring, Ch. 57) fits the budget; then pay it knowingly and batch the hop (Ch. 40 §40.4.2) so a burst does not queue.
4. **Never stage for "clean separation".** A decode thread feeding a book thread feeding a strategy thread is three times the hops for the same work and compounds three cores' noise (§41.3). Separation of concerns is a *source-code* property (Ch. 19's policies, Ch. 89's pure core); it does not need a thread.
5. **Write the topology down as data** — a table of {stage/shard, core, NUMA node, input rings, output rings, polling order} loaded at startup (Ch. 88) — and have the supervisor (Ch. 89 §89.4.3) verify the pins against `thread_siblings_list` (Ch. 49). A topology that lives in a dozen `pin(cpu)` calls drifts.

### 41.4.2 The many-source hot loop: polling order, batch caps, the timer queue

The loop of §41.2.3 as code, with the properties the design needs made explicit:

```cpp
// hotloop.hpp — a polling scheduler over heterogeneous sources. Every source is an SPSC/Disruptor-style ring (Ch. 37, 40)
// or the deadline queue (Ch. 47); draining is bounded per source so one source cannot monopolize an iteration.
template <class Src, class F> inline std::uint32_t drain(Src& s, std::uint32_t cap, F&& f) {
    std::uint32_t n = 0; typename Src::value_type v;
    while (n < cap && s.pop(v)) { f(v); ++n; }                 // pop = one acquire-load of the head + the slot read
    return n;
}
struct HotLoop {
    AckRing& acks; FeedRing& feed_a; FeedRing& feed_b; ControlRing& control; DeadlineQueue& timers;   // Ch. 47
    Core& core;                                                                                       // the deterministic core (Ch. 89 §89.2.2)
    static constexpr std::uint32_t FEED_CAP = 64;               // worst-case delay a lower-priority source sees ≈ FEED_CAP × per-message cost
    [[gnu::noinline]] void run(std::atomic<bool>& stop) {
        while (!stop.load(std::memory_order_relaxed)) {
            std::uint64_t now = rdtsc();                       // ONE clock read per iteration; time is an input (Ch. 89), stamped here
            std::uint32_t n = drain(acks,    ~0u,     [&](auto& m) { core.apply(now, m); });   // 1. acks/fills: the strategy is waiting; unbounded
            n += drain(feed_a, FEED_CAP, [&](auto& m) { core.apply(now, m); });                 // 2. market data, bounded …
            n += drain(feed_b, FEED_CAP, [&](auto& m) { core.apply(now, m); });                 //    … per line (A/B arbitration inside apply — Ch. 61)
            n += timers.expire(now, [&](auto& t) { core.apply(now, t); });                      // 3. deadlines: polled, fire as events (Ch. 47)
            n += drain(control, 1,        [&](auto& m) { core.apply(now, m); });                // 4. config / limits / kill: rare, never starved
            if (n == 0) idle();                                 // 5. pause; shadow traffic (Ch. 53); flush telemetry (Ch. 86) — never a syscall
            else core.flush_outputs();                          // TX doorbells for everything this iteration produced (batched — Ch. 67)
        }
    }
};
```

- **Order = priority, caps = fairness.** Acks first and uncapped (the venue's reply unblocks the strategy's next decision and carries the position change the risk gate needs — Ch. 90, 91); market data bounded so that a 10 000-message open does not hold the loop for 10 000 × 80 ns = 800 µs before a kill command is seen; control last and capped at one (it is rare, but when it is a kill-switch it must be seen within one iteration — a bound of `FEED_CAP × cost` ≈ 5 µs here, which §41.5 says to measure and assert).
- **One clock read per iteration, stamped onto every event.** `rdtsc` costs ~20–40 cycles (Ch. 17); reading it per message is affordable but unnecessary and, worse, makes the core's inputs depend on *when* it ran. Read once, pass `now` into `apply`, and journal it with the event (Ch. 94, 96) — the deterministic core never reads a clock (Ch. 89 §89.2.2).
- **The deadline queue is just another source.** Ch. 47 builds it; here it is `expire(now, f)`, bounded by its own per-tick work, producing events the core applies like any message. A timer fire that is *not* an event in the same ordered stream cannot be replayed (Ch. 96 §96.2.2).
- **Idle is not free time.** With nothing to do, the loop runs the Ch. 53 warming hook (touch the book's hot levels, run a shadow decode) and flushes telemetry (Ch. 86) — bounded work that keeps the next real message fast. It never blocks: `epoll_wait(…, 0)` is a syscall, `sleep_for` is a context switch (Ch. 46 §46.5).
- **Batching the outputs.** A loop iteration that produced three orders rings the TX doorbell once (Ch. 67, 74 §74.2) — the output side of the same amortization the input side gets from draining.

### 41.4.3 Mapping stages and shards onto cores and NUMA nodes

- **One hot thread per isolated core, SMT sibling empty** (Ch. 46 §46.4.1, Ch. 49): a stage or shard that shares a core with anything pays that thing's cache footprint and scheduling noise. Pin with `sched_setaffinity` from the topology table (Ch. 48), verify with `/proc/<pid>/task/*/stat` field 39.
- **Keep a hop's two ends on one L3 and one NUMA node** (Ch. 16): a cross-socket line transfer is ~2× a same-socket one (Appendix E), and the ring's memory should be allocated on the *consumer's* node (the consumer misses on it; the producer's write-allocate is a one-time cost — Ch. 16 §16.4). The NIC's RX core and the decode stage sit on the NIC's node (Ch. 64 DDIO lands the packet in that L3).
- **Shards do not share lines, by construction and by check.** Each shard's book, order table and limits live in its own arena (Ch. 27) aligned to the cache line; the only shared memory is the dispatch ring and read-mostly published snapshots (Ch. 38). `perf c2c` (Ch. 36 §36.3, Appendix I) on a replayed open must show zero HITM between shard cores.
- **Housekeeping on the other side of the box.** Supervisor, telemetry scrapers, loggers' flush threads, timers that sleep (Ch. 47 §47.4.5) go on the housekeeping cores of Ch. 51's layout, and their rings with the hot threads are one-directional (hot thread produces, housekeeping consumes) so the hot side never waits on them (Ch. 42).

### 41.4.4 The slot-ownership protocol as types

The table of §41.2.4 becomes enforceable when a slot is reachable only through a **move-only handle that exists for exactly the interval in which the holder has the right**:

```cpp
// slotproto.hpp — ownership of a zero-copy slot as a move-only, [[nodiscard]] handle (Ch. 40's claim/publish/consume as types).
#include <atomic>
#include <cstdint>
#include <span>
#include <utility>

template <class T, std::size_t SIZE> struct Ring {
    static_assert((SIZE & (SIZE - 1)) == 0);
    alignas(64) std::atomic<std::uint64_t> cursor{0};      // producer: last published sequence + 1
    alignas(64) std::atomic<std::uint64_t> gate{0};        // slowest consumer's sequence (the wrap gate) — written by the last stage
    alignas(64) T slots[SIZE];

    // CLAIMED → the producer may write; publishing on destruction unless released explicitly. Non-copyable, non-movable-out-of-scope by accident.
    class [[nodiscard]] WriteSlot {
        Ring* r; std::uint64_t seq;
    public:
        WriteSlot(Ring* ring, std::uint64_t s) : r(ring), seq(s) {}
        WriteSlot(WriteSlot&& o) noexcept : r(std::exchange(o.r, nullptr)), seq(o.seq) {}
        WriteSlot(const WriteSlot&) = delete; WriteSlot& operator=(const WriteSlot&) = delete;
        T& operator*() { return r->slots[seq & (SIZE - 1)]; }
        void publish() && { r->cursor.store(seq + 1, std::memory_order_release); r = nullptr; }   // the ONLY way the bytes become visible
        ~WriteSlot() { if (r) std::move(*this).publish(); }                                       // forgetting to publish is not possible
    };
    // PUBLISHED → a consumer's read-only view, valid until it advances its sequence past `seq`.
    class [[nodiscard]] ReadView {
        const T* p; std::uint64_t seq;
    public:
        ReadView(const T* ptr, std::uint64_t s) : p(ptr), seq(s) {}
        const T& operator*() const { return *p; }
        std::span<const std::byte> bytes() const { return std::as_bytes(std::span<const T, 1>(p, 1)); }
        std::uint64_t sequence() const { return seq; }
    };
    bool try_claim(std::uint64_t seq, WriteSlot& out) {       // FREE → CLAIMED, granted by the wrap gate
        if (seq - gate.load(std::memory_order_acquire) >= SIZE) return false;
        out = WriteSlot(this, seq); return true;
    }
    template <class F> std::uint64_t consume(std::uint64_t& my_seq, F&& f) {   // PUBLISHED → consumed-by-me, in a batch (Ch. 40 §40.4.2)
        std::uint64_t avail = cursor.load(std::memory_order_acquire), n = 0;
        for (; my_seq < avail; ++my_seq, ++n) f(ReadView{&slots[my_seq & (SIZE - 1)], my_seq});   // the view dies at the end of f: it cannot outlive the grant
        return n;
    }
    void release_through(std::uint64_t seq) { gate.store(seq, std::memory_order_release); }     // last stage only: → FREE for the producer
};
```

- **Rights follow the handle's lifetime.** A `WriteSlot` publishes when it dies, so an early return cannot leave a claimed-but-unpublished slot stalling every consumer (a classic Disruptor hang). A `ReadView` is passed *into* the consumer's callback and destroyed when it returns, so a stage cannot keep a pointer past its sequence advance without copying — and a copy is visible in review. Both are `[[nodiscard]]`: a claimed slot that is ignored is a compile-time warning, not a hang. (Ch. 80 shows the same ownership discipline with Rust's borrow checker enforcing the view's lifetime at compile time; in C++ it is the handle discipline plus §41.4.5's test.)
- **Only the last stage releases.** The wrap gate is the slowest sequence; in a pipeline that is the last stage's, in a fan-out it is the minimum over the parallel stages (Ch. 40 §40.4.1). Making `release_through` callable from exactly one place is the "single writer per sequence" rule (Ch. 34, 38) in type form.
- **Across processes, the same types over a shared segment** (Ch. 57): the ring lives in `MAP_SHARED` memory, `slots` hold indices or offsets, never pointers (Ch. 57 §57.4.2), and the sequences are the only coordination. The one new failure is a consumer process that dies holding a grant — its sequence never advances, the gate never moves, the producer stalls at `try_claim`. Ch. 89 §89.4.3's supervisor detects the dead consumer and either re-points the gate past it (for a non-critical consumer: Ch. 42's "drop the slow reader" policy) or kills the path (a dead *gateway* is a safe-state event — Ch. 90).
- **Debug builds poison.** Under `#ifndef NDEBUG`, `release_through` fills freed slots with `0xDE` and `ReadView` records `(stage, seq)` in a per-stage "views outstanding" counter decremented in its destructor — so §41.4.5's test can see a view that outlived its grant.

### 41.4.5 Testing the protocol: a stage that holds a view one sequence too long

The bug this protocol exists to prevent is a stage that keeps using slot *N*'s bytes after advancing its sequence past *N* — which is legal C++, silently wrong, and shows up only when the producer laps the ring under load. Make it fail deterministically:

- **Poison-on-free with a canary read.** Debug builds fill a released slot (above). A test stage copies a field *after* advancing its sequence, then the test drives the producer around the ring once and asserts the copy equals `0xDEDEDEDE…`: if the stage's logic ever reads post-advance, the assertion fires on every run, not one in a thousand.
- **Outstanding-view accounting.** Each `ReadView` constructor increments `views_outstanding[stage]`, each destructor decrements; the consume loop asserts the counter is zero *before* advancing `my_seq`. A view stashed in a member, a lambda capture or a container trips it immediately.
- **ThreadSanitizer on the laps.** Ch. 45's TSan run of the ring test with a ring of 4 slots (not 4096) forces the producer to overwrite slots that a buggy stage is still reading within the first few hundred messages; a correct implementation is race-free under TSan because every grant is an acquire/release edge.
- **The cross-process variant:** kill the consumer process mid-batch (`SIGKILL`) and assert the producer stalls at `try_claim` within one ring's worth of messages and that the supervisor's recovery (re-point or safe-state) runs within its budget — Ch. 89 §89.3's measurement, with the ring protocol as the thing under test.

## 41.5 Pitfalls & anti-patterns

- **A thread per concern.** Decode thread, book thread, strategy thread, risk thread, send thread — five hops, five cores' noise, a queue at every boundary during the open, for work that fits one core (§41.3). Threads are for throughput and fault domains, not for module boundaries. (Counter that exposes it: Ch. 85's per-stage stamps showing hop time ≫ work time.)
- **Staging *onto* the critical path to "parallelize".** A pipeline never reduces one message's latency — it adds hops; only removing a consumer from the waited-for path does (§41.2.2). If the strategy waits for the risk stage's answer, that stage's hop is on the path, and only a shm ring with both ends spinning (Ch. 57) keeps it near 100 ns.
- **Per-message hops in a burst.** An SPSC without cached indices and a consumer that pops one slot per acquire pay a line transfer per message; a 32-message burst becomes a 35 µs queue (§41.3). Batch at the consumer (Ch. 40 §40.4.2), cache the indices (Ch. 37), and keep the hot message in one line.
- **Polling in arrival order, uncapped.** `while (feed.pop(m)) …` before anything else lets a 10 000-message open delay an ack or a kill command by the whole burst. Fixed priority order and per-source caps (§41.4.2); assert the worst-case control-message delay in a replay test.
- **Reading the clock per message or inside the core.** Makes the core's behaviour depend on scheduling and defeats replay (Ch. 89 §89.2.2); one `rdtsc` per iteration, stamped onto the events (§41.4.2, Ch. 96).
- **Raw pointers into slots.** A `const Msg*` saved for "later" is a use-after-free the allocator cannot see; the ring's `ReadView` and the outstanding-view counter (§41.4.4–§41.4.5) make it a test failure. Across processes it is additionally a pointer into another address space (Ch. 57 §57.4.2).
- **Two writers on one SPSC.** Two shards returning results into one SPSC "because it is just a ring" is a data race that corrupts indices and hangs the loop — the §41.3 harness did exactly this in its first draft. One ring per writer, or an MPSC with per-slot publication (Ch. 40 §40.4.1).
- **Blocking anywhere in the loop.** A `std::mutex` on the control path, an `epoll_wait` with a timeout, a `sleep_for` in `idle()`: each is a context switch (Ch. 46) and, on a staged path, a wake-up that turns a 150 ns hop into 10 µs. The hot loop's only wait is `pause`.
- **Topology by accident.** Pins spread over the code, a stage moved to a sibling core by a deploy, a ring allocated on the wrong NUMA node by whichever thread touched it first (Ch. 16). The topology table and its supervisor check (§41.4.1, §41.4.3) make the shape an artifact, not an emergent property.

## 41.6 Exercises & checklist

1. Reproduce §41.3 on an isolated core set (Appendix C). Report the per-hop cost from `hops.cpp` and compare with Appendix E's cross-core line-transfer row; then add cached indices and consumer batching to the SPSC and show the microburst row collapse.
2. Add a fourth source to `HotLoop` — a second control ring with cap 1 — and measure the worst-case delay a control message sees while the feed delivers a 10 000-message burst, as a function of `FEED_CAP`. Plot it; pick the cap that bounds the delay under 2 µs.
3. Build the shard variant with a *shared* TX stage (a merge ring) and measure what the merge hop costs against giving each shard its own venue session (Ch. 89 §89.4.5).
4. Instrument `ReadView` with the outstanding-view counter and write the poison-canary test of §41.4.5; introduce a deliberate post-advance read in a stage and confirm the test fails on the first lap.
5. Port the ring and handles to a `MAP_SHARED` segment (Ch. 57) with a forked consumer; `SIGKILL` the consumer mid-batch and measure how long the producer stalls before the supervisor re-points the gate.
6. Take a real strategy's stage costs from Ch. 85's stamps and apply the §41.2.2 rule: which boundaries, if any, pay to be hops? Justify each with a number.

**Checklist**

- [ ] The topology is written down (stage/shard → core, node, rings, polling order) and verified by the supervisor at start-up.
- [ ] Every hop on the critical path has a measured cost and a stated reason (off-path consumer, or fault/trust domain) — none exist for "separation".
- [ ] Run-to-completion within a shard; shards by a key with zero cross-shard reads (checked with `perf c2c`).
- [ ] The hot loop polls in fixed priority order with per-source caps; the worst-case control/ack delay under a replayed burst is measured and asserted.
- [ ] One `rdtsc` per iteration; the deterministic core never reads a clock; timer fires are events.
- [ ] Slots are reached only through `WriteSlot`/`ReadView`; the last stage alone moves the wrap gate; debug builds poison freed slots and count outstanding views.
- [ ] Rings are SPSC with exactly one writer each, or MPSC with per-slot publication; indices are cached and consumers batch.
- [ ] Nothing in the loop blocks; `idle()` warms and flushes, never sleeps.

## 41.7 References

- Thompson, M., Farley, D., Barker, M., Gee, P., Stewart, A. *Disruptor: High performance alternative to bounded queues for exchanging data between concurrent threads.* LMAX, 2011 — the claim/publish/gate protocol this chapter formalizes.
- Dean, J., Barroso, L. A. "The Tail at Scale." *CACM* 56(2), 2013 — why tails compound across stages.
- Intel. *Intel 64 and IA-32 Architectures Optimization Reference Manual*, §"Multicore and Hyper-Threading" (coherence traffic, `pause`) — the hop's hardware cost.
- Kleppmann, M. *Designing Data-Intensive Applications*, Ch. 11 (stream processing: partitioning vs pipelining) — the same decision at datacenter scale.
- Linux kernel documentation: `Documentation/scheduler/sched-domains.rst`, `Documentation/admin-guide/kernel-per-CPU-kthreads.rst` — what else runs on a "dedicated" core.

## 41.8 Additional Reading

- Fowler, M. "The LMAX Architecture" (2011) — the business-logic-on-one-thread argument, in prose.
- Aeron documentation, "Agents and Agent Runners" (`org.agrona.concurrent.Agent`) — a production many-source duty cycle with exactly §41.4.2's shape.
- Cook, C. "When a Microsecond Is an Eternity." CppCon 2017 — the single-threaded hot path and why it stays single-threaded.
- Meta Engineering, "Scaling services with Shard Manager" — the sharding-key discussion at scale.
- Rust's `std::sync::mpsc` and `crossbeam` source — the slot-ownership protocol with the borrow checker as the enforcement mechanism (ties Ch. 80).

---

*Next: Ch. 42 — Flow Control, Backpressure & Conflation on the Hot Path: what the loop does when a consumer is slower than its input — the policy taxonomy for a full channel by consumer class, conflation via per-instrument latest-value slots and a dirty bitmap, staleness as a first-class metric, and degraded modes at the open.*
