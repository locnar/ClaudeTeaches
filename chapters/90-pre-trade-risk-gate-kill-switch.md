# Part XIII — Observability & Operations in Production

# Chapter 90 — The Pre-Trade Risk Gate & Kill-Switch Path

> **Prerequisites:** Ch. 89 (process topology — the Risk role and the gateway that executes the kill-switch; §89.4.4's safe-state mechanism, whose *triggers* this chapter supplies), Ch. 28 (flat arrays indexed by id — the limit records), Ch. 8–9 (hot/cold split, sizing a record to a cache line), Ch. 30 and Ch. 87 §87.4.3 (scaled integers and checked arithmetic — the gate's math), Ch. 19 §19.4.4 (strong types — `Price`, `Qty`, `Notional`, `SymbolId`), Ch. 13 (branchless evaluation), Ch. 31 §31.4.1 (bitmaps — the live-order set the kill walks), Ch. 38 and Ch. 88 (seqlock/RCU publication — how limits change under a running gate), Ch. 47 (TSC token buckets and timers), Ch. 42 (staleness budgets — one of the triggers), Appendix J.3 and K.4 (what the regulators and venues require).
>
> **Leads into:** Ch. 91 (idempotent order entry — the gateway *after* the gate; the position accumulators built here are what Ch. 91 reconciles), Ch. 93 (the crash handler's kill-switch handoff is this chapter's trigger path entered from a signal handler), Ch. 94–95 (every gate decision and every kill is a journaled, sequenced event), Ch. 99 §99.3 (the ~40 ns row, now implemented), Ch. 77 §77.4.1 (the same checks in FPGA fabric), Appendix M (the readiness state machine the kill drives to `disarmed`). Ch. 89 said *where* the risk check lives and *who* executes a kill; this chapter is *what* the check is, how it runs in ~15 ns, and how long it takes from a trigger to a flat book.

---

## 90.1 Why it matters: the one stage you may not skip, budgeted at 40 ns

Every order a firm sends to a US equity venue must pass pre-trade risk controls **before** it reaches the exchange — SEC Rule 15c3-5 (the Market Access Rule, 2010) requires "reasonably designed" systematic controls on order size, price, credit and capital exposure, and the prevention of erroneous orders; MiFID II RTS 6 (Art. 15–20) requires the same of EU algorithmic traders, plus a documented **kill functionality** that cancels all outstanding orders immediately; every venue adds its own price collars, message-rate throttles and self-match-prevention rules (Appendix K.4). The checks are not optional, cannot be post-trade, and sit on the tick-to-trade critical path: Ch. 99 §99.3 budgets them at **~40 ns p50 / ~120 ns p99.9**, Ch. 1 §1.2.3 at 50–200 ns including order encode, and the FPGA chapter (Ch. 77 §77.4.1) moves them into fabric precisely because they are simple, mandatory and hot. The book has pointed at this gate from seven chapters and two appendices and never built it.

The same regulation names the other half: the **kill-switch**. When something is wrong — a strategy process dead with four hundred working orders (Ch. 93 §93.1), a position that disagrees with the venue's drop-copy, a reject rate that says the venue is refusing us, a feed whose staleness budget is blown (Ch. 42 §42.2.3), a heartbeat lost (Ch. 89 §89.4.3), an operator's hand on the button — the system must reach a **safe state**: no new orders, every working order cancelled, positions flattened or held per policy. Ch. 89 §89.4.4 established the *mechanism* (the surviving gateway cancels on behalf of the dead) and listed the triggers in a parenthetical; this chapter builds the triggers as runtime monitors, the trigger-to-safe-state path, and measures it — because "immediately" in RTS 6 is a number, and §90.3 shows the difference between a kill that walks a 4 MiB order table (300–400 µs) and one that walks a live-order bitmap (4 µs for 400 orders).

The design constraints are the book's: the gate is a **pure function of (order, limits, position)** → accept/reject bitmask (Ch. 89 §89.2.2 — deterministic, replayable, journaled); its data is **flat, cache-line-sized and indexed by `SymbolId`/account slot** (Ch. 28, 8–9), published to the hot path with a seqlock or RCU swap (Ch. 38, 88) so limits change without a lock; its arithmetic is **checked** (Ch. 87 §87.4.3) because a notional overflow is an unbounded order; its evaluation is **branch-light** (Ch. 13) because rejects are rare and mispredicts are not free; and its throttles are **TSC token buckets** (Ch. 47) because a syscall is not allowed. §90.2 gives the check inventory and the data model; §90.3 measures the gate and the kill; §90.4 builds both.

## 90.2 Mental model

### 90.2.1 The check inventory: regulation → concrete per-order checks

