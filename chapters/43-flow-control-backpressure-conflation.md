# Part VI — Concurrency

# Chapter 43 — Flow Control, Backpressure & Conflation on the Hot Path

> **Prerequisites:** Ch. 42 (the hot loop and its sources — this chapter decides what each source does when its consumer is behind), Ch. 38 (the bounded ring whose `push` fails when full — the primitive every policy below is built on), Ch. 39 (seqlock publication — the conflation slot is one), Ch. 41 §41.4.1 (the Disruptor's wrap gate — "block the producer" as the default policy), Ch. 32 §32.4.1 (bitmaps and `tzcnt` — the dirty-bitmap drain), Ch. 1 §1.2.1 (queueing: why a 70 %-utilized consumer has an unbounded tail), Ch. 37 (false sharing — the dirty words and slots are written by two cores).
>
> **Leads into:** Ch. 48 (timers — staleness budgets are deadlines), Ch. 62 §62.4.3 (trading through a gap: the market-data side's degraded mode), Ch. 85 §85.6 and Ch. 95 §95.5 (the logger's and the capture's full-ring policies, now instances of this chapter's taxonomy), Ch. 87 (telemetry — sequence lag and message age as the staleness metrics), Ch. 90 §90.4.4 and Appendix M (who owns the degraded-mode state machine), Ch. 91 (the risk gate's feed-staleness trigger), Ch. 97 (the sequencer's slow-replica policy). Ch. 42 built the loop and priced the hop; the one question it deferred is this chapter's whole subject — the consumer is slower than the market, now what?

---

## 43.1 Why it matters: every trading system has a consumer slower than the open

At 09:30:00.000 the feed delivers more updates in one second than in the following ten minutes, and *something* downstream is slower than that burst — a strategy with 300 ns of decision logic against updates arriving every 50 ns, a risk process on the far side of a shm ring, a journal writer, a GUI. The book has met this moment five times and answered it five different ways: the ring's `push` "fails when full and the caller handles it" (Ch. 38 §38.4.1), the Disruptor's producer "waits on the slowest consumer — natural backpressure" (Ch. 41 §41.4.1), the logger "drops and counts, or overwrites oldest, never blocks" (Ch. 85 §85.6), the capture layer "drops and flags, never stalls the trade" (Ch. 95 §95.5), the feed handler "must never let downstream slowness back-pressure the receive path" (Ch. 62 §62.5). Each is right for its consumer and wrong for the others, and none of them names the thing a trading system actually cares about: **not how many messages were kept, but how stale the data was when a decision was made on it.** A strategy that acts on a 20 ms-old book during the open is trading against a market that has moved; a risk check on a 20 ms-old position is not a risk check. That is a direct P&L hazard, and no counter of "messages dropped" measures it.

Three ideas organize the chapter. **Policy is a property of the consumer class, not of the channel** (§43.2.1): the strategy wants the latest state and nothing else; the journal wants every record and may be arbitrarily late; the gateway's ack stream can lose nothing and must not be late; the GUI wants a sample. **Conflation is the policy for state** (§43.2.2, §43.4.2): when updates to one key supersede each other, the right structure is not a queue of updates but a per-key latest-value slot plus a dirty set — it bounds the staleness of every key to "one drain pass" regardless of the burst's size, and §43.3 shows it holding the acted-on data's median age at under a microsecond through a burst that a queue turned into 20 ms. **Staleness is a metric with a budget** (§43.2.3, §43.4.3): sequence lag and message age per channel, measured against the wire timestamp, published like any other telemetry (Ch. 87), with a per-consumer budget whose breach is an input to the degraded-mode state machine (§43.4.4) and to the kill-switch (Ch. 91). After this chapter the five earlier policies become one-line pointers.

## 43.2 Mental model

### 43.2.1 The policy taxonomy for a full channel, by consumer class

When a producer finds the channel full (or a consumer finds itself behind) there are exactly five things it can do:

```
   policy              what happens on full                    preserves              costs                                     right for
   ─────────────────   ─────────────────────────────────────   ────────────────────   ───────────────────────────────────────   ─────────────────────────────────
   BLOCK / THROTTLE    producer waits (Disruptor wrap gate)    every message, order   producer's latency → the hot path stalls  intra-core pipelines where the
                                                                                      NEVER on the tick-to-trade path           producer is not latency-critical
   DROP-NEWEST         push fails; the new message is lost     the backlog (old)      the LATEST state is what is lost          nothing on a trading path (the
                       (+ counter)                                                                                              newest is the valuable one)
   DROP-OLDEST         overwrite the oldest unread slot        the newest N           consumer may read a torn/overwritten      logs, flight recorders, telemetry
   (OVERWRITE)         (+ lapped counter)                                             slot unless versioned (Ch. 39)            samples — Ch. 85, 95, 99
   CONFLATE            per-key latest-value slot replaces the  latest state per key,  intermediate updates; trade prints and    strategy, risk, GUI — anything
                       queue; dirty set says which changed     bounded staleness      increments must NOT be conflated          that consumes STATE
   SAMPLE / DEGRADE    deliver every k-th, or a coarser form   a bounded rate         resolution                                GUIs, monitoring, non-critical
                                                                                                                                analytics
```

Mapped onto the consumers of Ch. 90's topology:

- **Order gateway (acks, fills, rejects, the venue's session stream):** *nothing may be dropped and nothing may be late.* Policy: size the channel for the worst burst the venue can emit (sequence-numbered, so a gap is detectable — Ch. 92) and BLOCK the *venue* via TCP flow control before the ring, never the strategy. If this channel is ever full, that is a Ch. 91 safe-state trigger, not a policy choice.
- **Strategy and risk (market data, positions, limits):** *latest state wins.* Policy: CONFLATE per instrument; the only queued items are the ones that are not state (trade prints feeding a volume accumulator are *increments* — they go through a lossless queue or are summed in the slot, never last-write-wins).
- **Journal / capture (Ch. 95):** *every record, order preserved, arbitrarily late is fine.* Policy: a large ring and an off-path writer; if it fills, DROP-and-flag at the capture layer (Ch. 95 §95.5) and raise the flag as an incident — but never BLOCK the producer.
- **Logger, flight recorder, telemetry samples (Ch. 85, 87, 99):** *recent is more valuable than complete.* Policy: DROP-OLDEST with a version word so the reader detects a torn slot; the overwritten count is itself a metric.
- **GUI / monitoring:** *a view, at human rate.* Policy: SAMPLE or CONFLATE at a coarse interval, off the hot box entirely.

The rule the table encodes: **a producer on the tick-to-trade path never blocks and never allocates for a slow consumer; the consumer's class decides whether the channel keeps everything (and is sized for it), keeps the newest, or keeps the latest-per-key.**

### 43.2.2 Conflation structures: latest-value slot + dirty set

Conflation replaces "a queue of updates" with "the current state per key, and the set of keys that changed since you last looked":

```
   producer (feed handler, one writer):                     consumer (strategy, one reader):
   on update(inst, px):                                      drain():
      slot[inst].ver += 1   (odd: writing)                      for each word w in dirty[]:  m = dirty[w]
      slot[inst].{t, px} = …                                       while m: b = tzcnt(m); m &= m-1
      slot[inst].ver += 1   (even: published)     ─────►              clear bit b of dirty[w]       (fetch_and, acq_rel)
      dirty[inst/64] |= 1 << (inst%64)   (fetch_or, release)          read slot[w*64+b] under the seqlock (Ch. 39)
                                                                      act on the LATEST value — the intermediate ones were never queued
   cost per update: 1 slot write (one line) + 1 RMW on the dirty word     cost per drain: #dirty keys, not #updates
   staleness per key ≤ one drain pass ≈ (#dirty keys) × service      memory: #keys × 64 B + #keys/8 B  — no queue, no allocation
```

- **The slot is a single-writer seqlock** (Ch. 39 §39.4.1–39.4.2): the feed handler writes, the version word guards against a torn read, the reader retries if the version is odd or changed. A 16–48-byte payload (BBO, last trade, book depth summary) fits with the version in one cache line, so an update is one line transfer when the reader next looks (Ch. 42 §42.2.2).
- **The dirty set is a bitmap drained with `tzcnt`** (Ch. 32 §32.4.1): 1 024 instruments are sixteen 64-bit words; a drain reads sixteen words and visits only set bits. The RMWs on dirty words are the one contended thing — producer `fetch_or`, consumer `fetch_and` on the same word — and §43.3 shows them costing a line transfer each, which is why the words are in their own lines and why a *deduplicating dirty queue* (an SPSC of keys plus a per-key "already queued" flag) is the alternative when keys are many and sparse: the queue carries each key at most once, so drain cost is still #dirty, but the producer's RMW is on a per-key byte it owns rather than a shared word.
- **Semantics decide what may be conflated.** *Last-write-wins* is correct for state (BBO, book level, position snapshot, mark); *merge* is required for increments (traded volume: sum into the slot under the same seqlock; order-count deltas: add) — and **trade prints, fills and acks are never conflated** because each one is a fact the strategy or the position must see. A conflating channel that carries a fill has lost money.
- **Depth-1 mailbox / triple buffer** is the single-key degenerate case: one slot, one reader, latest value — the right structure for a derived quantity published from one stage to the next (a fair value, a signal), cheaper than a ring because there is nothing to drain.

### 43.2.3 Staleness as a first-class metric

Two numbers describe how far behind a consumer is, and both belong in the telemetry segment (Ch. 87):

- **Sequence lag** — producer's published sequence minus the consumer's, per channel: how many messages (or dirty keys) are waiting. Free to compute (both sequences are already in shared memory), coarse (a message is not a time), and the input to the wrap gate and to a slow-consumer alarm.
- **Message age** — the time between an update's *wire* timestamp (NIC hardware stamp, Ch. 64/67; or the ingress `rdtsc` of Ch. 86's trace header) and the moment a consumer *acts* on it. This is the number that is P&L: it is what §43.3 measures as "age of the data acted on", and it is what a **staleness budget** is set against — e.g. strategy ≤ 20 µs at p99.9, risk position ≤ 50 µs, GUI ≤ 500 ms. The budget's breach is an *event* (into the hot loop's control source — Ch. 42 §42.4.2), not a log line.

