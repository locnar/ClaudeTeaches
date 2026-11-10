# Part IX — Market Data, NIC & Fabric

# Chapter 71 — Reliable Multicast & Feed Recovery

> **Prerequisites:** Ch. 67 (multicast sockets, buffer sizing), Ch. 68 (TCP internals — the recovery channel is often TCP), Ch. 69 (zero-copy decoding, A/B arbitration, sequence-gap detection — this chapter extends the recovery half), Ch. 100-preview (the deterministic state machine that a rebuilt book must feed), Ch. 78-preview (the fabric that fans out the feed), Ch. 1 (the tail — recovery must not create one).
>
> **Leads into:** Ch. 72 (multicast membership and dark-feed detection — the silent failure that produces no gap for this chapter to recover), Ch. 73–74 (the receive hardware under the feed), Ch. 100 (restart/late-join rebuild as a fault domain), Ch. 105 (capturing both lines for deterministic replay across gaps). The recovery deep-dive Ch. 69 pointed at: what to do when the lossy multicast feed drops a packet and the strategy would otherwise be blind.

---

## 71.1 Why it matters: market data is lossy, and a gap blinds the strategy

Market data arrives as **UDP multicast** (Ch. 67): the exchange sends each update *once*, to a multicast group, and the fabric (Ch. 78) fans it out to every participant. Multicast is the right design — one send reaches thousands of subscribers with no per-subscriber cost — but UDP is **unreliable by construction**: there is no retransmission, no acknowledgment, no ordering guarantee. Packets are dropped — in the fabric under a microburst (Ch. 69), in the NIC's RX ring when it overflows (Ch. 69, 73), in the kernel, in your own slow consumer. And a dropped market-data packet is not a minor blip: it is a **gap in your view of the order book** (Ch. 32). Miss the packet that added a large order to the bid, and your book is *wrong* — you think there's liquidity that isn't there, or you miss liquidity that is. Trading on a book with an undetected gap is trading on a lie (Ch. 98's "the wire is untrusted" extended to "the wire is *lossy*").

Ch. 69 established the first line of defense: **sequence numbers** on every message and **A/B line arbitration** (the exchange sends two identical multicast feeds on independent paths; you take whichever packet arrives first per sequence number, and a gap on one line is usually filled by the other). A/B handles the common case — an isolated drop on one path — for free, with no recovery round-trip. But A/B is not enough: sometimes *both* lines drop the same sequence (a burst at the source, a fabric event affecting both paths), or you join late (start-of-day, a restart — Ch. 100), or your consumer fell behind and the gap is large. Then you have a real gap that arbitration can't fill, and you must **recover** — actively fetch the missing data — while the live feed keeps streaming and the hot path keeps trading.

