# Part XIII — Observability & Operations in Production

# Chapter 117 — Case Study: The Exchange Simulator

> **Prerequisites:** Ch. 33 (the order book — the matching engine is the venue-side use of the same ladder, pool and FIFO), Ch. 32 §32.4.4 (slot maps and generational handles), Ch. 103 §103.2.2 (the deterministic single-threaded core — the simulator is one), Ch. 116 (deterministic simulation testing — the fault-injecting `SimVenue` of §116.4.3 is this chapter's engine grown up), Ch. 109–110 (captures and wire-level timestamps — where the latency model's samples come from), Ch. 99 §99.4.5 (exchange-provided timestamps), Ch. 55 (a simulated clock is a deadline queue), Appendix J.1/J.3 (price-time, pro-rata, auctions, order types, STP), Appendix L.5 (generators and seeds).
>
> **Leads into:** Ch. 118 (the CI latency gate drives the system through this harness), Ch. 119 (shadow runs — the simulator is the venue that is *not* real), Ch. 120 (the capstone consumes the harness this chapter builds: §120.4.3 no longer has to describe it), Ch. 107 (thresholds, budgets and the safe-state sequence are tuned against it), Ch. 111 (replay through the sequencer with the simulator as the venue).

---

## 117.1 Why it matters: every chapter that said "in a simulation harness" was pointing here

Ch. 70's exercises replay a microburst "through a simulator"; Ch. 72's inject a gap "on both lines"; Ch. 107 tunes its thresholds "in the closed loop"; Ch. 116 tests the gateway against a "simulated venue"; Ch. 118 gates latency "on a replayed capture"; and Ch. 120 §120.4.3 lists what a simulation harness must model — queue position, fills, partials, rejects, the venue's latency — without building one. This chapter builds it, measures it, and in doing so gives the book its first **venue-side** proof of the order-book design: a matching engine is Ch. 33's book with the matching rule applied on the inside, and if the book is right it matches ten million orders per second on one core (§117.3: **11 M ops/s price-time, 11 M pro-rata, p50 54–62 cycles, p99 under 1,300**).

A simulator earns its place by being *honest* in the two places backtests lie. The first is the **latency model**: a venue is not a function; it is a queue with a delay distribution whose tail decides who wins a race, and a lognormal fit to captured ack times puts the p99.9 at 82 µs where the capture says 869 (§117.3) — a simulator with the wrong tail proves a strategy profitable that loses in production. The second is **queue position**: a resting order's fill depends on how much size is ahead of it under the venue's allocation rule, which the simulator must track per order from the same MBO stream the strategy sees. The matching core, the pro-rata variant, self-trade prevention and the auction cross (§117.2.1–§117.2.2, §117.4.1–§117.4.3) are the *easy* part and are built to be exact; the latency and queue models (§117.2.3–§117.2.4, §117.4.4–§117.4.5) are where the measurement budget goes, because they are where a simulation turns into fiction.

The chapter also closes a loop the testing chapter opened: the deterministic core's test bed is a deterministic venue — the same inputs, the same seed, the same fills, bit for bit, on every run — so that a fault schedule (§117.4.6: gap, A/B skew, microburst, disconnect, a slow ack) is a *replayable input* and a failing scenario is a seed to hand to a colleague.

## 117.2 Mental model

### 117.2.1 The matching engine is the book, run from the inside

A price-time CLOB matching engine (Appendix J.1) keeps, per instrument, exactly Ch. 33's structures — a dense tick-indexed ladder of levels, a pool of orders, an intrusive FIFO per level, a best-bid/best-ask cursor — and applies one rule on every incoming order: **match against the opposite side's best level while the incoming price crosses it, FIFO within the level; rest the remainder**. Three design choices follow from the book chapter and are kept:

