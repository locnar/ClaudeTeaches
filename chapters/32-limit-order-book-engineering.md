# Part IV — Memory Management

# Chapter 32 — Limit Order Book Engineering

> **Prerequisites:** Ch. 31 (flat containers, intrusive FIFOs, slot maps and §31.4.6's small-array search — the parts this chapter assembles), Ch. 9 (data-oriented layout — the ladder is an SoA decision), Ch. 29–30 (allocation cost; the order pool), Ch. 35 §35.4.1 (occupancy bitmaps, `tzcnt`/`blsr`), Ch. 8 (cache lines — every structure here is sized to one), Ch. 3 (the per-event latency *distribution* is the result, not a mean), Appendix J.2 (BBO, depth, market-by-order vs market-by-price feeds).
>
> **Leads into:** Ch. 37 (the strategy reads this book: OFI and microprice straight off the BBO line), Ch. 43 (publishing the BBO to readers with a seqlock), Ch. 67 (the feed handler that calls `add`/`remove` on every message) and Ch. 69 §69.4.2 (snapshot recovery — §32.4.5 is the book's half of it), Ch. 97 (the book inside the deterministic core), Ch. 102 (captured data as the only honest input), Ch. 112 §112.3 (the "book update / strategy" stage this chapter's numbers feed). The multi-venue consolidated book is a separate, later item; §32.4.6 says where it attaches.

---

## 32.1 Why it matters: the book is the data structure on the critical path

Every market-data message ends in a book update, and the capstone's budget (Ch. 112 §112.3) puts "book update / strategy" at ~150 ns p50 and ~500 ns p99.9 — and names it the stage whose *tail* dominates tick-to-trade, because a cold level, a mispredicted rare path or a TLB miss on a rarely touched page lands here. Ch. 31 §31.4.5 sketched the design and promised a number it never produced: the `std::map<Price, std::list<Order>>` book is "an order of magnitude too slow". This chapter builds both books, replays five million captured-style events through each, and reports the distributions. The promise was roughly right at the median (3.6×, 16 vs 58 ns once the harness floor is subtracted) and wrong in an instructive way at the tail: the fast book's p99.9 is set by *where its cold orders live*, not by the container, and the baseline's by its 4.8 million hot-path allocations.

Three decisions set the shape of a book before any code is written, and each is a memory-layout decision in disguise: whether the feed is **market-by-order** (every order visible, with an id — ITCH, CME MBO) or **market-by-price** (aggregated quantity per level — most MBP-10/L2 feeds), which decides whether an order table exists at all; how **price levels** are indexed — a dense array keyed by tick, a sorted small array, a tree — which decides the cost of "find the level" and of "find the new best after the best emptied"; and how **order ids** are mapped to orders, which on an MBO feed is the single hottest lookup in the process. The fourth decision is the one Ch. 1 asks of everything: the access distribution is *touch-dominated* — most adds, cancels and executions happen within a few ticks of the best — so the structure is optimised for the touch and tolerates a slower path for the depth, with the one event that moves the touch itself (a gap, a halt reopening, a trend that walks the price across the dense window) designed and measured as the tail event it is.

## 32.2 Mental model

### 32.2.1 Market-by-price (L2) vs market-by-order (L3) vs top-of-book: memory and per-message work

| Feed | What a message says | State per instrument | Per-message work | What you cannot know |
|---|---|---|---|---|
| **Top-of-book** (BBO only) | new best bid/ask and sizes | 16–32 bytes | one store | depth, queue position, order flow |
| **Market-by-price** (L2, MBP-n) | "level p on side s now has quantity q (and n orders)" | *levels* × 16 B — a few hundred bytes for the active window | index the level, store q — §32.3 measures ~1 ns net | which orders, your queue position, cancels vs executions (some feeds flag them) |
| **Market-by-order** (L3, MBO) | add / modify / cancel / execute *order id* | *orders* × 32 B + levels + an id map — tens of MiB venue-wide | id lookup, unlink/relink, level aggregate update — §32.3 measures ~16 ns net | nothing about the visible book; hidden/iceberg quantity stays hidden |

L3 costs ten times L2 per message and holds a hundred times the state, and it is the only one that gives **queue position** (how many shares are ahead of yours at a level — the input every passive strategy prices), per-order cancel/execute attribution (the order-flow signals of Ch. 37) and the ability to detect the venue's own matching from the sequence of executions. A venue that offers both usually sends MBO on a higher-bandwidth feed; the choice is a strategy decision first and an engineering budget second, and the engineering budget is this chapter.

### 32.2.2 Indexing price levels: dense ladder, sorted store, tree

Prices are integers in ticks (Ch. 34 §34.2.2), and the levels that matter are a contiguous band around the best. That makes the **dense tick-indexed ladder** — `Level ladder[WINDOW]` with `index = tick − base` — the natural hot structure: O(1) to any level, no search, no allocation, 16 bytes per level so four levels share a cache line and the whole active band of a liquid instrument (a few hundred ticks) is a few KiB. Its two costs are the **window**: the ladder covers `WINDOW` ticks and the price can walk out of it, which forces a **re-centre** (§32.4.1: move the window, migrate the levels that fall off it to a cold store, pull the cold ones that come back) — a tail event whose cost is proportional to `WINDOW`; and **sparsity**: an instrument whose levels are far apart (an illiquid option, a bond quoted in large increments) wastes the ladder and is better served by a **sorted small array** of occupied levels searched with §31.4.6's SIMD scan or branchless search (tens of ns, no window, O(n) insert that is cheap at n ≤ 64). The tree (`std::map`) is the structure with no good case: O(log n) dependent cache misses to find a level and an allocation to create one. A **hybrid** — dense ladder for the band, sorted array beyond it — is what this chapter builds, and the occupancy **bitmap** over the ladder (§32.4.2) is what makes "the best level emptied, find the next" a `tzcnt`.

### 32.2.3 Order identity: three id maps

An MBO feed names orders by a venue-assigned id and every cancel, modify and execution arrives with that id, so `id → order` is the hottest lookup in the process (§32.3: about half of all events). Three designs, measured in §32.3:

- **Direct addressing** (the brainstorm's D04): ITCH order reference numbers, CME order ids and most venues' ids are **near-monotone** — assigned from a counter, increasing within a session. `slot[id & (SLOTS − 1)]` holds the pool index; the pool entry holds the id, and comparing it is the validation (a stale or unknown id finds a different id in the slot and is rejected). No hashing, one predictable compare, and the slots of *recent* ids are adjacent — the prefetcher's favourite pattern. The requirement is that `SLOTS` exceeds the span of ids simultaneously in flight; a venue that assigns ids from several counters (one per matching engine or gateway) needs one table per counter or a prefix split.
- **Slot map with generational handles** (§31.4.4): when *you* assign the handle — the gateway's in-flight order table (Ch. 99), a book you rebuilt from a snapshot and index internally — the handle carries the slot index and the generation check rejects a reused slot. Not applicable directly to a venue-assigned id, which is why it is not in this chapter's table; it fronts the pool in §32.4.3 all the same, because the pool slot's id compare *is* a generation check.
- **Flat open-addressing hash** (§31.4.1): the general answer for opaque ids (a 64-bit hash of an exchange-side string, a venue that recycles ids randomly). One multiply-shift, a probe, a key compare; backward-shift deletion keeps the probe sequences short without tombstones. `std::unordered_map` is in the table only as the thing to stop doing: a node allocation per add.

### 32.2.4 The access distribution, and what a re-centre is

The synthetic stream in §32.3 is shaped to captured statistics rather than drawn from a capture (the chapter's one honesty caveat, stated again in §32.3): about half of all events are adds and almost all the rest cancels, executions a few percent; adds land at the touch ~45 % of the time and at geometrically decreasing distances behind it; cancels prefer recent orders; the mid random-walks and, once, gaps 2 % (a halt reopening). Against that distribution the dense ladder is hot where it counts and the cold store sees a few percent of events; the three re-centres in five million events are each a ~25 µs excursion — **a thousand times the median**, once every ~1.7 M events. That is the shape of a well-designed book: a flat body and a rare, bounded, *understood* spike, which Ch. 47's flow control and Ch. 97's deterministic core can plan for because its cause is a single, loggable event with a known cost.

## 32.3 Measure it: five million events through five books

**Machine, toolchain, flags.** `lob.cpp` (the complete harness — the book, the three id maps, the L2 book, the baseline, the stream generator and the histogram — is 300 lines; the hot parts are in §32.4) was **run on the author's 4-vCPU KVM guest** (Appendix L.2: TSC 2.70 GHz, Linux 6.18, no isolation; GCC 13.3 `-O2 -std=c++20 -march=native`, Clang 18.1 `-march=x86-64-v3`, pinned with `taskset -c 2`). Per-event latency is an `rdtsc` pair around `apply()`, 4.5 M samples after a 500 k warm-up, sorted for percentiles; the **harness floor** (the pair with an empty body) is the first row and is *included* in every other row. The figures are VM-measured; the orderings and ratios transfer to the reference machine, the absolute medians should come down ~20 % at 2.9 GHz with a quiet L2, and the p99.99 column is the guest's scheduler, not the book. Stream: 5 M events, one instrument, ~10 k live orders held by the generator's floor (34 k at the end, partial executions accumulate), 4096-tick ladder per side, a 2²⁰-order pool, 2²² direct-address slots.

```
   ns / event (VM-measured, GCC 13.3)                  p50     p90     p99    p99.9   p99.99    allocations     notes
   (rdtsc pair alone — the harness floor)             11.9    11.9    14.8    27.4     30.4          0
   L3 ladder + bitmap + direct-addressed ids          28.1    46.7   171.1   328.9   4828.1          0          3 re-centres: 25.8 us mean, 34.4 us worst
   L3 ladder + flat open-addressing id map            28.1    94.1   261.5   414.1   5982.2          0          (the hash in FRONT of the same book)
   L3 ladder + std::unordered_map id map              43.7   135.6   290.4   516.3   8170.4   2 405 042      one node per add
   L2 (market-by-price) ladder, set-level updates     13.3    14.1    14.1    28.9     33.3          0          ~1.4 ns net: a store and two bitmap ops
   std::map<Price, std::list<Order>> + unordered_map  70.4   151.9   303.0   497.0   4706.7   4 821 532      Ch. 31 §31.4.5's baseline
   Clang 18.1, same stream:   ladder 28.9 / 46.7 / 168.9 / 343.7     map 82.2 / 164.4 / 317.0 / 515.6
```

Read it in four steps. **The median**: the ladder book costs ~16 ns net per event (28.1 − 11.9) against ~58 ns for the map/list baseline — 3.6×, Ch. 31's "order of magnitude" was an overstatement at the median on a 34 k-order book that fits L2; size the baseline's working set past L3 (exercise 2) and it becomes one. **The allocations column** is the real verdict: the baseline allocates on every add (a list node, and an `unordered_map` node), 4.8 M times in 5 M events; the ladder book allocates *zero* times (the `operator new` counter wraps the timed region — Ch. 29's instrument, kept on), so its tail cannot contain an allocator, and the baseline's p99.99 is where the allocator's own tail shows. **The id map rows**: putting a flat hash in front of the same book doubles p90 (47 → 94 ns: a multiply, a probe and a key compare on a line the direct map never touched); `std::unordered_map` adds the node allocation and a dependent pointer chase on top. **The L2 row** says what you pay for L3: ~1.4 ns net for a set-level update against ~16 ns for an add/cancel with its order-table work — the 10× of §32.2.1, measured.

Then the tail, which is this chapter's lesson about *where state lives*. The ladder book's p99 (171 ns) and p99.9 (329 ns) are not the ladder: they are **cancels of old orders** — orders added a million events ago sit in pool slots and id slots that have long left the cache (the pool is 32 MiB, the slot array 16 MiB), so a cancel of one is two or three DRAM lines, and a cancel at a level that *emptied* adds the bitmap scan for the new best. The baseline pays the same misses *plus* its own; that is why the gap narrows from 3.6× at p50 to 1.5× at p99.9. The fix is not in this chapter's structures but in Ch. 59's warming and in the pool's allocation order (exercise 4: a LIFO free list keeps recently freed slots hot; an order that lives long is cold by definition). The three **re-centres** are the p99.99+ column's known residents: 25.8 µs mean, 34.4 µs worst, each a 64 KiB ladder copy plus a 4096-slot bitmap rebuild plus the cold migrations — `WINDOW`-proportional, so a 1024-tick window would cost a quarter of it four times as often (exercise 3 sweeps it). Everything else above ~5 µs is the guest's scheduler (no `isolcpus` here — Ch. 56).

**What the numbers do not say.** The stream is synthetic. Its proportions follow published ITCH statistics and its shape (touch-dominated adds, recency-biased cancels, a drifting mid, one gap) is what makes the ladder's hot/cold split exercise its paths, but the real distribution of touched levels, the burstiness of the open (Ch. 67 §67.2.4) and the venue's actual id scheme are things only a capture gives you — Ch. 102's replay harness takes `lob.cpp`'s `apply()` unchanged, and exercise 1 is to feed it one.

## 32.4 Techniques

### 32.4.1 The dense ladder with a cold overflow and re-centring

```cpp
// lob.cpp (excerpt) — one side of the book. Level is 16 bytes (four per line), Order 32 (two per line).
struct Level { u32 qty; u32 count; u32 head, tail; };                  // head/tail: pool indices of the level's FIFO
struct Order { u64 id; u32 qty; u32 prev, next; i32 tick; u8 side; u8 pad[3]; };
static_assert(sizeof(Level) == 16 && sizeof(Order) == 32);              // Ch. 28 §28.4.3: a layout contract

struct ColdLevels {                                                    // levels outside the window: sorted, pre-reserved, searched branchlessly (§31.4.6)
    struct E { i32 tick; Level L; }; std::vector<E> v;
    u32 lower_bound(i32 tick) const { const E* a = v.data(); u32 n = u32(v.size()); if (!n) return 0; const E* b = a;
        while (n > 1) { u32 half = n / 2; b += (b[half].tick < tick) * half; n -= half; } return u32((b->tick < tick) + (b - a)); }
    Level* find(i32 tick);  Level& get_or_insert(i32 tick);  void erase(i32 tick);   // insert/erase shift: rare, cold
};
template <u32 WINDOW, u32 POOL> struct FastBook {
    static constexpr u32 MARGIN = WINDOW / 8;                          // re-centre when the touch gets this close to an edge
    struct Side {
        Level ladder[WINDOW];                                          // index = tick - base
        u64 occ[WINDOW / 64]; u64 summary;                             // occupancy bitmap + one bit per word (§32.4.2)
        i32 base, best;                                                // ladder[0] is tick `base`; best = INT32_MIN/MAX when empty
        ColdLevels cold{1u << 16};
    };
    alignas(64) Side sides[2];                                         // 0 = bid, 1 = ask
    alignas(64) struct { i32 bid, ask; u32 bid_qty, ask_qty; } bbo;    // its own line: what readers want (§32.4.4)
    alignas(64) Order pool[POOL]; u32 free_head;                       // the order table: stable slots, free list through `next`
    static constexpr u32 SLOTS = POOL * 4; u32 slot[SLOTS];            // direct addressing (§32.4.3)
    Level scratch[WINDOW];                                             // a re-centre builds the new ladder here

    static u32 idx_of(const Side& s, i32 tick) { return u32(tick - s.base); }        // >= WINDOW means "outside" (one unsigned compare)
    static bool better(u8 side, i32 a, i32 b) { return side == 0 ? a > b : a < b; }

    void add(u64 id, u8 side, i32 tick, u32 qty) {
        Side& s = sides[side];
        u32 i = idx_of(s, tick); bool improves = better(side, tick, s.best);
        if (improves && (i >= WINDOW || i < MARGIN || i >= WINDOW - MARGIN)) [[unlikely]] { recentre(s, tick); i = idx_of(s, tick); }
        u32 o = free_head; free_head = pool[o].next;                   // pop the pool: no allocation, ever
        Order& ord = pool[o]; ord.id = id; ord.qty = qty; ord.tick = tick; ord.side = side; ord.next = UINT32_MAX;
        Level& L = (i < WINDOW) ? s.ladder[i] : s.cold.get_or_insert(tick);   // a deep add outside the window is cold, not a re-centre
        if (L.count == 0) { L.head = o; ord.prev = UINT32_MAX; if (i < WINDOW) { s.occ[i / 64] |= 1ull << (i % 64); s.summary |= 1ull << (i / 64); } }
        else { pool[L.tail].next = o; ord.prev = L.tail; }
        L.tail = o; L.qty += qty; ++L.count;
        slot[id & (SLOTS - 1)] = o;
        if (improves) s.best = tick;
        if (improves || tick == s.best) publish_bbo();
    }
    void recentre(Side& s, i32 around) {                               // the tail event (§32.3: ~26 us for WINDOW = 4096)
        i32 new_base = around - i32(WINDOW / 2);
        std::memset(scratch, 0, sizeof scratch);
        for (u32 w = 0; w < WINDOW / 64; ++w) for (u64 word = s.occ[w]; word; word &= word - 1) {   // every occupied level, via the bitmap
            u32 i = w * 64 + u32(std::countr_zero(word)); i32 tick = s.base + i32(i); u32 j = u32(tick - new_base);
            if (j < WINDOW) scratch[j] = s.ladder[i]; else s.cold.get_or_insert(tick) = s.ladder[i];   // fell off the window -> cold
        }
        for (u32 k = 0; k < s.cold.v.size();) { u32 j = u32(s.cold.v[k].tick - new_base);              // cold levels now inside -> hot
            if (j < WINDOW) { scratch[j] = s.cold.v[k].L; s.cold.v.erase(s.cold.v.begin() + k); } else ++k; }
        std::memcpy(s.ladder, scratch, sizeof scratch); s.base = new_base;
        rebuild_bitmap(s);
    }
    /* remove(), find_best(), publish_bbo(): §32.4.2–§32.4.4 */
};
```

- **The window is a policy, not a size.** `WINDOW` ticks centred on the touch; `MARGIN` decides how close to an edge the *best* may come before the window moves. Only an add that **improves the best** can trigger a re-centre — a deep order outside the window goes to the cold store (`get_or_insert`, a ~20-element sorted insert) without moving anything, because the price did not move. That one rule is what keeps re-centres at three per five million events.
- **Orders never move.** A re-centre copies `Level` records (16 bytes: the FIFO's head/tail *indices*) into the new positions; the orders they point at stay in their pool slots and their intrusive links stay valid. This is why the FIFO is index-linked through the pool and not pointer-linked through the ladder.
- **Cold is a sorted small array, deliberately.** Levels beyond the window are rare, few (the stream's 1 023 at the end are the stale depth left behind by the drift) and queried by exact tick; a sorted `std::vector<E>` reserved once, searched with §31.4.6's branchless form, costs ~20 ns to find and an O(n) shift to insert or erase — on the cold path only. A `flat_map` is the same thing with a name; an `unordered_map` would allocate.
- **Re-centre cost is `WINDOW`-proportional** (64 KiB memset + copy + a 4096-slot bitmap rebuild ≈ 26 µs here) and its frequency is drift-proportional. The ring-indexed variant — `index = tick & (WINDOW − 1)` with a moving `base`, so a re-centre clears only the vacated slots and never copies — makes the cost proportional to the levels that *move* instead; it costs a wrap-aware bitmap scan in `find_best` and is exercise 3.

### 32.4.2 Occupancy bitmaps and finding the next best

When the best level empties, the next best is the highest (bids) or lowest (asks) occupied slot inside the ladder — a scan the bitmap turns into a handful of instructions (Ch. 35 §35.4.1): `summary` has one bit per 64-slot word, so `countl_zero(summary)` names the highest non-empty word and `countl_zero(occ[w])` the slot within it. Two levels of 64-way fan-out cover 4096 ticks in two `lzcnt`s; a third level would cover 262 144 — the first rungs of the van Emde Boas-style ladder the brainstorm's D05 proposes and this chapter does not build. The cold store's best (its last or first element) is compared against the ladder's so a crossed or stale cold level is never missed:

```cpp
    i32 find_best(const Side& s, u8 side) const {                      // after the best emptied: bitmap scan toward the inside, then cold
        i32 hot = side == 0 ? INT32_MIN : INT32_MAX;
        if (side == 0) { u64 sum = s.summary; while (sum) { u32 w = 63 - u32(std::countl_zero(sum)); if (s.occ[w]) { hot = s.base + i32(w * 64 + 63 - std::countl_zero(s.occ[w])); break; } sum &= ~(1ull << w); } }
        else           { u64 sum = s.summary; while (sum) { u32 w = u32(std::countr_zero(sum)); if (s.occ[w]) { hot = s.base + i32(w * 64 + std::countr_zero(s.occ[w])); break; } sum &= sum - 1; } }
        if (!s.cold.v.empty()) { i32 c = side == 0 ? s.cold.v.back().tick : s.cold.v.front().tick; if (better(side, c, hot)) return c; }
        return hot;
    }
```

The bitmap is maintained on the two transitions only — a level's count going 0 → 1 (set) and 1 → 0 (clear, and clear the summary bit if the word is now empty) — so the common add-to-existing-level and cancel-leaving-others-behind paths never touch it. The same bitmap answers "how many levels between the touch and p" (`popcount` of a masked range — depth-in-levels for a signal) and drives the re-centre's migration loop.

### 32.4.3 The order table: pool, direct addressing, and the alternatives

```cpp
    Order* lookup(u64 id) { u32 o = slot[id & (SLOTS - 1)]; return (o != UINT32_MAX && pool[o].id == id) ? &pool[o] : nullptr; }
    void remove(u64 id, u32 exec_qty /* 0 = cancel all */) {
        Order* ord = lookup(id); if (!ord) [[unlikely]] return;         // unknown or stale id: a no-op, never a corruption
        Side& s = sides[ord->side]; i32 tick = ord->tick; u32 i = idx_of(s, tick); bool hot = i < WINDOW;
        Level& L = hot ? s.ladder[i] : *s.cold.find(tick);
        u32 q = exec_qty && exec_qty < ord->qty ? exec_qty : ord->qty;
        L.qty -= q; ord->qty -= q;
        if (ord->qty) { if (tick == s.best) publish_bbo(); return; }    // partial execution: the order keeps its queue position
        if (ord->prev != UINT32_MAX) pool[ord->prev].next = ord->next; else L.head = ord->next;   // unlink: O(1), no search
        if (ord->next != UINT32_MAX) pool[ord->next].prev = ord->prev; else L.tail = ord->prev;
        --L.count; u32 o = u32(ord - pool); slot[id & (SLOTS - 1)] = UINT32_MAX; ord->id = 0; ord->next = free_head; free_head = o;
        if (L.count == 0) {
            if (hot) { s.occ[i / 64] &= ~(1ull << (i % 64)); if (!s.occ[i / 64]) s.summary &= ~(1ull << (i / 64)); } else s.cold.erase(tick);
            if (tick == s.best) [[unlikely]] { s.best = find_best(s, ord->side); publish_bbo(); return; }
        }
        if (tick == s.best) publish_bbo();
    }
```

- **The pool is the order table.** Stable slots (an order's index never changes while it lives — the FIFO links depend on it), a free list threaded through `next`, LIFO reuse so the slot a cancel just freed is the hot one the next add takes. The id stored *in* the slot is what makes direct addressing safe: a late cancel for an id whose slot was reused finds another id there and is rejected — the generation check of §31.4.4 with the venue's own counter as the generation.
- **Direct addressing** costs one masked load and one compare, and the slots of consecutive ids are consecutive bytes (`slot[]` is a `u32` array: sixteen recent orders per cache line). The constraint is `SLOTS > span of ids in flight`; 2²² here against ~3 M ids per 5 M events, and a session-long run needs the span bounded by the venue's id behaviour (ITCH reference numbers are per-day monotone; the span in flight is the number of orders that have been *added and not yet removed* plus the gap, which `slot[]` sized to a power of two above the venue's documented daily maximum covers). A venue that reuses ids within a session breaks the "monotone" premise and needs the flat hash — §32.3 measured what that costs (+47 ns at p90).
- **The slot map** (§31.4.4) is this structure's cousin for ids *you* assign — the gateway's in-flight table, timers, the replay harness's internal order numbering. When a book is rebuilt from a snapshot that carries no order ids (an MBP snapshot feeding an L3-shaped book), the rebuilt orders get slot-map handles and the venue's later ids are mapped to them through the direct table; the two coexist.
- **Execution semantics** are the venue's, not the book's: an execution reduces the order's quantity *in place* so queue position survives (as above); a *modify* that reduces quantity keeps position on most venues while one that increases it or changes price is a cancel + add that loses it — the book must do exactly what the venue does, which is why Ch. 109's venue double replays the venue's own conformance examples through `apply()`.

### 32.4.4 BBO vs depth: the hot/cold split and publication

The `bbo` member sits on its own cache line and is rewritten only when an event touches the best (`publish_bbo()` in the code above: four stores). That line is what everything downstream reads — the strategy's microprice and OFI (Ch. 37 §37.4.4), the risk gate's reference price (Ch. 98), the telemetry sampler (Ch. 94) — and it is what Ch. 43's seqlock publishes to other threads and processes: the writer bumps a sequence word around the four stores, readers retry on a torn read. Keeping the BBO *separate* from the ladder is the hot/cold split at the structure level: the ladder's lines are written by every event and read by few; the BBO line is written by the ~30 % of events that touch the best and read by everyone. Depth *beyond* the touch — "quantity within n ticks" for an impact estimate — is a prefix sum over the ladder that the strategy computes lazily (Ch. 37 §37.4.4 skips it when the update did not reach the depth it uses); the brainstorm's D07 (a Fenwick tree over level quantities for O(log n) cumulative depth) is the structure for strategies that query it on every event, and is not built here.

### 32.4.5 Snapshot rebuild and increment splice

Ch. 69 §69.4.2 owns the *protocol* half of recovery — request the snapshot, buffer live increments, splice by sequence number; this is the book's half. A snapshot is a list of levels (MBP) or of orders (MBO) with a sequence number; the rebuild is: clear both sides (`memset` ladder and bitmap, reset the pool's free list, `0xFF` the slot table — the setup path, allowed to be slow), choose `base` so the snapshot's best is central (one re-centre's worth of work, done once), then `add()` every snapshot order in the snapshot's own order (which *is* queue priority order on every venue that publishes one) and replay the buffered increments whose sequence exceeds the snapshot's. Two book-side rules make the splice correct: increments *older* than the snapshot are dropped, not applied (applying a cancel the snapshot already reflects would hit `lookup() == nullptr` and be ignored, but applying an *add* the snapshot already contains would double it); and during the rebuild the book is marked *not trustworthy* (Appendix M's readiness state) so no strategy reads a half-built BBO. The cost is `n_orders × ~16 ns` plus the buffered replay — a 50 k-order book rebuilds in under a millisecond, which is why Ch. 69 can afford to resync rather than trade through a gap.

### 32.4.6 Consolidated and multi-venue books (where they attach)

A consolidated view across venues is *not* a merged ladder: each venue's book is kept intact (so a dropped feed can be backed out and attribution survives — Ch. 69 §69.4.4), and consolidation is a small *ranking* over the venues' BBO lines — at most a dozen (price, size, venue) triples sorted branch-free with §31.4.6's sorting-network paragraph — recomputed when any venue's BBO changes. The depth-merge, venue-latency-aware "where will this order really fill" view, the NBBO rules and the fee/rebate adjustments are the multi-venue chapter's, a later item in this book's queue; what this chapter supplies it is one `FastBook` per venue and a BBO line per book that is cheap to read.

## 32.5 Verify the codegen: the add/cancel fast path

Compile the book's `add()` and `remove()` as out-of-line functions (`lob_asm.cpp`: `FastBook<4096, 1 << 20>` behind two `noinline` wrappers; `g++ -O2 -std=c++20 -march=x86-64-v3 -S -masm=intel`) and read the hot paths against the rules the design promised. GCC 13.3's `remove()`, from entry to the unlink:

```
   mov   r9, rsi                      ; id
   and   r9d, 4194303                 ; id & (SLOTS-1): the direct-address index — a mask, no hash, no modulo
   mov   edi, DWORD PTR 4[rdi+r9*4]   ; slot[]: one load
   cmp   edi, -1 ; je .L175           ; empty slot -> the no-op path
   sal   rbp, 5 ; add rbp, rbx        ; pool index * 32: the Order's address (a shift, because sizeof(Order) == 32)
   cmp   rsi, QWORD PTR 132288[rbp]   ; pool[o].id == id: the validation compare
   jne   .L175
   ...
   sub   r11d, DWORD PTR 66056[rbx+r14] ; tick - base
   cmp   r11d, 4095 ; ja .L116        ; one unsigned compare decides hot ladder vs cold store (idx_of's >= WINDOW)
   cmp   edx, r8d ; cmovbe ecx, edx   ; exec quantity min(): a cmov, not a branch
   sub   DWORD PTR [rax], edx         ; L.qty -= q
   sub   DWORD PTR 132296[rbp], edx   ; ord.qty -= q ; jne -> partial execution, return
   mov   DWORD PTR 132304[rdx+rbx], ecx   ; pool[prev].next = next  (the unlink: four indexed stores at most)
   ...
   mov   DWORD PTR 4[rbx+r9*4], -1    ; slot[] cleared ; pool slot pushed on the free list (two stores)
```

What to check, and what this listing shows: **no `call` on the hot path** — `remove()` has one `call` in 285 instructions and it is `memmove`, inside the cold store's `erase` (the cold path); `add()` has two, `recentre` and `ColdLevels::get_or_insert`, both behind `[[unlikely]]` compares. **No `div`/`imul` for the index arithmetic** — the slot index is an `and`, the pool address a `sal 5`, the ladder index a `sub` and an unsigned `cmp` against 4095 (the `WINDOW` power-of-two contract, Ch. 28 §28.4.3). **The quantity `min` is a `cmov`**, the hot/cold decision and the stale-id rejection are predictable compares (they predict "hot" and "valid" on this stream), and the only data-dependent branch that mispredicts is "did this level empty and was it the best" — rare by the access distribution. If a `call` to `operator new`, a `div`, or a `std::__throw_*` appears in these two functions, the contract is broken; Ch. 28 §28.4.4 turns that check into a link-time assertion.

## 32.6 Pitfalls & anti-patterns

- **Tick-size changes and price bands break the dense index.** The ladder assumes one tick per slot; a venue whose tick size changes with price (a decimal-pip band table, LULD bands, a halt that reopens in a different band) silently maps two prices to one slot or leaves gaps. Index by *tick number within the band* and re-centre on a band change; keep the band table in the instrument's static data (Ch. 96 reloads it), never inferred from the feed.
- **Crossed and locked books during recovery.** The §32.3 stream's halt-gap leaves old asks below new bids until they are cancelled — exactly what a snapshot-splice race produces. A crossed BBO is a *state*, not a bug: the strategy must treat it as "not trustworthy" (Appendix M) rather than as free money, and the book must not "fix" it by executing orders the venue did not execute.
- **Order-id reuse across sessions.** A direct-address table keyed on ids that restart from 1 at each session (or per matching engine) maps today's order onto yesterday's slot; on session roll, clear the table with the book. Ids that are *not* monotone within a session (some venues hash them) need the flat map — §32.3 is the cost — not a bigger slot table.
- **L2 feeds that hide queue position.** An MBP book cannot tell you how much is ahead of your order; a strategy that infers it from level quantity minus its own order is wrong the first time someone ahead cancels. If queue position matters, the data feed decision (§32.2.1) is made for you.
- **Re-centring on every out-of-window add.** The design's single most important rule (§32.4.1): only an add that improves the best may move the window. Without it a deep resting order far from the touch triggers a 26 µs copy, and a hostile or careless participant can trigger one per message.
- **Holding `Order*` across a re-centre or `Level*` across anything.** Orders are stable (pool slots), levels are not (a re-centre copies them, a cold erase shifts them). Hold ticks and pool indices; recompute `Level*` from the tick (`idx_of`) each time — it is a subtract and a compare.
- **Measuring with everything hot.** A book that is only ever probed at the touch in a microbenchmark looks like 16 ns flat; §32.3's cancels of old orders are the real p99.9 and they only exist in a stream with realistic order lifetimes. Replay a capture (Ch. 102), with the warm-up (Ch. 59) your production process will actually have.

## 32.7 Exercises & checklist

**Exercises**

1. **A real capture.** Replace `make_stream()` with a reader for a day of ITCH (Ch. 102's journal, or the public NASDAQ sample files) for one symbol; keep `apply()` unchanged. Report the §32.3 table, the re-centre count, and the fraction of events that hit the cold store. How far off was the synthetic stream?
2. **The baseline past L3.** Run the `std::map` baseline and the ladder with 1 M live orders (lower the generator's cancel rate) so neither fits the L3; confirm with `perf stat -e LLC-load-misses` that the baseline's per-event misses scale with `log2(levels)` and the ladder's do not. At what working set does the "order of magnitude" claim become true?
3. **Window sweep and the ring ladder.** Sweep `WINDOW` ∈ {512, 1024, 4096, 16384}: re-centre cost vs frequency on the §32.3 stream, and the p99.9 that results. Then implement the ring-indexed ladder of §32.4.1 (index = `tick & (WINDOW − 1)`, moving `base`, wrap-aware `find_best`) and show the re-centre cost becoming proportional to the levels moved.
4. **Pool locality.** Instrument the pool: the age (in events) of each cancelled order and the cache misses the cancel took (`perf mem` or `rdtsc` buckets by age). Then try (a) a FIFO free list, (b) allocating from per-level mini-pools, (c) a `MADV_HUGEPAGE` pool (Ch. 16); which flattens the p99.9 of §32.3?
5. **Snapshot splice.** Build a book from a 20 k-order MBO snapshot, buffer 10 k live increments that straddle the snapshot's sequence, splice (§32.4.5), and verify against a book that saw the whole stream: identical BBO, level quantities and FIFO order. Measure the rebuild time and whether any strategy-visible read saw the half-built state.
6. **MBO vs MBP from the same stream.** Derive the MBP updates the §32.3 stream implies (the harness does this once, outside the timing) and measure a strategy that needs only the top five levels on each book. Where is the 10× per-message difference spent, and what did the L2 book lose (queue position, cancel attribution)?

**Checklist — the order book**

- [ ] The feed type (MBO vs MBP vs top-of-book, §32.2.1) was chosen for what the *strategy* needs (queue position, order-flow attribution), and the book's shape follows it.
- [ ] Price levels live in a **dense tick ladder** with a **cold sorted overflow**; only an add that improves the best may **re-centre** (§32.4.1), and the re-centre's cost and frequency are measured on captured data (§32.3).
- [ ] Orders live in a **stable-slot pool** with an index-linked FIFO per level; **zero allocations** on the hot path, proven by the `operator new` counter in the replay harness.
- [ ] The **order-id map** matches the venue's id scheme — direct addressing for near-monotone ids (table sized above the in-flight span), flat hash for opaque ones — and the pool slot's stored id rejects stale ids (§32.4.3).
- [ ] The **BBO sits on its own line**, is rewritten only when the best changes, and is published through Ch. 43's seqlock (§32.4.4).
- [ ] **Snapshot rebuild** drops pre-snapshot increments, marks the book untrustworthy until spliced, and costs `n × ~16 ns` (§32.4.5).
- [ ] The hot path's asm shows no `call`, no `div`, a `cmov` for the quantity `min` (§32.5); tick-band changes, crossed books and id reuse are handled as states, not surprises (§32.6).

## 32.8 References

- NASDAQ, *TotalView-ITCH 5.0 Specification* and CME Group, *MDP 3.0 Market by Order* — the two MBO feeds whose message semantics (add/replace/delete/execute, order reference numbers, snapshot sequencing) §32.2.1 and §32.4.3 follow.
- Ch. 31 §31.4.4 (slot maps) and §31.4.6 (small-array search); Ch. 35 §35.4.1 (occupancy bitmaps) — the building blocks assembled here.
- M. D. Gould et al., *"Limit order books"* (Quantitative Finance 13(11), 2013) — the empirical properties of order flow (touch-dominated activity, order lifetimes) the §32.3 stream is shaped to.
- Ch. 69 §69.4.2 — the protocol half of snapshot recovery that §32.4.5 completes.
- The data-structures brainstorm of 2026-11-03 (D04 direct addressing, D05 bitmap trees, D07 Fenwick depth) — the structures this chapter uses two of and points at the rest of.

## 32.9 Additional Reading

- C. Cook, *"When a Microsecond Is an Eternity"* (CppCon) — the order-book and allocation discipline in production HFT code.
- D. Lemire's and S. Slotin's writings on branchless and SIMD search — the cold store's search, and where a sorted array beats a ladder.
- J. Hasbrouck, *Empirical Market Microstructure* — what queue position and order-flow attribution are worth, which is the argument for paying L3's 10×.

---

*Next: Ch. 33 — Memory Mapping. The book above lives in one process's memory; Ch. 33 is how memory is mapped, shared and locked — `mmap`, shared memory, file-backed mappings, page faulting and pre-faulting — so that the next process (Ch. 64) can read this book's BBO line without a copy.*