```
   source                      requirement                                  concrete check at the gate                            data it reads
   ─────────────────────────   ──────────────────────────────────────────   ───────────────────────────────────────────────────   ──────────────────────────────
   15c3-5(c)(1)(i); RTS 6 §15  max order size / notional, "fat finger"      qty ≤ max_qty[sym]; px·qty ≤ max_notional[sym]       per-symbol record
   15c3-5(c)(1)(ii); venue     price collar / erroneous-price band           collar_lo[sym] ≤ px ≤ collar_hi[sym] (vs last/NBBO)   per-symbol record (collar updated from the feed)
   15c3-5(c)(1)(i); RTS 6 §15  position & open-exposure limits               |position[sym] ± qty| ≤ pos_limit[sym];              per-symbol accumulators
                                                                             open_notional[acct] + notional ≤ limit[acct]           per-account record
   15c3-5(c)(1)(i)             credit / capital thresholds                   open_notional, daily P&L drawdown ≤ limits[acct]      per-account record
   venue port rules; RTS 6 §20 message-rate throttle (orders/s per port)      token bucket[port] has a token                        per-port bucket (TSC-refilled)
   RTS 6 §15, venue            duplicate-order prevention                     same (sym, side, px, qty) within Δt → reject           per-symbol last-order fingerprint
   venue; RTS 6 §15            self-match prevention                          would cross our own resting order → reject/cancel     per-symbol own-book summary (best own bid/ask)
   firm policy; Appendix M     enable flags: symbol / strategy / session      enabled[sym] & armed[strategy] & state == trading      per-symbol flag, per-strategy flag, readiness word
   RTS 6 §12; 15c3-5(c)(1)(ii) kill functionality                             kill word == 0                                         one global word
   venue                       cancel-on-disconnect                           armed at logon (Appendix K) — a *session* property, not a per-order check
```

Three observations shape the implementation. **Everything is a compare against a per-symbol or per-account record plus a handful of accumulators** — no lookups beyond one array index each, no strings, no allocation. **The accumulators (position, open notional, P&L) are written by exactly one component** — the gateway's fill/ack stream (Ch. 91) — and read by the gate; that single-writer rule (Ch. 34, 38) is what lets the gate read them without a lock. **The collars and limits change** — a collar follows the market, a limit is raised by an operator — on a timescale of milliseconds to minutes, which is Ch. 88's hot-reload problem: publish a new record, never mutate the one the gate is reading.

### 90.2.2 Data layout for a ~40 ns gate

```
   SymLimits[sym]  (64 B, one line, read-mostly; replaced by RCU/seqlock on change — Ch. 88)        SymState[sym] (64 B, accumulators; single writer: the fill stream)
   ┌──────────────────────────────────────────────────────────────────────────────────┐          ┌──────────────────────────────────────────────────────────┐
   │ max_qty  max_notional  collar_lo  collar_hi  pos_limit  flags(enabled, …)  pad   │          │ position  open_qty_bid  open_qty_ask  own_best_bid/ask   │
   └──────────────────────────────────────────────────────────────────────────────────┘          │ last_fingerprint  last_t  …                               │
   AcctLimits[acct] (64 B): max_open_notional  daily_loss_limit  …                                └──────────────────────────────────────────────────────────┘
   AcctState[acct]  (64 B): open_notional  realized_pnl  tokens  last_refill  rate  burst
   kill (its own line): std::atomic<uint32_t>;  readiness word (Appendix M.1): its own line

   gate(order) touches: SymLimits[s] + SymState[s] + AcctLimits[a] + AcctState[a] + kill + readiness = 6 lines, 4 of them hot for the active symbols
   8 192 symbols × 128 B = 1 MiB — L2-resident on the reference machine (1.25 MiB L2); the active few hundred live in L1
```