The relation that makes conflation the right default: under a queue, message age grows without bound while arrival rate exceeds service rate (Ch. 1 §1.2.1 — the open is exactly that condition, for seconds); under conflation, the age of the value acted on is bounded by one drain pass regardless of how long the overload lasts, because superseded updates were never queued. The consumer is still "behind" — it has not seen every tick — but the thing it acts on is at most one pass old. That is the trade: **completeness for bounded staleness**, and for state it is the correct trade.

### 43.2.4 Degraded modes and load shedding at the open

Policies are per channel; a *mode* is system-wide and owned by one component. The readiness state machine of Appendix M.1 and Ch. 90 §90.4.4 has `trading`; this chapter adds that `trading` has sub-modes, entered when a staleness budget or a sequence-lag threshold is breached, each with a declared shedding plan: **normal** (everything on) → **conflating** (depth beyond the top *k* levels is no longer applied; the GUI and analytics feeds are sampled at 1/10; the journal keeps everything) → **pulled** (quotes withdrawn on instruments whose book age exceeds the budget — Ch. 62 §62.4.3's stale-data kill, now driven by *consumer* lag rather than feed gaps) → **safe-state** (Ch. 91's kill-switch, when the *ack* channel or the position view is stale). The owner is the risk/supervisor role (Ch. 90 §90.2.1), the inputs are the staleness metrics of §43.2.3, the transitions are logged and replayable (Ch. 95, 97), and §43.4.4 builds it. Load shedding is the *plan*; the mode machine is what makes it a decision instead of an accident.

