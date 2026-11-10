# Part IX — Market Data, NIC & Fabric

# Chapter 73 — Multi-Venue Book Building, Normalization, Symbology & Smart Order Routing

> **Prerequisites:** Ch. 70 (the feed handler's decode — this chapter consumes its output), Ch. 72 (per-venue A/B arbitration and gap recovery — the state a venue's book is in when it feeds the consolidation), Ch. 33 (one `FastBook` per venue and the BBO line it publishes; §33.4.6 hands the merge to this chapter), Ch. 38 (symbol keys, dense ids and the perfect hash), Ch. 32 (flat tables), Ch. 10 (SoA per-instrument state), Ch. 78 (hardware RX timestamps — cross-venue alignment), Appendix J (NBBO, the SIP, the venue anatomy table in J.6).
>
> **Leads into:** Ch. 74 (membership and dark feeds — a venue that goes silent is backed out here), Ch. 79 (coordinated sends to several venues), Ch. 102 (hot-reloaded reference data — the symbology tables), Ch. 104 (the risk gate reads the consolidated top), Ch. 106 (the gateway's sessions — one per venue), Ch. @QUOTING@ (quoting across venues), Ch. 118 (the capstone's tick-to-trade path, multi-venue).

---

## 73.1 Why it matters: one instrument, N feeds, one decision

A single-venue futures shop never meets this chapter: one feed, one book, one session. A US-equities shop meets it on every tick: the same instrument trades on sixteen lit venues and a dark pool count, each with its own feed (ITCH on MoldUDP64, PITCH, MDP-style — Appendix J.6), its own symbol for the instrument (`AAPL` on one, a `SecurityID` integer on another, an ISIN behind both), its own sequence space, its own hardware timestamps, and its own session for order entry. The strategy wants *one* answer — "what is the best price I can hit, where, and how much is there" — in the same ~100 ns budget a single-venue strategy spends on one book (Ch. 118 §118.3). Three pieces of engineering stand between the decoders and that answer, and each is a per-tick cost:

- **Normalization** — turning sixteen message vocabularies into one internal event the book builder applies, *without copying* and without deciding anything twice (§73.2.1).
- **Symbology** — turning sixteen venue keys into one dense internal instrument id, once, at the edge, so that everything downstream is an array index (§73.2.2; measured in §73.3: 4.3 ns for an 8-byte symbol through a flat table, 0.4 ns for a dense venue id, 16 ns for the `std::unordered_map<std::string>` you must not use).
- **Consolidation** — keeping every venue's book intact, deriving the consolidated top (and depth where it pays) when a venue's top changes, and *backing a venue out* the instant its feed is declared stale — 34 cycles on an L1-hot instrument in §73.3, 89 when the instrument's line is in L2 (§73.2.3).

And on the way out, **smart order routing**: the venue choice is a latency decision as much as a fee decision — the venue with the best displayed price may be the one whose one-way latency (Appendix K) means your order arrives after the quote is gone — and the decision is a 24-cycle branch-free score over the venues' tops (§73.4.5), followed by Ch. 79's coordinated send when it is several orders at once.

The chapter keeps the per-venue mechanics where they already live (the book structure in Ch. 33, the session protocols in Ch. 106, the gap machinery in Ch. 72) and owns the *merge*: what to normalize on the hot path and what to leave venue-native (§73.2.1), the symbology tables and their lifecycle (§73.2.2, §73.4.1–§73.4.2), the consolidated book with attribution and back-out (§73.2.3, §73.4.3), cross-venue timestamp alignment (§73.2.4, §73.4.4), and routing as a latency decision (§73.4.5). Appendix J.6 is the venue-by-venue reference this chapter instantiates.

## 73.2 Mental model

### 73.2.1 Normalize what the decision needs; leave the rest venue-native

A "normalized market-data event" is the most over-built structure in a feed handler. The temptation is a 200-byte `MarketEvent` with every field any venue could send, filled by sixteen translators, copied into a queue, and read by a strategy that uses four of its fields. The budget says otherwise: the decision reads **instrument id, side, price (scaled), quantity, event kind (add/modify/delete/trade), and the venue's sequence and RX timestamp** — about 32 bytes — and everything else (order ids, attribution flags, trade conditions, MPID, auction indicators) is consumed only by the per-venue *book*, which is venue-specific anyway, or by the capture (which records raw bytes, Ch. 108). So:

