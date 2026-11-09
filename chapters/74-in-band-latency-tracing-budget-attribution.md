# Part XII — Observability & Operations in Production

# Chapter 74 — In-Band Latency Tracing & Budget Attribution

> **Prerequisites:** Ch. 17 (the TSC as the stamp: `rdtscp`, cross-core comparability, TSC→ns calibration), Ch. 1 §1.2.3 (the tick-to-trade budget this chapter attributes), Ch. 34 and Ch. 37 (the SPSC ring and the Disruptor slot the trace header rides in), Ch. 51 (shared-memory rings between processes), Ch. 73 (the binary log that receives the finished trace record), Ch. 60 (PTP, the PHC and `PTP_SYS_OFFSET_PRECISE` — the bridge from your TSC to the venue's clock), Ch. 54 (the decoders that see the venue timestamp fields first), Ch. 3 and Ch. 75 §75.2.2 (percentiles are not additive).
>
> **Leads into:** Ch. 75 (the per-stage histograms this chapter's stamps feed, the shm telemetry segment and the scraper), Ch. 81 (spike forensics starts from one trace record whose stage delta is over budget), Ch. 84 (the capstone's tick-to-trade decomposition is this chapter's output, read as a table), Appendix J.5 (the venue timestamp fields, tabulated). Ch. 73 made the hot path *record* instead of *format*; this chapter makes every message carry its own stopwatch — a correlation ID plus a vector of stage stamps — through every ring and process it crosses, so that the budget can be attributed per stage, per message, in production, and then split three ways between you, the wire and the matching engine using the timestamps the venue itself puts on the wire.

---

## 74.1 Why it matters: the budget you cannot attribute

Ch. 1 §1.2.3 handed you a table: wire→host 0.5–1 µs, decode 100–400 ns, strategy 50–300 ns, risk and encode 50–200 ns, host→wire 0.4–0.8 µs. Every chapter since has optimised one row of it. Ch. 84 will close the book by measuring the whole thing wire-to-wire with NIC hardware timestamps (Ch. 60 §60.4.2). Between those two there is a hole the book has so far papered over with an assertion: *"instrument every stage boundary with a TSC stamp and keep a histogram per stage."* That sentence hides three engineering problems. First, the stages do not live in one function, or even one process — Ch. 78 split them into a feed handler, a strategy and a gateway talking through shared-memory rings — so a stamp taken in the feed handler has to *travel* to the gateway before anyone can subtract it from anything. Second, the stamps have to be joinable: the gateway must know *which* tick's decode stamp belongs to *this* order, which is a correlation problem, not a timing one. Third, the stamps are only half the picture: when a race is lost, the question the desk asks is not "where did our 1.8 µs go" but "were we slow, was the wire slow, or did the exchange's engine take 40 µs on that packet" — and the only clock that can answer that is the venue's, carried in fields (`SendingTime`, `TransactTime`, `TrdRegTSTimeIn`, the ITCH nanoseconds-since-midnight) that the decoders of Ch. 54 have been skipping over.

The consequence of the hole is an operational one. Without per-message attribution you have a wire-to-wire histogram (Ch. 60) whose p99.9 moved from 2.1 to 2.9 µs this morning and no way to say which stage moved it; you have Ch. 75's per-stage histograms, each recorded by the thread that owns the stage, but their percentiles cannot be added back into an end-to-end number (§75.2.2) and they cannot tell you whether *the same* messages were slow in two stages or different ones; and you have the venue's fill reports with their timestamps, which nobody compares to the host's because the clocks are in different domains. This chapter fixes all three with one small structure: a **trace header** — a correlation ID and a fixed vector of raw TSC stamps — written into the message's ring slot at the first touch and appended to at every stage boundary, so that the gateway, at TX enqueue, holds the complete timeline of that message from NIC to NIC in 48 bytes and hands it to the binary log (Ch. 73) and the histogram aggregator (Ch. 75) in one record. §74.2 defines the header and the three carriage options; §74.3 measures what it costs the hot thread and the ring (the answer is "one `rdtscp` and one extra cache line per message, measured"); §74.4 builds it, carries it across rings and processes, assigns contention-free IDs, aggregates off-path, and then opens the venue's timestamps: which fields exist on CME, Eurex and Nasdaq, what each one measures, and how to read gateway-in against matching-engine-in to separate your latency from the wire and from them. §74.4.6 adds the per-packet ledger that joins a fabric tap's capture to the host's stamps for the days when the two disagree.

## 74.2 Mental model

### 74.2.1 The trace header: a correlation ID and a vector of stage stamps

```
   struct TraceHdr {              // 48 bytes, trivially copyable, lives in the ring slot beside the payload
       uint64_t corr;             // the join key: venue sequence number (market data) or ClOrdID (orders)
       uint64_t t[5];             // raw TSC at: 0 first host touch  1 decoded  2 decided  3 risk-checked  4 tx-enqueued
   };

   NIC RX ──► feed handler ──ring──► strategy ──ring──► gateway ──► NIC TX
    t_hw_rx     t[0]  t[1]            t[2]               t[3] t[4]    t_hw_tx
    (PHC)       (TSC) (TSC)           (TSC)              (TSC)        (PHC)
     └─ Ch. 60 ─┘                                                 └─ Ch. 60 ─┘
```

Three design decisions are baked into those two lines. **The stamps are raw TSC ticks**, not nanoseconds: converting costs a multiply and a shift per stamp and, more importantly, the conversion factor belongs to the *reader* (Ch. 17 §17.4.1 — "stamp, don't format"). On one host the invariant TSC is synchronised across cores at boot (Ch. 17 §17.2.2; the kernel refuses to use `tsc` as the clocksource otherwise), so a stamp taken on the feed handler's core is directly subtractable from one taken on the gateway's core — *no* conversion happens on the hot path at all, in any process. **The vector is fixed-size** with a stage index decided at design time, not a growable list of (name, time) pairs: the header's size is a compile-time constant, the slot layout is fixed (Ch. 9), and a stage that does not run (a rejected order never reaches `t[4]`) leaves a zero the aggregator treats as "not reached". **The correlation ID is the message's own identity**, not a generated trace ID: for market data it is the venue's sequence number (unique per session per feed, already decoded in Ch. 55's gap detector), and for an order it is the `ClOrdID` you were going to generate anyway (§74.4.3). Nothing is allocated, nothing is looked up, nothing is hashed on the hot path.

### 74.2.2 In-band vs out-of-band carriage

The header has to get from the feed handler's core to the gateway's core. There are three ways, and the choice is a cache-line budget (Ch. 7, 33):

- **In-band:** the header is a field of the ring slot (Ch. 34 §34.4.1's `Slot`, Ch. 37's event), written by the producer next to the payload and read by the consumer in the same pass. A 40-byte book delta plus a 48-byte header no longer fits one 64-byte line — the slot becomes 128 bytes — so the producer writes and the consumer reads *two* adjacent lines per message instead of one. The adjacent-line prefetcher (Ch. 10) fetches the pair together, and the hardware does exactly the same amount of coherence work it would for any 128-byte slot. This is the design the chapter recommends, and §74.3 shows the second line is the whole cost.
- **Out-of-band side table:** the slot stays 64 bytes and carries a 4-byte index into a separate array of `TraceHdr`. Same two lines of traffic per message — but the second one is in a *different* region with its own cache set and its own TLB entry, and a consumer that is more than a few messages behind finds the side-table line already evicted from the producer's L2 and has to pay a core-to-core transfer for it separately from the slot (Ch. 7 §7.2.3, Appendix E). It saves nothing and adds a dependent load; use it only when you cannot change the slot layout (a vendor-defined ring, Ch. 64's ef_vi descriptors).
- **Out-of-band per-stage logging:** no header at all; every stage writes `(corr, stage, tsc)` into its own thread's binary log (Ch. 73) and the join happens offline. Zero bytes on the ring, but *every stage* now does a log reserve-and-publish per message (~5–20 ns each — Ch. 73 §73.3) instead of one store, the join is an offline sort-merge over gigabytes, and nothing is attributable *live*. It is the right design for a stage you cannot touch (a third-party library) and the wrong one for the pipeline you own.

