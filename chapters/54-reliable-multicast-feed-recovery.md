# Part IX — Market Data, NIC & Fabric

# Chapter 54 — Reliable Multicast & Feed Recovery

> **Prerequisites:** Ch. 51 (multicast sockets, buffer sizing), Ch. 53 (zero-copy decoding, A/B arbitration, sequence-gap detection — this chapter extends the recovery half), Ch. 74 (the deterministic state machine that a rebuilt book must feed), Ch. 60 (the fabric that fans out the feed), Ch. 1 (the tail — recovery must not create one).
>
> **Leads into:** Ch. 52 (TCP internals — the recovery channel is often TCP), Appendix E. The recovery deep-dive Ch. 53 pointed at: what to do when the lossy multicast feed drops a packet and the strategy would otherwise be blind.

---

## 54.1 Why it matters: market data is lossy, and a gap blinds the strategy

Market data arrives as **UDP multicast** (Ch. 51): the exchange sends each update *once*, to a multicast group, and the fabric (Ch. 60) fans it out to every participant. Multicast is the right design — one send reaches thousands of subscribers with no per-subscriber cost — but UDP is **unreliable by construction**: there is no retransmission, no acknowledgment, no ordering guarantee. Packets are dropped — in the fabric under a microburst (Ch. 53), in the NIC's RX ring when it overflows (Ch. 53, 55), in the kernel, in your own slow consumer. And a dropped market-data packet is not a minor blip: it is a **gap in your view of the order book** (Ch. 25). Miss the packet that added a large order to the bid, and your book is *wrong* — you think there's liquidity that isn't there, or you miss liquidity that is. Trading on a book with an undetected gap is trading on a lie (Ch. 72's "the wire is untrusted" extended to "the wire is *lossy*").

Ch. 53 established the first line of defense: **sequence numbers** on every message and **A/B line arbitration** (the exchange sends two identical multicast feeds on independent paths; you take whichever packet arrives first per sequence number, and a gap on one line is usually filled by the other). A/B handles the common case — an isolated drop on one path — for free, with no recovery round-trip. But A/B is not enough: sometimes *both* lines drop the same sequence (a burst at the source, a fabric event affecting both paths), or you join late (start-of-day, a restart — Ch. 74), or your consumer fell behind and the gap is large. Then you have a real gap that arbitration can't fill, and you must **recover** — actively fetch the missing data — while the live feed keeps streaming and the hot path keeps trading.