- **Limits and state are separate lines** so a limit *change* (rare, from another core) never invalidates the line the fill stream writes at full rate, and the fill-stream writes never evict the read-mostly limits (Ch. 36's false-sharing rule applied to *update rates*, Ch. 8's hot/cold split applied to *writers*).
- **Everything the gate needs for a symbol is at `sym` in two arrays** — no map, no indirection (Ch. 28 §28.4.1); `SymbolId` is a dense integer assigned at reference-data load (Ch. 19 §19.4.4's strong type), and the universe is sized at start-up (Ch. 26).
- **Replicated per NUMA node** on a dual-socket box (Ch. 16 §16.4.2): the limits arrays are read-mostly and small; a gate on socket 1 must not take a remote miss on every order.
- **Audit fields are cold** — the gate's *decision* record (what was checked, against what values, with what result) is emitted as a journal event (Ch. 94), not kept in the hot record; RTS 6 requires the audit trail, not that it live in L1.

### 90.2.3 Checked arithmetic and branch-light evaluation

A notional is `px × qty` in scaled integers (Ch. 30): 10⁸-scaled price × 10⁹ shares overflows `int64` at a price of 92 — a fat-finger quantity is exactly the input that overflows, and signed overflow is UB (Ch. 87 §87.2.2), so the optimizer may delete the check that follows. `__builtin_mul_overflow` compiles to `imul` + `jo` (Ch. 31), and the gate treats the overflow flag as a reject of its own class (§90.4.1). For the other checks the evaluation is **a bitmask accumulated with arithmetic on booleans** — `r |= (qty > L.max_qty) * QTY` — so the common all-pass path has no data-dependent branches to mispredict (Ch. 13 §13.4.3): eight `cmp`/`setcc` pairs and some `or`s, ~15 cycles, and the one branch is `if (r) reject_path()` which predicts "not taken" essentially always. §90.3's "25 % fat-finger" row shows the branch-light gate costing the same with a quarter of the orders rejected as with none.

### 90.2.4 The kill-switch path: triggers, propagation, safe state

```
   TRIGGERS (runtime monitors, housekeeping core — Ch. 86's cadence)      PROPAGATION                        SAFE STATE (the gateway — Ch. 89 §89.4.4)
   ─────────────────────────────────────────────────────────────────      ───────────────────────────────    ───────────────────────────────────────────────
   reject-rate spike (venue refusing us)              ─┐                                                     1. kill word = 1 → the gate rejects everything (one load, already on the path)
   position ≠ drop-copy position beyond tolerance (Ch. 91) │                                                 2. readiness → disarmed (Appendix M.1): may_send() false in every role
   P&L drawdown / loss limit (AcctState)               ├─► kill word (atomic store, release)              3. walk the LIVE-ORDER SET → one cancel per working order into the TX ring
   feed staleness budget breached (Ch. 42 §42.2.3)     │    + control event into every hot loop (Ch. 41)      (or the venue's mass-cancel message, if it has one — one message)
   heartbeat lost / process dead (Ch. 89 §89.4.3, 89)  │    + journal record (Ch. 94) — the kill is an event 4. flatten or hold per policy (an IOC per net position, or nothing)
   operator command (control ring)                    ─┘                                                     5. verify: zero working orders in the in-flight table (Ch. 91); alert (Ch. 86)
   time-to-safe-state = detect + propagate + cancels on the wire + venue acks  — §90.3 measures the middle two
```

- **Triggers are monitors over telemetry the system already publishes** (Ch. 86): the reject counter's rate, the position-vs-drop-copy delta Ch. 91 computes, the P&L accumulator, Ch. 42's staleness series, Ch. 89's heartbeat sequence. They run on a housekeeping core at Ch. 86's cadence (tens of microseconds to milliseconds) — the gate itself checks only the *word*.
- **The word is the whole hot-path cost**: one relaxed load per order of a line that never changes until the day it does. On that day the store is a release and the gate's next load sees it — no IPI, no signal (Ch. 46 §46.4.3 — a signal on a hot thread is the wrong tool; Ch. 93 §93.4.3's `eventfd` handoff from a *crash* handler is the one exception, because the dying process has no loop left).
- **The cancels are the time.** Ch. 89's gateway owns the session and the in-flight table (Ch. 91); it must enumerate the working orders and emit a cancel for each — which is O(working orders) if it has a live-order *set*, and O(table capacity) if it scans the table (§90.3: 4 µs vs 300–400 µs). Venues that offer a **mass cancel** (one message cancelling all orders for a session/account/symbol — CME's Order Mass Action, Nasdaq OUCH's per-session cancel-on-disconnect trigger, FIX `OrderMassCancelRequest`) collapse the wire time to one message; use it where it exists and keep the per-order path as the fallback, because a mass-cancel *ack* is also the venue's confirmation, which the per-order path gets one at a time.

## 90.3 Measure it: the gate's cost, and the time from trigger to cancels queued

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -march=native`), run pinned on the 4-vCPU Intel Xeon VM of Ch. 41 §41.3 (TSC 2.70 GHz). The gate is pure integer work over L1/L2-resident lines and transfers to the reference machine at the cycle count (~30 cycles ≈ 11 ns at 2.9 GHz); the kill-switch walks are memory-bound and scale with the table's footprint. Labelled VM-measured.*

**1. The gate.** Eight checks over the §90.2.2 layout (size, checked notional, notional limit, collar, position after the order, account open notional with checked add, TSC token bucket, kill word), returning a reject bitmask; 10⁶ orders over 8 hot symbols (L1) and over 8 192 random symbols (512 KiB of limit records — L2):

```cpp
// riskgate.cpp (the gate; full file has the harness and the kill-switch walks). Build: g++ -O2 -std=c++20 -march=native riskgate.cpp -o riskgate
struct alignas(64) SymLimits  { i64 max_qty, max_notional, collar_lo, collar_hi, pos_limit, position; u32 enabled; u32 pad; };   // 64 B (position shown here for brevity; §90.4.1 splits it out)
struct alignas(64) AcctLimits { i64 max_notional_open, open_notional, daily_loss_limit, pnl; u64 tokens, last_refill, rate_per_s, burst; };
static_assert(sizeof(SymLimits) == 64 && sizeof(AcctLimits) == 64);
enum Reject : u32 { OK = 0, DISABLED = 1, QTY = 2, NOTIONAL_OVF = 4, NOTIONAL = 8, COLLAR = 16, POSITION = 32, ACCT = 64, RATE = 128, KILLED = 256 };
struct Gate {
    SymLimits* sym; AcctLimits* acct; alignas(64) std::atomic<u32> kill{0}; u64 tsc_per_s;
    inline u32 check(u32 s, u32 a, i64 px, i64 qty, bool buy, u64 now) {
        const SymLimits& L = sym[s]; AcctLimits& A = acct[a]; u32 r = OK;
        r |= (L.enabled == 0) * DISABLED;  r |= (qty > L.max_qty) * QTY;                                  // booleans → bits: no data-dependent branch (Ch. 13)
        i64 notional; if (__builtin_mul_overflow(px, qty, &notional)) return r | NOTIONAL_OVF;            // imul + jo (Ch. 87 §87.4.3): the fat finger that overflows
        r |= (notional > L.max_notional) * NOTIONAL;  r |= ((px < L.collar_lo) | (px > L.collar_hi)) * COLLAR;
        i64 newpos = L.position + (buy ? qty : -qty);  r |= ((newpos > L.pos_limit) | (newpos < -L.pos_limit)) * POSITION;
        i64 open; if (__builtin_add_overflow(A.open_notional, notional, &open)) return r | NOTIONAL_OVF;  r |= (open > A.max_notional_open) * ACCT;
        u64 dt = now - A.last_refill;                                                                      // token bucket on the TSC (Ch. 47): lazy refill, one predictable branch
        if (dt > tsc_per_s / 1000) { A.tokens = std::min(A.burst, A.tokens + dt * A.rate_per_s / tsc_per_s); A.last_refill = now; }
        if (A.tokens == 0) r |= RATE; else if (r == OK) --A.tokens;
        r |= kill.load(std::memory_order_relaxed) * KILLED;                                                // the kill word: one load of a line that never changes — until it does
        return r;
    }
};
```

```
                                                     mean ns/order     p50     p99    p99.9    rejected