- **The per-venue book applies venue-native messages.** `FastBook<Venue>::apply(const ItchAddOrder&)` works on the wire overlay (Ch. 70 §70.2.2); there is no translation step between decode and book. Each venue's book is a template instance over that venue's message types (Ch. 22's static polymorphism), so the only "normalization" is the *key* (§73.2.2) and the *price scale* (Ch. 35 §35.4.1: every venue's price is converted to the internal scaled integer at decode — a shift and a multiply, or nothing when the venue's scale is already yours).
- **The consolidation consumes BBO lines, not messages.** When a venue's book changes its top (and only then — most adds and cancels deep in the book change nothing the consolidation reads), the book publishes its `(bid_px, bid_qty, ask_px, ask_qty)` line (Ch. 33's BBO line, seqlock-published in Ch. 46) and the consolidation re-ranks the venues (§73.2.3). The normalized *event* that crosses the process boundary (Ch. 103) is the 32-byte decision record, stamped with the venue id and the hardware RX timestamp.
- **Leave venue-native everything that does not change a decision.** Trade conditions, odd-lot flags, attribution — keep the raw message in the capture and in the venue book; decode on demand off the hot path (Ch. 108 §108.4.6's one-decoder-two-bindings pattern).

The rule: **normalize the key and the price scale at decode, publish a BBO line per venue, consolidate the lines, and never build a universal message.**

### 73.2.2 Symbology: every venue key becomes one dense id, once

An instrument has as many names as venues: a ticker (`AAPL`, `BRK.B` vs `BRK B` vs `BRK/B` — the three conventions disagree), a venue-assigned integer (`Stock Locate` on Nasdaq, 16-bit and reassigned daily; CME/Eurex `SecurityID`, 32/64-bit and stable per contract), an ISIN/CUSIP/FIGI behind all of them, an OSI symbol for an option (21 characters encoding root, expiry, type and strike), and a *per-session* alias in some FIX dialects. The engineering answer is a **dense internal instrument id** — `uint32_t` in `[0, N)` for the day's universe — assigned at start of day from the reference-data feed(s) and used as the index into every per-instrument array downstream (books, SoA strategy state, risk limits, telemetry counters). Three tables implement it:

| Table | Key → value | Built when | Lookup cost (§73.3) |
|---|---|---|---|
| **venue key → internal id**, one per venue | 8-byte symbol (ITCH, PITCH) → id; `SecurityID`/`Locate` → id | start of day from the venue's security-definition / symbol-directory messages (Appendix J.6); updated intraday on instrument additions | 8-byte symbol: Fibonacci flat table, **4.3 ns**; dense venue id: **direct index, 0.4 ns** |
| **internal id → per-venue keys** | id → `[venue] → venue key` (for order entry: the symbol or `SecurityID` the venue's session expects) | same | an array of `N × V` 8-byte keys, indexed |
| **external identifiers** | ISIN/CUSIP/FIGI ↔ id (for reference data, compliance, the capture's manifest) | same; control plane only | a string-keyed map, never on the hot path |

Two properties matter beyond cost. **Lifecycle**: venue ids like `Locate` are *reassigned daily*, so the table is a daily build input (a perfect hash — Ch. 38 §38.4.4 — when the universe is closed at the open, or a flat table when it grows intraday), and an intraday addition (a new listing, a venue's symbol directory update) is a hot-reload of reference data (Ch. 102 §102.4.1): a new table version published by pointer swap, old readers finishing on the old version (Ch. 47's reclamation). **Trust**: the venue's key is wire input; a `Locate` of 65535 indexes past the end of the table unless bounds-checked once at the edge (Ch. 101 §101.4.1); after the edge, the internal id is *trusted* and every array index downstream is unchecked — which is the point of doing it once.

### 73.2.3 The consolidated book: attribution, back-out, and what "consolidated" means

Keep every venue's book intact and derive the consolidated view from the venues' tops; never merge ladders (Ch. 33 §33.4.6). The reasons are the two things a merged ladder cannot do:

- **Attribution** — the order you send goes to *a venue*, so the question is never "how much is at the best" but "how much is at the best *on each venue*, and at what price one tick worse on each". The consolidated top is a ranking of `V` `(price, qty, venue)` triples per side, with a bitmask of the venues *at* the best (§73.3's `Nbbo` struct: 20 bytes); the depth view, when a strategy needs it, is the same ranking over `V × k` triples for the top *k* levels of each venue — still small, still branch-free, and recomputed only when a venue's top-*k* changes.
- **Back-out** — when Ch. 72 declares a venue's feed gapped-and-unrecovered, or Ch. 74 declares it dark, or the venue halts the symbol, that venue's contribution must leave the consolidated view *now* (its quotes may be minutes stale) without disturbing the others. With per-venue books that is `per[v] = {}` and a re-rank: **89 cycles** in §73.3, the same as any top update; with a merged ladder it is "walk the ladder and remove every level this venue contributed" — unbounded, and the venue tags you needed to do it are the attribution you did not keep.

What the consolidated top is *not*: it is not the **NBBO** the SIP computes (Appendix J.2), which uses the SIP's own (slower) view of the venues, protected-quote rules and round-lot filters; your consolidated top is faster and *different*, and a strategy that must trade "at or better than the NBBO" for compliance needs the regulatory definition, computed from the SIP feed or reconstructed with its rules, as a *separate* line (Appendix K.8). Keep both; name them differently.

### 73.2.4 Time across venues: aligned at the NIC, not at the socket

Each venue's feed arrives on its own line, through its own cross-connect and possibly its own NIC, with its own serialization and switch delays (Appendix K); the venue's own timestamp in the message (`TransactTime`, the MoldUDP64 session's... none — ITCH's nanosecond-since-midnight field) was stamped by *its* matching engine on *its* clock. Comparing "venue A's quote changed before venue B's" needs a common clock, and the only one you have is **your NIC's hardware RX timestamp** (Ch. 75 §75.2, PTP-disciplined to the venues' grandmasters within ~100 ns — Ch. 78). So: stamp every packet at RX in hardware; carry the stamp with the decision record; let the consolidation order venues' updates by RX time, *not* by arrival at the socket (which adds the kernel's or the poll loop's jitter) and *not* by the venue's own timestamp (whose offset from yours is the one-way latency you measured in Ch. 78 §78.4.2 and Appendix K — stable to tens of ns but *different per venue*). The venue timestamp has one hot-path use: **staleness** — `rx_ts − venue_ts` is the one-way latency of this packet, and a jump in it means the path changed (a microwave fade, a switch failover — Appendix K.3, K.6) before any gap or heartbeat says so (Ch. 72 §72.4.4's liveness signals).

The model: **per-venue books apply venue-native messages with the key and price scale normalized at decode; a dense internal id, assigned once a day and bounds-checked once at the edge, indexes everything downstream; the consolidated view is a ranking of the venues' BBO lines with attribution and O(1) back-out, ordered by hardware RX time; and routing is a scored decision over those lines that counts latency as a cost.**

## 73.3 Measure it: symbology lookups, the consolidated update, back-out, and the routing score

*Toolchain and machine: Appendix L.3. The run below is on the 4-vCPU KVM guest (GCC 13.3, `-O2 -std=c++20 -march=native`, TSC at 2.7 GHz — divide cycles by 2.7 for ns); the reference box runs the same source with GCC 14.2.*

Eight thousand instruments, four venues. The symbology tables of §73.2.2 (venue A: 8-byte symbols through a Fibonacci-hashed open-addressing table — Ch. 38 §38.4.3; venue B: dense `SecurityID`s through a direct index; and, for contrast, `std::unordered_map<std::string>`), then the consolidation of §73.2.3 — per-venue tops in one 64-byte line per instrument, the 20-byte `Nbbo` result in a separate array (a hot/cold split, Ch. 10) — updated and re-ranked, backed out and re-ranked, both on random instruments (the realistic case: the working set is 8,000 × 84 bytes = 672 KiB, L2-resident) and on one L1-hot instrument (the compute alone); and the §73.4.5 routing score:

```cpp
// venues.cpp (excerpt) — the consolidated top across V venues with attribution; full file in the repo.
constexpr int V = 4;
struct VenueTop { int32_t bid_px, ask_px, bid_qty, ask_qty; };                 // 16 B per venue
struct alignas(64) Consolidated { VenueTop per[V]; };                           // 64 B: one cache line per instrument
struct Nbbo { int32_t best_bid, best_ask, bid_qty, ask_qty; uint8_t bid_venues, ask_venues; };   // + bitmask of venues at the best

static inline void recompute(const Consolidated& c, Nbbo& n) {                 // V is small and fixed: a straight-line pass
    int32_t bb = INT32_MIN, ba = INT32_MAX, bq = 0, aq = 0; uint8_t bm = 0, am = 0;
    for (int v = 0; v < V; ++v) { const VenueTop& t = c.per[v];
        if (t.bid_qty) { if (t.bid_px > bb) { bb = t.bid_px; bq = t.bid_qty; bm = uint8_t(1 << v); } else if (t.bid_px == bb) { bq += t.bid_qty; bm |= uint8_t(1 << v); } }
        if (t.ask_qty) { if (t.ask_px < ba) { ba = t.ask_px; aq = t.ask_qty; am = uint8_t(1 << v); } else if (t.ask_px == ba) { aq += t.ask_qty; am |= uint8_t(1 << v); } } }
    n = {bb, ba, bq, aq, bm, am};
}
static inline void back_out(Consolidated& c, Nbbo& n, int v) { c.per[v] = {0, 0, 0, 0}; recompute(c, n); }   // a stale venue leaves in O(V)
```

```
== symbology: venue key -> internal id (8,000 instruments, random access) ==
venue A: 8-byte symbol, open-addressing Fibonacci hash    12.5 cyc  (4.3 ns)
venue B: dense SecurityID, direct index                    1.2 cyc  (0.4 ns)
std::unordered_map<std::string> (the thing to avoid)      46.7 cyc (16.1 ns)   + the std::string construction, not counted
== consolidated top across 4 venues (cycles) ==
update one venue's top + recompute, random instrument     89.3 cyc (30.8 ns)   two lines from L2 (per-venue tops + Nbbo)
back out a stale venue + recompute, random instrument     89.5 cyc (30.8 ns)   the same cost: O(V), no ladder walk
same instrument hot in L1: update + recompute             34.3 cyc (11.8 ns)   the compute alone
== SOR: score 4 venues from the consolidated top + fee + measured latency, pick the max (branch-free) ==
                                                          23.7 cyc  (8.2 ns)
sizeof(Consolidated) = 64 B, sizeof(Nbbo) = 20 B
```

Read it as a budget. The symbology lookup is **4 ns per message** for a symbol-keyed venue and **free** for a dense-id venue — which is the argument for translating to a dense id *once*: every downstream array index after the edge is the 0.4 ns row. The consolidated update is **12 ns of compute and 31 ns when the instrument's lines come from L2**, and the difference is Ch. 8's, not this chapter's: an 8,000-instrument universe does not fit L1, so the realistic per-tick cost is the L2 number, and the way to cut it is to shrink what a tick touches (the 64-byte per-venue line *is* that: four venues' tops in one line, the result in another) — not to optimize the four-iteration loop. Back-out costs exactly an update, which is what makes "declare the venue stale and keep trading on the others" a hot-path operation rather than a recovery procedure. And the routing score — 24 cycles for four venues — is small enough to run on every marketable decision; what it cannot be is a *second* pass over anything larger than the tops.

## 73.4 Techniques

### 73.4.1 The symbology build: from definition feeds to dense ids

Each venue publishes its instrument universe before the open — Nasdaq's `Stock Directory` (`R`) messages on the ITCH feed itself, CME's security-definition feed (`MDP 3.0` channel `d`), Eurex's reference data (RDI), Cboe's symbol file — and some publish additions intraday. The build:

1. **Collect** every venue's definitions into a per-venue list of `(venue key, instrument attributes)`; join across venues on a stable external identifier (ISIN/FIGI for cash equities, root+expiry+strike for listed derivatives) to decide which venue keys *are the same instrument*. This is control-plane code: strings, maps, a database if you like — minutes before the open, never on the hot path.
2. **Assign** dense ids `0…N−1` in a stable order (sorted by external id, so a replay of the same day builds the same ids — Ch. 108 §108.4.5) and emit three artefacts: the per-venue `key → id` tables, the `id → per-venue key` array for the gateway's encoders (Ch. 106 §106.4.2), and the per-instrument attribute SoA (tick size, lot size, price scale per venue — Ch. 35 §35.2.2, the risk limits — Ch. 104).
3. **Publish** them as a versioned reference-data snapshot (Ch. 102 §102.4.1): one contiguous allocation, pre-faulted (Ch. 34), pointer-swapped into the feed handlers and the gateway; the version number is in every decision record and in the capture header so a replay uses the same ids.
4. **Verify at the readiness gate** (Appendix M): every venue's live symbol-directory message matched a table entry; a venue key that arrives with no entry *after* the gate is an unknown instrument — counted (an "unknown symbol" counter in the telemetry segment, Ch. 100 §100.4.2), not traded.

The per-venue `key → id` table is a **perfect hash** (Ch. 38 §38.4.4) when the venue's universe is closed at the build, and a Ch. 32 flat table sized for the day's expected additions otherwise; the `id` side is always an array.

### 73.4.2 The edge translation: bounds-check once, index forever

The one place the venue's key is looked up:

```cpp
// feed_edge.hpp — per-venue edge: wire key -> internal id, exactly once per message.
template <class Venue> struct Edge {
    const SymTable<typename Venue::Key>* sym;   // the published reference-data snapshot's table for this venue (Ch. 102: RCU pointer)
    uint32_t n_instruments;
    // Returns the internal id or NO_INSTRUMENT; the caller counts and drops unknowns (Ch. 101 §101.4.1).
    [[gnu::always_inline]] uint32_t translate(typename Venue::Key k) const noexcept {
        uint32_t id = sym->find(k);                                         // 4 ns (symbol) or 0.4 ns (dense id) — §73.3
        return id < n_instruments ? id : NO_INSTRUMENT;                     // the ONE bounds check; downstream indexing is unchecked
    }
};
```

Everything after `translate` indexes with `id`: `books[v][id]`, `state.px[id]`, `limits.max_qty[id]`, `telemetry.msgs[id]`. The `Key` type is the venue's: `Sym8` (Ch. 38 §38.4.2) for ITCH/PITCH symbols — one 8-byte load, no byte swap, no `std::string` — or `uint32_t` for `SecurityID`/`Locate` venues, where `find` is a direct index into a `n_venue_ids`-sized array of internal ids (65,536 entries for a 16-bit `Locate`; the "unknown" value fills the gaps). For a venue whose message carries *both* (ITCH has `Stock Locate` *and* the symbol), use the integer: it is the venue telling you the dense id for free.

### 73.4.3 The consolidated top with attribution and O(1) back-out

The §73.3 structures are the technique; the discipline around them:

- **One 64-byte line per instrument holds the `V` venue tops**, written by whichever feed-handler thread owns that venue (one writer per 16-byte `VenueTop` slot — no two venues' handlers write the same bytes, but they share the line: this is *intentional* sharing of a line that changes a few times per millisecond per instrument, not false sharing of a hot counter — Ch. 44 §44.2 — and it is the trade for a one-line read on the consumer side). Where the venue handlers are separate *processes* (Ch. 103), the line lives in the shared segment (Ch. 67) and the per-slot single-writer rule is the segment's contract.
- **The `Nbbo` result is recomputed by the consumer**, not the producers: the strategy thread reads the line (one seqlock-protected read, Ch. 46 — or, with 16-byte slots written by one `movdqa` each, a torn read across *different* venues' slots is tolerable because each slot is internally consistent and the re-rank is idempotent), runs `recompute` (12 ns), and keeps `Nbbo` in its own SoA row. Producers never compute what consumers can derive in 12 ns from one line.
- **Attribution travels with the result**: the venue bitmask says *where* the best is; a `qty_at_best[v]` array (another 16 bytes) says how much per venue, which is what the router needs (§73.4.5).
- **Back-out is a state flag, not a search**: each venue has a `live` bit in a per-venue status word (set by Ch. 72's gap logic, Ch. 74's dark-feed detector, the venue's own halt message); `recompute` masks a venue whose bit is clear (`if (t.bid_qty & live_mask[v])` — branch-free) so backing out is one store to the status word and the next re-rank. Re-admission after recovery is the reverse: the venue's book is rebuilt from its snapshot (Ch. 33 §33.4.5), its top republished, its bit set.
- **Depth, when it pays**: the same structure over `k` levels per venue (`VenueTop per[V][k]`, 4 venues × 3 levels = 192 bytes, three lines) and a ranking over `V·k` triples by price — a sorting network (Ch. 32 §32.4.6) for 12 elements, still branch-free, still tens of ns. Beyond `k = 3`–5 the question is almost always answered by the per-venue book directly ("how much on venue *v* within 5 ticks" is a Ch. 33 ladder scan, not a merge).

### 73.4.4 Cross-venue alignment and staleness from timestamps

Implement §73.2.4 as two numbers per decision record:

- **`rx_ts`** — the NIC hardware RX timestamp of the packet that produced this update (Ch. 75 §75.2; with kernel bypass, the descriptor's timestamp field — Ch. 82), converted to the common PTP-disciplined timeline once per packet (Ch. 78 §78.4.1). The consolidation and the strategy order venues' updates by `rx_ts`. When two venues' updates for the same instrument arrive within the clock's uncertainty (~100 ns), the order is *unknowable* and the code must not pretend otherwise — the re-rank is idempotent precisely so the order of near-simultaneous updates does not change the result.
- **`one_way = rx_ts − venue_ts`** — per packet, per venue, an EWMA (Ch. 40 §40.4.3) and a deviation; a jump of more than the venue's measured jitter band (Appendix K.6's path-diversity numbers; a microwave-to-fiber failover is milliseconds — Appendix K.3) marks the venue **degraded** before any sequence gap does: the quotes are correct but *old*, and a router that hits them will be late. Publish it as a gauge (Ch. 100 §100.4.2) and let the router's latency term (§73.4.5) read the live value rather than yesterday's constant.

### 73.4.5 Smart order routing as a latency decision

Routing a marketable order means choosing the venue(s) to send to, and the choice is a score with four terms, all of which are numbers the hot path already has:

```cpp
// sor.hpp — score the venues for a marketable buy of qty at or below limit; branch-free over V. (venues.cpp measures 24 cycles.)
struct VenueCost { int32_t take_fee_e4; int32_t one_way_ns; int32_t fill_prob_e4; };   // fee/rebate at 1e-4 per share; measured; estimated
inline int route(const Consolidated& c, const Nbbo& n, const VenueCost* vc, uint8_t live_mask) {
    int64_t best = INT64_MIN; int bv = -1;
    for (int v = 0; v < V; ++v) {
        const VenueTop& t = c.per[v];
        int64_t px_pen = int64_t(t.ask_px - n.best_ask) * 10000;                               // ticks worse than the consolidated best, 1e-4 units
        int64_t score  = -(px_pen + vc[v].take_fee_e4) * vc[v].fill_prob_e4 / 10000              // expected edge after fees, weighted by fill probability
                         - vc[v].one_way_ns / 100;                                               // latency penalty: 100 ns ~ one 1e-4 unit (tune per strategy)
        bool better = (t.ask_qty != 0) & ((live_mask >> v) & 1) & (score > best);
        best = better ? score : best; bv = better ? v : bv;                                      // cmov, no branch (Ch. 15)
    }
    return bv;
}
```

- **Price** is the venue's displayed top relative to the consolidated best (the attribution of §73.4.3).
- **Fee/rebate** per venue per instrument class, from the reference-data snapshot (venue fee schedules are tiered and change monthly — a `constexpr`-shaped table, Ch. 21, published with the symbology).
- **Fill probability** — the honest version of "expected fill": a per-venue estimate from your own fill history (Ch. 118 §118.4.5's post-trade analytics feed it back: hit rate vs latency, per venue, per time of day), because displayed size is not available size (hidden orders, other racers) and the venue whose quote you reach last will not fill you.
- **Latency** — the measured one-way (§73.4.4, Appendix K), as a live number: the venue 300 ns further away costs 300 ns of race, which on a fast-moving quote is the difference between a fill and a miss.

Two consequences for the send path: when the score says *several* venues (the order is larger than any one venue's top, or the strategy sweeps), the sends go out as a **coordinated batch** with departure times chosen so they *arrive* together (Ch. 79 §79.4.3 — the venue 300 ns further away is sent 300 ns earlier), because a sequential send lets the first venue's fill print and move the others' quotes before your later orders arrive; and when the venue's session is throttled (Appendix K.4), the per-venue token bucket in the risk gate (Ch. 104 §104.4.3) is a term the router must read *before* scoring, or it will choose a venue it cannot send to. The regulatory overlay — Reg NMS order-protection, trade-through, the NBBO as the SIP defines it — is a constraint on the *decision*, not on these mechanics; Appendix K.8 maps it to the code paths.

## 73.5 Pitfalls & anti-patterns

- **The universal normalized message.** A 200-byte struct filled by sixteen translators and read for four fields: a copy per message and a translation that decides nothing (§73.2.1). Normalize the key and the scale; publish BBO lines; keep messages venue-native.
- **`std::string` symbols past the edge.** 16 ns plus an allocation per lookup (§73.3), against 0.4 ns for a dense id. Translate once; index forever (§73.4.2).
- **Trusting the venue's integer id as an index without the one bounds check.** A `Locate` of 65535 or a `SecurityID` from a malformed message indexes past the table (Ch. 101 §101.4.1). One check at the edge; none after.
- **Merging ladders.** No attribution, no O(1) back-out, and a stale venue's quotes poison the merged book until a walk removes them (§73.2.3).
- **Ordering venues by socket arrival or by the venue's timestamp.** The first adds your own jitter, the second adds a per-venue offset you measured once and that just changed with a path failover (§73.2.4, §73.4.4). Hardware RX time, PTP-disciplined.
- **Calling your consolidated top "the NBBO."** The SIP's NBBO has rules yours does not implement; compliance checks use the regulatory definition (§73.2.3, Appendix K.8).
- **Routing on displayed price alone.** The best-displayed venue 400 ns further away is the one that will not fill you; the score needs latency and fill probability (§73.4.5).
- **Sequential sends to several venues.** The first fill moves the others' quotes before your later orders arrive; coordinate departure times (Ch. 79 §79.4.3).
- **Symbology rebuilt intraday by replacing the table in place.** A reader mid-lookup sees a half-written table; publish a new version by pointer swap and reclaim the old one safely (Ch. 102 §102.4.1, Ch. 47).
- **Ids that differ between live and replay.** If the dense ids depend on arrival order of definition messages, the replay assigns different ids and nothing matches; assign in a stable sorted order and record the version (§73.4.1, Ch. 108).

## 73.6 Exercises & checklist

**Exercises**

1. **Build the tables.** From two venues' public symbol directories (Nasdaq's `R` messages in a sample ITCH file; a CME security-definition sample), join on ISIN/root and assign dense ids; emit the three artefacts of §73.4.1 as a generated header. Measure the per-venue lookup cost with the §73.3 harness.
2. **Shrink the line.** Measure `recompute` for 4, 8 and 16 venues with the per-venue tops in one, two and four cache lines, random instruments over an 8,000-instrument universe; confirm the cost is lines-touched, not loop length. Then add a `qty_at_best[V]` array and re-measure.
3. **Back-out drill.** Drive the consolidation from two replayed captures with a gap injected on one venue (Ch. 72 ex. 3); measure the time from gap detection to the venue's disappearance from `Nbbo`, and confirm the other venue's top is unchanged throughout.
4. **Alignment.** Capture the same instrument on two venues with hardware RX timestamps; plot `rx_ts − venue_ts` per venue over a session; find the path-change events and compare their lead over the first sequence gap or heartbeat miss.
5. **Route and measure.** Implement §73.4.5 with your fee schedule and measured one-way latencies; replay a day and compute, per venue, the fill rate of routed orders vs the fill rate the displayed size predicted (Ch. 118 §118.4.5). Re-tune the latency weight and repeat.
6. **Coordinated sweep.** Send a three-venue sweep sequentially and with Ch. 79's scheduled departures; capture at the venue-side taps (Ch. 108) and compare arrival spreads.

**Checklist — multi-venue**

- [ ] Per-venue books apply **venue-native** messages; only the **key and price scale** are normalized at decode; no universal message struct (§73.2.1).
- [ ] Every venue key becomes a **dense internal id** at the edge — one lookup, one bounds check — and everything downstream is an array index (§73.2.2, §73.4.2).
- [ ] Symbology is a **versioned reference-data snapshot**, built before the open in a replay-stable order, hot-reloaded by pointer swap, verified at the readiness gate (§73.4.1).
- [ ] The consolidated top is a **ranking of per-venue BBO lines** with a venue bitmask and per-venue size; back-out is a **status bit**, O(V) (§73.4.3).
- [ ] Cross-venue ordering uses **hardware RX timestamps**; `rx_ts − venue_ts` per venue is a live staleness gauge that degrades a venue before a gap does (§73.4.4).
- [ ] The router scores **price, fee, fill probability and measured latency**, reads the throttle first, and **coordinates** multi-venue departures (§73.4.5, Ch. 79).
- [ ] "Consolidated top" and "NBBO" are **different names for different things**; compliance uses the regulatory one (§73.2.3, Appendix K.8).

## 73.7 References

- Nasdaq, *TotalView-ITCH 5.0* and *MoldUDP64* specifications (the `R` Stock Directory message, `Stock Locate`); CME Group, *MDP 3.0 Market Data* (security definition channel) and *iLink 3*; Deutsche Börse, *Eurex Enhanced Order Book Interface (EOBI)* and *Reference Data Interface (RDI)*; Cboe, *PITCH* and *BOE* specifications — the per-venue keys and definition feeds of §73.2.2 and §73.4.1 (Appendix J.6 tabulates them).
- SEC, *Regulation NMS* (Rule 611 order protection, Rule 603 and the SIPs; the 2020 Market Data Infrastructure rule) — why the regulatory NBBO is not your consolidated top (§73.2.3).
- OpenFIGI / Bloomberg, *FIGI*; ISO 6166 (ISIN); OCC, *Option Symbology Initiative* — the external identifiers that join venue keys (§73.4.1).
- IEEE 1588-2019 (PTP) and the Ch. 78 references — the common timeline of §73.2.4.
- J. Hasbrouck, *Empirical Market Microstructure* (OUP, 2007); M. O'Hara, *Market Microstructure Theory* — the fragmentation and routing economics behind §73.4.5.

## 73.8 Additional Reading

- Appendix J (*Market-Structure Primer*), especially **J.6** (venue anatomy) and J.2 (NBBO); Appendix K (*Exchange Connectivity*) — per-venue latency, throttles and the regulatory overlay (K.8).
- Ch. 33 (*LOB*) — the per-venue book; Ch. 38 (*Hashing*) — the symbol key and the perfect hash; Ch. 72/74 — the feed states that trigger back-out; Ch. 79 — coordinated sends; Ch. 102 — reference-data hot reload; Ch. 106 — one session per venue; Ch. 118 §118.4.5 — the fill-rate feedback that tunes the router.

---

*Next: Ch. 74 — Multicast Group Management & Dark-Feed Detection: the membership machinery (IGMP/MLD, snooping, the querier) that keeps a venue's feed flowing, and the silent failure — a feed that simply stops — that produces no gap for Ch. 72 to recover and that this chapter's consolidation backs out the moment it is detected.*