The hard constraint is the theme of the whole book: **recovery must not stall the hot path** (Ch. 1). The naive reaction to a gap — "stop, request the missing packets, wait for them, then continue" — is a catastrophe: it blocks the strategy on a network round-trip (Ch. 52's TCP recovery channel is milliseconds), during which live updates pile up and you fall further behind, and it does so at the worst time (the open, when gaps are most likely — Ch. 53). Correct recovery runs *alongside* the live feed, off the hot path, and the strategy either continues on the parts of the book it can still trust or safely pauses *that instrument* while recovering — never blocks the whole engine. This chapter covers the recovery mental model — the three-channel design (§54.2), measures gap rates and recovery latency (§54.3), details A/B arbitration, retransmission, snapshot recovery, and gap-fill without stalling (§54.4), and warns about the ways recovery makes things worse (§54.5).

## 54.2 Mental model: sequence gaps and the three-channel recovery design

The canonical low-latency market-data recovery architecture has **three channels**, each solving a different failure:

```
   1. INCREMENTAL MULTICAST (A + B)   the live feed: every update, sequenced, sent twice (two paths)
        seq: 100 101 102 [ ] 104 ...  <- gap at 103 detected by sequence discontinuity
                          │
   2. RETRANSMISSION       ───────────> request "resend 103" (unicast, often TCP — Ch.69), off hot path
        (fills small, recent gaps)      exchange resends the specific missing message(s)

   3. SNAPSHOT / REFRESH   ───────────> a periodic full state-of-book image (multicast or on request)
        (fills large gaps / late join)  rebuild the book from the snapshot + apply increments since
```

The pieces:

- **Sequence numbers are the ground truth.** Every incremental message carries a monotonic sequence number (per feed, or per instrument). A **gap** is a discontinuity — you received 102 then 104, so 103 is missing. Detection is trivial and cheap (Ch. 53); *recovery* is the work. The sequence number is also what A/B arbitration keys on and what makes deterministic replay possible (Ch. 74–75).
- **A/B arbitration (channel 1, the free recovery).** Two identical feeds on independent paths (Ch. 53). You track the highest contiguous sequence seen across *both*; a packet dropped on A but received on B leaves no gap. This handles the majority of losses with zero recovery latency — no request, no round-trip. It's the first and best line of defense; the other channels handle only what A/B can't.
- **Retransmission (channel 2, small recent gaps).** When both lines miss a sequence, request a **resend** of the specific message(s) over a unicast channel — typically a TCP connection to a retransmission server (Ch. 52). Good for small, recent gaps (a handful of messages). Bounded: retransmission servers hold a limited recent history and rate-limit requests (a NAK storm — §54.5 — can overwhelm them).
- **Snapshot / refresh (channel 3, large gaps and late join).** A **full image** of the current book state (all price levels for an instrument), published periodically on a multicast channel or served on request. When your gap is too large to retransmit (you fell far behind, or you're joining at start-of-day, or after a restart — Ch. 74), you **rebuild the book from a snapshot** and then apply the incremental updates *from the snapshot's sequence number forward*. This is the recovery of last resort and the cold-start path.

The mental model: **A/B fills most holes for free; retransmission fills small recent holes; snapshots rebuild from scratch — and all three run off the hot path while the live feed keeps flowing.** The strategy consumes the *contiguous, trusted* prefix of the sequence; recovery races to extend that prefix without ever blocking it. Different exchanges name these differently (ITCH/glimpse, various "recovery" and "refresh" channels — Ch. 53), but the three-channel shape is universal.

## 54.3 Measure it: gap rates, recovery latency, and hot-path impact

Three quantities: **how often gaps happen**, **how long recovery takes**, and — the one that matters most — **whether recovery touches the hot path.**

- **Gap rate, especially at the open (Ch. 53).** Instrument the feed handler to count gaps (per line and post-arbitration) and correlate with load. Representative pattern (figures pending real runs): steady-state post-A/B gap rate near zero (A/B fills almost everything); at market open (microburst), the raw per-line drop rate spikes (NIC ring overflow, fabric queuing — Ch. 53/60) and the *post-arbitration* gap rate rises when both lines drop together. The open is where recovery is exercised — measure it there, not in the quiet mid-session.
- **Recovery latency by channel:**

| Recovery path | Typical latency | When used |
|---|---|---|
| **A/B arbitration** | ~0 (no request) | isolated single-line drops (the common case) |
| **Retransmission** (TCP resend) | ~100 µs – few ms | small, recent gaps both lines missed |
| **Snapshot rebuild** | ~ms – 100s of ms | large gaps, late join, restart (Ch. 74) |

- **Hot-path impact — the critical measurement.** Prove that a gap-and-recovery event on one instrument does **not** add latency to the hot path for *other* instruments, and does not stall the engine. Measure the strategy's latency distribution (Ch. 3) during an induced recovery: with correct design it's flat (recovery is off-path); with a naive blocking design it shows a stall the length of the recovery round-trip. This is the pass/fail test of the chapter.

The lessons:

- **A/B does the heavy lifting — measure post-arbitration gaps, not raw drops.** The raw per-line drop rate looks alarming; the post-A/B gap rate is what actually needs recovery, and it's far lower. Sizing recovery infrastructure to raw drops over-provisions; sizing to post-A/B gaps is right.
- **Recovery latency spans orders of magnitude by channel** — near-zero (A/B), sub-millisecond (retransmit), up to hundreds of ms (snapshot). The design goal is to resolve as many gaps as possible on the *cheaper* channels (A/B first, retransmit for small, snapshot only when necessary) so the expensive rebuild is rare.
- **The only latency that matters for the hot path is zero.** Recovery latency is a *recovery* metric, not a hot-path metric — as long as recovery is off-path (§54.4), the strategy never pays it. If the strategy's tail moves during recovery, the design is wrong (§54.5).

## 54.4 Techniques

### 54.4.1 A/B line arbitration and gap detection

The foundation (extending Ch. 53):

- **Arbitrate across both lines by sequence.** Maintain the highest contiguous sequence delivered to the strategy; receive both A and B; deliver each sequence once (from whichever line arrives first), dropping duplicates. A single-line loss leaves no gap because the other line fills it. This is stateless, cheap, and handles the common case with zero recovery latency — do it in the feed handler on the hot path (it's just sequence bookkeeping, Ch. 53).
- **Detect gaps precisely and early.** A gap is a sequence discontinuity *after* arbitration. Detect it the instant it appears, mark the affected instrument(s), and hand the gap to the **off-path recovery subsystem** — the hot path's job is to *detect and flag*, not to recover. Distinguish a true gap from out-of-order arrival (buffer briefly, Ch. 53's reorder handling) so a reorderable delay isn't mistaken for a loss and doesn't trigger needless recovery.
- **Per-instrument vs per-feed sequencing.** Some feeds sequence per instrument, some per feed (channel). Per-instrument gaps let you recover and pause *one* instrument while others trade on; per-feed gaps are coarser. Track at the finest granularity the feed provides so a gap contains its blast radius (Ch. 74) to the smallest set of instruments.

### 54.4.2 Retransmission and snapshot recovery off the hot path

The active recovery, kept entirely off the strategy's path:

- **A dedicated recovery thread/process (ties Ch. 74).** Recovery — issuing retransmission requests, receiving resends, requesting/consuming snapshots, rebuilding the book — runs on a **housekeeping core** (Ch. 42), in a separate thread or process (Ch. 74), never on the hot path. It communicates the recovered state back via the same lock-free publication the config uses (Ch. 73): the rebuilt/patched book is published atomically for the strategy to pick up.
- **Retransmission for small recent gaps.** On a post-A/B gap, the recovery thread sends a resend request over the unicast recovery channel (TCP — Ch. 52) for the specific missing sequence(s), receives the resent messages, and splices them into the stream in order. Rate-limit and coalesce requests (§54.5 — avoid NAK storms); respect the retransmission server's bounded history (old gaps must go to snapshot, not retransmit).
- **Snapshot rebuild for large gaps and cold start.** When the gap is too large (beyond the retransmission window), or on late join / restart (Ch. 74), the recovery thread consumes a **snapshot** (the full book image), rebuilds the book, and then applies buffered incremental updates *from the snapshot's sequence forward* to catch up to live. This is the same rebuild used at start-of-day and after a crash-restart (Ch. 74) — recovery and cold-start warm-up (Ch. 46) share this path.
- **Buffer live increments during recovery.** While rebuilding from a snapshot (which takes ms — §54.3), live increments keep arriving; buffer them (sequenced) so that once the snapshot is applied you can replay the buffered increments and reach live without a second gap. Size the buffer for the worst rebuild time under open-load increment rates (Ch. 53).

### 54.4.3 Trading through a gap: pause, continue, or safe-state

The policy question: what does the *strategy* do while an instrument is being recovered? It must never block the whole engine, but it also must not trade on a book it knows is wrong (Ch. 72):

- **Contain the gap to its instrument(s).** With per-instrument sequencing (§54.4.1), a gap affects only the instruments it touches. The strategy **continues trading every other instrument** at full speed (the engine is not stalled) and handles *only the affected instruments* specially. This is fault containment (Ch. 74) applied to data quality.
- **Mark the affected instrument stale and choose a policy.** For the gapped instrument, the strategy marks its book **stale/untrusted** and applies a policy: *pause* quoting/trading that instrument until recovered (the safe default — don't trade a known-wrong book, Ch. 72); or, if the gap is provably in a part of the book you don't use (a deep level you don't quote), *continue* on the trusted part. Never trade the affected instrument as if the book were correct — a known gap is a correctness hazard (Ch. 72), and trading through it is the kind of bug that loses money silently.
- **Resume cleanly when recovery completes.** When the recovery thread publishes the patched/rebuilt book (§54.4.2), the strategy atomically picks up the trusted book (Ch. 73's atomic swap) and resumes normal trading of that instrument — no tear, no stale read (Ch. 73). The transition from stale→recovered is a clean, lock-free publication, exactly like a config reload (Ch. 73).
- **Kill-switch integration (Ch. 72, 74).** If recovery *fails* (snapshot unavailable, persistent gaps, recovery falling further behind than catching up), escalate to safe-state for the affected instruments (Ch. 74's kill-switch) rather than trading blind or blocking forever. A feed you can't recover is a feed you stop trading, not one you guess at.

## 54.5 Pitfalls & anti-patterns: blocking on recovery and NAK storms

- **Blocking the hot path on recovery (the cardinal error).** Stopping the strategy to request-and-wait for missing packets — a network round-trip (ms, §54.3) on the hot path (Ch. 1), during which live updates pile up and you fall further behind. Recovery is *always* off-path (§54.4.2); the hot path detects and flags, the strategy contains the gap to its instrument and continues everything else (§54.4.3).
- **NAK storms.** On a widespread loss (a fabric event affecting many subscribers), every participant simultaneously requests retransmission, overwhelming the retransmission server and the network — amplifying the outage. Rate-limit and coalesce requests, back off (Ch. 32), and fall through to *snapshot* recovery (which doesn't per-message-request) for large/widespread gaps instead of hammering retransmission (§54.4.2).
- **Recovery amplifying the microburst.** Requesting recovery *during* the open burst (Ch. 53) that caused the drop — adding request/resend traffic to an already-congested fabric (Ch. 60) at the worst moment. Prefer A/B (free) and brief buffering/reorder tolerance first; defer/rate-limit active recovery so it doesn't pour fuel on the burst.
- **Trading through an undetected or unrecovered gap.** The worst outcome: acting on a book with a hole as if it were complete — a correctness/financial bug (Ch. 72) that's silent until it costs money. Detect every gap (sequence numbers, §54.4.1), mark the instrument stale, and never trade it as trusted until recovered (§54.4.3).
- **Mistaking reordering for loss.** UDP can deliver out of order (Ch. 53); treating a briefly-reordered packet as a gap triggers needless recovery. Buffer a small reorder window (Ch. 53) before declaring a gap (§54.4.1).
- **Stale snapshot / sequence mismatch.** Applying increments to a snapshot without correctly aligning sequence numbers — the snapshot is a state *as of* a sequence, and you must apply exactly the increments *after* it (no gap, no overlap). An off-by-one in snapshot-to-increment splicing rebuilds a subtly wrong book (§54.4.2).
- **Slow consumer causing self-inflicted gaps.** If your own consumer falls behind (a slow hot path, undersized socket buffers — Ch. 51, a blocked feed handler), *you* drop packets and manufacture gaps — the loss is on your side, not the network. Keep the feed handler fast and non-blocking (Ch. 53), size socket/ring buffers (Ch. 51, 53), and never let downstream slowness back-pressure the receive path.
- **Coarse gap granularity stalling everything.** Per-feed (not per-instrument) gap handling that pauses *all* instruments because one had a gap — over-broad blast radius. Track sequencing at the finest granularity available and contain the gap to its instruments (§54.4.1, §54.4.3).
- **No escalation on recovery failure.** Recovery that loops forever (falling further behind, snapshot unavailable) without escalating to safe-state — trading blind or hanging. Escalate to the kill-switch (Ch. 74) when recovery can't keep up (§54.4.3).

## 54.6 Exercises & checklist

**Exercises:**

1. **A/B fill rate.** Feed two lines with injected single-line drops; measure how many gaps A/B arbitration fills with zero recovery (§54.4.1) vs the raw per-line drop rate — quantify why post-A/B gaps are what recovery must handle (§54.3).
2. **Off-path recovery.** Induce a both-line gap on one instrument and measure the strategy's latency distribution for *other* instruments during recovery — prove it's flat (off-path, §54.4.2). Then implement a naive blocking recovery and show the stall (§54.5).
3. **Snapshot rebuild.** Simulate a large gap / late join; rebuild the book from a snapshot, buffer live increments during the rebuild, and splice them to reach live without a second gap (§54.4.2). Verify sequence alignment (no off-by-one, §54.5).
4. **NAK storm.** Simulate widespread loss across many consumers; show naive per-message retransmission overwhelms the server, then add rate-limiting/coalescing and snapshot fallthrough (§54.5).
5. **Stale-instrument policy.** Implement per-instrument gap containment: on a gap, mark that instrument stale and pause it while trading others; on recovery, atomically resume (Ch. 73). Verify no other instrument is affected and no trade occurs on the stale book (§54.4.3).

**Checklist:**

- [ ] **Sequence numbers** on every message; **gaps detected** precisely and early, with a **reorder window** so reordering isn't mistaken for loss (§54.4.1).
- [ ] **A/B arbitration** fills single-line drops with **zero recovery latency**; recovery infrastructure is sized to **post-A/B** gaps, not raw drops (§54.3, §54.4.1).
- [ ] **All active recovery** (retransmission, snapshot rebuild) runs **off the hot path** on a housekeeping core/process (Ch. 42, 74); the hot path only **detects and flags** (§54.4.2).
- [ ] Recovery uses the **cheapest sufficient channel** — A/B → retransmit (small recent) → snapshot (large/late-join); live increments are **buffered during rebuild** and spliced by sequence (§54.4.2).
- [ ] Gaps are **contained per-instrument**; the strategy **continues all other instruments** and marks the gapped one **stale** — never trading a known-wrong book (Ch. 72), never blocking the engine (§54.4.3).
- [ ] Recovery is **rate-limited/coalesced** (no NAK storms) and **doesn't amplify the open microburst** (§54.5).
- [ ] Recovered state is **published atomically** (Ch. 73) — clean stale→trusted resume, no tear; recovery failure **escalates to safe-state** (Ch. 74), never loops or trades blind (§54.4.3, §54.5).
- [ ] The **own-consumer path is fast enough** not to self-inflict gaps (buffers sized — Ch. 51, 53; feed handler non-blocking — Ch. 53) (§54.5).

## 54.7 References

- The exchange market-data specifications and their **recovery/refresh** mechanisms — e.g. Nasdaq ITCH + Glimpse (snapshot), CME MDP 3.0 (incremental + snapshot + recovery), and the various A/B feed + retransmission designs (Ch. 53 references) (§54.2).
- **PGM (Pragmatic General Multicast)**, RFC 3208, and NAK-based reliable-multicast literature — retransmission and NAK-storm avoidance (§54.4.2, §54.5).
- Ch. 53 (sequencing, A/B arbitration, gap detection — the foundation this extends), Ch. 51 (multicast sockets/buffers), Ch. 74 (deterministic rebuild / kill-switch), Ch. 73 (atomic publication of recovered state), Ch. 52 (the TCP recovery channel), Ch. 60 (the fabric that fans out and drops).
- Reliable-multicast and financial-feed-handler design write-ups — the three-channel architecture in practice (§54.2).

## 54.8 Additional Reading

- Talks and papers on market-data feed handlers, gap recovery, and start-of-day/late-join handling in trading systems — real three-channel recovery designs (§54.2, §54.4).
- The reliable-multicast literature (PGM, NORM, SRM) and NAK-storm-avoidance techniques adapted to financial feeds (§54.4.2, §54.5).
- Ch. 52 (*Advanced TCP*) — the retransmission/snapshot channel's transport; Ch. 74 (*Process Topology*) — recovery as a separate fault domain and the kill-switch; Ch. 75 (*Capture/Replay*) — capturing both lines for deterministic replay across gaps; Ch. 60 (*Fabric*) — where the drops happen; **Appendix E** — recovery-channel latencies; **Appendix F** — A/B / snapshot / NAK / sequence-gap glossary.

---

*Next: Ch. 55 — NIC Features & Offloads, the hardware under the receive path: RSS, hardware timestamping, busy-polling, checksum/segmentation offload, and Solarflare/Onload — pushing work into the card and getting packets to the decoder faster.*