gate, 8 hot symbols (L1-resident), rdtsc per order       34.1          15.6    20.7    32.6        0     (the rdtsc pair itself is ~12 ns of the p50)
gate, 8 192 symbols random (512 KiB, L2), rdtsc/order    36.2          15.6    23.0    34.8        0
gate, hot, tight loop (no per-order rdtsc)               11.1           —       —       —          0     ← the gate alone: ~30 cycles
gate, 25 % fat-finger rejects (branch-light mix)         11.0           —       —       —     250 000    ← rejects cost nothing extra
```

- **The gate is ~11 ns — a quarter of the 40 ns budget** (Ch. 99 §99.3), eight checks and a token bucket included, and L2-resident limit records cost ~0 extra at the median (the active symbols are in L1; the hardware prefetcher does nothing for random symbols, so the p99 rises 2 ns). There is room in the budget for the self-match and duplicate checks of §90.4.2 (two more compares each) and for the `SymState` line split of §90.4.1 (one more line, L1-hot).
- **Branch-light pays exactly as promised**: a mix with a quarter of the orders rejected costs the same 11 ns as all-pass. A version with `if (qty > max) return QTY;` chains would show the Ch. 13 mispredict cost (~15–20 cycles each) on every reject *and* a polluted predictor for the passes.
- **The p99.9 of 33–35 ns** against the 120 ns budget row is the VM's noise floor on a pinned-but-shared vCPU; on isolated cores (Ch. 51–52) the gate's tail is its L2 miss (~14 cycles) — this is the stage with the *flattest* distribution on the whole path, which is why Ch. 77 moves it to fabric last, not first.

**2. The kill-switch: trigger → flag → cancels queued.** A 65 536-slot in-flight order table (64 B per slot, 4 MiB — Ch. 91's shape) with 400 / 4 000 / 40 000 working orders at random slots; the kill sets the word and enumerates the working orders, queuing one cancel record per order into the TX ring. Two enumerations: a table walk (`if (tab[i].live)` over every slot) and a **live-order bitmap** (one bit per slot, 8 KiB, maintained on send/release — Ch. 31 §31.4.1) walked with `tzcnt`:

```
                                  400 working orders      4 000 working orders     40 000 working orders
