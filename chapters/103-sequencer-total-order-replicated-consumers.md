# Part XIII — Observability & Operations in Production

# Chapter 103 — The Sequencer: Total-Order Broadcast & Replicated Deterministic Consumers

> **Prerequisites:** Ch. 96 §96.2.2 (the deterministic state machine — `apply(state, input)`; this chapter produces the single ordered input stream it presupposes) and §96.3 (single-host restart time — the baseline a replica beats), Ch. 101 (the journal and snapshot + replay catch-up — the sequenced stream *is* the journal), Ch. 45–46 (one-ring broadcast with sequence barriers; the hop-cost model — the sequencer is one more hop), Ch. 64 and Ch. 68 (shm rings and reliable multicast — the two fan-out transports; A/B arbitration is what the sequencer's feed input has already done), Ch. 47 (the slow-replica policy), Ch. 52 §52.2.3 (timer fires as events), Ch. 73 (hardware timestamps — how the added hop is measured), Ch. 80 §80.4.1 (Aeron Cluster — the off-the-shelf comparison), Ch. 97–98 (the gate decisions and order transitions that become sequenced events).
>
> **Leads into:** Ch. 105 (spike forensics — a replica's divergence digest is the first forensic input), Ch. 106–107 (the sequencer's own health is a host and network telemetry concern), Ch. 111 (the capstone's architecture gains the sequencer and a replica). Not in this chapter: leader leases, fencing tokens and split-brain prevention on the *order path* — that is Ch. 104 (Appendix F defines *fencing token*); this chapter scopes to producing one order, consuming it identically in many places, and standing a replica up in place of a dead primary.

---

## 103.1 Why it matters: one truth, or the replay lies

Ch. 96 §96.2.2 made the core a fold over an input stream and promised exact replay; Ch. 101 journaled the stream; Ch. 96 §96.4.6 compared a live run's digest with its replay's to find the first diverging event. All of it assumes something the book never built: **a single, total order over every input** — ticks (after A/B arbitration, Ch. 68), order acks and fills (Ch. 98), timer fires (Ch. 52), operator commands, configuration changes (Ch. 95) — agreed on by every component that applies them. In Ch. 96's reference topology the inputs arrive over point-to-point rings: the strategy sees the feed in one order, the gateway sees acks interleaved with the strategy's orders in *its* order, the risk process sees fills and limit updates in a third; each process's journal is its own truth, and "replay the system" means reconciling three logs whose relative order was never recorded. The **sequencer** is the component that removes the ambiguity: one process (or one switch, or one FPGA — Ch. 75, 84) receives every input, stamps a global sequence number, and fans the sequenced stream out; every consumer applies *that* stream, in *that* order. It is the dominant architecture of exchanges' matching systems (Nasdaq INET's, CME's, every LMAX-style venue) and of many trading firms' cores — and the book has a single glossary line for it (Appendix J.6).

What it buys, in order of importance. **Bit-identical replicas**: a gateway, a risk engine, a strategy, a journal writer and a GUI feed that all fold the same sequenced stream through deterministic `apply` functions hold states that agree to the bit — verifiable by comparing a running digest (§103.3 shows two replicas and a replay from the log producing the same digest over 400 000 events, and a replica that peeks at a clock diverging on the first run). **Replay that is one log**, not three. **Hot standby for free**: a second copy of any consumer, already applying the stream, is a replica that can take over in the time it takes to tell it to start emitting — against Ch. 96 §96.3's restart-and-replay path. **Position and risk views that agree** because they were computed from the same fills in the same order. The price is **one hop** on the critical path (§103.3: ~450 ns on the VM, ~100–150 ns on the reference machine over a shm ring, more over multicast) and a new single point of failure — the sequencer itself — whose failover is §103.4.5.

## 103.2 Mental model

### 103.2.1 The sequencer and its stream

```
   INPUTS (unordered, many sources)                 SEQUENCER (one process, one core, one writer)            SEQUENCED STREAM (the one truth)
   ──────────────────────────────────               ──────────────────────────────────────────────           ─────────────────────────────────────────────
   feed A/B → arbitrated ticks (Ch. 68) ─┐           poll sources in fixed order, bounded batch (Ch. 46)       seq  t_seq  kind  payload
   venue acks / fills / rejects (Ch. 98) ─┤           stamp: seq = next++, t_seq = rdtsc()                    1    …      Tick   …
   timer fires (Ch. 52)                  ─┼─► merge ► append to the log ring (Ch. 45: one ring, N readers)    2    …      Fill   …      ─► journal writer (Ch. 101): the log IS the journal
   operator commands / config (Ch. 95)   ─┤           publish cursor (release)                                3    …      Timer  …      ─► gateway, risk, strategy, GUI: each a REPLICA
   strategy outputs (orders) ← also inputs!─┘          fan-out: shm ring on-host (Ch. 64) / multicast (Ch. 68) 4    …      Cmd    …          of the same fold, holding identical state
                                                      across hosts, with gap detection + retransmit
   the strategy's ORDER is an event in the stream too: the gateway acts on it when it reads it, so "what did the strategy decide at seq 3" is a fact every replica shares
```

- **One writer stamps.** The sequencer is the only component that assigns `seq`; it is single-threaded on one core (Ch. 46 §46.4.2's loop with the inputs as sources), it does not interpret payloads, and its only state is the next sequence number and the ring cursor. Its per-event cost is a copy into the log slot and a cursor store — tens of nanoseconds; §103.3 measures the hop it adds.
- **The stream is a Disruptor ring with many readers** (Ch. 45 §45.4.1's one-ring fan-out) on one host — each consumer tracks its own sequence, the slowest gates the sequencer's wrap (which is Ch. 47's BLOCK policy, legitimate here only because every consumer is *supposed* to keep up; §103.4.4 handles the one that does not). Across hosts it is a **reliable multicast** of the same records (Ch. 68's machinery, now on the *output* side: sequence gap detection, NAK/retransmit from the sequencer's or the journal's buffer) or a transport like Aeron (Ch. 80).
- **Outputs are inputs.** A strategy's decision to send an order is *published back into the sequencer* as an event; the gateway sends when it reads that event from the stream. This closes the loop that makes everything replayable: the order's position in the total order relative to the ticks that caused it and the fills that followed it is recorded by construction, and a replica of the strategy that reads the same stream makes the same decision at the same `seq` (which is also how hot standby works — §103.2.3).
- **Time is stamped by the sequencer**, once, into every record (`t_seq`); consumers never read a clock (Ch. 96 §96.2.2, Ch. 52 §52.2.3). The sequencer's clock is the system's clock.

### 103.2.2 Replicated deterministic consumers and the digest

Every consumer is `state = apply(state, event)` over the stream (Ch. 96 §96.2.2); two consumers running the same `apply` from the same snapshot over the same stream hold the same state — **state-machine replication** (Schneider 1990), with the sequencer as the agreement protocol reduced to its cheapest form: there is one proposer, so there is nothing to agree on. The property is checked, not assumed: each replica folds a **running digest** (a 64-bit hash of `(seq, state-delta)` — Ch. 96 §96.4.6) and publishes it every *k* events; a monitor compares replicas' digests at equal `seq`, and a disagreement names the first event at which two copies of the "same" program did different things — a clock read, uninitialized memory, a data race, a different binary, a floating-point reduction in a different order (Ch. 34). §103.3's harness does exactly this: two replicas and a replay from the log agree; a replica that consults `rdtsc()` inside `apply` does not.

### 103.2.3 Standby: hot, warm, cold — RPO, RTO and what each survives

```
   mode            what the standby does continuously                   RPO (data lost)            RTO (time to serve)                      survives
   ─────────────   ─────────────────────────────────────────────────    ────────────────────────   ──────────────────────────────────────   ───────────────────────────────────────
   HOT replica     applies the live stream, OUTPUT SUPPRESSED;          0 (it has every event      the election: tell it to emit (µs–ms)   process, host (on another host),
                   same state, same digest, as the primary             the primary applied)         + session takeover (Ch. 98 §98.4.4)      rack (on another rack); needs the stream
   WARM standby    receives the JOURNAL (shipped records, seconds       the shipping lag           catch-up replay of the lag (§103.3:        host, rack, site (journal shipped off-site);
                   behind), applies lazily; process is up               (seconds of events)         ~3 ns/event → 10⁶ events in ~3 ms)       does not need the live stream at the site
                                                                                                    + session takeover
   COLD restore    nothing; a snapshot (Ch. 101 §101.4.4) exists           since the last snapshot    process start + snapshot load + replay    anything, slowly; the Ch. 96 §96.3 path
                                                                        (seconds–minutes)           from the journal + warm-up (Ch. 59)
```

- **Hot standby is a replica with its outputs discarded.** It holds the gateway's in-flight table (Ch. 98) and the risk accumulators (Ch. 97) to the bit; on failover it is told to start emitting (one control event *in the stream*, so every replica agrees on the exact `seq` at which emission moved) and takes over the venue session (logon with the true sequence numbers — Ch. 98 §98.4.4). RTO is dominated by the venue session, not by state.
- **Warm standby is journal shipping**: the sequenced log (which is the journal) streamed to another host or site; the standby applies it as it arrives or on demand. Its RPO is the shipping lag; its RTO is the catch-up — §103.3 measures the fold at ~3 ns/event, so even a minute's backlog at 100 k events/s is ~20 ms of replay.
- **Cold restore is Ch. 96 §96.3's path**, kept for the failures the others cannot survive (a bad deploy that corrupts every replica identically — determinism replicates bugs perfectly).
- **The single-emitter rule**: at any instant exactly one replica of the gateway emits to the venue; everything else is suppressed. Enforcing that across a partition (leases, fencing tokens, the venue's own session exclusivity) is Ch. 104; within one host and one sequencer it is the control event's position in the stream.

### 103.2.4 The sequencer as the single point of failure

Everything downstream is replicable *because* there is one sequencer; the sequencer itself is therefore the one component whose failure stops the system. The mitigations are standard and §103.4.5 details them: a **standby sequencer** that reads the same stream (it knows the last `seq`) and the same inputs, promoted by the supervisor (Ch. 96 §96.4.3) with the venue's cancel-on-disconnect as the backstop during the gap; **inputs that are themselves sequenced** (the feed's venue sequence numbers, the session's `MsgSeqNum`s — Ch. 98) so a new sequencer can prove it has not skipped or duplicated an input; the **journal as the recovery point** (the last published `seq` is on disk/in shm within microseconds — Ch. 101 §101.4.3). The alternative — consensus among several sequencers (Raft/Paxos, Aeron Cluster — Ch. 80 §80.4.1) — removes the SPOF at the cost of a quorum round trip per batch (tens of µs across hosts), which is why it appears *behind* the hot path (the cluster that owns the journal and the standby election) and not on it.

## 103.3 Measure it: the hop the sequencer adds, the digest, and catch-up

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -march=native -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 46 §46.3 (TSC 2.70 GHz, no isolation). Four spinning threads on four vCPUs leave the hypervisor nothing, so the tails here are steal, not design; medians and the hop ratios transfer (divide the hop figures by ~2.5–3 for the reference machine's line-transfer cost). Labelled VM-measured.*

**The harness.** A producer (core 1) emits two interleaved input streams at 1 µs intervals — three "ticks" then one "ack", 400 000 events — into two SPSC rings; a **sequencer** (core 2) polls both with a bounded batch per source (Ch. 46 §46.4.2), stamps `seq`, appends to a 2²⁰-slot log ring and publishes its cursor; two **replicas** (cores 3 and 0) each fold the log into a `State{px[256], pos[256], digest}` with the same `apply`; replica A also echoes each tick's producer timestamp back on a return ring so the producer measures the **round trip producer → sequencer → replica → producer** on its own clock (Ch. 46 §46.3's method). Afterwards the log is replayed single-threaded into a third `State`, and a fourth replica applies the log while consulting `rdtsc()` inside `apply`.

```cpp
// seqr.cpp (abridged). Build: g++ -O2 -std=c++20 -march=native -pthread seqr.cpp -o seqr
struct Ev { std::uint64_t seq, t, payload; std::uint32_t kind, inst; };
static Ev* logbuf; alignas(64) static std::atomic<std::uint64_t> cursor{0};             // the sequencer's single-writer cursor: one ring, N readers (Ch. 45)
struct State { std::int64_t pos[256], px[256]; std::uint64_t digest = 0xcbf29ce484222325ull;
    void apply(const Ev& e) { if (e.kind == 0) px[e.inst] = std::int64_t(e.payload); else pos[e.inst] += std::int64_t(e.payload & 0xff) - 128;
        digest ^= e.seq ^ e.payload ^ (std::uint64_t(e.inst) << 32); digest *= 0x100000001b3ull; } };     // running digest: FNV-style over (seq, delta) — Ch. 96 §96.4.6
// sequencer: fixed source order, ≤ 8 per source per iteration so neither starves the other; stamp, append, publish
std::thread seqt([&] { pin(2); std::uint64_t v, s = 0; for (;;) {
    int n = 0; while (n < 8 && feed.pop(v)) { logbuf[s & (LOG - 1)] = {s, v, v * 0x9E3779B97F4A7C15ull, 0, std::uint32_t(v & 255)}; ++s; ++n; }
    n = 0;     while (n < 8 && acks.pop(v)) { logbuf[s & (LOG - 1)] = {s, v, v, 1, std::uint32_t(v & 255)}; ++s; ++n; }
    cursor.store(s, std::memory_order_release); _mm_pause(); } });
// replica: batch-consume from its own sequence to the cursor (Ch. 45 §45.4.2); never reads a clock
auto replica = [&](State& st, int cpu, bool echo) { pin(cpu); std::uint64_t next = 0; for (;;) {
    std::uint64_t c = cursor.load(std::memory_order_acquire); if (c == next) { _mm_pause(); continue; }
    for (; next < c; ++next) { const Ev& e = logbuf[next & (LOG - 1)]; st.apply(e); if (echo && e.kind == 0) while (!rtt_back.push(e.t)) _mm_pause(); } } };
```

```
producer → sequencer → replica A → producer (RTT, 3 ring hops)   p50 1 244–1 996 ns    p90 2 030    p99 4.8–20 µs    p99.9 41 µs–1.2 ms  (4 spinners on 4 vCPUs: steal)
⇒ the sequencer's own hop ≈ 430–480 ns on this host (Ch. 46 §46.3's per-hop figure) ≈ 100–150 ns on the reference machine over a shm ring

sequenced 400 001 events;  digest replica A = 3010d7041d729871   replica B = 3010d7041d729871   replay-from-log = 3010d7041d729871   → IDENTICAL
catch-up: replaying 400 001 events into a fresh State took 1.28–1.37 ms = 3.2–3.4 ns/event  (≈ 300 M events/s)
replica that consults rdtsc() inside apply():  digest 1d887fa021e774e8   → DIVERGED (on the first run; every run)
```

Reading it:

- **The price of one truth is one hop.** Against Ch. 46 §46.3's point-to-point rows, the sequencer inserts one ring hop between producer and consumer: ~450 ns here, ~100–150 ns on the reference machine with both ends spinning on the same socket (Ch. 96 §96.2.1's per-hop figure), and the sequencer's own work (a 40-byte copy and a release store) is invisible inside it. For the single-process build of Ch. 111 §111.3 that is ~10 % of the software budget; for the process-per-role topology it is one more of the "~3 shm hops" already being paid, and it *replaces* the strategy→gateway and gateway→risk rings rather than adding to them — the sequencer's fan-out is how those roles talk.
- **Replication is exact and cheap to verify.** Two replicas on two cores and a cold replay from the log agree to the bit over 400 001 events; the digest costs two multiplies per event. This is the measurement Ch. 96 §96.4.6 asked for and the one a production monitor publishes every *k* events (Ch. 93).
- **Catch-up is ~3 ns per event** for a state that fits in cache: a warm standby a minute behind at 100 k events/s replays in ~20 ms; a cold restore's replay of a full day (50 M events) is ~150 ms before warm-up — against Ch. 96 §96.3's single-host restart, the state-rebuild term disappears into the session-takeover term. Real `apply` functions (a book update, a gate decision) cost 20–100 ns, so scale accordingly: still sub-second for a day.
- **Determinism is fragile and the digest proves it.** One `rdtsc()` in `apply` — the mildest possible impurity — produced a different digest on the first run. Ch. 96 §96.2.2's rule is not advice; it is the precondition for everything in this chapter, and the digest comparison is how it is enforced in production rather than hoped for.

## 103.4 Techniques

### 103.4.1 Building the sequencer: the loop, the stamp, the log

```cpp
// sequencer.hpp — the whole component: Ch. 46's hot loop with the inputs as sources, appending to a Disruptor-style log ring (Ch. 45) that is also the journal (Ch. 101).
struct alignas(64) SeqRecord { std::uint64_t seq; std::uint64_t t_seq; std::uint32_t kind; std::uint32_t src_seq; std::uint8_t payload[40]; };   // 64 B: one line per event
class Sequencer {
    LogRing<SeqRecord, 1 << 22>& log;                   // shm segment (Ch. 64): readers = gateway, risk, strategy shards, journal writer, standby sequencer
    FeedIn& feed; AckIn& acks; TimerIn& timers; CmdIn& cmds; OrdersIn& orders;   // inputs; `orders` = strategies' decisions coming BACK as events (§103.2.1)
    std::uint64_t next = 0;
    template <class Src> std::uint32_t pull(Src& s, std::uint32_t cap, std::uint64_t now) {
        std::uint32_t n = 0; typename Src::Msg m;
        while (n < cap && s.pop(m)) { SeqRecord& r = log.claim(next); r.seq = next; r.t_seq = now; r.kind = Src::KIND; r.src_seq = m.src_seq; m.copy_into(r.payload); ++next; ++n; }
        return n;                                       // claim() gates on the slowest reader's sequence (wrap protection) — §103.4.4 decides what to do about a slow one
    }
public:
    void run() { for (;;) { std::uint64_t now = rdtsc(); std::uint32_t n = 0;
        n += pull(acks, ~0u, now); n += pull(orders, ~0u, now);        // acks and strategy decisions: unbounded, they are what the gateway is waiting for
        n += pull(feed, 64, now); n += pull(timers, 64, now); n += pull(cmds, 1, now);   // Ch. 46 §46.4.2's priorities and caps
        if (n) log.publish(next); else _mm_pause(); } }  // ONE cursor store per iteration: the fan-out's batch (Ch. 45 §45.4.2)
};
```

- **Stateless beyond `next`**: the sequencer never decodes a payload; adding an input kind is adding a source. Its restart state is `next`, which is in the log (§103.4.5).
- **`src_seq` carries the input's own sequence** (the venue's tick sequence after arbitration, the session's `MsgSeqNum`, the timer id's generation) so a consumer or a replacement sequencer can prove continuity per source.
- **Publish once per iteration**, not per event: the readers batch-consume (Ch. 45 §45.4.2), so a burst of 64 ticks is one cursor store and one line transfer per reader — the per-message-hop queueing of Ch. 46 §46.3's microburst rows does not occur.
- **The log is the journal**: the Ch. 101 writer is just another reader of the ring, draining to storage off the hot path; there is no second journal format and no reconciliation between "what we sequenced" and "what we recorded".

### 103.4.2 Fan-out: shm ring on-host, reliable multicast across hosts

- **On one host**: the ring lives in a `MAP_SHARED` segment (Ch. 64), each consumer is a process with its own sequence word in the segment (Ch. 45's one-ring fan-out across processes); ~100–150 ns from publish to a spinning reader on the reference machine (§103.3). Place the sequencer and its hottest readers on one socket (Ch. 17, Ch. 46 §46.4.3).
- **Across hosts**: the same `SeqRecord`s over UDP multicast (Ch. 68's reliability layer inverted — the sequencer is now the *publisher*; receivers detect gaps by `seq` and NAK; retransmission is served from the ring by the sequencer's housekeeping thread or by the journal writer's buffer, never from the sequencer's hot loop), or over Aeron (Ch. 80 §80.4.1) which implements exactly this with its own flow control. Hardware-timestamp the egress and the far-end ingress (Ch. 73) to measure the cross-host hop (typically 2–5 µs through a switch — Ch. 75).
- **Readers that fall behind** are the policy question of §103.4.4; readers that are *off the box* (GUI, analytics) take a conflated or sampled view (Ch. 47 §47.2.1) fed by a dedicated reader, never the raw stream.

### 103.4.3 Replicas: the fold, the digest, the monitor

- **The replica is Ch. 96 §96.2.2's shell around `apply`**: read from its sequence to the cursor, apply each record, emit outputs to its own output ring — and for a *suppressed* standby, drop them (or better: write them to a shadow ring that the monitor compares with the primary's outputs, event for event — a free end-to-end test that the standby *would* have done the same thing).
- **The digest** (`digest = mix(digest, seq, state_delta)`) is updated inside `apply` from the *delta* the event caused (not the whole state — Ch. 96 §96.4.6) and published every *k* events as `(seq, digest)` into the telemetry segment (Ch. 93); a housekeeping monitor compares all replicas at equal `seq` and raises Ch. 105's forensic trigger on the first mismatch, with the `seq` as the pointer into the journal.
- **Snapshots for catch-up** (Ch. 101 §101.4.4): a replica writes `(seq, state)` every *N* events off-path; a new replica loads the latest snapshot and replays from `seq+1` (§103.3: ~3 ns/event for a cache-resident state). Snapshots are taken at *sequence boundaries*, so every replica's snapshot at `seq` is identical — and comparable.
- **Determinism hygiene** (the list every divergence hunt ends at): no clock, no RNG without a seeded, journaled generator, no floating-point reduction whose order depends on anything but the stream (Ch. 34), no iteration over a hash map (Ch. 31) whose order depends on allocation, no uninitialized memory (Ch. 94), one binary for all replicas (build id in the digest's epoch record — Appendix N).

### 103.4.4 The slow replica: gate, drop, or demote

The sequencer's wrap gate (Ch. 45) blocks the *sequencer* when the slowest reader falls a full ring behind — Ch. 47's BLOCK policy on the system's single most critical producer. The policy, by reader class (Ch. 47 §47.2.1): **critical readers** (gateway, risk) must never gate — their lag is a kill trigger (Ch. 97 §97.4.5: "the facts are stale") long before the ring wraps, and the ring is sized so that a reader that *could* gate it has already been killed; **journal writer**: never gate — its own ring is large and its overflow is drop-and-flag (Ch. 101 §101.5), i.e. it reads the log through a second, larger buffer; **non-critical readers** (GUI, analytics, a standby that is catching up): **demoted** — the supervisor (Ch. 96 §96.4.3) removes a reader that lags beyond a threshold from the gate set (its sequence no longer participates in the minimum), and it re-joins by snapshot + replay (§103.4.3). The sequencer thus never blocks in practice, and the ring's size is a *detection* budget, not a *tolerance* budget.

### 103.4.5 Sequencer failover and the standby sequencer

- **A standby sequencer reads the log** like any replica and so knows `next` to within one publish; it also subscribes to the same inputs (which are themselves sequenced — `src_seq`) but does not stamp. On promotion (the supervisor's decision from heartbeat loss — Ch. 96 §96.4.3; or a crash handler's handoff — Ch. 100 §100.4.3) it: reads the log to its end (`next`), reconciles each input's `src_seq` against the last stamped one (re-reading from the input rings anything the dead sequencer pulled but never published — inputs must be *re-readable* for this: the input rings keep a window, Ch. 68's retransmit buffer for the feed), and begins stamping at `next`. Consumers see a gap-free `seq`; a `SequencerEpoch` record marks the takeover in the stream so every replica agrees when it happened.
- **During the gap** (detect + promote: Ch. 96 §96.3's detect term, tens of µs to ms) nothing is sequenced, so no order can be sent — the gateway is a replica and has no input. That is the safe failure: the venue's cancel-on-disconnect is not triggered (the session is up) and working orders rest; the gateway's ageing timers (Ch. 52) are themselves sequenced events and so do not fire either — a deliberate consequence, bounded by the detect budget, which Ch. 97's monitors watch (a sequencer whose `seq` has not advanced in *T* while inputs are arriving is a kill trigger if *T* exceeds the budget).
- **Two sequencers stamping** is the catastrophic case — two truths — and within one host the supervisor's promotion is the single point of decision; across hosts it is the lease/fencing problem this chapter leaves to its own item. Every record carries the sequencer epoch, so a consumer that sees two epochs interleaved halts and alarms rather than applying both.
- **Consensus where the hop is affordable**: Aeron Cluster (Ch. 80 §80.4.1) replicates the *log* across hosts by Raft and elects the leader; its per-batch commit is a cross-host round trip (tens of µs), which is why it sits behind the on-host sequencer (as the journal's and standby's agreement layer) rather than in the tick-to-trade path.

### 103.4.6 Reducing what the other chapters carry

Ch. 96 §96.2.2's "a single ordered log" is produced here; Ch. 96 §96.4.1's point-to-point rings remain the *minimal* topology and this chapter is the alternative when more than one consumer must agree; Ch. 101's journal becomes the sequenced log's persistence; Appendix J.6's glossary line now points at a chapter; Ch. 111's architecture diagram gains the sequencer and a replica (§111.2 and §111.3's hop note).

## 103.5 Pitfalls & anti-patterns

- **A replica that reads a clock.** One `rdtsc()`, `clock_gettime` or `std::chrono::now()` inside `apply` and the digest diverges on the first run (§103.3). Time is `t_seq` in the record; timers are sequenced events (Ch. 52, §103.2.1).
- **Two emitters.** A standby that emits while the primary is alive doubles every order; emission moves on a control event *in the stream*, so every replica agrees on the `seq` at which it moved; the venue's session exclusivity is the backstop.
- **Two stampers.** Two sequencers after a messy failover produce two truths; the epoch in every record and a consumer that halts on an interleaved epoch are the detection; the supervisor's single decision is the prevention on one host.
- **The sequencer that decodes.** Payload inspection in the stamping loop is state and latency where neither belongs; the sequencer copies bytes and stamps.
- **Outputs that bypass the stream.** A strategy that sends to the gateway over a side ring has made its decision invisible to the replay and to its own replica. Decisions are events.
- **Publishing per event.** One cursor store per event is one line transfer per reader per event — the Ch. 46 §46.3 microburst queue; publish per iteration.
- **Letting a critical reader gate the sequencer.** A slow gateway that stalls the sequencer stalls the system; its lag is a kill trigger well before that (§103.4.4), and non-critical readers are demoted, not waited for.
- **A floating-point fold.** Summation order that depends on anything but the stream (threads, SIMD width — Ch. 36) diverges across replicas on different builds or cores; integers for state, or a fixed reduction order (Ch. 34).
- **Hash-map iteration in `apply`.** Iteration order depends on insertion history and allocation; two replicas with different histories (one restarted from a snapshot) differ. Flat arrays and explicit orders (Ch. 31).
- **Snapshots at arbitrary points.** A snapshot taken mid-batch or at a wall-clock time is not comparable across replicas; snapshot at a `seq`.
- **One copy of the journal.** The log is the system's memory; it is shipped (warm standby) and persisted (Ch. 101) from a dedicated reader, not from the sequencer's loop.
- **Treating the sequencer's hop as free.** It is one ring hop (§103.3); budget it (Ch. 111 §111.3) and keep both ends on one socket.

## 103.6 Exercises & checklist

1. Reproduce §103.3 on isolated cores; add a third replica and a shm-segment fan-out (Ch. 64) with the replicas as processes; confirm the digests and measure the hop with the sequencer on the same and on the other socket (Ch. 17).
2. Introduce each determinism bug of §103.4.3 in turn (clock read, unordered-map iteration, an FP accumulator, an uninitialized field) and record the `seq` at which the digest monitor catches it.
3. Implement snapshot + replay catch-up: snapshot every 10⁵ events, kill replica B, restart it from the latest snapshot and measure time to digest agreement.
4. Build the standby sequencer of §103.4.5 with re-readable input rings; kill the primary mid-burst; verify the stream is gap-free with one epoch record and measure detect + promote against Ch. 96 §96.3.
5. Put the strategy's orders back into the stream (§103.2.1) and show, from the log alone, the exact tick that caused each order and the fill that followed — then hot-standby the gateway with output suppression and compare its shadow outputs to the primary's.
6. Replace the on-host fan-out with reliable multicast to a second host (Ch. 68, 73); measure the cross-host hop wire-to-wire and the time a far replica needs to catch up after a 1-second link loss.

**Checklist**

- [ ] One sequencer stamps every input (ticks, acks/fills, timer fires, commands, config, strategy decisions); it is stateless beyond `next`, decodes nothing, publishes once per iteration.
- [ ] The sequenced log is the journal; every record carries `seq`, `t_seq`, `kind`, `src_seq` and the sequencer epoch.
- [ ] Every consumer is a deterministic fold over the stream; no clock, RNG, FP-order or iteration-order dependence; one binary.
- [ ] Each replica publishes a running digest every *k* events; a monitor compares replicas at equal `seq` and triggers forensics on mismatch.
- [ ] Snapshots are taken at `seq` boundaries; catch-up is snapshot + replay with a measured rate.
- [ ] Hot standby = replica with outputs suppressed; emission moves on a control event in the stream; exactly one emitter per venue session.
- [ ] Critical readers' lag is a kill trigger; non-critical readers are demoted, not waited for; the sequencer never blocks in practice.
- [ ] A standby sequencer exists, inputs are re-readable, failover is measured, and an interleaved epoch halts consumers.
- [ ] The sequencer's hop is in the latency budget; sequencer and hot readers share a socket.

## 103.7 References

- Schneider, F. B. "Implementing Fault-Tolerant Services Using the State Machine Approach: A Tutorial." *ACM Computing Surveys* 22(4), 1990 — state-machine replication; the sequencer as the degenerate agreement protocol.
- Lamport, L. "Time, Clocks, and the Ordering of Events in a Distributed System." *CACM* 21(7), 1978 — total order from a single sequence.
- Birman, K., Joseph, T. "Reliable Communication in the Presence of Failures." *ACM TOCS* 5(1), 1987; Défago, X., Schiper, A., Urbán, P. "Total Order Broadcast and Multicast Algorithms: Taxonomy and Survey." *ACM Computing Surveys* 36(4), 2004 — the fixed-sequencer class this chapter implements.
- Thompson, M. et al. *Disruptor* technical paper, LMAX, 2011, and Fowler, M. "The LMAX Architecture" (2011) — the sequenced-input, replicated-business-logic design in a venue.
- Ongaro, D., Ousterhout, J. "In Search of an Understandable Consensus Algorithm (Raft)." USENIX ATC 2014 — the consensus alternative (Aeron Cluster, Ch. 80).
- Nasdaq. *INET* / *Genium INET* architecture overviews (sequencer-based matching); Chronicle Software, *Chronicle Queue* documentation (a sequenced log as the system of record).

## 103.8 Additional Reading

- Kleppmann, M. *Designing Data-Intensive Applications*, Ch. 10 ("Consistency and Consensus": total order broadcast) and Ch. 12 ("Stream Processing": the log as the system of record).
- Kreps, J. "The Log: What every software engineer should know about real-time data's unifying abstraction." LinkedIn Engineering, 2013.
- Aeron Cluster documentation and Thompson's "Aeron Cluster: Fault-Tolerant Trading Systems" talks — a production replicated-log system with the same determinism requirements.
- Cachin, C., Guerraoui, R., Rodrigues, L. *Introduction to Reliable and Secure Distributed Programming*, 2nd ed., Ch. 6 (total-order broadcast) — the formal treatment.
- Gray, J. "Why Do Computers Stop and What Can Be Done About It?" Tandem TR 85.7, 1985 — process pairs, the original hot standby.

---

*Next: Ch. 104 — Failover Without Split-Brain: Leases, Fencing & Session Takeover on the Order Path: the question this chapter left open — when may the standby gateway start *sending*? Failure detection over two independent paths, a lease on a monotonic clock, the epoch as a fencing token checked on every message, the venue's own cancel-on-disconnect and session exclusivity, and a measured detect → promote → first accepted order of ~1.8 ms on the VM with a 2 ms lease, with a resumed zombie's stale-token orders fenced.*