The header is written by exactly one thread at a time — the producer of the ring it currently sits in — so there is no false sharing on it (Ch. 33); it changes hands with the slot's publish/acquire pair (Ch. 34) and needs no additional ordering.

### 74.2.3 Causality across processes: tick → decision → order

Market data and orders are two message streams with two different correlation IDs, and the question "how long from *this* tick to *that* order" needs a link between them. The link is a **parent ID**: when the strategy decides on an order in response to a tick, the order's trace header is initialised with `t[0..2]` *copied from the tick's header* and the tick's `corr` stored beside the order's own `ClOrdID`. From the gateway's point of view the order record then contains the complete causal chain: the venue sequence number that triggered it, the first-touch stamp of that packet, and every stage until TX. A decision that consumes *several* ticks (a book rebuild, a signal over a window) names the *last* one — the message whose arrival made the decision fire — because tick-to-trade is defined from the triggering tick (Ch. 1 §1.2.3). Orders with no parent (a timer-driven cancel, an operator command) carry `parent = 0` and are attributed from `t[2]` only.

### 74.2.4 Three clocks on one message

A message's life is stamped by three clocks that must be reconciled off-path, never on it:

```
   clock           who writes it                       resolution / sync          what it can attribute
   ─────────────── ─────────────────────────────────── ────────────────────────── ───────────────────────────────────────────
   venue clock     SendingTime, TransactTime,          1 ns fields; venue PTP to   venue egress → your NIC (one-way feed latency);
                   TrdRegTSTimeIn/Out, ITCH ns-since-  UTC (MiFID II RTS 25:      gateway-in → engine → gateway-out (their time);
                   midnight (§74.4.5)                   ≤ 100 µs divergence,       your TX → gateway-in (the wire, order side)
                                                        1 µs granularity for HFT)
   NIC PHC         hardware RX/TX timestamps (Ch. 57,   ~10 ns; PTP to the same    wire-to-wire tick-to-trade; the bridge to the
                   60)                                  UTC via the grandmaster    venue clock (both are UTC)
   host TSC        every t[i] in the trace header       ~0.4 ns ticks; same        per-stage attribution inside the host, across
                                                        counter on every core;     processes; bridged to the PHC by
                                                        PTP_SYS_OFFSET_PRECISE     PTP_SYS_OFFSET_PRECISE (Ch. 60 §60.4.1)
```

Inside the host, TSC stamps are compared to TSC stamps. At the edges, the NIC's hardware stamps are compared to each other (Ch. 60 §60.4.2). The venue's timestamps are compared to the NIC's — both UTC, both PTP-disciplined — and never to the TSC directly. The reader (the §74.4.4 aggregator) converts TSC→PHC once per record using the cross-timestamp the host publishes (Ch. 60 §60.4.1), with the error bound that cross-timestamp carries. §74.4.5's attribution method falls straight out of this table.