## 43.3 Measure it: a replayed microburst against a slow consumer

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -march=native -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 42 §42.3 (TSC 2.70 GHz, no isolation). Ages are producer-stamp to consumer-action across two cores; the VM's per-vCPU TSC offset (~150 ns, measured at start-up by a two-way minimum-latency exchange and subtracted, negatives clamped to zero) is the resolution floor of the age columns. Labelled VM-measured.*

**The experiment.** 100 000 updates over 1 024 instruments (uniform random), produced at ~120–180 ns intervals on core 1 — a 6–8 M upd/s microburst lasting ~13–18 ms; a consumer on core 3 that spends **300 ns** of "strategy work" per update it acts on, i.e. it is 2–3× slower than the burst. Four channels between them, same producer, same consumer: a 2²⁰-slot ring (nothing is dropped — queue everything), a 1 024-slot ring with DROP-NEWEST, a 1 024-slot OVERWRITE ring (drop-oldest; the reader re-syncs when lapped and discards a slot the writer overtook during the read), and the conflator of §43.2.2. Metric: the **age of the data acted on** — consumer's clock at the moment it starts its 300 ns of work minus the producer's stamp on the value it is working with.

```cpp
// flow.cpp (the conflator; the three rings are the Ch. 38 SPSC with the policy at push). Build: g++ -O2 -std=c++20 -march=native -pthread flow.cpp -o flow
struct alignas(64) Slot { std::atomic<std::uint64_t> ver{0}; std::uint64_t t; std::int64_t px; };       // one line: version + payload
struct Conflator {
    Slot slots[NINST]; alignas(64) std::atomic<std::uint64_t> dirty[NINST / 64]; std::uint64_t overwritten = 0;
    void push(const Upd& u) {                                                                           // producer, single writer
        Slot& s = slots[u.inst]; auto v = s.ver.load(std::memory_order_relaxed);
        s.ver.store(v + 1, std::memory_order_relaxed); std::atomic_thread_fence(std::memory_order_release);   // odd: writing
        s.t = u.t; s.px = u.px;
        std::atomic_thread_fence(std::memory_order_release); s.ver.store(v + 2, std::memory_order_release);   // even: published
        if (dirty[u.inst / 64].fetch_or(1ull << (u.inst % 64), std::memory_order_release) & (1ull << (u.inst % 64))) ++overwritten;   // was already dirty: an update was superseded
    }
    bool pop(Upd& u) {                                                                                  // consumer: next dirty key, latest value
        for (int w = 0; w < NINST / 64; ++w) {
            std::uint64_t m = dirty[w].load(std::memory_order_acquire); if (!m) continue;
            int b = __builtin_ctzll(m); dirty[w].fetch_and(~(1ull << b), std::memory_order_acq_rel);     // clear BEFORE reading: a concurrent update re-dirties
            Slot& s = slots[w * 64 + b];
            for (;;) { auto v1 = s.ver.load(std::memory_order_acquire); if (v1 & 1) continue;             // seqlock read (Ch. 39)
                       u.t = s.t; u.px = s.px; std::atomic_thread_fence(std::memory_order_acquire);
                       if (s.ver.load(std::memory_order_relaxed) == v1) break; }
            u.inst = w * 64 + b; return true;
        }
        return false;
    }
};
```