The hard constraint is the theme of the whole book: **recovery must not stall the hot path** (Ch. 1). The naive reaction to a gap — "stop, request the missing packets, wait for them, then continue" — is a catastrophe: it blocks the strategy on a network round-trip (the recovery channel's round-trip is hundreds of µs to ms — §71.3), during which live updates pile up and you fall further behind, and it does so at the worst time (the open, when gaps are most likely — Ch. 69). Correct recovery runs *alongside* the live feed, off the hot path, and the strategy either continues on the parts of the book it can still trust or safely pauses *that instrument* while recovering — never blocks the whole engine. This chapter covers the recovery mental model — the three-channel design (§71.2), measures gap rates and recovery latency (§71.3), details A/B arbitration, retransmission, snapshot recovery, and gap-fill without stalling (§71.4), and warns about the ways recovery makes things worse (§71.5).

## 71.2 Mental model: sequence gaps and the three-channel recovery design

The canonical low-latency market-data recovery architecture has **three channels**, each solving a different failure:

```
   1. INCREMENTAL MULTICAST (A + B)   the live feed: every update, sequenced, sent twice (two paths)
        seq: 100 101 102 [ ] 104 ...  <- gap at 103 detected by sequence discontinuity
                          │
   2. RETRANSMISSION       ───────────> request "resend 103" (unicast, often TCP — Ch.68), off hot path
        (fills small, recent gaps)      exchange resends the specific missing message(s)

   3. SNAPSHOT / REFRESH   ───────────> a periodic full state-of-book image (multicast or on request)
        (fills large gaps / late join)  rebuild the book from the snapshot + apply increments since
```

The pieces:

- **Sequence numbers are the ground truth.** Every incremental message carries a monotonic sequence number (per feed, or per instrument). A **gap** is a discontinuity — you received 102 then 104, so 103 is missing. Detection is trivial and cheap (Ch. 69); *recovery* is the work. The sequence number is also what A/B arbitration keys on and what makes deterministic replay possible (Ch. 100–105).
- **A/B arbitration (channel 1, the free recovery).** Two identical feeds on independent paths (Ch. 69). You track the highest contiguous sequence seen across *both*; a packet dropped on A but received on B leaves no gap. This handles the majority of losses with zero recovery latency — no request, no round-trip. It's the first and best line of defense; the other channels handle only what A/B can't.
- **Retransmission (channel 2, small recent gaps).** When both lines miss a sequence, request a **resend** of the specific message(s) over a unicast re-request channel — UDP unicast to the re-request server in MoldUDP64 (Nasdaq), a TCP Replay channel in CME MDP 3.0 (Ch. 68 for the TCP case). Good for small, recent gaps (a handful of messages). Bounded: retransmission servers hold a limited recent history and rate-limit requests (a NAK storm — §71.5 — can overwhelm them).
- **Snapshot / refresh (channel 3, large gaps and late join).** A **full image** of the current book state (all price levels for an instrument), published periodically on a multicast channel or served on request. When your gap is too large to retransmit (you fell far behind, or you're joining at start-of-day, or after a restart — Ch. 100), you **rebuild the book from a snapshot** and then apply the incremental updates *from the snapshot's sequence number forward*. This is the recovery of last resort and the cold-start path.

The mental model: **A/B fills most holes for free; retransmission fills small recent holes; snapshots rebuild from scratch — and all three run off the hot path while the live feed keeps flowing.** A fourth signal sits beside the three channels: **time** — a dead line produces no gap at all, so liveness (per-channel age against the venue's heartbeat cadence) and cross-message book invariants must be checked alongside sequence (§71.4.4). The strategy consumes the *contiguous, trusted* prefix of the sequence; recovery races to extend that prefix without ever blocking it. Different exchanges name these differently (ITCH/glimpse, various "recovery" and "refresh" channels — Ch. 69), but the three-channel shape is universal.

## 71.3 Measure it: gap rates, recovery latency, and hot-path impact

*Toolchain and machine: Appendix L.3.*

Three quantities: **how often gaps happen**, **how long recovery takes**, and — the one that matters most — **whether recovery touches the hot path.**

- **Gap rate, especially at the open (Ch. 69).** Instrument the feed handler to count gaps (per line and post-arbitration) and correlate with load. Representative pattern (figures pending real runs): steady-state post-A/B gap rate near zero (A/B fills almost everything); at market open (microburst), the raw per-line drop rate spikes (NIC ring overflow, fabric queuing — Ch. 69/78) and the *post-arbitration* gap rate rises when both lines drop together. The open is where recovery is exercised — measure it there, not in the quiet mid-session. A reproducible harness needs no exchange: replay a two-line capture (Ch. 106) or a synthetic sequenced stream through two local UDP sockets with a configurable per-line drop/reorder probability into the arbitrator; count raw per-line gaps vs post-A/B gaps; timestamp gap-detect → recovered (`rdtsc`, Ch. 20) per channel; and record the *unaffected* instruments' latency histogram (HdrHistogram, Ch. 3 / Appendix L) during the induced recovery — that histogram is the pass/fail artefact below.
- **Recovery latency by channel:**

| Recovery path | Typical latency | When used |
|---|---|---|
| **A/B arbitration** | ~0 (no request) | isolated single-line drops (the common case) |
| **Retransmission** (unicast resend) | ~100 µs – few ms | small, recent gaps both lines missed |
| **Snapshot rebuild** | ~ms – 100s of ms | large gaps, late join, restart (Ch. 100) |

- **Hot-path impact — the critical measurement.** Prove that a gap-and-recovery event on one instrument does **not** add latency to the hot path for *other* instruments, and does not stall the engine. Measure the strategy's latency distribution (Ch. 3) during an induced recovery: with correct design it's flat (recovery is off-path); with a naive blocking design it shows a stall the length of the recovery round-trip. This is the pass/fail test of the chapter.

The lessons:

- **A/B does the heavy lifting — measure post-arbitration gaps, not raw drops.** The raw per-line drop rate looks alarming; the post-A/B gap rate is what actually needs recovery, and it's far lower. Sizing recovery infrastructure to raw drops over-provisions; sizing to post-A/B gaps is right.
- **Recovery latency spans orders of magnitude by channel** — near-zero (A/B), ~100 µs to a few ms (retransmit — a colocated round-trip plus the server's history lookup), up to hundreds of ms (snapshot). The design goal is to resolve as many gaps as possible on the *cheaper* channels (A/B first, retransmit for small, snapshot only when necessary) so the expensive rebuild is rare.
- **The only latency that matters for the hot path is zero.** Recovery latency is a *recovery* metric, not a hot-path metric — as long as recovery is off-path (§71.4), the strategy never pays it. If the strategy's tail moves during recovery, the design is wrong (§71.5).

## 71.4 Techniques

### 71.4.1 A/B line arbitration and gap detection

The foundation (extending Ch. 69):

- **Arbitrate across both lines by sequence — the mechanics are Ch. 69 §69.4.3.** Deliver each sequence once, from whichever line arrives first, dropping duplicates with the sliding-window/bitmap check; a single-line loss leaves no gap because the other line fills it. It is stateless, cheap, handles the common case with zero recovery latency, and stays in the feed handler on the hot path. This chapter starts where it ends: the sequence that *neither* line delivered.
- **Detect gaps precisely and early.** A gap is a sequence discontinuity *after* arbitration. Detect it the instant it appears, mark the affected instrument(s), and hand the gap to the **off-path recovery subsystem** — the hot path's job is to *detect and flag*, not to recover. Distinguish a true gap from out-of-order arrival (buffer briefly, Ch. 69's reorder handling) so a reorderable delay isn't mistaken for a loss and doesn't trigger needless recovery.
- **Per-instrument vs per-feed sequencing.** Some feeds sequence per instrument, some per feed (channel). Per-instrument gaps let you recover and pause *one* instrument while others trade on; per-feed gaps are coarser. Track at the finest granularity the feed provides so a gap contains its blast radius (Ch. 100) to the smallest set of instruments.

### 71.4.2 Retransmission and snapshot recovery off the hot path

The active recovery, kept entirely off the strategy's path:

- **A dedicated recovery thread/process (ties Ch. 100).** Recovery — issuing retransmission requests, receiving resends, requesting/consuming snapshots, rebuilding the book — runs on a **housekeeping core** (Ch. 55), in a separate thread or process (Ch. 100), never on the hot path. It communicates the recovered state back via the same lock-free publication the config uses (Ch. 99): the rebuilt/patched book is published atomically for the strategy to pick up.
- **Retransmission for small recent gaps.** On a post-A/B gap, the recovery thread sends a resend request over the unicast recovery channel (TCP — Ch. 68) for the specific missing sequence(s), receives the resent messages, and splices them into the stream in order. Rate-limit and coalesce requests (§71.5 — avoid NAK storms); respect the retransmission server's bounded history (old gaps must go to snapshot, not retransmit).
- **Snapshot rebuild for large gaps and cold start.** When the gap is too large (beyond the retransmission window), or on late join / restart (Ch. 100), the recovery thread consumes a **snapshot** (the full book image), rebuilds the book, and then applies buffered incremental updates *from the snapshot's sequence forward* to catch up to live. This is the same rebuild used at start-of-day and after a crash-restart (Ch. 100) — recovery and cold-start warm-up (Ch. 61) share this path.
- **Buffer live increments during recovery.** While rebuilding from a snapshot (which takes ms — §71.3), live increments keep arriving; buffer them (sequenced) so that once the snapshot is applied you can replay the buffered increments and reach live without a second gap. Size the buffer for the worst rebuild time under open-load increment rates (Ch. 69).

### 71.4.3 Trading through a gap: pause, continue, or safe-state

The policy question: what does the *strategy* do while an instrument is being recovered? It must never block the whole engine, but it also must not trade on a book it knows is wrong (Ch. 100 §100.4.4's safe-state discipline):

- **Contain the gap to its instrument(s).** With per-instrument sequencing (§71.4.1), a gap affects only the instruments it touches. The strategy **continues trading every other instrument** at full speed (the engine is not stalled) and handles *only the affected instruments* specially. This is fault containment (Ch. 100) applied to data quality.
- **Mark the affected instrument stale and choose a policy.** For the gapped instrument, the strategy marks its book **stale/untrusted** and applies a policy: *pause* quoting/trading that instrument until recovered (the safe default — don't trade a known-wrong book, Ch. 100 §100.4.4); or, if the gap is provably in a part of the book you don't use (a deep level you don't quote), *continue* on the trusted part. Never trade the affected instrument as if the book were correct — a known gap is a correctness hazard (Ch. 100 §100.4.4), and trading through it is the kind of bug that loses money silently.
- **Resume cleanly when recovery completes.** When the recovery thread publishes the patched/rebuilt book (§71.4.2), the strategy atomically picks up the trusted book (Ch. 99's atomic swap) and resumes normal trading of that instrument — no tear, no stale read (Ch. 99). The transition from stale→recovered is a clean, lock-free publication, exactly like a config reload (Ch. 99).
- **Kill-switch integration (Ch. 98, 100).** If recovery *fails* (snapshot unavailable, persistent gaps, recovery falling further behind than catching up), escalate to safe-state for the affected instruments (Ch. 100's kill-switch) rather than trading blind or blocking forever. A feed you can't recover is a feed you stop trading, not one you guess at.

### 71.4.4 Feed liveness, data-quality invariants and session health beyond gaps

Everything above is triggered by a sequence discontinuity. Three failures are not: a feed that *stops* (no gap — the last sequence was fine), a feed that keeps sequencing but delivers a *wrong* book (a crossed or locked BBO, a negative size, a level at an impossible price — every message individually valid, Ch. 98 §98.4.3's per-message range checks all pass), and an *order session* that is dying while the market-data side looks perfect. The fourth signal is **time**, the fifth is **cross-message consistency**, and the sixth is the **session layer**:

- **Liveness: per-channel last-packet age against the venue's cadence.** Every channel has an expected arrival rhythm — the venue's heartbeat on idle channels (MoldUDP64: a zero-message heartbeat every second; most binary feeds similar) and sequence advance in continuous trading. Keep `now − last_packet_tsc` per channel and per instrument (`book_age_ns` in the telemetry segment, Ch. 97 §97.4.2), and threshold it **per session phase**: tens of ms mid-session on a liquid channel, 2–3 heartbeat intervals pre-open, after a halt and after hours; drive the phase from the venue's session-state messages (Appendix J.7), not the wall clock. The mechanism behind a silent line — a lapsed multicast membership, a missing querier, `rp_filter` — and the watchdog that implements this check in the poll loop are Ch. 72 §72.4.4; this chapter owns what happens *next*: the stale-instrument policy of §71.4.3, triggered by age instead of by a gap.
- **A/B arrival skew as a leading indicator.** Record, per sequence, which line won arbitration and by how much (the A−B arrival delta, from NIC timestamps — Ch. 73). A line whose skew drifts by tens of µs over an hour has a path change (a re-routed cross-connect, a switch failing over — Appendix K); a line whose win rate falls to zero is *about* to be the dark one. Both are visible minutes before anything the sequence numbers can show (Ch. 111 §111.4.3 samples them beside the NIC counters).
- **Cross-message book invariants at decode time.** After applying an update, check the book: `bid ≤ ask` (crossed = wrong; locked = legal pre-open on some venues, a defect mid-session), no zero quantity on a side with a price, best prices inside the venue's collar, and — the time signal again — the instrument's age. Five predicted branches per update, measured at **~2.1–2.2 ns (6 cycles) per update** on the authoring host (GCC 13.3 and Clang 18.1, `-O2`; 38 instructions, no calls — a `[[gnu::noinline]]` check function over a `Bbo{bid_px, ask_px, bid_qty, ask_qty, last_update_tsc}` against a `Collar{lo, hi}` and a maximum age), i.e. within the noise of the decode stage (Ch. 69 §69.3). A violated invariant is treated exactly like a gap: the instrument is marked stale, recovery is requested (a snapshot — the book is wrong, not merely incomplete) and nothing trades on it (§71.4.3). The invariant counters are published per instrument (Ch. 97); any nonzero mid-session delta is a ticket.
- **Venue session health on the order side.** The order-entry session has its own liveness protocol — in FIX, `Heartbeat` (35=0) at the negotiated `HeartBtInt`, `TestRequest` (35=1) when a heartbeat is late, `ResendRequest`/`SequenceReset` for sequence recovery (Appendix J.3); binary protocols (OUCH/SoupBinTCP, native gateways) have the same three ideas with other names. Monitor: heartbeats received vs expected, `TestRequest`s *you* had to send (the venue went quiet) and ones the venue sent *you* (you went quiet — the gateway thread stalled), sequence-number mismatches on logon, and the **oldest unacknowledged order age** gauge (Ch. 97 §97.4.2) — the fastest indicator of a session that is up at TCP level and dead at application level. Two missed heartbeats or an unanswered `TestRequest` is a session failure: stop sending, assume working orders are at risk, and run the gateway failover of Ch. 100 §100.4.4 (cancel-on-disconnect, standby logon with sequence recovery).
- **The stale-data kill.** The market-data counterpart of Ch. 100's kill-switch: when an instrument's book age exceeds its phase threshold, or an invariant fails, or *both* lines of its channel are dark, the strategy pulls its quotes on that instrument and stops taking — the same per-instrument stale flag §71.4.3 sets on a gap, so the hot path has one predicted branch for all six signals. The threshold and the action are per instrument and per phase, published to the telemetry segment, and the gate (Appendix M) refuses to arm with any instrument stale.
- **Source failover beyond A/B.** When both lines are dark or a channel is persistently gapped and unrecoverable, the alternatives are: a *secondary venue feed* for the same instruments (a different market-data product, often slower — a consolidated or top-of-book feed), a *warm standby feed handler* on another host or NIC path already joined and arbitrating (Ch. 61's warming applied to the handler itself — it must be warm *before* it is needed), or the last-resort decision to stop trading the venue. Failover is a *publication* (Ch. 99 — the strategy atomically switches its book source), measured like a restart (Ch. 100 §100.3): time from dark-detect to trading on the secondary source, and the tail of that time.

The **measurement** that belongs with this section is the trade-off the liveness threshold makes: **time-to-detect a silent line vs false-positive rate**, per session phase. Representative, from a two-line replay (Ch. 106) with a line cut at random points: a fixed 50 ms threshold detects a mid-session cut in 50 ms with zero false positives on a liquid channel, but pages 40 times an hour pre-open; a fixed 3 s threshold is silent pre-open and leaves the strategy quoting on a dead book for 3 s mid-session — 30,000 messages' worth. The phase-aware threshold (50 ms continuous, 2.5 heartbeat intervals otherwise) detects in 50 ms where it matters and pages zero times a night. The invariant checks' cost (above) is the other half: ~2 ns per update buys a wrong-book detector that no sequence number can provide.

## 71.5 Pitfalls & anti-patterns: blocking on recovery and NAK storms

- **Blocking the hot path on recovery (the cardinal error).** Stopping the strategy to request-and-wait for missing packets — a network round-trip (hundreds of µs to ms, §71.3) on the hot path (Ch. 1), during which live updates pile up and you fall further behind. Recovery is *always* off-path (§71.4.2); the hot path detects and flags, the strategy contains the gap to its instrument and continues everything else (§71.4.3).
- **NAK storms.** On a widespread loss (a fabric event affecting many subscribers), every participant simultaneously requests retransmission, overwhelming the retransmission server and the network — amplifying the outage. Rate-limit and coalesce requests, back off (Ch. 42), and fall through to *snapshot* recovery (which doesn't per-message-request) for large/widespread gaps instead of hammering retransmission (§71.4.2).
- **Recovery amplifying the microburst.** Requesting recovery *during* the open burst (Ch. 69) that caused the drop — adding request/resend traffic to an already-congested fabric (Ch. 78) at the worst moment. Prefer A/B (free) and brief buffering/reorder tolerance first; defer/rate-limit active recovery so it doesn't pour fuel on the burst.
- **Trading through an undetected or unrecovered gap.** The worst outcome: acting on a book with a hole as if it were complete — a correctness/financial bug (Ch. 100 §100.4.4's safe-state discipline) that's silent until it costs money. Detect every gap (sequence numbers, §71.4.1), mark the instrument stale, and never trade it as trusted until recovered (§71.4.3).
- **A lost membership produces no gap (the failure this chapter cannot see).** Everything here is keyed on a sequence discontinuity; a feed whose IGMP membership lapsed (no querier on the VLAN → the switch ages the port out ~260 s after the last query; a join refused by `igmp_max_memberships`; `rp_filter` dropping the source) simply *stops*, with the last sequence intact, no gap, no NIC drop counter and no socket error. The book stays internally consistent and goes stale. Detection is time-based — per-channel last-packet age against the venue's heartbeat cadence, A compared with B — and lives in Ch. 72's dark-feed watchdog; its output feeds the same per-instrument stale policy as §71.4.3.
- **Silence is not a quiet market.** A channel that has delivered nothing for a minute is dark until the venue's heartbeat proves it quiet; a book that passes every per-message check can still be crossed. Both are detected by time and by cross-message invariants (§71.4.4), never by sequence numbers, and both trigger the same stale-instrument policy as a gap.
- **Mistaking reordering for loss.** UDP can deliver out of order (Ch. 69); treating a briefly-reordered packet as a gap triggers needless recovery. Buffer a small reorder window (Ch. 69) before declaring a gap (§71.4.1).
- **Stale snapshot / sequence mismatch.** Applying increments to a snapshot without correctly aligning sequence numbers — the snapshot is a state *as of* a sequence, and you must apply exactly the increments *after* it (no gap, no overlap). An off-by-one in snapshot-to-increment splicing rebuilds a subtly wrong book (§71.4.2).
- **Slow consumer causing self-inflicted gaps.** If your own consumer falls behind (a slow hot path, undersized socket buffers — Ch. 67, a blocked feed handler), *you* drop packets and manufacture gaps — the loss is on your side, not the network. Keep the feed handler fast and non-blocking (Ch. 69), size socket/ring buffers (Ch. 67, 69), and never let downstream slowness back-pressure the receive path.
- **Coarse gap granularity stalling everything.** Per-feed (not per-instrument) gap handling that pauses *all* instruments because one had a gap — over-broad blast radius. Track sequencing at the finest granularity available and contain the gap to its instruments (§71.4.1, §71.4.3).
- **No escalation on recovery failure.** Recovery that loops forever (falling further behind, snapshot unavailable) without escalating to safe-state — trading blind or hanging. Escalate to the kill-switch (Ch. 100) when recovery can't keep up (§71.4.3).

## 71.6 Exercises & checklist

**Exercises:**

1. **A/B fill rate.** Feed two lines with injected single-line drops; measure how many gaps A/B arbitration fills with zero recovery (§71.4.1) vs the raw per-line drop rate — quantify why post-A/B gaps are what recovery must handle (§71.3).
2. **Off-path recovery.** Induce a both-line gap on one instrument and measure the strategy's latency distribution for *other* instruments during recovery — prove it's flat (off-path, §71.4.2). Then implement a naive blocking recovery and show the stall (§71.5).
3. **Snapshot rebuild.** Simulate a large gap / late join; rebuild the book from a snapshot, buffer live increments during the rebuild, and splice them to reach live without a second gap (§71.4.2). Verify sequence alignment (no off-by-one, §71.5).
4. **NAK storm.** Simulate widespread loss across many consumers; show naive per-message retransmission overwhelms the server, then add rate-limiting/coalescing and snapshot fallthrough (§71.5).
5. **Stale-instrument policy.** Implement per-instrument gap containment: on a gap, mark that instrument stale and pause it while trading others; on recovery, atomically resume (Ch. 99). Verify no other instrument is affected and no trade occurs on the stale book (§71.4.3).
6. **Liveness threshold trade-off.** Replay a two-line capture and cut one line at random times in each session phase; sweep the dark threshold (10 ms … 5 s) and plot time-to-detect against false positives per hour, per phase. Then implement the phase-aware threshold and confirm it dominates every fixed one (§71.4.4). Add the five book-invariant checks to the decoder and measure their cost per update (should be a handful of cycles).

**Checklist:**

- [ ] **Sequence numbers** on every message; **gaps detected** precisely and early, with a **reorder window** so reordering isn't mistaken for loss (§71.4.1).
- [ ] **A/B arbitration** fills single-line drops with **zero recovery latency**; recovery infrastructure is sized to **post-A/B** gaps, not raw drops (§71.3, §71.4.1).
- [ ] **All active recovery** (retransmission, snapshot rebuild) runs **off the hot path** on a housekeeping core/process (Ch. 55, 100); the hot path only **detects and flags** (§71.4.2).
- [ ] Recovery uses the **cheapest sufficient channel** — A/B → retransmit (small recent) → snapshot (large/late-join); live increments are **buffered during rebuild** and spliced by sequence (§71.4.2).
- [ ] Gaps are **contained per-instrument**; the strategy **continues all other instruments** and marks the gapped one **stale** — never trading a known-wrong book (Ch. 98), never blocking the engine (§71.4.3).
- [ ] Recovery is **rate-limited/coalesced** (no NAK storms) and **doesn't amplify the open microburst** (§71.5).
- [ ] Recovered state is **published atomically** (Ch. 99) — clean stale→trusted resume, no tear; recovery failure **escalates to safe-state** (Ch. 100), never loops or trades blind (§71.4.3, §71.5).
- [ ] **Liveness, invariants and session health** are checked beside sequence: per-channel age with **phase-aware thresholds**, A/B skew trended, **crossed/locked/zero-size/collar** checks per update, venue **heartbeat/TestRequest** accounting and **oldest-unacked-order age** on the order session; all drive the same **stale-instrument kill** and are published to the telemetry segment (§71.4.4).
- [ ] The **own-consumer path is fast enough** not to self-inflict gaps (buffers sized — Ch. 67, 69; feed handler non-blocking — Ch. 69) (§71.5).

## 71.7 References

- The exchange market-data specifications and their **recovery/refresh** mechanisms — e.g. Nasdaq ITCH + Glimpse (snapshot), CME MDP 3.0 (incremental + snapshot + recovery), and the various A/B feed + retransmission designs (Ch. 69 references) (§71.2).
- **PGM (Pragmatic General Multicast)**, RFC 3208, and NAK-based reliable-multicast literature — retransmission and NAK-storm avoidance (§71.4.2, §71.5).
- Ch. 69 (sequencing, A/B arbitration, gap detection — the foundation this extends), Ch. 67 (multicast sockets/buffers), Ch. 100 (deterministic rebuild / kill-switch), Ch. 99 (atomic publication of recovered state), Ch. 68 (the TCP recovery channel), Ch. 78 (the fabric that fans out and drops).
- Reliable-multicast and financial-feed-handler design write-ups — the three-channel architecture in practice (§71.2).

## 71.8 Additional Reading

- Talks and papers on market-data feed handlers, gap recovery, and start-of-day/late-join handling in trading systems — real three-channel recovery designs (§71.2, §71.4).
- The reliable-multicast literature (PGM, NORM, SRM) and NAK-storm-avoidance techniques adapted to financial feeds (§71.4.2, §71.5).
- Ch. 68 (*Advanced TCP*) — the retransmission/snapshot channel's transport; Ch. 100 (*Process Topology*) — recovery as a separate fault domain and the kill-switch; Ch. 105 (*Capture/Replay*) — capturing both lines for deterministic replay across gaps; Ch. 78 (*Fabric*) — where the drops happen; **Appendix E** — the kernel-stack vs bypass RX floor under the live feed; **Appendix F** — A/B / snapshot / NAK / sequence-gap glossary.

---

*Next: Ch. 72 — Multicast Group Management & Dark-Feed Detection, the failure this chapter cannot see: a feed whose IGMP membership silently lapsed (no querier on the VLAN, a refused join, `rp_filter`) delivers no gap and no drop counter — just a perfectly consistent, perfectly stale book. How membership actually works (IGMP/MLD, snooping, the querier), how to join correctly and early, how to verify it from the host, and the dark-feed watchdog that turns silence into a decision.*