kill via table walk (65 536 × 64 B)   271–387 µs  (4–6 ns/slot)   288–328 µs             409–469 µs       ← the cost is the TABLE, not the orders
kill via live bitmap (8 KiB)            4.4 µs  (11 ns/order)       28.8 µs (7 ns/order)   312 µs (8 ns/order) ← proportional to the work
```

- **Walking the table is a 300 µs stall regardless of how many orders are live** — 4 MiB of cold lines at ~5 ns each; with 400 orders that is 99.4 % wasted. The bitmap makes the kill proportional: **4.4 µs for 400 orders**, 7–11 ns per cancel queued. Every structure that the kill-switch must enumerate — working orders, open quotes per instrument, armed TTL timers (Ch. 47) — carries a live bitmap or an intrusive live list for exactly this reason.
- **Time-to-safe-state, assembled.** Detect (a housekeeping monitor at Ch. 86's cadence: 10–100 µs; or a crash handler's `eventfd` — Ch. 93 §93.3: ~20 µs to the gateway) + propagate (one release store; the gate's next order is rejected within one loop iteration — <1 µs) + enumerate (4.4 µs for 400) + **put the cancels on the wire** (per-order: 400 × Ch. 67's ~0.5–1 µs TX cost ≈ 200–400 µs serialized on one session; mass-cancel: one message, ~1 µs) + the venue's round trip and acks (tens to hundreds of µs, venue-dependent). The software is the smaller part when the venue offers a mass cancel, and the *dominant* part — the cancel storm — when it does not, which is why Ch. 67's pacing and multiple sessions matter for the kill as much as for quoting. RTS 6's "immediately" is, in practice, **sub-millisecond to the wire for hundreds of orders**, and the structures above are what make it so.

## 90.4 Techniques

### 90.4.1 The gate as a pure function over flat records, with limits published lock-free

```cpp
// gate.hpp — the production shape: limits (read-mostly, RCU-published) separate from state (single-writer accumulators); strong types (Ch. 19 §19.4.4).
struct alignas(64) SymLimits { Qty max_qty; Notional max_notional; Price collar_lo, collar_hi; Qty pos_limit; std::uint32_t flags; std::uint32_t version; char pad[12]; };
struct alignas(64) SymState  { Qty position, open_bid, open_ask; Price own_best_bid, own_best_ask; std::uint64_t last_fingerprint, last_t; };    // written ONLY by the fill/ack stream (Ch. 91)
struct alignas(64) AcctState { Notional open_notional, realized_pnl; std::uint64_t tokens, last_refill; };
struct GateTables {                                                                            // one instance per NUMA node (Ch. 16 §16.4.2)
    std::atomic<const SymLimits*> sym_limits;                                                  // RCU pointer: a new limits array is built off-path, validated, then swapped (Ch. 88 §88.4.1–§88.4.2)
    SymState* sym_state; const AcctLimits* acct_limits; AcctState* acct_state;
    alignas(64) std::atomic<std::uint32_t> kill{0}; alignas(64) const std::atomic<std::uint8_t>* readiness;   // Appendix M.1's word
};
struct Decision { std::uint32_t reject; Notional notional; Qty pos_after; };                   // what is journaled with the order (Ch. 94): the checked values, not just the verdict
inline Decision gate(const GateTables& g, const Order& o, std::uint64_t now) noexcept {
    const SymLimits& L = g.sym_limits.load(std::memory_order_acquire)[o.sym.value];           // one acquire load; the array is immutable once published
    const SymState& S = g.sym_state[o.sym.value]; AcctState& A = g.acct_state[o.acct]; const AcctLimits& AL = g.acct_limits[o.acct];
    std::uint32_t r = 0;
    r |= ((L.flags & ENABLED) == 0) * DISABLED;  r |= (o.qty > L.max_qty) * QTY;
    Notional n; if (checked_mul(o.px, o.qty, n)) return {NOTIONAL_OVF, {}, {}};               // Price × Qty → Notional with overflow detection (Ch. 19 §19.4.4 operator*)
    r |= (n > L.max_notional) * NOTIONAL;  r |= ((o.px < L.collar_lo) | (o.px > L.collar_hi)) * COLLAR;
    Qty after = S.position + (o.side == Side::Buy ? S.open_bid + o.qty : -(S.open_ask + o.qty));   // worst-case exposure: position + open same-side quantity (RTS 6 §15's "outstanding")
    r |= ((after > L.pos_limit) | (after < -L.pos_limit)) * POSITION;
    Notional open; if (checked_add(A.open_notional, n, open)) return {NOTIONAL_OVF, n, after};  r |= (open > AL.max_open_notional) * ACCT;
    r |= (A.realized_pnl < -AL.daily_loss_limit) * LOSS;
    r |= (o.side == Side::Buy ? (o.px >= S.own_best_ask) : (o.px <= S.own_best_bid)) * SELF_MATCH;                  // would cross our own resting order (§90.4.2)
    r |= (o.fingerprint == S.last_fingerprint && now - S.last_t < DUP_WINDOW) * DUPLICATE;                           // same (sym, side, px, qty) within Δt
    r |= (!take_token(A, now)) * RATE;                                                         // §90.4.3
    r |= (g.readiness->load(std::memory_order_relaxed) != TRADING) * NOT_ARMED;                // Appendix M.1
    r |= g.kill.load(std::memory_order_relaxed) * KILLED;
    return {r, n, after};
}
```

- **Pure, and journaled with its inputs.** `gate()` reads tables and the order and returns a `Decision`; the caller journals `{order, decision}` (Ch. 94) so the audit trail RTS 6 Art. 17 demands is the same record that makes the decision replayable (Ch. 89 §89.2.2); a reject's *reason bits* and the checked values go with it.
- **Limits are RCU-published** (Ch. 88 §88.4.1–§88.4.2): an operator's change or a collar update builds a new `SymLimits[]` (or a copy-on-write page of it) off the hot path, validates it (§90.4.4), stores the pointer with release; the gate's acquire load sees either the old array or the new one, never a torn record; the old array is reclaimed after a grace period (Ch. 39). For a *per-symbol* collar that moves with every tick, a per-record seqlock (Ch. 38) on `SymLimits[s]` is the alternative — one version check per gate call instead of a pointer load.
- **State has one writer.** `SymState` and `AcctState` are written by the gateway's ack/fill handler (Ch. 91 §91.4.3) on the gateway's core; the gate on the same core (the typical in-line placement — Ch. 89 §89.2.1's "mandatory in-line check") reads them with plain loads; a gate in a *separate* risk process reads them across a shm ring's publication (Ch. 57) with the seqlock. Never two writers, never a lock.
- **Worst-case exposure, not just position.** RTS 6 §15 and 15c3-5 both count *outstanding* orders: the check is position plus open same-side quantity plus this order, so a strategy cannot blow the limit by resting ten orders that each pass alone.

### 90.4.2 Self-match prevention and duplicate detection without a book lookup

The gate must not walk the order book (Ch. 28) — that is tens of nanoseconds of pointer chasing per order. Two summaries, maintained by the single writer:

- **Own best bid / own best ask per symbol** (`SymState.own_best_*`): updated on ack (new resting order improves our best), fill and cancel (recompute from the per-symbol live-order list — Ch. 91's table is indexed by symbol for this), so the self-match check is two compares. Venues' own SMP (Nasdaq's *Self Match Prevention*, CME's *SMP ID*) is the backstop — it cancels or rejects at the matching engine — but a venue-side cancel is a round trip and a message you pay for; the gate's compare is free.
- **Order fingerprint**: `hash(sym, side, px, qty)` stamped on the order by the strategy (Ch. 19 §19.4.4's `OrderFingerprint` type) and compared with the last one for the symbol inside a window (a few ms); a match is the duplicate-order class of erroneous order (a retry loop, a replayed command). The venue's **duplicate ClOrdID rejection** (Ch. 91 §91.4.1) is the second line; it catches resends, not logically duplicated new orders.

### 90.4.3 Throttles: TSC token buckets per venue port

```cpp
// a token bucket refilled from the TSC: no clock syscall, one predictable branch per check (Ch. 47 §47.4.2: throttles are not timers)
struct Bucket { std::uint64_t tokens, last_refill; std::uint64_t rate_per_s, burst; };
inline bool take_token(Bucket& b, std::uint64_t now_tsc) {
    std::uint64_t dt = now_tsc - b.last_refill;
    if (dt > TSC_PER_MS) [[unlikely]] { b.tokens = std::min(b.burst, b.tokens + mul_shift(dt, b.rate_per_tsc_q32)); b.last_refill = now_tsc; }   // refill at most every ms; fixed-point rate (Ch. 30), no division
    if (b.tokens == 0) return false; --b.tokens; return true;
}
```

- **One bucket per venue port/session**, sized to the venue's published limit minus headroom (Appendix K.4), plus a firm-level bucket; rate as a Q32 fixed-point *tokens per TSC tick* so the refill is a multiply and a shift.
- **A rejected-for-rate order is not an error** — it is the strategy's signal to pace (Ch. 67's scheduled transmission can spread the burst) — but a *sustained* rate rejection is a trigger candidate (§90.4.5): a strategy in a tight loop re-sending is exactly the erroneous-order pattern the rule exists for.
- **Throttle the cancels too, deliberately not**: cancels are usually exempt from venue order-rate limits or have their own, more generous ones; the kill-switch path (§90.4.6) bypasses the bucket and pays the venue's cancel-rate limit, which the mass-cancel message sidesteps.

### 90.4.4 Changing limits under a running gate: validate, publish, verify

- **Validation before swap** (Ch. 88 §88.4.3): a new limits set is checked off-path for monotonic sanity (collar_lo < collar_hi, max_qty ≤ pos_limit…) and against the *current* state (no position already outside the new limit without an explicit flatten plan) before it is published; a bad limit set is the fat finger one level up.
- **Four-eyes and a journal**: every limit change is a signed control event (Ch. 88, 94) with before/after values — RTS 6 Art. 16 requires it, and it is the first thing post-mortem (Ch. 93 §93.4.5) asks for.
- **Verify on the hot path, cheaply**: `SymLimits.version` is journaled with each decision, so a replay can prove which limits each order was checked against.

### 90.4.5 Kill-switch triggers as runtime monitors

Each trigger is a monitor over published telemetry (Ch. 86), evaluated on a housekeeping core, writing one word:

- **Reject-rate spike**: venue rejects per second per session over a short window exceeding a threshold (and *any* `NOTIONAL_OVF` or `COLLAR` reject from our own gate above a trickle — our strategies should not be producing those).
- **Position drift vs drop-copy**: |our position − drop-copy position| > tolerance for longer than the reconciliation lag (Ch. 91 §91.4.5 computes the delta); a drift that persists means we do not know what we own.
- **P&L drawdown** against `daily_loss_limit` (the gate rejects new risk at the limit; the kill flattens at a second, larger one).
- **Feed staleness** (Ch. 42 §42.2.3): the *ack* channel's or position view's age over budget is a kill; the book's age over budget is Ch. 42's `Pulled` mode, a softer trigger.
- **Heartbeat / liveness** (Ch. 89 §89.4.3): a dead strategy → kill *that strategy's* orders (scoped kill: the live bitmap is per strategy as well as global); a dead gateway → the venue's cancel-on-disconnect is the safety net and the standby's logon is the recovery (Ch. 89 §89.4.4).
- **Operator**: a control-ring command, authenticated (Ch. 87), journaled; the physical button is a process that sends it.
- **Each trigger has a scope** (symbol, strategy, account, session, everything) and the kill word is a small bitset of scopes the gate tests with one `and`; a per-strategy flag array sits next to it for scoped kills.

### 90.4.6 The safe-state sequence, measured and rehearsed

```cpp
// kill.cpp — trigger → safe state in the gateway (Ch. 89 §89.4.4); the enumeration is the live-order bitmap (Ch. 31 §31.4.1), never a table walk.
void Gateway::kill(Scope scope, Reason why, std::uint64_t now) {
    tables.kill.fetch_or(scope.bits, std::memory_order_release);        // 1. the gate rejects from the next order; every hot loop sees the control event (Ch. 41 §41.4.2)
    readiness.store(DISARMED, std::memory_order_release);               // 2. Appendix M.1: may_send() false in every role
    journal.emit(KillEvent{scope, why, now});                           //    the kill is a sequenced, replayable event (Ch. 94, 95)
    if (session.supports_mass_cancel()) tx.push(MassCancel{session.id, scope});   // 3a. one message where the venue offers it
    for (std::uint32_t w = 0; w < LIVE_WORDS; ++w)                      // 3b. and/or per-order cancels: proportional to working orders (§90.3: 4.4 µs for 400)
        for (std::uint64_t m = live[w] & scope.mask_for(w); m; m &= m - 1) {
            std::uint32_t slot = w * 64 + __builtin_ctzll(m);
            tx.push(Cancel{inflight.id_of(slot)}); inflight.mark(slot, OState::CancelSent, now);   // Ch. 91's table records the transition
        }
    if (policy.flatten) for (auto s : positions.nonzero()) tx.push(Ioc{s, -positions[s]});          // 4. optional flatten: one IOC per net position
    stats.kill_initiated(now);                                          // 5. verification runs off-path: zero working orders within T, else escalate (Ch. 86 alert)
}
```

- **Order of operations is safety-first**: stop new risk (1, 2) before cancelling old (3) before adding flattening orders (4) — a flatten before the word is set can be followed by a strategy's new order.
- **Scoped kills** (a dead strategy, a halted symbol) use the same path with a mask; the global kill is the mask of everything.
- **Rehearse at T-5 every day** (Appendix M.4's kill-switch dry run): on a test symbol or in a venue's test session, trigger → verify → re-arm, with the measured time-to-zero-working-orders recorded; a kill-switch that has never been fired is a hypothesis.
- **Verification closes the loop**: the in-flight table (Ch. 91) must reach zero working orders within a budget (venue RTT + cancel storm); if it does not — rejects on the cancels, a dead session — the escalation is cancel-on-disconnect (drop the session deliberately — Appendix K) and a human.

## 90.5 Pitfalls & anti-patterns

- **The gate as a map lookup.** `limits.find(symbol_string)` is a hash, a compare and a miss per order (Ch. 28 §28.3) — ten times the gate's budget before the first check. Dense `SymbolId`, flat arrays (§90.2.2).
- **Position-only exposure.** Checking `position + qty` without open same-side quantity lets ten resting orders each pass and together breach the limit; RTS 6 counts outstanding orders (§90.4.1).
- **Unchecked notional.** `px * qty` overflowing `int64` on the fat-finger quantity is UB (Ch. 87 §87.2.2) and the check after it may be deleted; `__builtin_mul_overflow` (§90.2.3).
- **A branch per check.** Early-return chains mispredict on every reject and pollute the predictor (Ch. 13); accumulate a bitmask and branch once (§90.3's 11.0 vs 11.1 ns is the proof it is free).
- **Mutating limits in place.** A collar updated by another core while the gate reads it is a torn record and a wrong decision with no trace; RCU-publish or seqlock (§90.4.1), and journal the version.
- **Two writers on the accumulators.** A strategy "pre-booking" position while the gateway books fills makes the position a race; the fill stream is the only writer (Ch. 91 §91.4.3).
- **The kill that walks the table.** 300–400 µs regardless of live orders (§90.3); a live bitmap or intrusive live list makes it proportional.
- **Cancels through the order throttle.** A kill paced by the order-rate bucket is a kill that takes seconds; cancels bypass it (§90.4.3) and the mass-cancel message bypasses the wire cost.
- **Flatten before stop.** Sending IOCs before the kill word is set races a strategy's next order; stop, cancel, then flatten (§90.4.6).
- **A kill delivered by signal.** `SIGUSR1` to the hot thread is a 2.5 KB sigframe and a mis-routed interrupt (Ch. 46 §46.4.3); the word plus a control-ring event is the path — the crash handler's `eventfd` (Ch. 93) is the sole exception, for a process with no loop left.
- **Gate without journal.** A verdict without the values it was checked against is unauditable (RTS 6 Art. 17) and unreplayable (Ch. 89 §89.4.6); `Decision` carries the checked notional, exposure and limits version (§90.4.1).
- **Never rehearsed.** A kill-switch exercised only in incidents has unknown timing and unknown failure modes; Appendix M.4's daily dry run on a test instrument is the minimum.

## 90.6 Exercises & checklist

1. Reproduce §90.3's gate rows on an isolated core; add the self-match and duplicate checks of §90.4.2 and the `SymState` split of §90.4.1; confirm the gate stays under 15 ns and show its asm (Ch. 4) — count the `setcc`s and the single conditional branch.
2. Replace the bitmask accumulation with early returns and measure the 25 %-reject mix; then measure a 1 %-reject mix. Explain both with `perf stat -e branch-misses`.
3. Implement RCU-published limits with a grace period (Ch. 39) and hot-reload a collar 1 000 times per second while the gate runs; verify with the journaled `version` that every decision used a consistent record.
4. Build the live-order bitmap into Ch. 91's in-flight table and measure the kill for 400/4 000/40 000 orders; then add a venue mass-cancel path in the Ch. 99 simulation harness and compare time-to-zero-working-orders including the simulated venue RTT.
5. Write the five monitors of §90.4.5 against the Ch. 86 telemetry segment and drive a replayed open (Ch. 94) through a reject storm, a position drift and a dead strategy; record each trigger's detection latency.
6. Map your venue's rulebook (collars, throttles, SMP, mass cancel) onto the §90.2.1 table; note every check the venue performs that the gate does not, and vice versa.

**Checklist**

- [ ] Every 15c3-5 / RTS 6 / venue check in §90.2.1 is implemented as a compare against a flat per-symbol or per-account record; the gate touches ≤ 6 cache lines and runs in ≤ 15 ns on isolated cores.
- [ ] Notional and exposure arithmetic is checked (`__builtin_*_overflow`); overflow is a reject class.
- [ ] Evaluation accumulates a bitmask; one branch on the verdict; a reject-heavy mix costs the same as all-pass.
- [ ] Limits are RCU- or seqlock-published and versioned; accumulators have one writer (the fill stream); the gate takes no lock.
- [ ] Exposure counts position + open same-side quantity + this order.
- [ ] Throttles are TSC token buckets per port and per firm; cancels bypass them.
- [ ] Every decision is journaled with its checked values and limits version.
- [ ] Triggers are monitors over published telemetry with scopes; the kill is one word plus a control event, never a signal to a hot thread.
- [ ] The kill enumerates working orders via a live bitmap or list (never a table walk), uses the venue's mass cancel where available, stops before cancelling before flattening, and verifies zero working orders within a budget.
- [ ] The kill-switch is rehearsed daily (Appendix M.4) with the time-to-safe-state recorded.

## 90.7 References

- U.S. Securities and Exchange Commission. *Rule 15c3-5 — Risk Management Controls for Brokers or Dealers with Market Access*, 17 CFR 240.15c3-5 (2010), and the 2014 *Responses to Frequently Asked Questions Concerning Risk Management Controls for Brokers or Dealers with Market Access*.
- European Commission. *Commission Delegated Regulation (EU) 2017/589 (MiFID II RTS 6)*, Arts. 12 (kill functionality), 15–20 (pre-trade controls, throttles, surveillance).
- CME Group, *iLink 3 — Order Mass Action Request*; Nasdaq, *OUCH 5.0 Specification* and *Self Match Prevention*; FIX Trading Community, *FIX 5.0 SP2 — OrderMassCancelRequest (q)* — venue-side mass cancel and SMP.
- McKenney, P. E. *Is Parallel Programming Hard, And, If So, What Can You Do About It?*, Ch. 9 (RCU) — the publication mechanism for limits.
- GCC manual, §"Built-in Functions to Perform Arithmetic with Overflow Checking"; Intel SDM Vol. 1 §3.4.3 (flags) — the cost of a checked multiply.
- Ch. 99 §99.3 and Ch. 1 §1.2.3 of this book — the budget rows this chapter implements.

## 90.8 Additional Reading

- FIA. *Best Practices for Automated Trading Risk Controls and System Safeguards* (2015/2024 editions) — the industry's own check inventory, with the kill-switch section.
- ESMA, *Guidelines on systems and controls in an automated trading environment* (2012) — the pre-RTS 6 reasoning.
- Nanex / SEC–CFTC *Findings Regarding the Market Events of May 6, 2010* and the Knight Capital SEC order (2013) — what the absence of these controls costs.
- Chronicle Software, "Risk Checks in Microseconds" (blog) and the Aeron Cluster "pre-trade risk" sample — comparable software gates.
- Agner Fog, *Optimizing software in C++*, §"Bool operations" — why `r |= (a > b) * BIT` compiles the way it does.

---

*Next: Ch. 91 — Idempotent Order Entry & Order-State Recovery: the gateway on the far side of the gate — ClOrdIDs as slot + generation integers, the preallocated in-flight order table with O(1) ack correlation and stale/duplicate rejection, the unknown-state matrix after a crash, session resynchronization, and reconciliation against drop-copy and venue order status within a time budget.*
