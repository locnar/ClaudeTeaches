# Part XIII — Observability & Operations in Production

# Chapter 107 — The Quoting Engine & Market-Making Mechanics

> **Prerequisites:** Ch. 106 (the gateway — every quote, cancel and mass-cancel goes through its session machine and encode path), Ch. 105 (order identity, the in-flight table, the unknown-state matrix — a quote is an order with a short life), Ch. 104 (the risk gate and its throttles — the message budget), Ch. 40 (the strategy core and its per-instrument SoA state), Ch. 36 (the theoretical price at 53 cycles and the implied-vol surface it reads), Ch. 46 (seqlock-published tables), Ch. 50 (conflation and staleness budgets — a requote storm is a flow-control failure), Ch. 79 (paced and coordinated transmission), Ch. 55 (deadlines: quote TTLs, refresh timers).
>
> **Leads into:** Ch. 108 (what the crash handler must leave behind when a quoter dies: its resting quotes), Ch. 112 (failover — the standby must pull or inherit the quotes), Ch. 119 (the capstone's ledger gains a second critical path, quote-pull), Ch. 89 (the batch tier that publishes the Greeks), Appendix M (the gate: no quotes before the readiness state machine says `armed`).

---

## 107.1 Why it matters: the maker's critical path is the cancel, not the order

Every chapter so far has measured the taker's path: a tick arrives, a decision is made, *one* new order leaves. A market maker's life is the mirror image. It has hundreds or thousands of **resting two-sided quotes** on the book at any moment — across an options chain, a futures curve, an ETF and its basket — and every tick in the underlying makes some of them *wrong*: a quote that was fair a microsecond ago is now a free option for whoever hits it first. The maker's critical path is therefore not "how fast can I send a new order" but **how fast can I pull a quote that has become stale** — the **quote-pull latency**, decision → cancel on the wire — and, right behind it, how fast can I re-post the corrected one. The taker who races you to your stale quote is running the Ch. 119 tick-to-trade path; you are running this chapter's. Both start from the same tick.

The mechanics that follow from that are the chapter: a **quote lifecycle** in which the cancel is always sent before the replacement (cancel-first discipline, §107.2.1); **one tick → N requotes** — a single underlying move invalidates a whole chain, and the engine must decide which of 2,000 series to requote first, under a venue **message budget** it cannot exceed (§107.2.2: measured in §107.3, the bare walk over a 2,000-series chain is ~5 µs on one core and a full requote at no threshold is 8,000 messages, 12 µs of CPU and 30+ µs of 10G wire); **delta thresholds and hysteresis** so that the engine requotes what moved and ignores what did not — the difference between 8,000 and 800 messages per tick (§107.2.3); **mass quote and mass cancel** — the venue-side primitives that turn N messages into one, with semantics that differ per venue and that decide what a kill-switch can do in one round trip (§107.2.4; one mass-cancel is 30 cycles of encode against 6.5 µs for 4,000 individual binary cancels or 60 µs for FIX); and the **batch tier** — vol surfaces and Greeks computed off-path and published through a seqlock so the hot path reads, never solves (§107.2.5).

The chapter builds the hot loop (§107.4.1), measures the pull path and makes it a ledger row beside tick-to-trade (§107.3, §107.4.2), designs the fan-out under budget with prioritization and thresholds (§107.4.3–§107.4.4), maps the venue primitives (§107.4.5) and the safe-state sequence for a quoter (§107.4.6), and closes with the pitfalls that turn a maker into a liquidity donor (§107.5).

## 107.2 Mental model

### 107.2.1 The quote lifecycle and the cancel-first discipline

A quote is two orders — a bid and an ask at `theo ± half-spread`, sized for the inventory you are willing to take — resting on the venue with the state machine of Ch. 105 §105.2.2 (sent → acked → resting → filled/cancelled/replaced). It differs from a taker's order in three ways. **It is expected to be cancelled**, not filled: a good maker cancels or replaces the vast majority of its quotes unfilled, and the cancel/replace path is the hot path, exercised thousands of times per second. **It has two sides that move together**: a theo move shifts both; an inventory change skews both (bid down, ask down when you are long). **Its danger is asymmetric**: a quote that is too *good* for the counterparty is adverse selection — the fill you get is the one you did not want. From the third point follows the **cancel-first discipline**: when a quote is wrong, the first message out is the cancel of the *dangerous* side (the side the market moved toward), then the other side, then the replacements. Never "replace in place" with a single cancel/replace unless the venue guarantees the old quote is dead before the new one is live (most do, *per message*, but a cancel/replace that is rejected — price out of band, throttle — leaves the old quote resting, which is why the ack of a replace must be checked as carefully as a new order's). And never re-post before the cancel has been *sent* — not acked (that would cost a round trip per side), but on the wire ahead of the new order in the session's sequence, so the venue processes them in that order.