**Results (VM-measured):**

```
channel / policy                 burst length   acted on   superseded or dropped   age of the data acted on (µs): p50      p99       max     consumer idle after
queue everything (2^20 ring)        12.7–14.7 ms   100 000          0                                           19 000   28 100    28 300     42–43 ms  ← 2× the burst
drop-newest (1 024 ring)             9.6–12.3 ms    14–19 k   81–86 k dropped (the NEWEST)                         570–610   2 230–2 670  2 330–2 740   ≈ burst end
drop-oldest (1 024 overwrite)        8.8–9.0 ms      7.6–8.1 k 76–86 k lapped (+ torn reads discarded)             91–97      332–334    353     ≈ burst end
conflate (slot + dirty bitmap)      15.1–18.7 ms    25.5 k    74.4 k superseded in place                            0.6–0.7   367–453   1 480–1 815  ≈ burst end
conflate, round-robin word scan     20.5 ms         24.2 k    75.8 k superseded                                    12.1      535     2 040     ≈ burst end
```

Reading it:

- **A queue preserves every message and makes every one of them useless.** The 2²⁰ ring lost nothing, and the consumer acted on data that was **19 ms old at the median and 28 ms at the end** — a 13 ms burst took 43 ms to drain, and for the last 30 ms of that the strategy was trading on a book from before the open. This is Ch. 1 §1.2.1's queueing curve lived through: arrival > service for 13 ms is 13 ms of growing backlog, then a drain at the service rate. "No drops" was the wrong objective.
- **Drop-newest keeps the wrong half.** It kept the ring's 1 024-deep backlog of *old* updates and discarded the *newest* 85 000; the consumer's data was 600 µs old at the median and the latest state of most instruments was never seen at all. It is the default behaviour of a bounded `push` that returns `false` and is ignored — the policy you get by not choosing.
- **Drop-oldest bounds the age to the ring** — ~1 024 × 300 ns ≈ 300 µs — and that is exactly what the max shows (353 µs). It is the right shape for a log (Ch. 85) where "the most recent N" is the ask, and the wrong one for state: the consumer still processes updates that a later one has superseded, and the reader needs the lapped check (and a version word, in a real implementation) or it reads torn slots — the first draft of this harness did, and reported ages of 10¹⁵ µs.
- **Conflation acts on data that is sub-microsecond old at the median** — 0.6 µs, i.e. the slot the consumer reads is almost always the one written in the last few hundred nanoseconds — while doing *fewer* units of work (25 k) than drop-newest and never queueing. Its p99/max (0.4–1.8 ms) are the instruments at the *end* of the bitmap: an in-order scan keeps re-finding re-dirtied low words, so high words wait. The round-robin scan trades the median for fairness (12 µs p50) and still does not bound the tail here, because at 2–3× overload *some* instrument must wait ~#dirty × service ≈ 1 024 × 300 ns ≈ 300 µs per pass. That bound is the point: **under conflation the worst-case age is one drain pass over the live instruments, independent of the burst's length**; under a queue it is the burst's length times the overload factor. Priority-ordered drains (the strategy's active instruments first — §43.4.2) are how the tail is bought down.
- **The producer paid for conflation too**: the burst took 15–20 ms to emit against 9–13 ms for the rings, because every `push` performs a `fetch_or` on a dirty word that the consumer is concurrently `fetch_and`-ing — one cache-line transfer per update (Ch. 37), ~150 ns on this host. §43.4.2's deduplicating dirty *queue* moves that RMW onto a per-key flag the producer owns; the measurement is exercise 2.