- **Orders are pool slots addressed by index** (Ch. 32 §32.4.4; the venue's order id is the slot+generation handle it hands back in the ack), so a cancel is O(1) and the FIFO is two `uint32_t` links per order, not a `std::list`.
- **Levels are dense by tick** (Ch. 33 §33.4.1) with a best cursor that walks to the next non-empty level only when the best empties — the `fix_best_*` of the listing, O(levels crossed), which is why a quiet deep book costs nothing.
- **Fills are emitted, not stored**: the engine writes `(taker, maker, px, qty)` into a per-order output buffer that the venue's ack/trade-feed stage consumes and clears; it keeps no history (the journal does — Ch. 109).

The engine is **single-threaded and deterministic** (Ch. 103 §103.2.2): one input stream (orders, cancels, time) → one output stream (acks, fills, book events), no clock reads, no randomness except what arrives as input. That is what makes the simulator a *test oracle* and not just a mock.

### 117.2.2 Allocation rules, self-trade prevention, and auctions

**Pro-rata** (CME's and Eurex's default on many futures and options) allocates an incoming order across the resting orders at a level *in proportion to their size*, with the remainder from rounding going FIFO (and, on CME, a "FIFO top" allocation to the first order at the level before the pro-rata pass — a per-product parameter). Two consequences the simulator must reproduce: a large resting order gets a large share regardless of when it arrived (so *size*, not time, is queue priority — the queue-position model changes, §117.2.4), and the per-order fill size is a floor of a fraction, so a 1-lot resting order may never fill against small takers — the "pro-rata dust" that real makers size around. The cost is `O(orders at the level)` per aggressive order (every resting order is visited) against price-time's `O(orders filled)`; §117.3 measures them at equal throughput only because the pro-rata run keeps levels at a CME-like depth of hundreds of orders.

**Self-trade prevention** (STP; Appendix J.3) decides what happens when an incoming order would match a resting order from the *same firm* (same participant id, or the same account/group as the venue defines it): cancel the **newest** (the taker is rejected/cancelled; the resting order stays — Nasdaq's default), cancel the **oldest** (the resting order is cancelled and the taker continues), cancel **both**, or **decrement and cancel** (the smaller is cancelled, the larger reduced). The engine applies the mode *at match time*, per level walk, which is where it belongs — and the simulator must offer every mode the venues you quote on use, because a quoter's cancel-first discipline (Ch. 107) interacts with STP: a replacement that arrives before the cancel of the other side can self-match under "cancel oldest" and pull your own quote.

**Auctions** (opening, closing, re-opening after a halt): orders accumulate *without matching* during the call phase; at the cross, the engine finds the single price that **maximises executable volume** (ties broken by minimum imbalance, then by reference-price proximity — venue rules), executes every crossing order at that price in priority order, and resumes continuous trading with the remainder resting. The uncross is a scan over the crossing price range — §117.3: 40,000 orders over a 30-tick range in ~2,800 cycles — and it is where a simulator's *opening* behaviour comes from: the market-open microburst (Ch. 70 §70.2.4) is the post-uncross book being disseminated.

### 117.2.3 The latency model: an empirical distribution with the tail intact

Between the strategy's send and the venue's response sit: the wire (Ch. 80, Appendix K), the venue's gateway (a queue), the matching engine (a queue), and the wire back — and between the engine's match and the strategy's *market-data* view of it sits a different path (the dissemination feed's own queue and multicast). A simulator models each as a **delay sampled from a distribution**, and the distribution must come from **captured timestamps**, not from a parametric guess: the venue's own stamps (Ch. 99 §99.4.5 — `TransactTime` on the ack vs your TX stamp gives gateway+engine delay; the feed's timestamp vs the ack's gives dissemination skew) and the tap's (Ch. 110 — wire-to-wire). §117.3 makes the case numerically: a lognormal fitted to a captured ack-time sample with a 2% spike population reproduces the median exactly and *misses the p99.9 by 10×*. The simulator therefore samples from the **empirical inverse CDF** — a sorted table of quantiles, 3.7 cycles per draw — and keeps separate distributions for order-ack, fill, cancel-ack and market-data dissemination, per venue, per session phase (the open is its own regime), and optionally **conditioned on load** (the venue's delay grows with its queue, which the simulator knows because it *is* the queue). A **race** between your order and a competitor's is then two draws and a comparison, and the strategy's fill rate as a function of its own latency (Ch. 120 §120.4.5) becomes something the simulator can sweep.

### 117.2.4 The queue-position model

A resting order fills when everything ahead of it at its level has filled or cancelled. Under price-time, "ahead" is the resting size with earlier timestamps: when your order rests, `ahead = level.total` at that moment; every subsequent trade at the level reduces `ahead` (fills are FIFO, so they come off the front), every cancel *ahead of you* reduces it, and every add goes *behind* you. From an **MBO** feed (Ch. 33 §33.2) the simulator knows which order cancelled and can decrement `ahead` exactly; from an **MBP** feed it knows only the level's total and must *estimate* (the standard assumption: a cancel is equally likely anywhere, so `ahead -= cancel_qty × ahead / (ahead + behind)`). Under pro-rata, position is size-weighted: your expected share of each incoming trade is `your_qty / level.total`, so the model tracks the level's total and your size, and fills are fractional expectations resolved by the engine's actual allocation. §117.3 measures the per-event update at **11 cycles** — cheap enough to track every resting order in the strategy's own process, which is what Ch. 107's quoter does to know which quotes are near the front (worth keeping) and which are deep (cheap to pull).

The model: **the simulator is Ch. 33's book run as a venue — a deterministic single-threaded matching core with the allocation rule, STP mode and auction cross the real venue uses — wrapped in a latency model sampled from captured timestamps with the tail intact, a queue-position model fed by the same MBO stream the strategy sees, and a fault schedule driven by a seed, so that every scenario is a replayable input and every fill is bit-identical on every run.**

## 117.3 Measure it: millions of orders per second, the tails, the uncross, the latency model, the queue update

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native`, TSC at 2.7 GHz — divide cycles by 2.7 for ns); the reference box runs the same source with GCC 14.2.*

`matching.cpp` is the engine of §117.4.1 — dense ladder of 65,536 ticks, a 4 M-slot order pool, intrusive FIFOs, pro-rata and STP as modes — driven by four million synthetic operations (70% new orders of which 15% aggressive, 30% cancels of random live orders; 64 firms so STP fires; the resting book held at ~730 k orders for price-time, ~16 k for pro-rata to keep CME-like level depth) with per-operation `rdtsc` stamps, then a 40,000-order auction uncross. `latmodel.cpp` samples a 1 M-point "captured" ack-time distribution (a lognormal body at 38 µs with a 2% spike population at 300–900 µs — the shape real captures have) through an inverse-CDF table against a fitted lognormal, and runs the queue-position update per MBO event:

```cpp
// matching.cpp (excerpt) — the price-time match against one resting level; pro-rata and STP are the other branches.
template <bool IsBuy> Qty Book::match_level(Level& L, Price px, Qty qty, OrderId taker, Firm firm) {
    OrderId id = L.head;
    while (id && qty > 0) {
        Order& o = pool[id]; OrderId nx = o.next;
        if (o.firm == firm && stp != STP::None) {                               // self-trade prevention, at match time
            ++n_stp;
            if (stp == STP::CancelOldest || stp == STP::CancelBoth) { unlink(L, id); ++n_cancels; }
            if (stp == STP::CancelNewest || stp == STP::CancelBoth) return 0;    // taker cancelled: nothing rests
            id = nx; continue;
        }
        Qty f = std::min(qty, o.qty);
        o.qty -= f; L.total -= f; qty -= f; fills.push_back({taker, id, px, f}); ++n_fills;
        if (o.qty == 0) unlink(L, id);                                          // O(1): intrusive FIFO + pool free-list
        id = nx;
    }
    return qty;                                                                  // remainder rests (or walks to the next level)
}
```

```
== 4,000,000 operations (70% new / 30% cancel; 15% of news aggressive), cycles per operation ==
mode        mean   M ops/s @2.9 GHz   p50    p99    p99.9    max      fills      cancels    STP     resting
pricetime   264    11.0               62     870    1,300    496,286  941,878    1,414,666  15,114  733,976
nostp       228    12.7               54     652    1,106    199,862  962,521    1,411,152       0  738,631
stp-both    226    12.9               54     620    1,066    250,912  932,542    1,426,621  14,979  737,803
prorata     258    11.3               54   1,310    4,754    274,142  6,550,271  1,848,434  10,124   15,809   (levels ~400 deep)
auction:    40,000 orders over a 30-tick crossing range -> uncross price found, 603,886 volume, in 2,750–3,200 cycles
Clang 18.1, pricetime: mean 325 cyc (8.9 M ops/s), p50 88, p99 1,036, p99.9 1,488
== latency model ==
empirical inverse-CDF sample (4,096-point table)       3.7 cyc/draw
parametric lognormal (std::lognormal_distribution)    48.7 cyc/draw
           p50        p99        p99.9      p99.99
capture    38 us      597 us     869 us     896 us
lognormal  38 us       67 us      82 us      96 us        <- the fit is right at the median and 10x wrong in the tail
== queue-position model ==
FIFO rank update per MBO event, 4,096 tracked orders   10.8 cyc/event
```

Read the first table as a venue would. **Throughput is not the story — the tail is**: 11 M ops/s means a p50 of 62 cycles (23 ns) for a new order that rests, but the p99 at 870 and p99.9 at 1,300 cycles are the aggressive orders that walk levels and the cancels that touch a cold pool slot (the 730 k-order book is 17 MiB of pool — L2 misses, Ch. 8), and the `max` of 200–500 k cycles is a page-fault or a preemption on the guest (Ch. 61 — a real venue's engine is on an isolated core with the pool pre-faulted, Ch. 34). **Pro-rata's p99.9 is 3.7× price-time's** at a *shallower* book because every aggressive order visits every order at the level — the venue's rule is a cost the simulator reproduces and the quoter should know (its 1-lot quotes are visited on every trade). **STP modes cost nothing** (the compare is on a field already loaded). The uncross is **~1 µs** for 40,000 orders — the opening is not where the engine's time goes; dissemination is. The latency-model rows are the chapter's thesis in two lines: the empirical sampler is **13× cheaper** than the parametric one *and* right in the tail where the parametric one is wrong by an order of magnitude. And the queue update at 11 cycles says the strategy can afford to know its position for every resting order it has.

## 117.4 Techniques

### 117.4.1 The matching core: Ch. 33's book with the rule inside

The listing in §117.3 is the inner loop; the engine around it:

- **`submit(side, px, qty, firm)`**: allocate a pool slot (free-list), then `while (qty > 0 && best_opposite crosses px) qty = match_level(...)`, then rest the remainder (`append` to the level's FIFO, bump `best_*` if it improves) or free the slot if nothing remains. Order types (Appendix J.3) are flags on the way in: **IOC** skips the rest step; **FOK** pre-walks the levels to check total available size before matching (one extra pass, no fills if short); **post-only** rejects if it would cross; **market** is a limit at the far end of the ladder with IOC.
- **`cancel(id)`**: validate the handle's generation (Ch. 32 §32.4.4 — the simulator's pool reuses slots, exactly as the venue's does, which is why the order id the venue returns must carry a generation or the simulator will cancel the wrong order after a slot is reused: the benchmark's `live` vector hits that case and tolerates it; production code must not), unlink in O(1), fix the best cursor if the level emptied.
- **Replace** (cancel/replace): the venue's semantics decide whether a size *decrease* keeps queue priority (most do) and whether a price change or a size increase loses it (all do); the simulator implements the venue's table, not a convenience.
- **Determinism**: the engine reads no clock and no RNG; the *venue's* randomness (a randomized auction uncross time, a speed bump's jitter) arrives as an input event scheduled by the harness (§117.4.4) from the harness's seeded generator (Appendix L.5).

### 117.4.2 Pro-rata and self-trade prevention as modes

Both are the `Alloc` and `STP` enums of the listing, set per instrument from the venue's product configuration (the simulator's instrument table — the same reference data as Ch. 73 §73.4.1 — carries the venue's allocation rule and STP mode per product, because real venues mix them: CME runs price-time on some products and pro-rata-with-FIFO-top on others). The pro-rata pass is two loops over the level — proportional shares with `floor(qty × o.qty / total)`, then the rounding remainder FIFO — and the STP skip inside it must, when *every* resting order at the level is the taker's own firm, resolve to the mode's rule rather than loop (the benchmark's first version did not, and hung: a lesson in why the simulator is tested with the same seeds and invariants as the core — Ch. 116 §116.4.2's "a level's total equals the sum of its orders" property catches the state, and a watchdog on the harness catches the hang).

### 117.4.3 The auction cross

`uncross()` in the listing: for each price in `[best_ask, best_bid]` (the crossing range), cumulative buy volume at or above and sell volume at or below, executable = min, pick the max (tie → min imbalance); then execute: walk the crossing orders on each side in priority order at the uncross price, emitting fills, and leave the remainder resting. Venue variants the simulator exposes as parameters: the tie-break order (imbalance → reference price → last trade), **imbalance publication** during the call (the venue disseminates the indicative price and imbalance every *n* seconds — an input the strategy sees), **randomized uncross time** within a window (LSE/Euronext-style — the harness schedules it), and the **order-type restrictions** of the call phase (market-on-open, limit-on-close). The simulator runs the opening uncross and then *disseminates* the resulting book as the burst the feed handler must survive — the market-open microburst of Ch. 70 §70.2.4, generated deterministically.

### 117.4.4 The latency model from captures: per-path empirical samplers on a simulated clock

The harness is an event loop on a **simulated clock** (Ch. 116 §116.2.3; a deadline queue — Ch. 55 §55.4.1): every effect is an event at a scheduled time, and the delays come from samplers built from captures:

```cpp
// latmodel.hpp — one sampler per path, built from captured (venue stamp - our stamp) deltas; O(1) draws, tail intact.
struct EmpiricalDelay {
    std::vector<std::uint32_t> q;                                      // 4,097 quantiles of the captured delay, in ns
    explicit EmpiricalDelay(std::vector<std::uint32_t> samples, int n = 4096) : q(n + 1) {
        std::sort(samples.begin(), samples.end());
        for (int i = 0; i <= n; ++i) q[i] = samples[std::min(samples.size() - 1, std::size_t(double(i) / n * samples.size()))];
    }
    std::uint32_t operator()(std::uint64_t r) const noexcept {           // r: one draw from the harness's seeded generator
        std::uint32_t i = std::uint32_t(r >> 52), f = std::uint32_t((r >> 20) & 0xffffffffu);
        return q[i] + std::uint32_t((std::uint64_t(q[i + 1] - q[i]) * f) >> 32);   // linear interpolation inside the quantile: 2 loads, no branch
    }
};
struct VenueModel {
    EmpiricalDelay order_ack, fill_report, cancel_ack, md_dissemination;   // one per path, per venue, per session phase
    std::uint32_t queue_delay_ns(std::size_t backlog) const noexcept { return std::uint32_t(backlog * per_order_ns); }   // load-conditioned term: the engine IS the queue
    std::uint32_t per_order_ns;
};
```

Build the samplers **from the ledger** (Ch. 99 §99.4.6 joins your TX stamp to the venue's `TransactTime` and the tap's stamps — Ch. 110): one histogram per (venue, path, phase), refreshed from each day's capture, versioned with the simulator so a backtest records which day's latency it ran under. Keep the **spike population** (the 2% at 300–900 µs in §117.3's synthetic capture — real venues have them at the open, on halts, on their own GC pauses) — it is the part a parametric fit discards and the part that decides race outcomes. Add the **load term** when the venue's delay visibly grows with its queue (your own order rate is part of that load — a quoter's requote storm slows the venue for everyone, including itself). And model **dissemination separately from acks**: on most venues your fill ack arrives *before* the trade prints on the feed (the ack path is unicast, the feed is batched multicast); on some the opposite; the simulator must get the order right or a strategy that trades on its own fills will be mis-tested.

### 117.4.5 Queue position: FIFO rank and pro-rata share from the same MBO stream

The §117.3 update is the whole model for price-time with an MBO feed: per resting order of yours, `ahead` and `behind` in shares; on every book event at your level — a trade (reduces `ahead` by the traded size, FIFO), a cancel of an order whose id you know to be ahead (reduce `ahead`), an add (increase `behind`) — one branch-free update at 11 cycles. With an MBP feed, the cancel's position is unknown and `ahead` becomes an expectation (`ahead -= cancel × ahead / (ahead + behind)`), which the simulator should *also* track as a distribution if the strategy's decisions depend on it (a quote at the front is worth keeping; one with a wide uncertainty band is not). Under pro-rata the state is `(your_qty, level_total)` and the expected fill on a trade of size *t* is `t × your_qty / level_total` (plus the FIFO-top term if the venue has it and you are first); the engine's actual allocation resolves it when the simulated trade happens. In the strategy's process the model is what tells Ch. 107's quoter *which* quotes are cheap to pull (deep in the queue, no priority to lose) and which are not; in the simulator it is what makes a fill a consequence of *position*, not of a coin flip.

### 117.4.6 Fault schedules as inputs: gaps, A/B skew, microbursts, disconnects, slow acks

The harness's seed decides a **fault schedule** — a list of `(sim_time, fault)` events generated once from the seed and *journaled with the run* — and each fault is a transformation on the simulator's output stream, never a special case in the core:

- **Sequence gap on one line** (Ch. 72 §72.4.1): drop packets `[n, m)` from line A's dissemination; the feed handler must fill from B or request a retransmission from the simulator's retransmission server (which has its own latency sampler).
- **A/B skew and reorder** (Ch. 72, Ch. 70 §70.2.4): delay line B's copies by a sampled offset; reorder within a window.
- **Dark feed** (Ch. 74 §74.4.4): stop a group's dissemination with no gap — the silence the watchdog must detect.
- **Microburst** (Ch. 70 §70.2.4): replay the opening uncross's dissemination at line rate, or compress a captured burst's inter-arrival times.
- **Disconnect / slow ack / duplicate ack / reject** (Ch. 116 §116.4.3's `SimVenue` faults, now with the real matching core behind them): the session machine (Ch. 106) and the unknown-state matrix (Ch. 105 §105.2.3) are exercised against a venue that *actually* did or did not take the order.
- **Venue halt and re-opening auction** (§117.4.3): the state the strategy and the quoter must handle without resting a quote into a call phase.

Because the schedule is derived from the seed and the core is deterministic, a failing run is `(capture id, simulator version, latency-model version, seed)` — four numbers that reproduce it bit-for-bit on any machine (Ch. 116 §116.2.3), and shrink to a minimal failing schedule by bisection over the fault list (Ch. 116 §116.4.2's shrinking, applied to faults). That is what makes the simulator the test bed for Ch. 118's latency gates and Ch. 116's resilience drills, and it is the reason the core keeps its no-clock, no-RNG discipline: one `rdtsc` in the engine and the four numbers no longer reproduce the run.

## 117.5 Pitfalls & anti-patterns

- **A parametric latency model.** Right at the median, 10× wrong at p99.9 (§117.3); the race outcomes it predicts are fiction. Sample the empirical CDF from captures, keep the spikes, refresh daily (§117.4.4).
- **One delay for everything.** Acks, fills, cancels and dissemination are different paths with different distributions and a *relative order* the strategy can observe (§117.4.4).
- **A clock in the engine.** One `rdtsc` and the run is no longer reproducible from (capture, version, seed) (§117.4.6, Ch. 103).
- **Slot reuse without generations.** The venue's order id is reused; a cancel by stale id cancels someone else's order — in the simulator *and* in a real venue's session (§117.4.1, Ch. 32 §32.4.4).
- **Pro-rata as "price-time with a different fill size."** It changes queue priority from time to size and the per-trade cost from O(filled) to O(level); a quoter tuned on a price-time simulator loses on a pro-rata venue (§117.2.2, §117.3).
- **STP as an afterthought.** Cancel-newest vs cancel-oldest decides whether your own replacement pulls your own quote (§117.2.2, Ch. 107 §107.2.1).
- **Open-loop replay mistaken for simulation.** Replay assumes your orders change nothing; a simulator's book moves when you hit it (Ch. 120 §120.4.3). Use replay for fidelity and the simulator for counterfactuals — and know which one a number came from.
- **Queue position from MBP treated as exact.** It is an expectation with a widening band; track the band (§117.4.5).
- **Faults as special cases in the core.** A fault is a transformation of the output stream scheduled by the seed; the core never knows (§117.4.6).
- **A simulator faster than the venue.** The engine runs at 11 M ops/s; the venue it models does not. Without the latency model's queue term, the simulator never shows the venue slowing under your own load (§117.4.4).

## 117.6 Exercises & checklist

**Exercises**

1. **Reproduce §117.3.** Build `matching.cpp`; run the four modes; then pre-fault the pool and pin the process (Ch. 34, 56) and show the `max` column collapse. Plot the p99.9 against the resting book size and explain it with `perf stat -e cache-misses` (Ch. 2).
2. **Order types.** Add IOC, FOK, post-only and market orders; property-test (Ch. 116 §116.4.2) that a level's total equals the sum of its orders and that no FOK ever partially fills, over a million seeded operations.
3. **The venue's replace table.** Implement your venue's priority rules for cancel/replace (size down keeps, size up loses, price change loses); verify against the venue's published examples and its test environment.
4. **Build the latency model from your ledger.** Join a day's TX stamps to the venue's `TransactTime` (Ch. 99 §99.4.6); build the four samplers per session phase; compare p50/p99/p99.9 against a lognormal fit; then sweep your strategy's own latency in the simulator and plot fill rate vs latency (Ch. 120 §120.4.5).
5. **Queue position, two ways.** Track your resting orders' rank from an MBO capture exactly and from the derived MBP feed as an expectation; measure the error of the expectation at the moment of each fill.
6. **The seed that fails.** Run the gateway + quoter against the simulator over 10,000 seeds with the §117.4.6 fault schedule; for a failing seed, bisect the fault list to a minimal reproduction and hand it to the testing chapter's table-driven suite as a permanent regression.
7. **Open the market.** Generate an opening uncross for 2,000 instruments, disseminate it at line rate as a microburst, and measure the feed handler's drops and the book builder's recovery time (Ch. 70 §70.4.4, Ch. 72) against the budget.

**Checklist — the exchange simulator**

- [ ] The matching core is **Ch. 33's book with the rule inside**: pool slots with generations, dense ladder, intrusive FIFOs, O(1) cancel; **no clock, no RNG** (§117.4.1).
- [ ] **Allocation rule and STP mode per product** from the venue's configuration; pro-rata with the venue's FIFO-top/rounding rules; every STP mode the venues use (§117.4.2).
- [ ] The **auction cross** finds max executable volume with the venue's tie-break, publishes indicatives, and disseminates the post-uncross book as a burst (§117.4.3).
- [ ] **Latency is sampled from captured empirical CDFs**, one per (venue, path, phase), tail intact, load-conditioned, versioned with the run (§117.4.4).
- [ ] **Queue position** is tracked per resting order from the MBO stream (exactly) or MBP (as an expectation with a band), under the venue's rule (§117.4.5).
- [ ] **Faults are a seeded schedule** applied to the output stream — gap, skew, dark feed, microburst, disconnect, slow/duplicate ack, halt — journaled with the run (§117.4.6).
- [ ] A run is reproducible from **(capture id, simulator version, latency-model version, seed)**; a failing seed shrinks to a minimal fault list (§117.4.6).
- [ ] The simulator's throughput and tails are **measured** and known to be far above the venue's; the latency model, not the engine, sets the simulated venue's speed (§117.3).

## 117.7 References

- Appendix J.1 and J.3 — price-time priority, pro-rata, auctions, order types and TIF, STP; CME Group, *Matching Algorithms* (FIFO, Pro-Rata, Allocation, Threshold Pro-Rata, FIFO with LMM) and *Self-Match Prevention*; Nasdaq, *Self Match Prevention* and *Opening/Closing Cross* specifications; Deutsche Börse, *T7 Functional Reference* (matching principles, auctions, self-match prevention) — the venue rules §117.4.1–§117.4.3 parameterise.
- J. Hasbrouck, *Empirical Market Microstructure* (OUP, 2007), and R. Huang and H. Stoll's queue-position literature; M. Gould et al., "Limit order books," *Quantitative Finance* 13 (2013) — the queue-position and fill-probability models of §117.2.4.
- FoundationDB, *Testing Distributed Systems with Deterministic Simulation*; TigerBeetle, *VOPR* — deterministic simulation as the method §117.4.6 applies to a venue.
- Ch. 33 (the book), Ch. 103 (the deterministic core), Ch. 116 (DST and property tests), Ch. 99/110 (the stamps the latency model is built from), Appendix L.5 (seeded generators and O(1) sampling) — the mechanisms composed here.

## 117.8 Additional Reading

- Ch. 120 §120.4.3 (what the capstone consumes from this chapter) and §120.4.5 (fill rate and markout as a function of latency — the analysis the simulator's latency sweep feeds); Ch. 118 (the CI gate driven through the harness); Ch. 107 (the quoter tuned in the closed loop); Ch. 119 (shadow runs — the production counterpart).
- Open-source matching engines and simulators to read against this chapter's design: the LiquiBook and `abides`/`abides-markets` projects, and the exchange-published matching-engine test environments.

---

*Next: Ch. 118 — Performance Engineering in CI & the Profile Lifecycle: benchmarks as tests, the statistical gate on p99/p99.9, and the nightly replay driven through this chapter's simulator — plus the PGO/BOLT profile lifecycle that keeps the hot path's layout honest.*