### 107.2.2 One tick → N requotes: the fan-out and the message budget

An underlying tick of one increment changes the fair value of every series in its chain by `delta × dS` (plus the gamma term — Ch. 36 §36.4.4 publishes both). A chain is hundreds (a single-expiry equity option) to thousands (an index's weeklies, a futures curve with calendar spreads and butterflies, an ETF plus its constituents) of series; a liquid underlying ticks hundreds of times per second at the open. So the fan-out is `ticks/s × series × 4 messages` (two cancels, two new) — 100 × 2,000 × 4 = 800,000 messages per second per underlying *if you requote everything on every tick*, against a venue message budget that is typically **hundreds to a few thousand messages per second per session** (Appendix K.4's throttle; CME's and Eurex's published limits, Cboe's per-port rates), with an order-to-trade ratio the venue or the regulator caps as well (Appendix K.8). The budget is the design constraint: the engine's job is to spend each tick's allowance on the quotes whose staleness costs the most, and to *leave the rest alone or pull them* — never to queue them. §107.3 measures the shape: at threshold 0 the engine wants 8,000 messages per tick; at a one-tick threshold 5,300; at two ticks 800; the walk itself — reading 2,000 series' state, computing 2,000 theos from the published Greeks, comparing — costs 5 µs per tick on one core *before* any message is built, which is why the per-series state is SoA (Ch. 10) and the walk is ordered by priority, not by series index.

### 107.2.3 Delta thresholds, hysteresis, and prioritization

Three mechanisms shrink the fan-out without leaving money on the table:

- **Delta threshold**: requote a series only when its theo moved by at least `T` ticks from the theo at its last requote (`|theo_now − theo_last| ≥ T`). A deep-OTM series with delta 0.05 moves one tick per twenty underlying ticks; an ATM series with delta 0.5 moves every other tick. The threshold turns the chain's delta distribution into the message distribution — §107.3: `T = 1` cuts messages by a third, `T = 2` by 90%.
- **Hysteresis**: the threshold to *requote* is larger than the threshold that would make the quote *wrong* — a quote half a tick off is left alone; one a full tick off is requoted. The comparison is against the theo at the *last requote*, not the previous tick, so a theo oscillating around a boundary does not requote on every oscillation (the Ch. 50 §50.4.3 staleness-budget idea, applied per series). The gap between "wrong" and "requote" is the edge you concede to avoid the storm; it is a tuned number per series class, and it is *asymmetric*: the side the market moved toward (the one at risk of adverse fill) has the tighter threshold.
- **Prioritization**: when the budget cannot cover every series past threshold, order the walk by **expected loss from staleness** — `|delta| × size × (|theo move| − half-spread)` — so the ATM, large, most-moved quotes are pulled first; the tail of the chain is pulled (not requoted) or left to the next tick. §107.3's `budget = 64` rows show the effect: 128 messages sent, 1,300–2,000 series *deferred* — and a deferred series with a dangerous side is a series to **pull**, which costs one message instead of four.

### 107.2.4 Mass quote, mass cancel, purge ports, and cancel-on-disconnect

Venues built for market makers offer primitives that collapse N messages into one, and their semantics are the difference between a 30-cycle kill switch and a 60 µs one:

| Primitive | What it does | Where | Semantics to know |
|---|---|---|---|
| **Mass quote** (`MassQuote`, FIX `i`; CME iLink `MassQuote`; Eurex ETI `MassQuoteRequest`; Cboe BOE `QuoteUpdate`) | one message carrying up to *n* two-sided quotes (n = 10–200 per venue), each replacing that series' resting quote atomically | CME, Eurex, Cboe options, Deutsche Börse derivatives | per-entry acks/rejects (one bad entry does not reject the rest — check each); quotes are **replaced**, so the old quote is gone when the new one is live; sizes and price bands validated per entry; the message itself has a size cap and a per-session rate |
| **Mass cancel / cancel-all** (`OrderMassActionRequest`, FIX `CA`; iLink `OrderMassActionRequest`; ETI `DeleteAllOrders`/`DeleteAllQuotes`; BOE `PurgeOrders`) | cancel every order/quote matching a scope (session, instrument, product, side) in one message | all major derivatives venues; most equities venues per session | scope granularity differs (per product vs per session vs per instrument); the ack arrives **after** the venue has processed the cancels, which can be milliseconds for thousands of orders; some venues reject it under load |
| **Purge port / kill port** | a *separate* session (often a different TCP connection or even a different host) whose only purpose is to accept mass cancels, bypassing the order port's queue | Cboe (purge ports), Nasdaq (kill switch via a separate port), CME (via the Kill Switch tool / `OrderMassActionRequest` on any session) | the purge port is what you use when the order session is *the problem* (wedged, throttled, half-open); it needs its own warm connection and its own credentials, owned by the supervisor (Ch. 103 §103.4.3) |
| **Cancel-on-disconnect (CoD)** | the venue cancels your resting orders/quotes when your session drops | configurable per session on most venues; opt-in on some | the *detection* is the venue's TCP/heartbeat timeout — seconds, not microseconds (Ch. 106 §106.4.4); a session that is alive but wedged does not trigger it; it is the backstop, not the plan |
| **Quote protection / self-match-prevention (venue-side)** | the venue pulls or re-prices your quotes after a configured number of fills or a notional in a window ("quote protection", Eurex/CME "market-maker protection"); STP prevents your own orders from matching | derivatives venues | the counters and windows are per session and per product; they are a venue-side *circuit breaker* for the case where your engine is wrong about theo — configure them, test them (Appendix M's dry run), and treat a trigger as a kill event |

### 107.2.5 The batch tier and the hot path: who computes what

The hot path (§107.4.1) runs per tick in microseconds and must not *solve* anything: no implied-vol solve, no calibration, no Greeks from first principles. Those are the **batch tier**'s (Ch. 89 §89.4.3 on the GPU; a CPU thread pool on a housekeeping socket — Ch. 49 §49.4.3) products: a vol surface and, per series, `theo₀`, `delta`, `gamma`, `vega`, `theta` at a reference underlying `S₀`, recomputed every few hundred milliseconds or on a surface change, and **published** as one versioned table through a seqlock (Ch. 46 §46.4.1) or an RCU pointer swap (Ch. 102 §102.4.1). The hot path's theo is then a *first- or second-order update* from the published numbers: `theo = theo₀ + delta·(S − S₀) + ½·gamma·(S − S₀)²` — two FMAs per series, no `exp`, no `N(x)` — and the published table's `S₀` is refreshed often enough that the quadratic term is the correction, never the model. When the hot path *does* need the full price (a new series, a large move past the table's validity band), it uses Ch. 36's 53-cycle kernel from the published `σ`, never a solver. The contract between tiers is **version + validity band**: the table carries the `S` range it was computed for; a tick outside it marks the chain *degraded* (quotes widened or pulled) until the batch tier republishes — a Ch. 50 §50.4.4 degraded mode with a name.

The model: **a quoting engine keeps thousands of two-sided quotes correct under a message budget by walking a prioritized, SoA chain per tick, updating each theo from batch-published Greeks in two FMAs, requoting only the series past an asymmetric hysteresis threshold, sending cancels before replacements, collapsing what it can into mass quote/mass cancel messages, and measuring decision → cancel-on-wire as the critical path.**

## 107.3 Measure it: the fan-out per tick, the budget, and the pull path

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native`, TSC at 2.7 GHz — divide cycles by 2.7 for ns); the encode numbers it cites are Ch. 106 §106.3's.*

`quote.cpp` holds a 2,000-series chain (SoA `QuoteState` of 32 bytes per series, a `Greeks` row per series as the batch tier would publish it, a priority order by `|delta|`), ticks the underlying by ±1.5 ticks, and runs the §107.4.1 loop under four thresholds and two message budgets; then times the **pull path** — `pull_all()` encoding every resting quote's cancel as a 9-byte binary message and as a FIX `35=F` — against one mass-cancel:

```cpp
// quote.cpp (excerpt) — one underlying tick -> requotes across the chain, cancel-first, under a budget.
size_t Engine::on_tick(float dS_ticks, std::vector<Msg>& out) {
    out.clear(); size_t budget = budget_per_tick, deferred = 0;
    for (uint32_t k : order_by_priority) {                                                  // |delta| descending
        QuoteState& s = q[k]; const Greeks& g = greeks[k];
        int32_t theo = (int32_t)std::lround(g.theo + g.delta * dS_ticks);                  // first-order update from the published table
        int32_t moved = std::abs(theo - s.last_theo_ticks);
        if (moved < threshold_ticks) continue;                                              // hysteresis: compare against the LAST REQUOTE's theo
        if (budget < 4) { ++deferred; s.dirty = 1; continue; }                              // budget gone: defer (a dangerous side is PULLED instead — §107.4.4)
        if (s.bid_id) out.push_back({CANCEL, k, s.bid_px, 0, s.bid_id});                    // cancel-first: both sides
        if (s.ask_id) out.push_back({CANCEL, k, s.ask_px, 0, s.ask_id});
        s.bid_px = theo - spread_ticks; s.ask_px = theo + spread_ticks; s.bid_id = next_id++; s.ask_id = next_id++;
        out.push_back({NEW, k, s.bid_px, s.bid_qty, s.bid_id}); out.push_back({NEW, k, s.ask_px, s.ask_qty, s.ask_id});
        s.last_theo_ticks = theo; s.dirty = 0; budget -= 4;
    }
    return deferred;
}
```

```
== one underlying tick -> requotes across a 2,000-series chain (best of 200 ticks) ==
threshold  budget    cycles/tick   (us)    messages out   series deferred
   0       4096       35,970      12.4       8,000            0         <- requote everything: 4 msgs x 2,000
   0         64       16,354       5.6         128        1,968         <- the budget: 32 series requoted, the rest stale
   1       4096       22,520       7.8       5,320            0
   1         64       16,794       5.8         128        1,298
   2       4096       15,376       5.3         796            0         <- a 2-tick threshold: 10% of the series move
   2         64       15,368       5.3         128          167
   4       4096       15,182       5.2           0            0         <- the walk alone: ~7.6 cycles per series
== the pull path: decision -> every resting quote's cancel encoded, vs one mass-cancel ==
4,000 binary (OUCH-style) cancels    18,734 cyc   6.5 us CPU     36,000 B on the wire =  28.8 us of 10G serialization
4,000 FIX 35=F cancels              173,532 cyc  59.8 us CPU    204,213 B on the wire = 163.4 us of 10G serialization
1 mass-cancel                            30 cyc
Ch. 106 §106.3, for the per-message cost the fan-out multiplies: one NewOrderSingle encode p50 95 ns (FIX), ~27 ns (binary template)
```

Three readings. **The walk is the floor**: 5.2 µs per tick to *look at* 2,000 series even when nothing moves — 7.6 cycles per series for a load, two FMAs, a compare and a branch — which bounds the chain size one core can quote at a given tick rate (at 200 ticks/s that is 1 ms/s of a core; at 2,000 it is 10%) and argues for sharding chains across cores by underlying (Ch. 49 §49.4.3). **The threshold is the lever**: `T = 2` cuts 8,000 messages to 796 — a venue budget of a few thousand per second becomes feasible at tens of ticks per second — and the hysteresis is what keeps it from thrashing. **The pull path is a wire problem, not a CPU problem**: 6.5 µs of CPU to encode 4,000 binary cancels is nothing against 29 µs of serialization at 10G (163 µs for FIX), during which the *last* cancel in the batch is still on your side of the wire while the market moves; one mass-cancel is 30 cycles and ~20 bytes. The quote-pull latency you report (§107.4.2) is for the *first* cancel of the dangerous side; the time to pull the *whole* chain is what decides whether a mass-cancel primitive is mandatory for your chain size.

## 107.4 Techniques

### 107.4.1 The quoting hot loop: SoA chain, published Greeks, priority order

The engine's per-tick work is the §107.3 loop with three structural decisions around it:

- **State is SoA by series, in priority order.** `QuoteState` (resting prices, sizes, order handles, the theo at last requote, flags — 32 bytes) and the batch-published `Greeks` row (16–32 bytes) are two parallel arrays indexed by a dense series id (Ch. 73 §73.2.2 — the chain is an instrument universe); the walk order `order_by_priority` is a permutation recomputed by the batch tier when deltas change (every few hundred ms), so the hot loop touches series in the order that matters and the prefetcher streams both arrays (Ch. 12). Order handles are slot+generation (Ch. 105 §105.2.1), never venue order-id strings.
- **The theo is a Taylor update, not a model.** `theo₀ + delta·dS` (+ ½·gamma·dS² when the table's band allows a second-order term) from the published row; `dS` is the underlying's move from the table's `S₀`, so the update is exact in the table's frame and the hot loop never calls `exp` (§107.2.5). A tick that leaves the table's validity band flips the chain to *degraded* (§107.4.6) rather than extrapolating.
- **Output is a message batch, ordered.** The loop appends to a per-tick vector in cancel-first order (dangerous side first, then the other side, then news), the gateway encodes the batch with the Ch. 106 §106.4.2 template encoder (binary: ~27 ns/message), and the send is one `sendmsg`/one bypass TX burst with the cancels first in the session sequence. Where the venue offers mass quote, the news are coalesced into `MassQuote` blocks of the venue's maximum entry count (§107.4.5) *after* the cancels.
- **The loop is the Ch. 49 hot loop**: one core, polling the underlying's feed, the chain's own fills/acks (which change `bid_id`/`ask_id` and inventory), the batch tier's table version, and the timer queue (quote TTL refreshes — Ch. 55 §55.4.2), in that priority; the walk is bounded (`budget` is the batch cap of Ch. 49 §49.4.2) so a burst of ticks cannot starve the ack path.

### 107.4.2 Quote-pull latency: the second row in the ledger

Define it precisely and stamp it with Ch. 99's in-band header: **`t_tick`** — hardware RX timestamp of the underlying tick that made the quote wrong (Ch. 75); **`t_decide`** — the TSC when the loop marked the series for pull (the first iteration that passed the threshold); **`t_wire`** — the hardware TX timestamp of the packet carrying the *first cancel of the dangerous side* (Ch. 79 §79.3's TX stamp, or the tap's — Ch. 110). Quote-pull latency is `t_wire − t_tick`; its components are the same stages as tick-to-trade (RX → decode → the walk up to this series → encode → TX) with one addition — the walk's position: a series late in the priority order pays the walk of everything before it, which is the cost the prioritization in §107.2.3 exists to control. Report it as a **distribution per series class** (ATM vs OTM, by expiry) and add it as a row beside tick-to-trade in the capstone's ledger (Ch. 119 §119.3) and in the SLO set (Ch. 100 §100.4.4): a maker's p99.9 quote-pull is the number that bounds its adverse-selection cost, and it is measured, like everything else in this book, as a distribution from hardware stamps — never as the loop's CPU time.

### 107.4.3 Fan-out under a budget: prioritization and the deferred list

With the budget as a hard cap per tick (the venue's per-session rate divided by the tick rate you must sustain, minus headroom for acks' replacements and the TTL refreshes), the loop of §107.3 defers series it cannot afford. The deferred list is not a queue to drain later — by the next tick the theos have moved again — it is the input to a **pull decision**: a deferred series whose dangerous side is past the *wrong* threshold (the inner band of §107.2.3) is pulled on that side with one cancel (a quarter of a requote's messages), and the pull itself has priority over requotes further down the list. The order of spending is therefore: (1) pulls of dangerous sides, most-at-risk first; (2) requotes of the highest-priority series; (3) refreshes and re-posts of pulled sides when the budget allows and the theo has settled (hysteresis again). The cap is checked *per message* against the risk gate's token bucket (Ch. 104 §104.4.3 — the same bucket that would reject on the send path, consulted before the batch is built so no message is built only to be refused) and the gateway never sees a batch larger than the bucket can pass.

### 107.4.4 Thresholds and hysteresis as data, per series class

The thresholds are a table, not constants: per series class (moneyness band × expiry bucket × product), two numbers per side — `wrong` (pull if past it) and `requote` (re-post only if past it, `requote ≥ wrong`) — in ticks of theo, plus a `settle` band for re-posting a pulled side. They are published with the Greeks table (same version, same seqlock) so the batch tier can retune them with the surface, and they are *asymmetric*: after an up-move the ask side's `wrong` is tighter than the bid's. Tuning is empirical and off-path — the post-trade analytics of Ch. 119 §119.4.5 (fills vs quote age, markout per series class) say where the bands are costing edge or inviting adverse fills — and replayable: the thresholds are an input to the deterministic core (Ch. 103 §103.2.2), so a day's quoting replays bit-exactly with the day's table versions.

### 107.4.5 Venue primitives: using mass quote, mass cancel, and the purge port

- **Mass quote** replaces the `NEW` half of the batch: group the series to requote by product (the venue's grouping unit), fill `MassQuote` entries up to the venue's cap per message (CME: 10–20 entries per `MassQuote` on iLink 3; Eurex ETI: up to 100 per `MassQuoteRequest`; Cboe BOE `QuoteUpdate`: a block per message), and send them *after* the cancels of the sides that must die first — unless the venue guarantees replace semantics per entry, in which case the mass quote *is* the cancel and the replacement, and the dangerous side's entry is the first in the first block. Check **per-entry acks**: one rejected entry (price outside the band, size too large under quote protection) leaves that series' old quote in whatever state the venue's semantics say — resting on some, cancelled on others; the in-flight table (Ch. 105 §105.2.2) tracks each entry as its own order.
- **Mass cancel** is the kill-switch message (Ch. 104 §104.4.6): scoped as narrowly as the trigger allows (one product on a bad surface, the session on a wedge), sent from the order session if it is alive and from the **purge port** if it is not, and *followed* by individual cancels for anything the venue's ack says remained. The purge port's connection is owned and heartbeated by the supervisor process (Ch. 103 §103.4.3), not by the quoter, because the case it exists for is the quoter being dead or wedged.
- **Cancel-on-disconnect** is enabled on every quoting session and *not* relied upon: its detection is the venue's heartbeat timeout — seconds during which the quotes are live and stale. The plan is the supervisor's mass cancel within milliseconds of the quoter's death (Ch. 108 §108.4.3's kill-switch handoff sends it, or the watchdog does on a missed heartbeat — Ch. 103 §103.3's detect-to-live metric, with "quotes pulled" as its first milestone).
- **Quote protection** is configured per product at the venue to a number of fills/notional per window that your engine would never legitimately hit in a correct state, and a trigger is treated as evidence the engine is wrong: the chain goes to safe state (§107.4.6) and a human looks.

### 107.4.6 Safe state for a quoter: widen, pull, mass-cancel, and the order of operations

A quoter's safe state is *no resting quotes it cannot defend*. The sequence, triggered by any of: the batch table's validity band exceeded, the surface version stale past its TTL, a quote-protection trigger, the feed degraded (Ch. 72 §72.4.4, Ch. 74), the risk gate's kill (Ch. 104 §104.4.5), or the operator:

1. **Stop posting** — the loop's `NEW` output is disabled (one flag, read per iteration).
2. **Widen or pull by severity**: a stale-but-valid surface → widen (replace at `theo ± k·spread`, k = 3–5), which is a requote under budget; an invalid surface or a degraded feed → pull the dangerous sides, then everything, prioritized as §107.4.3.
3. **Mass cancel** for the scope (product → session), from the order session, with the purge port as the fallback if no ack within the session's timeout; then reconcile against the venue's order status (Ch. 105 §105.4.5) until the venue says *zero resting*.
4. **Re-arm** only through the readiness gate (Appendix M): a fresh table version, the feed live, the reconciliation clean, a dry-run cancel/replace — measured and logged, because the cost of re-arming on a wrong surface is the cost of the whole chapter.

Rehearse it (Appendix M's T-5 dry run; the resilience drills of Ch. 116 §116.4.7) and *measure* it: time from trigger to the venue's "zero resting" ack is the quoter's detect-to-safe number, and it is dominated by the venue's processing of the mass cancel, which is why the first message out is the pull of the single most dangerous quote, not the mass cancel.

## 107.5 Pitfalls & anti-patterns

- **Measuring tick-to-trade and calling it done.** A maker's loss is on the quote it failed to pull; quote-pull latency is the metric, per series class, from hardware stamps (§107.4.2).
- **Replace before cancel, or re-post before the cancel is on the wire.** The old quote is live while the new one is being built; the dangerous side is still resting (§107.2.1).
- **Requoting everything on every tick.** 8,000 messages per tick against a budget of thousands per second; the throttle rejects, the venue warns, the order-to-trade ratio breaches (§107.2.2, §107.3). Thresholds, hysteresis, priority.
- **Thresholds compared against the previous tick instead of the last requote.** A theo oscillating around a boundary requotes on every oscillation — the storm with extra steps (§107.2.3).
- **Symmetric thresholds.** The side the market moved toward is the one that gets picked off; it needs the tighter band (§107.4.4).
- **Solving on the hot path.** An implied-vol solve or a full Black-Scholes per series per tick is 300–500 cycles × 2,000 — a millisecond per tick; the batch tier publishes, the hot path updates in two FMAs (§107.2.5, Ch. 36).
- **Extrapolating outside the table's band.** A Taylor update is exact in the table's frame and wrong far from `S₀`; a tick past the band is a degraded-mode trigger, not a larger `dS` (§107.4.1).
- **Deferred series left resting.** The budget ran out and the series' dangerous side stayed on the book; a deferred series past the `wrong` band is *pulled* (§107.4.3).
- **Ignoring per-entry mass-quote rejects.** One rejected entry, one resting stale quote (§107.4.5). Every entry is an order in the in-flight table.
- **Relying on cancel-on-disconnect.** Seconds of detection during which the chain is live and stale; the supervisor's mass cancel in milliseconds is the plan, CoD the backstop (§107.4.5).
- **The purge port owned by the quoter.** The case it exists for is the quoter being dead; the supervisor owns it (§107.4.5).
- **Re-arming on an unverified surface.** The one way to lose more after a safe-state than before it (§107.4.6).

## 107.6 Exercises & checklist

**Exercises**

1. **Reproduce the fan-out table.** Build `quote.cpp` with your own chain's delta distribution (from a real surface); sweep thresholds and budgets; plot messages per tick and the walk's cost vs chain size. Find the chain size one core can serve at your tick rate.
2. **Quote-pull ledger.** Instrument the loop with Ch. 99's stamps; replay a captured underlying feed (Ch. 109) through it with the gateway's encoder and a TX stamp; report quote-pull latency as a distribution per series class, and show how the priority order changes the p99.9 for ATM series.
3. **Hysteresis.** Feed a theo that oscillates ±0.6 ticks around a boundary; count requotes with the comparison against the previous tick vs against the last requote; then make the bands asymmetric and measure the adverse-fill rate in a simulator (the next chapter's) against a random taker.
4. **Mass primitives.** Encode 2,000 cancels as individual messages and as the venue's mass-cancel; measure CPU and bytes; then measure (on the venue's test environment) the time from send to the "zero resting" ack for each, at 200 and 2,000 resting orders.
5. **Safe-state drill.** Trigger each of §107.4.6's conditions in a test session; time the sequence to zero-resting; make the supervisor send the mass cancel on a `SIGKILL` of the quoter and measure that too.
6. **Degraded mode.** Push a tick outside the batch table's band and confirm the chain widens/pulls rather than extrapolates; measure how long until the batch tier republishes and the chain re-arms through the gate.

**Checklist — the quoting engine**

- [ ] **Cancel-first**: the dangerous side's cancel is the first message of every requote batch and is on the wire before any replacement (§107.2.1, §107.4.1).
- [ ] **Quote-pull latency** (`t_wire − t_tick` for the first dangerous-side cancel) is stamped, reported per series class as a distribution, and in the SLO set beside tick-to-trade (§107.4.2).
- [ ] Per-series state is **SoA in priority order**; the theo is a **Taylor update** from a batch-published, versioned Greeks table with a **validity band** (§107.4.1, §107.2.5).
- [ ] Requotes are gated by **asymmetric `wrong`/`requote` thresholds** compared against the **last requote's theo**, published as data with the surface (§107.2.3, §107.4.4).
- [ ] A per-tick **message budget** from the venue's rate and the gate's bucket; deferred series past the `wrong` band are **pulled**, not left (§107.4.3).
- [ ] **Mass quote** blocks after the cancels with per-entry acks tracked; **mass cancel** scoped to the trigger; the **purge port** warm and owned by the supervisor; **CoD** on but not relied upon; **quote protection** configured and treated as a kill (§107.4.5).
- [ ] The **safe-state sequence** (stop posting → widen/pull → mass cancel → reconcile → re-arm via the gate) is rehearsed and its detect-to-zero-resting time measured (§107.4.6).
- [ ] The whole day's quoting **replays bit-exactly** from the captured feed, table versions and threshold tables (§107.4.4, Ch. 103).

## 107.7 References

- CME Group, *iLink 3 Binary Order Entry* (`MassQuote`, `OrderMassActionRequest`, quote protection / market-maker protection) and *CME Globex Messaging Efficiency Program*; Deutsche Börse, *T7 Enhanced Trading Interface* (`MassQuoteRequest`, `DeleteAllQuotes`, quote-protection ("MMP") parameters); Cboe, *BOE Specification* (`QuoteUpdate`, `PurgeOrders`, purge ports) and *US Options Risk Management*; Nasdaq, *Kill Switch* documentation — §107.2.4's primitives and their per-venue semantics.
- FIX Trading Community, *FIX Protocol* (`MassQuote` 35=i, `OrderMassActionRequest` 35=CA, `MassQuoteAcknowledgement`) — the message family.
- M. Avellaneda and S. Stoikov, "High-frequency trading in a limit order book," *Quantitative Finance* 8 (2008); Á. Cartea, S. Jaimungal and J. Penalva, *Algorithmic and High-Frequency Trading* (CUP, 2015) — inventory skew, spread setting and adverse selection, the economics behind §107.2.1 and §107.4.4.
- SEC Rule 15c3-5 and ESMA MiFID II RTS 6 (order-to-trade ratios, kill functionality, market-maker obligations) — Appendix K.8 maps them to code paths.
- Ch. 36 (the pricing kernel), Ch. 46 (seqlock publication), Ch. 50 (conflation and staleness budgets), Ch. 79 (paced/coordinated TX), Ch. 104–106 (the gate, order identity, the session) — the mechanisms this chapter composes.

## 107.8 Additional Reading

- Ch. 119 §119.3 — the ledger with the quote-pull row; Ch. 119 §119.4.5 — the post-trade analytics that tune the thresholds; Ch. 89 §89.4.3 — the batch tier; Ch. 55 §55.4.2 — quote TTLs and refresh deadlines; Ch. 112 — a standby that must inherit or pull the quotes; **Appendix M** — the gate every re-arm goes through; **Appendix K.4** — the throttles the budget is derived from.
- The next chapter's exchange simulator (Ch. @SIMULATOR@) is the closed loop in which thresholds, budgets and the safe-state sequence are tested against a venue model before the open.

---

*Next: Ch. 108 — Crash Handling, Core Dumps & Post-Mortem Diagnostics: what a dying process must leave behind — for a quoter, the first thing is a mass cancel — and how to capture the state that explains the death without stalling the survivors.*