What transfers to the reference machine: the ratios. Line transfers are ~2.5× cheaper there (Ch. 42 §42.3), so the conflator's push cost falls to ~60 ns and its median age to a few hundred ns; the queue's 19 ms median is a property of arithmetic (overload × duration), not of the machine.

## 43.4 Techniques

### 43.4.1 Declaring the policy per channel, in the type

The policy is part of the channel's type, chosen by the consumer class and visible at the construction site — not an `if (!push) …` someone forgets:

```cpp
// channel.hpp — the policy is a template parameter; the hot-path producer never blocks whatever the policy.
enum class OnFull { Fail, Overwrite };                       // Block is deliberately NOT an option for a hot-path producer
template <class T, std::size_t N, OnFull P> struct Channel;  // Fail: SPSC of Ch. 38 with a dropped counter; Overwrite: versioned overwrite ring (Ch. 85's logger)

using AckChannel      = Channel<VenueMsg, 1 << 16, OnFull::Fail>;        // full == incident: the gateway asserts and trips safe-state (Ch. 91)
using JournalChannel  = Channel<Record,   1 << 20, OnFull::Fail>;        // full == drop-and-flag (Ch. 95 §95.5), never block
using LogChannel      = Channel<LogRec,   1 << 14, OnFull::Overwrite>;   // recent beats complete (Ch. 85)
using BookChannel     = Conflator<BookTop, NINST>;                       // state: latest per instrument (§43.4.2)
using FillChannel     = Channel<Fill,     1 << 14, OnFull::Fail>;        // facts, never conflated; full == incident

static_assert(!std::is_same_v<decltype(strategy_in), Channel<BookTop, 1 << 20, OnFull::Fail>>, "a queue of book updates to a strategy is the §43.3 row 1 failure");
```