## 74.3 Measure it: what the stamp and the header cost

*Toolchain and machine: Appendix L.3. The numbers below were produced by the listing on a 4-vCPU Intel Xeon VM (TSC 2.70 GHz, no PMU exposed) with GCC 13.3, `-O2 -std=c++20 -march=native`, Linux 6.18; they are the hot thread's own CPU cost, which the VM measures reliably. Cross-core transfer costs are the reference machine's (Appendix E) and are labelled representative.*

The question is what a producer thread pays, per message, to (a) take the stamps and (b) write the header — in-band (128-byte slot) or out-of-band (64-byte slot plus side table) — against a baseline that writes only the payload. Both runs stream into a warm, wrapping 4096-slot ring (256 KiB / 512 KiB, L2-resident) with no consumer, so the number is the write cost and not the back-pressure of Ch. 34's full-ring spin:

```cpp
// tracehdr1.cpp — the producer's cost of stamping and writing a trace header (single thread, warm wrapping ring).
// Build: g++ -O2 -std=c++20 -march=native tracehdr1.cpp -o tracehdr1
#include <cstdint>
#include <cstdio>
#include <x86intrin.h>
static inline std::uint64_t stamp() { unsigned a; return __rdtscp(&a); }            // ordered end-stamp, Ch. 17 §17.2.1
struct Delta    { std::uint64_t seq; std::int64_t px; std::int32_t qty; std::int32_t side; std::uint64_t pad[2]; };   // 40-B payload
struct TraceHdr { std::uint64_t corr; std::uint64_t t[5]; };                                                        // 48-B header
struct SlotA { Delta d; std::uint64_t pad[3]; };                                        // 64 B: payload only
struct SlotB { TraceHdr tr; Delta d; std::uint64_t pad[5]; };                           // 128 B: in-band header
struct SlotC { Delta d; std::uint32_t tidx; std::uint32_t pad0; std::uint64_t pad[2]; };// 64 B + index into a side table
static constexpr std::size_t N = 1 << 12;
alignas(64) static SlotA ra[N]; alignas(64) static SlotB rb[N]; alignas(64) static SlotC rc[N]; alignas(64) static TraceHdr side[N];

template <int V> [[gnu::noinline]] double run(int msgs) {
    std::uint64_t t0 = stamp();
    for (int i = 0; i < msgs; ++i) {
        std::uint64_t tin = stamp();                                  // t[0]: first touch — every variant takes it
        Delta d{std::uint64_t(i), 100000 + (i & 7), 1 + (i & 3), i & 1, {0, 0}};
        if constexpr (V == 0) { auto& s = ra[i & (N - 1)]; s.d = d; asm volatile("" : : "r"(&s) : "memory"); }
        if constexpr (V == 1) { auto& s = rb[i & (N - 1)]; s.d = d; s.tr.corr = i; s.tr.t[0] = tin; s.tr.t[1] = stamp();
                                asm volatile("" : : "r"(&s) : "memory"); }
        if constexpr (V == 2) { auto& s = rc[i & (N - 1)]; s.d = d; s.tidx = i & (N - 1); auto& tr = side[s.tidx];
                                tr.corr = i; tr.t[0] = tin; tr.t[1] = stamp(); asm volatile("" : : "r"(&s), "r"(&tr) : "memory"); }
        asm volatile("" : : "r"(tin));
    }
    return double(stamp() - t0) / msgs;
}
int main() {
    for (int r = 0; r < 3; ++r)
        std::printf("A no header %.1f | B in-band header (+corr +2 stamps) %.1f | C out-of-band table %.1f   cycles/msg\n",
                    run<0>(5'000'000), run<1>(5'000'000), run<2>(5'000'000));
}
```

```
   variant                                        bytes written/msg   lines touched/msg   cycles/msg (3 runs)
   ───────────────────────────────────────────── ─────────────────── ─────────────────── ──────────────────────
   rdtscp alone (1M in a loop)                           —                   —            45.6 – 46.3
   A  64-B slot, payload only, one stamp               40                  1             49.1 / 50.6 / 54.3
   B  128-B slot, in-band header: +corr, +2 stamps     64                  2             96.1 / 96.8 / 102.6
   C  64-B slot + out-of-band table: same fields        44 + 24            2             98.0 / 98.5 / 98.6
   B − A: the header's marginal cost                                                      ≈ 47 cycles ≈ 1 rdtscp
```

Read it with the stamp cost in hand. On this VM an `rdtscp` is ~46 cycles (17 ns at 2.7 GHz — the virtualised TSC read is slower than the 25–35 cycles Ch. 17 §17.3 measures on the bare reference machine; representative there: 10–13 ns), and *the entire marginal cost of the in-band header is one extra stamp*: the three extra stores (`corr`, `t[0]`, `t[1]`) and the second cache line are free in the shadow of the `rdtscp`, because the stores retire asynchronously through the store buffer (Ch. 11) and the line is L2-resident and written in full. The out-of-band table costs the same on the producer and, as §74.2.2 argued, more on the consumer, which has to fetch two unrelated lines. The honest conclusion: **a stage boundary costs one `rdtscp`, and the header costs nothing beyond its stamps**, so a five-stage header is ~5 × 10–17 ns ≈ 50–85 ns of a 2 µs tick-to-trade — 3–4 %, all of it in the counter reads. Use the unfenced `rdtsc` for the *intermediate* stamps if that 3 % matters (Ch. 17 §17.2.1: the reordering window is a few instructions, far below the stage granularity you are attributing) and keep `rdtscp` at the two edges.

What the VM cannot measure is the consumer's side when the ring crosses cores: the 128-byte slot moves two lines core-to-core instead of one. On the reference machine a line that was just written by another core costs ~70–100 ns to pull (Appendix E, "cache-line transfer between cores"), the adjacent-line prefetcher fetches the pair on the first touch, and the pair is consumed together, so the representative added latency at the consumer is one overlapped line transfer, ~20–40 ns — the same cost a 128-byte payload would have, and smaller than a single `rdtscp`. §75.3's measurement of the telemetry histogram record (a few stores) is the analogous number on the aggregation side.

## 74.4 Techniques

### 74.4.1 The trace header and the stamping points

```cpp
// trace.hpp — the in-band trace header and its stamping points; shared by every process in the pipeline (Ch. 78).
#pragma once
#include <cstdint>
#include <x86intrin.h>

namespace trace {
enum Stage : std::uint8_t { FirstTouch = 0, Decoded = 1, Decided = 2, RiskOk = 3, TxEnqueued = 4, kStages = 5 };

struct Hdr {                                            // 56 bytes; the slot that carries it pads to 128 (Ch. 9)
    std::uint64_t corr;                                 // venue seq (market data) or ClOrdID (orders) — the join key
    std::uint64_t parent;                               // for an order: the venue seq of the tick that caused it; 0 = none
    std::uint64_t t[kStages];                           // raw TSC; 0 = stage not reached
};
static_assert(sizeof(Hdr) == 56 && alignof(Hdr) == 8);

inline std::uint64_t edge_stamp()  { unsigned a; return __rdtscp(&a); }   // ordered: first and last stage
inline std::uint64_t inner_stamp() { return __rdtsc(); }                 // unordered: a few-instruction window is below stage granularity

inline void begin(Hdr& h, std::uint64_t corr, std::uint64_t t0) { h.corr = corr; h.parent = 0; h.t[0] = t0;
                                                                   for (int i = 1; i < kStages; ++i) h.t[i] = 0; }
template <Stage S> inline void mark(Hdr& h) { static_assert(S > FirstTouch && S < TxEnqueued); h.t[S] = inner_stamp(); }
inline void end(Hdr& h) { h.t[TxEnqueued] = edge_stamp(); }
inline void derive(Hdr& order, const Hdr& tick, std::uint64_t cl_ord_id) {    // §74.2.3: the causal link
    order = tick; order.parent = tick.corr; order.corr = cl_ord_id;              // t[0..2] inherited, t[3..4] still 0
}
}  // namespace trace
```

The stamping discipline: `begin` runs where the packet is first touched — in the feed handler's poll loop, *after* the NIC descriptor says the frame is complete and *before* the decoder runs, which is the earliest software point and the one the hardware RX stamp (Ch. 57 §57.4.2) is compared against to measure DMA-to-first-touch (Ch. 58, 68). `mark<Decoded>` follows the Ch. 54 decoder, `mark<Decided>` the strategy's decision, `mark<RiskOk>` the pre-trade checks (Ch. 76); `end` is taken by the gateway immediately after the order frame is handed to the NIC TX ring (Ch. 57, 61, 64), so that `t_hw_tx − t[4]` is the NIC's own egress latency and nothing else. Stamps that would land inside a stage (inside the book update, say) are not stage stamps; if you need them, they are Ch. 81's flight-recorder records, not header fields.

### 74.4.2 Carrying the header through rings, Disruptor slots and shm segments

The header travels in the slot. For Ch. 34's SPSC ring and Ch. 51's cross-process ring the slot type simply gains a `trace::Hdr` member and pads to 128 bytes:

```cpp
struct alignas(128) MdSlot {            // feed handler -> strategy (shm ring, Ch. 51)
    trace::Hdr tr;                      // 56 B
    BookDelta  d;                       // 40 B (Ch. 25/54)
    std::uint8_t pad[128 - 56 - 40];
};
struct alignas(128) OrderSlot {         // strategy -> gateway
    trace::Hdr tr;                      // 56 B, derived from the tick's header (trace::derive)
    NewOrder   o;                       // 48 B (Ch. 54's OUCH/iLink encoder input)
    std::uint8_t pad[128 - 56 - 48];
};
static_assert(sizeof(MdSlot) == 128 && sizeof(OrderSlot) == 128);
```

Three details make this work across *processes* (Ch. 78) rather than threads. The header holds **no pointers** (Ch. 78 §78.5: raw pointers in shared memory are garbage in the other process) — it is six integers. The stamps are **raw TSC**, valid in any process on the host because it is one counter (§74.2.1); a pipeline that spans two hosts (a remote risk check, Ch. 65) converts at the boundary using each host's PHC cross-timestamp (Ch. 60 §60.4.1) and carries *nanoseconds UTC* from there — the header's integers are then UTC ns, and the stage that crosses the host boundary is attributed in UTC. And the header is **copied, not shared**, at every ring hop: the strategy reads the `MdSlot`, derives the `OrderSlot` header from it and publishes the new slot — the Disruptor's multi-stage-on-one-ring design (Ch. 37 §37.4.1) is the exception, where the stages append to the *same* slot's header in sequence-barrier order and the header's single-writer-at-a-time property comes from the barrier.

The message's ID across hops is the `corr` field, so the *feed handler* decides the market-data correlation ID — the venue sequence number straight from the packet header (MoldUDP64, EOBI, MDP 3.0 — Appendix J.5), which Ch. 55's gap detector has already parsed. No hash, no map, no counter.