Every `Fail` channel exposes `dropped()` and `high_water()`; every `Overwrite` channel exposes `lapped()`; every conflator exposes `superseded()` and `dirty_count()` — all scraped into the telemetry segment (Ch. 87) as the sequence-lag family of §43.2.3.

### 43.4.2 Building the conflator: bitmap, deduplicating queue, priorities, merge semantics

- **Per-instrument slot + bitmap** (§43.3's listing) when the key space is dense and small (≤ a few thousand instruments on a shard): 64 B per key, drain cost #dirty, memory one page per 64 keys. Keep the dirty words in their own cache lines (`alignas(64)`) and *clear the bit before reading the slot* so an update that lands during the read re-dirties the key rather than being lost.
- **Deduplicating dirty queue** when keys are many and sparse (a 100 k-instrument universe of which 2 k move): an SPSC ring of keys plus a per-key `std::atomic<uint8_t> queued` flag. Producer: `if (!queued.exchange(1, acq_rel)) ring.push(key)` — the RMW is on a byte only this key touches, so a hot instrument costs the producer one *local* RMW per update and one ring push per *drain interval*; consumer: pop key, `queued.store(0)`, read slot. Drain order is arrival order of first-dirtying, which is also a fairness policy.
- **Priority drains.** The strategy's *active* instruments (those with working orders or signals near trigger) live in a separate small bitmap drained first and uncapped; the rest are drained with a per-pass cap. The worst-case age for the active set becomes #active × service — tens of instruments, single-digit microseconds — which is the tail §43.3 could not buy with a fair scan.
- **Merge, not overwrite, for increments.** The slot's writer applies `volume += d` and `count += 1` under the same seqlock when the update is an increment; the reader takes the accumulated value and (if it needs deltas) keeps its last-seen copy. Mark the slot's fields as *state* or *accumulator* in the type (`Price last; Accum<Qty> volume;`) so the writer cannot assign where it must add.
- **What must bypass the conflator**: fills, acks, rejects, trade prints that drive a per-trade decision, control messages — each is a fact, queued losslessly on a `Fail` channel whose fullness is an incident (§43.4.1). Decide this per message type at design time and `static_assert` it (`Conflatable<T>`), because a conflated fill is a silent position error (Ch. 92).

### 43.4.3 Measuring staleness and setting budgets

- **Instrument the act, not the receipt.** Age is stamped where the consumer *uses* the value (the strategy's decision point, the risk gate's position read), from the wire/ingress timestamp carried in the slot or the Ch. 86 trace header — the same histogram machinery as Ch. 86 §86.4.4, one record per act, aggregated off-core (Ch. 87).
- **Three series per channel**: `lag` (sequence or dirty count — sampled), `age_p99` and `age_max` per interval (from the histogram), `dropped/lapped/superseded` (counters). A channel with rising `lag` and flat `age` is a conflator doing its job; rising `age` with any policy is a slow consumer and an alert.
- **Budgets as data, per consumer** (hot-reloaded — Ch. 89): `{channel, p99_budget, max_budget, action}`; the compare is one predicted branch per act (Ch. 87 §87.4.4's budget compare); a breach emits a *control event* into the hot loop (Ch. 42 §42.4.2) — never a log line waited on.
- **Validate on a replayed open** (Ch. 95): the only honest test of a staleness budget is the real burst at the real service time, which is what §43.3 approximates. A budget that only holds at the average rate is not a budget.

### 43.4.4 Degraded modes: the shedding plan as a state machine

```cpp
// mode.hpp — degraded modes owned by the risk/supervisor role; read by the hot path as ONE word (Appendix M.1's may_send shape).
enum class Mode : std::uint8_t { Normal, Conflating, Pulled, SafeState };
struct ModeMachine {
    std::atomic<Mode> mode{Mode::Normal};                                  // published; hot threads read with one relaxed load + predicted branch
    Mode step(const Staleness& s) {                                         // runs on the supervisor's cadence, inputs are §43.4.3's series
        if (s.ack_age_max > budget.ack_max || s.position_age_max > budget.pos_max) return set(Mode::SafeState);   // the facts are stale: stop (Ch. 91)
        if (s.book_age_p99 > budget.book_pull)                             return set(Mode::Pulled);              // quotes withdrawn on stale instruments (Ch. 62 §62.4.3)
        if (s.book_age_p99 > budget.book_conflate || s.depth_lag > budget.depth_lag) return set(Mode::Conflating);  // depth beyond top-k off; GUI/analytics sampled 1/10
        return set(Mode::Normal);
    }
    Mode set(Mode m) { if (m != mode.load(std::memory_order_relaxed)) { mode.store(m, std::memory_order_release); journal(m); } return m; }  // every transition is a replayable event (Ch. 95, 97)
};
```

- **One owner, one word, hysteresis.** Transitions down (towards `SafeState`) are immediate; transitions up require the metric under budget for a dwell time (seconds), so the mode does not flap through the open. The hot path reads the word; it never computes the mode.
- **Each mode is a declared plan**, written next to the table in §43.2.1: *what* is conflated harder, *which* feeds are sampled, *which* instruments are pulled, *who* is told (Ch. 87's alerting). Shedding that is not written down is what happens anyway, in the order the code happened to be written.
- **Rehearse it** (Appendix M.4's kill-switch dry run): a replayed open with the consumer deliberately slowed must walk the machine `Normal → Conflating → Pulled` and back, with the transitions in the journal and the staleness series showing *why*.

### 43.4.5 Retiring the scattered policies

With the taxonomy in place, the earlier chapters' statements become references: Ch. 38 §38.4.1's "the caller handles fullness" is §43.4.1's `OnFull`; Ch. 41 §41.4.1's wrap-gate backpressure is the BLOCK row, legitimate only off the tick-to-trade path; Ch. 85 §85.6's and Ch. 99's overwrite rings are DROP-OLDEST with a version word; Ch. 95 §95.5's drop-and-flag is DROP-NEWEST-as-incident on a `Fail` channel; Ch. 62 §62.4.3's stale-instrument pause is the `Pulled` mode driven from the feed side; Ch. 90 §90.4.4's safe-state is the terminal mode. Each of those sections now carries a one-line pointer here rather than its own restatement.

## 43.5 Pitfalls & anti-patterns

- **Sizing the queue bigger.** The fix for a full ring at the open is never a larger ring: it converts drops into staleness and hides the overload for longer (§43.3 row 1 — 2²⁰ slots, 28 ms of stale trading). Conflate state; size lossless channels for the venue's worst burst and treat fullness as an incident.
- **Ignoring `push`'s return value.** That is DROP-NEWEST by omission, keeping the oldest backlog and discarding the latest state. Every `Fail` channel's drop counter is published, and a non-zero count on an ack or fill channel is a safe-state trigger.
- **Conflating facts.** A conflator carrying fills, acks or trade prints drops money: superseded fills are missing position, superseded acks are orders in unknown state (Ch. 92). `static_assert(Conflatable<T>)`.
- **Overwriting increments.** A "last trade volume" slot assigned instead of summed under-reports every burst. Mark accumulators in the type (§43.4.2).
- **Torn reads from an overwrite ring.** A reader lapped mid-copy reads half of two records; without the version word (Ch. 39) it is silent. §43.3's first draft produced ages of 10¹⁵ µs.
- **Clearing the dirty bit after reading the slot.** An update that lands between the read and the clear is lost until the next unrelated update; clear first, read second, let a concurrent update re-dirty.
- **Measuring drops instead of age.** Drop counters say how the channel behaved; message age says what the strategy traded on. The second is the P&L number and the one with a budget.
- **Backpressuring the receive path** (Ch. 62 §62.5). A feed handler that stalls because a strategy is slow manufactures gaps on the wire for *every* consumer; the receive path drains into conflators and lossless journals and never waits for anyone.
- **Blocking a hot producer "briefly".** The Disruptor's wrap gate is correct between pipeline stages that are all off the tick-to-trade path; on it, a gated producer is a stalled strategy. Hot-path producers fail, overwrite or conflate — they do not wait.
- **A mode that nobody owns.** Shedding spread across stages (the GUI feed drops here, the depth is skipped there) with no state machine produces an inconsistent system under load and no record of what it did. One owner, one word, journaled transitions (§43.4.4).

## 43.6 Exercises & checklist

1. Reproduce §43.3 on isolated cores with the service time at 100, 300 and 1 000 ns; plot the median and p99 age for each policy against overload factor. Identify where the queue's median crosses 1 ms.
2. Implement the deduplicating dirty queue of §43.4.2 and measure the producer's push cost against the bitmap conflator under the same burst (the shared-word RMW vs the per-key flag).
3. Add a priority bitmap of 32 "active" instruments drained first; measure their age distribution against the rest under 3× overload.
4. Replace the strategy's book input in a Ch. 29/Ch. 90 toy system with a conflator, keep fills on a lossless channel, and verify with a replay (Ch. 95) that positions match the lossless run exactly while the book's acted-on age falls.
5. Build the staleness series of §43.4.3 into the telemetry segment (Ch. 87) and drive the mode machine of §43.4.4 from them; replay an open with the consumer slowed 5× and show the journaled transitions.
6. Audit one existing system: list every channel, its consumer class, its current on-full behaviour and the behaviour the table in §43.2.1 prescribes. Count the mismatches.

**Checklist**

- [ ] Every channel's policy is declared in its type from its consumer's class; no hot-path producer blocks or allocates on full.
- [ ] State goes through conflators (latest per key, dirty set, seqlock slots); facts (fills, acks, rejects, trade prints) go through lossless channels whose fullness is an incident.
- [ ] Increments are merged in the slot, never overwritten; conflatability is `static_assert`ed per message type.
- [ ] Dirty bits are cleared before the slot is read; dirty words and slots are in their own cache lines.
- [ ] Staleness (message age at the point of use) is measured per channel, published, and budgeted per consumer; breaches are control events.
- [ ] Degraded modes are a journaled state machine with one owner and a written shedding plan per mode, rehearsed on a replayed open.
- [ ] Drop, lapped and superseded counters are published; a non-zero drop on an ack/fill channel trips safe-state.
- [ ] The receive path never waits on a consumer.

## 43.7 References

- Kleinrock, L. *Queueing Systems, Volume I: Theory.* Wiley, 1975 — the arrival-vs-service arithmetic behind §43.3's first row.
- Thompson, M. et al. *Disruptor* technical paper, LMAX, 2011 — wrap-gate backpressure (the BLOCK row).
- Lameter, C. "Effective Synchronization on Linux/NUMA Systems" (2005) and the Linux `seqlock` documentation — the single-writer slot.
- Aeron documentation, "Flow Control" and "Conflation" strategies — a production messaging system's taxonomy of the same policies.
- Reactive Streams specification §1 (backpressure semantics) — the vocabulary, and why request-based backpressure is not for the hot path.
- Tene, G. "How NOT to Measure Latency" (2015) — coordinated omission; message age must be measured against the intended (wire) time, not the dequeue time.

## 43.8 Additional Reading

- Brooker, M. "Cache, Conflation and the Cost of Freshness" (blog) — the completeness-vs-freshness trade in another domain.
- CME Group, *MDP 3.0 Market Data — Conflation* and ICE, *iMpact Conflated Feeds* documentation — venue-side conflation, and what it means for a consumer that conflates again.
- Welsh, M., Culler, D., Brewer, E. "SEDA: An Architecture for Well-Conditioned, Scalable Internet Services." SOSP 2001 — staged pipelines with per-stage admission control and load shedding; the ideas, minus the thread pools.
- Gregg, B. *Systems Performance*, 2nd ed., Ch. 2 (queueing theory for practitioners).
- Agrona's `ManyToOneRingBuffer` and `BroadcastTransmitter` source — a lossless MPSC and a lapped-reader broadcast ring, with the exact "lapped" detection the OVERWRITE row needs.

---

*Next: Ch. 44 — Coroutines & Async Models, which turns Part VI's concurrency story to a different angle: C++20 coroutines, stackful vs stackless, event loops, and async I/O without thread-per-connection — and where they fit (and don't) on the latency hot path.*