### 74.4.3 Contention-free correlation IDs and the tick → order link

Orders need an ID you generate: the `ClOrdID` the venue echoes back on every acknowledgement, fill and reject. Make it do double duty as the trace ID:

```cpp
struct ClOrdIdGen {                                 // one per strategy *process*; never shared, never atomic
    std::uint64_t next;                             // high 16 bits: shard id (Ch. 78 §78.4.5); low 48: per-process counter
    explicit ClOrdIdGen(std::uint16_t shard, std::uint64_t restart_epoch)
        : next((std::uint64_t(shard) << 48) | (restart_epoch << 32)) {}    // epoch keeps IDs unique across restarts
    std::uint64_t operator()() { return next++; }   // one add; the venue requires uniqueness per session, this gives it per fleet
};
```

A shared atomic counter across strategy processes would put a contended line on the order path (Ch. 33 — a ~70–100 ns transfer per order on the reference machine); a per-process counter with a shard prefix is one register increment and still unique fleet-wide. The restart epoch (the supervisor's restart count for this shard — Ch. 78 §78.4.3) keeps a restarted process from re-issuing IDs the venue has seen. For venues whose `ClOrdID` is a string (FIX tag 11) encode the integer as fixed-width hex; for iLink 3's `ClOrdID` (20-char) and OUCH's `Order Token` (14 bytes) the same. The gateway logs `(ClOrdID, parent seq, t[0..4], t_hw_tx)` once per order; when the venue's acknowledgement arrives, the gateway logs `(ClOrdID, venue fields..., t_hw_rx_ack)` as a second record with the same `corr` — and the two join offline on `corr` without any state kept on the hot path.

### 74.4.4 Off-path aggregation: from stamps to per-stage histograms

The gateway, at `end()`, hands the finished header to two consumers, both off the hot path:

- **The binary log (Ch. 73)** receives the raw record — `{corr, parent, t[0..4], t_hw_rx, t_hw_tx}` — as one 80-byte log entry at the site-ID cost Ch. 73 §73.3 measured (~5–20 ns). This is the *evidence*: every message's complete timeline, decoded offline, joinable to the capture (Ch. 80) by `corr` and to the venue's fills by `ClOrdID`. It is what Ch. 81 reads when one message's stage delta is over budget.
- **The histogram aggregator (Ch. 75)** receives *deltas*: `t[i+1] − t[i]` for each adjacent pair that was reached, plus `t[4] − t[0]` end-to-end, each recorded into its own HdrHistogram in the shm telemetry segment (Ch. 75 §75.4.1). Keep the end-to-end histogram as its own series — per-stage percentiles do not sum to the end-to-end percentile (Ch. 75 §75.2.2), and the gap between Σ p99.9(stage) and p99.9(end-to-end) is itself a diagnostic: a large gap means the slow messages are slow in *different* stages (independent noise); a small gap means one mechanism stretches every stage (a frequency drop, an SMI — Ch. 82).

Where to do the subtraction matters: the gateway thread does the six subtractions and six histogram `record` calls (each a few stores, Ch. 75 §75.3) — ~30–60 ns — or it logs the raw record and a housekeeping thread subtracts. Choose the former only if the gateway's budget has room; the latter costs one log entry and moves everything else off-core.

### 74.4.5 Exchange-provided timestamps: attributing the budget between you, the wire and the matching engine

Every major venue timestamps what it sends you. The fields below are the ones that matter; Appendix J.5 tabulates them per protocol with offsets.

```
   venue / protocol          field                        meaning (venue's PTP-disciplined UTC clock)
   ───────────────────────── ──────────────────────────── ───────────────────────────────────────────────────────────────
   CME MDP 3.0 (market data) SendingTime (packet header)  when the feed component sent this UDP packet            [egress]
                             TransactTime (per message)   when the matching engine processed the event            [engine]
   CME iLink 3 (orders)      RequestTime (you -> them)    your own stamp, echoed back on the response             [echo]
                             SendingTimeEpoch (them)      when the gateway sent the message to you                [egress]
                             TransactTime (ExecutionReport) matching-engine time of the fill/ack                  [engine]
   Eurex T7 EOBI (md)        TransactTime (packet/msg)    matching-engine time of the event                       [engine]
                             TrdRegTSTimePriority         the order's time priority (queue position!)             [priority]
   Eurex T7 ETI (orders)     TrdRegTSTimeIn               gateway received your request (first byte in)           [gw-in]
                             TrdRegTSTimeOut              gateway sent the response                               [gw-out]
                             TrdRegTSEntryTime / ExecutionTime  engine accepted / matched                         [engine]
   Nasdaq ITCH 5.0 (md)      Timestamp (u48 ns since midnight) when the event occurred in the matching engine   [engine]
   Nasdaq OUCH 5.0 (orders)  Timestamp (u64 ns since midnight) on Accepted/Executed/Canceled responses           [engine]
   FIX (any)                 52 SendingTime, 60 TransactTime  ms or µs resolution; venue-dependent              [egress/engine]
```

With your NIC's hardware RX/TX stamps on the same UTC (Ch. 60) the attribution is subtraction, and each difference has a name:

- **One-way feed latency** = `t_hw_rx − SendingTime` (CME) or `t_hw_rx − TransactTime` (EOBI/ITCH, which has no separate egress stamp — so this number includes the venue's own engine→feed→wire time). On a co-located box this is single-digit microseconds and *flat*; a drift in its median is the fabric (Ch. 62) or your PTP (Ch. 60); a spike on one packet with a flat neighbourhood is a venue-side or switch-queue event, not yours. Plot it per feed line: the A/B skew (Ch. 55) is `t_hw_rx(A) − t_hw_rx(B)` for the same sequence number.
- **Your tick-to-trade** = `t_hw_tx − t_hw_rx` (Ch. 60 §60.4.2), decomposed per stage by the header. This is the only part you control.
- **Wire out** = `TrdRegTSTimeIn − t_hw_tx` (Eurex gives it directly; CME's `RequestTime` echo lets you compute `SendingTimeEpoch − RequestTime` as the round trip *minus* your path, and iLink 3's ack timestamps give gateway and engine times). A co-located wire-out of ~1–3 µs that jumps to 30 µs for a burst of orders is the venue's gateway queue — the throttle of Appendix K.4 — not your NIC.
- **Their time** = `TrdRegTSEntryTime − TrdRegTSTimeIn` (gateway→engine) and `TrdRegTSTimeOut − TrdRegTSExecutionTime` (engine→gateway). These are the matching engine's own queueing and are the same for every participant *in principle*; in practice they vary per gateway and per partition, which is why desks measure them per session.
- **Queue position** (Eurex) = compare your order's `TrdRegTSTimePriority` with the priority stamps of the resting orders at the same price level in EOBI's `OrderAdd` messages: the count ahead of you is your queue position at entry, and the lag `TrdRegTSTimePriority − t_hw_tx` is how long the venue took to give you a place in line. CME publishes order-level priority in MBO (MDP 3.0 `MDOrderPriority`) rather than a timestamp.

The caveats are not footnotes. **Both sides must be disciplined to the same UTC**: MiFID II RTS 25 requires HFT-class participants and venues to stay within 100 µs of UTC with 1 µs granularity, and most venues do far better — but 100 µs is the *guarantee*, and a one-way feed latency computed against a venue clock that is 30 µs off is a 30 µs lie. Verify by *symmetry*: with the venue's `TrdRegTSTimeIn` and `TrdRegTSTimeOut` and your own `t_hw_tx`/`t_hw_rx` you have four stamps on two clocks, and `(TimeIn − t_tx) − (t_rx_ack − TimeOut)` should be near zero on a symmetric path; a persistent offset is clock error, not asymmetry (the same argument PTP makes about its own path — Ch. 60 §60.5). **Resolution ≠ accuracy**: a nanosecond field written by a software gateway from `clock_gettime` is good to the gateway host's PTP (~hundreds of ns to a few µs), not to the nanosecond. **Egress ≠ wire**: `SendingTime` is taken when the feed component *handed the packet to its stack*, not when the first bit left the venue's switch port; the venue's own egress jitter is inside your "one-way feed latency". And **per-gateway, per-partition**: the venue's stamps come from the machine that handled *your* session or *your* products' partition; compare like with like.

### 74.4.6 The per-packet ledger: joining a tap capture to the host's stamps

When the host's numbers and the venue's disagree, the arbiter is a fabric tap (Ch. 62 §62.4.3, Ch. 80 §80.4.1) that timestamps every frame on its own PTP-disciplined clock as it passes. Join it to the trace record with one script per side:

```bash
# tap side: one line per frame — tap timestamp, MoldUDP64 sequence number (bytes 10–17 of the UDP payload), length
tshark -r tap.pcapng -Y 'udp.port==26477' -T fields -e frame.time_epoch -e data.data -e frame.len \
  | awk '{ seq = strtonum("0x" substr($2, 21, 16)); printf "%s %d %s\n", $1, seq, $3 }' > tap.ledger
# host side: the gateway's trace records, decoded from the Ch. 73 binary log, one line per order:
#   corr parent t_hw_rx(UTC ns) t0..t4 (TSC) t_hw_tx(UTC ns)  — with TSC converted by the Ch. 60 §60.4.1 cross-timestamp
blogdecode trace.blog --tsc2utc=cross.json > host.ledger
# join on the venue sequence number (the tick) and on ClOrdID (the order), then difference adjacent columns:
join -1 2 -2 2 <(sort -k2 tap.ledger) <(sort -k2 host.ledger) | awk '{ printf "seq %d  tap->nic %.0f ns  nic->touch %.0f ns  touch->tx %.0f ns\n",
    $1, ($4 - $2*1e9), ($5 - $4), ($10 - $5) }' | sort -k4 -n | tail -20        # the 20 worst tap→NIC deltas
```

`pcapng` carries each interface's timestamp resolution (`if_tsresol`) and the capture hardware's PTP state in its interface block; `tshark -T fields -e frame.time_epoch` emits them at the stored resolution. The join key is the venue sequence number for market data (it is in both the frame and the header) and the `ClOrdID` for orders (it is in the frame's payload and in the header). The resulting ledger has one row per message with up to eight stamps on it — tap-in, NIC-RX, first touch, decoded, decided, risk, TX-enqueued, NIC-TX, tap-out — and the differences between adjacent columns *are* the attribution: tap-in→NIC-RX is the last switch hop plus your NIC's ingress (Ch. 57 §57.3 measures the NIC alone, Ch. 62 the hop), NIC-RX→first-touch is DMA, DDIO and the poll loop's reaction (Ch. 58, 68), the middle columns are the header's stages, NIC-TX→tap-out is your egress hop. For an FPGA stage on the path (Ch. 71) the fabric's own stage stamps, exported in a trailer the host strips, slot into the same row; Ch. 71 §71.4 notes the trailer, this ledger is where it is read.

## 74.5 Verify the codegen: a stage boundary is a counter read and a store

```
   $ g++ -O2 -std=c++20 -march=icelake-server -c trace_use.cpp && objdump -d --no-show-raw-insn -C trace_use.o
   ...                                   # mark<Decided>(hdr) inlined into the strategy's decide():
      rdtsc                              # inner_stamp(): unordered, 2 µops on Ice Lake (Ch. 17 §17.3: ~20–25 cycles latency)
      shl    $0x20,%rdx
      or     %rdx,%rax
      mov    %rax,0x20(%rdi)             # hdr.t[Decided] = stamp — one store into the slot's first line
   ...                                   # end(hdr) in the gateway:
      rdtscp                             # edge_stamp(): ordered after prior loads
      shl    $0x20,%rdx
      or     %rdx,%rax
      mov    %rax,0x30(%rdi)             # hdr.t[TxEnqueued]
```

Three instructions and a store per stage, exactly as §74.3 predicted (GCC 13.3 and Clang 18.1, `-O2 -march=icelake-server`, checked with `objdump`); `trace::derive` is two 32-byte `vmovdqu` copies plus two scalar stores (56 bytes copied, `parent` and `corr` overwritten) in both compilers. Two things to watch in *your* build: GCC wraps the `rdtscp` out-parameter in a stack-protector canary load/compare (`%fs:0x28`) unless the TU is built `-fno-stack-protector` — the same artefact Ch. 73 §73.5 noted on the logger — and the `rdtscp` form is what makes `end()` ordered after the TX ring's doorbell store only if an `mfence`/release precedes it (Ch. 17 §17.2.1: `rdtscp` waits for prior *loads*, not stores). The one thing to check in *your* build is that `Hdr` sits at offset 0 of a 128-byte-aligned slot (`static_assert(offsetof(MdSlot, tr) == 0)`), so the six stamps and the correlation ID share the slot's first line and the payload the second: a header that straddles the line boundary doubles the consumer's dependent loads for no reason (Ch. 9 §9.4).

## 74.6 Pitfalls & anti-patterns

- **Converting to nanoseconds on the hot path.** A multiply-shift per stamp is cheap, but it bakes the TSC frequency into the record and makes a recalibration (Ch. 17 §17.4.2) silently wrong for everything already logged. Stamp raw ticks; convert in the reader with the calibration that was current *at that time* (Ch. 17's `tsc_mult`/`tsc_shift` from the vDSO data, or the PTP cross-timestamp).
- **Comparing TSC to the venue's clock.** The TSC is not UTC. Compare venue stamps to NIC hardware stamps (both UTC); bridge TSC→PHC once per record with `PTP_SYS_OFFSET_PRECISE` (Ch. 60 §60.4.1) and carry its error bound. A "feed latency" of −2 µs is this bug, not time travel.
- **A generated trace ID.** A UUID, a hash of the payload, or a shared atomic counter — all of them cost more than the stamps and none is needed: the venue sequence number and the `ClOrdID` already identify the message. If you must synthesise an ID, make it per-process (§74.4.3).
- **A growable list of (name, time) pairs.** Variable-size headers break the fixed slot layout (Ch. 9), need a length field and a bounds check per append, and tempt every stage to add "just one more" stamp until the header is a log. Fixed vector, design-time stage enum; everything else is Ch. 81's flight recorder.
- **Stamping inside a stage.** A stamp around the book update's inner loop is not a stage boundary; it inflates the header and, taken with `rdtscp`, serialises a loop that Ch. 11 taught you to keep in flight. Stage stamps belong at ring hops and at the two NIC edges.
- **Adding per-stage percentiles into an end-to-end number.** Ch. 75 §75.2.2: percentiles are not additive. Record the end-to-end delta as its own histogram and read the Σ-vs-end-to-end gap as a signal (§74.4.4).
- **Trusting a venue timestamp's resolution as its accuracy.** A nanosecond field from a software gateway is a microsecond-class number; a `SendingTime` is an egress-to-stack time, not a wire time; the venue's clock may be anywhere inside its stated tolerance (§74.4.5). Verify with the symmetry check before you report a one-way number to anyone.
- **The header as the only evidence.** Five stamps say *where* the time went, not *why*. The why is Ch. 81: the flight recorder frozen on the over-budget record, the kernel trace, the host counters — all keyed by the same `corr`.
- **Letting the header leak onto the wire.** The trace header is host-internal; the encoder (Ch. 54) must not copy it into the order frame (one venue's message-length check rejects it, another's silently accepts 56 bytes of your timing as padding). The slot layout keeps it separate from the payload; the encoder takes `&slot.o`, never `&slot`.

## 74.7 Exercises & checklist

**Exercises**

1. Rebuild `tracehdr1.cpp` with `rdtsc` for the intermediate stamps and `rdtscp` only at the edges; measure the delta. Then move `TraceHdr` to the *end* of the 128-byte slot and measure a two-thread version (producer on one core, consumer on another, ring wrapping) with the Ch. 3 harness: does the consumer's dequeue cost change when the header and the payload straddle the line boundary?
2. Add `trace::Hdr` to Ch. 34's SPSC ring and Ch. 51's cross-process ring, with `static_assert`s on size and offset. Run the Ch. 78 three-process pipeline and verify, from the gateway's log alone, that `t[0] < t[1] < t[2] < t[3] < t[4]` for every order and that `parent` names a sequence number that the feed handler logged.
3. Build the aggregator: six HdrHistograms (five stage deltas + end-to-end) in the Ch. 75 segment, fed by the gateway. Replay a captured microburst (Ch. 80) and report Σ p99.9(stage) against p99.9(end-to-end). Explain the gap.
4. Decode `SendingTime`/`TransactTime` (CME MDP 3.0) or `TransactTime` (EOBI) in your Ch. 54 decoder and plot `t_hw_rx − SendingTime` for one session. Identify the three regimes you see (pre-open, open, continuous) and the spikes; for each spike, check whether the neighbouring packets moved too (your side) or not (theirs).
5. With an order-entry simulator that echoes `TrdRegTSTimeIn`/`TimeOut` (or a venue test environment), implement the symmetry check of §74.4.5 and inject a 20 µs offset into the simulated venue clock. Show that the check detects it and the raw one-way numbers do not.
6. Build the §74.4.6 ledger from a `pcapng` taken on a mirror port (or a software `tcpdump -j adapter_unsynced` on a second host as a stand-in) and the gateway's log. Sort by tap→NIC delta; what is the floor, and is it the switch hop Ch. 62 measured?

**Checklist**

- [ ] Every message in the pipeline carries a `trace::Hdr` in its slot: `corr` (venue seq / `ClOrdID`), `parent`, five raw-TSC stage stamps; no pointers, no strings, no conversion on the hot path.
- [ ] Stamping points: first touch (edge, `rdtscp`), decoded, decided, risk-ok (inner, `rdtsc`), TX-enqueued (edge); nothing inside a stage.
- [ ] Slot is 128 bytes, header at offset 0, `static_assert`ed; the encoder never sees the header.
- [ ] `ClOrdID` = shard ‖ restart-epoch ‖ per-process counter; no shared atomics on the order path.
- [ ] Gateway logs the raw record (Ch. 73) *and* feeds six histograms (Ch. 75); end-to-end kept as its own series.
- [ ] TSC→UTC conversion only in the reader, via the published cross-timestamp with its error bound (Ch. 60 §60.4.1).
- [ ] Venue fields decoded and stored: `SendingTime`/`TransactTime`, `TrdRegTS*`, ITCH/OUCH timestamps (Appendix J.5); one-way feed latency and wire-out plotted per line and per session.
- [ ] Symmetry check running on the order path; a persistent offset is a clock alarm (Ch. 60, 82).
- [ ] A tap ledger script exists and has been run once against a real capture, so the day you need it is not the day you write it.

## 74.8 References

1. Intel Corporation. *Intel® 64 and IA-32 Architectures Software Developer's Manual*, Vol. 3, §18.17 "Time-Stamp Counter" (invariant TSC, `RDTSCP`).
2. CME Group. *MDP 3.0 Market Data — Binary Packet Header and Message Header* (`SendingTime`, `TransactTime`, `MsgSeqNum`); *iLink 3 Binary Order Entry — Message Header and Business Layer* (`SendingTimeEpoch`, `RequestTime`, `TransactTime`). CME Client Systems Wiki.
3. Deutsche Börse / Eurex. *T7 Enhanced Order Book Interface (EOBI) Manual* (`TransactTime`, `TrdRegTSTimePriority`); *T7 Enhanced Trading Interface (ETI) Manual* (`TrdRegTSTimeIn`, `TrdRegTSTimeOut`, `TrdRegTSEntryTime`, `TrdRegTSExecutionTime`, `RequestTime`).
4. Nasdaq. *TotalView-ITCH 5.0 Specification* (Timestamp: nanoseconds since midnight); *OUCH 5.0 Specification*; *MoldUDP64 Protocol Specification*.
5. European Commission. *Commission Delegated Regulation (EU) 2017/574 (MiFID II RTS 25)* — level of accuracy of business clocks (divergence from UTC and timestamp granularity by trading activity).
6. IEEE Std 1588-2019, *Precision Clock Synchronization Protocol*; Linux `PTP_SYS_OFFSET_PRECISE` ioctl, `Documentation/driver-api/ptp.rst`.
7. Linux man pages: `tshark(1)`, `join(1)`; *PCAP Next Generation (pcapng) Capture File Format*, IETF draft-ietf-opsawg-pcapng (interface description block, `if_tsresol`).
8. Gregg, B. *Systems Performance*, 2nd ed., Addison-Wesley 2020 — Ch. 4 "Observability Tools" (tracing vs sampling, the cost of instrumentation).

## 74.9 Additional Reading

- OpenTelemetry Specification, *Trace Context (W3C)* — the general-purpose shape of correlation and span propagation; useful as a contrast to the fixed-header design here (why the hot path cannot afford it).
- Cook, C. "When a Microsecond Is an Eternity: High Performance Trading Systems in C++", CppCon 2017 — measurement discipline on the order path.
- Nyberg, T. et al. *LMAX Disruptor Technical Paper* — the multi-stage single-ring pipeline whose slots are the natural home for a stage-stamp vector (Ch. 37).
- Corbet, J. "Timestamping network packets", LWN.net — `SO_TIMESTAMPING` and hardware RX/TX stamps from the host's side (Ch. 57).
- FIX Trading Community, *FIX Latency Benchmarking Best Practices* — vendor-neutral definitions of gateway-in/out and engine timestamps.

---

*Next: Ch. 75 — Zero-Overhead Application Telemetry, the metrics sibling of this chapter and of Ch. 73: how the stage deltas this chapter produces become counters, gauges and per-stage latency histograms that leave a pinned busy-poll process without a syscall, why percentiles must be merged and never averaged, and the shared-memory segment and off-core scraper that turn them into SLOs, alerts and triggers.*
