# Part XIII — Observability & Operations in Production

# Chapter 91 — Idempotent Order Entry & Order-State Recovery

> **Prerequisites:** Ch. 90 (the gate that precedes the gateway on the order path; the position accumulators it reads are written here), Ch. 89 §89.2.1 and §89.4.4 (the gateway role — owns the venue session, sequence numbers and working-order state; executes the kill), Ch. 28 §28.4.4 (slot maps and generational handles — the in-flight table *is* one), Ch. 27 (pools — the table is preallocated), Ch. 31 §31.4.4 (packing a 64-bit id), Ch. 47 §47.4.2 (cancel-if-no-ack and unacked-order ageing — the timers this chapter arms), Ch. 94 (the journal that makes id allocation crash-safe), Ch. 19 §19.4.4–§19.4.5 (`ClOrdId`, `Price`, `Qty` strong types; type-state for the session), Appendix J.3 (the order lifecycle) and J.5 (OUCH/FIX field names).
>
> **Leads into:** Ch. 93 (what the crash handler must preserve for this chapter's recovery — the journal and the table), Ch. 94 §94.4.4 (gap-free persistence of the send/ack records), Ch. 96 (the sequencer: acks and fills as sequenced inputs, replicas that rebuild the same table), Ch. 102 (the capstone's recovery path), Appendix M.4 (T-5: "session logon and sequence reconciliation, positions" — the gates this chapter implements). Ch. 90 decided whether an order may leave; this chapter is everything that happens to it afterwards — how it is named so that nothing can be confused with it, how its state is tracked in O(1) under a flood of acks and fills, and how, after a crash, the firm learns what it actually owns.

---

## 91.1 Why it matters: a duplicated or orphaned order is a real position

The market-data side loses a packet and gets a gap it can repair from the other line (Ch. 61). The order-entry side loses a packet — or a process, or a session — and gets **an order in an unknown state**: it may have reached the venue or not, been acknowledged or not, filled or not, and the strategy that sent it is now deciding what to do next on the basis of a position it does not know. Resend, and the order may be at the venue twice — a doubled position; do not resend, and the hedge that order *was* may never exist — an unhedged position. Both are money, and the second kind is the one that killed Knight Capital in 45 minutes: an order path that re-sent on every tick what it thought had not gone out. Nothing in the 94 chapters so far develops order identity, in-flight state or post-crash reconciliation; Ch. 89 §89.2.1 assigns "working-order state" to the gateway in one sentence, Appendix J.3 gives the lifecycle as prose, and the glossary's *drop-copy* entry (Appendix F) points at a chapter that never mentions it.

The engineering is small and specific. **Identity**: a client order id (FIX tag 11 `ClOrdID`, OUCH's order token) is a *slot index plus a generation* packed into one 64-bit integer (Ch. 28 §28.4.4, Ch. 31) — never a string, never a counter alone — so that a response for a *dead* order (a late ack after the slot was reused) is rejected by a single compare, and the venue's own duplicate-ClOrdID rejection is the backstop rather than the mechanism. **State**: a fixed-capacity **in-flight order table** indexed by that slot (a slot map of Appendix J.3's state machine as data), preallocated (Ch. 27), with O(1) ack/fill/reject correlation — §91.3 measures 21 ns per response including a timestamp against 85 ns for the `unordered_map<string>` design — and every transition journaled *before* the send (Ch. 94: journal-before-send is what makes the table rebuildable). **Recovery**: after a kill, the **unknown-state matrix** (§91.2.3) says, per order state and order type, whether to resend, cancel, query or wait; the session protocol's resynchronization (FIX `ResendRequest`/`SequenceReset`, SoupBinTCP's sequence number on login) recovers the responses that were in flight; reconciliation against the **drop-copy** feed and the venue's **order-status** request turns *unknown* into *known* within a measured budget — §91.3 rebuilds a 20 000-order working set from a 2 M-record journal in 17 ms and shows which orders remain unknown until the venue answers. Position accumulators and limits stay in Ch. 90; this chapter owns post-send state.

## 91.2 Mental model

### 91.2.1 Order identity: slot + generation, never a string

```
   ClOrdID (64 bits)  =  [ generation : 32 ][ slot : 32 ]        e.g. 0x0000_0007_0000_2A3F  → slot 10815, 7th use of that slot
   ─────────────────────────────────────────────────────
   slot        → index into the in-flight table (O(1), no hash)
   generation  → bumped every time the slot is RELEASED; a response carrying an old generation is for a dead order → rejected in one compare
   on the wire → FIX tag 11 as 16 hex digits (or decimal), OUCH token as 14 ASCII chars: encode at the gateway's edge (Ch. 60 §60.4), decode with std::from_chars; the hot path never sees the string
   uniqueness  → per session per day by construction (the table is per session; the generation never repeats within a run; the day/session prefix is implicit in the session)
   crash-safe  → the LAST allocated generation per slot is in the journal (Ch. 94); a restart resumes from max(gen)+1 per slot, so a pre-crash order's id can never be reissued
```

Why not a counter? A monotonic counter is unique but says nothing about *which* order it is without a lookup; a slot says where the order's state lives. Why the generation? Because the slot is reused — ten thousand orders a second through a 65 536-slot table reuses every slot every few seconds — and the venue, the drop-copy and a slow session resend can all deliver a response for the *previous* occupant: without the generation it is correlated to the wrong order (a fill booked against a new order; a cancel ack that marks a live order dead). The generation makes a stale response **detectable by one compare** (Ch. 28 §28.4.4's stale-handle check), and the detection is a *metric* (stale responses per second — a non-zero rate during normal operation means the session is replaying something it should not).

### 91.2.2 The in-flight table and the per-order state machine

```
   InFlight[slot] (64 B, one line): gen  state  side  venue  sym  px  qty  leaves  t_sent  t_acked  t_last  strategy  timer_id(Ch. 47)  next_free / next_live
                                     ────  ─────  ─────────────────────────────────────────────────────────────────────  ──────────────  ───────────────────────────
                                     identity  Appendix J.3 as data: Sent → Acked → PartFilled → Filled | Cancelled | Rejected | Replaced ; CancelSent ; Unknown
   indexes over the table (maintained by the single writer — the gateway's ack/fill handler):
     live bitmap (Ch. 31 §31.4.1)      — the kill-switch's enumeration (Ch. 90 §90.3: 4 µs for 400 orders, not 300 µs)
     per-symbol intrusive live list    — own best bid/ask for self-match (Ch. 90 §90.4.2); per-symbol cancel on a halt
     per-strategy live list            — scoped kill of a dead strategy (Ch. 89 §89.4.4)
     unacked ageing: timer per Sent order (Ch. 47 §47.4.2) — cancel-if-no-ack fires → state Unknown, emit cancel + order-status request
   every transition is a journal record {ClOrdID, from, to, venue fields, t} written BEFORE the action that causes it (journal-before-send — Ch. 94 §94.4.4)
```

The table is a slot map (Ch. 28 §28.4.4) whose values are *stable* (no erase-by-swap — intrusive links point at slots) and whose handle *is* the ClOrdID. Correlation of any venue message is: decode the id (two shifts), index the slot, compare the generation and the state's liveness — then apply the transition. There is no search, no hash, no allocation and no lock: the gateway thread is the single writer of the table and of the Ch. 90 accumulators it updates on fills; readers elsewhere (risk, telemetry, the kill monitor) see seqlock-published snapshots (Ch. 38) or the journal.

### 91.2.3 The unknown-state matrix after a crash

A gateway (or the whole host) dies and comes back. For each order the rebuilt table says what it *last knew*; the venue knows what *happened*. The gap between them is the matrix:

```
   last known state     what may be true at the venue             resend?                                   action on recovery
   ──────────────────   ───────────────────────────────────────   ───────────────────────────────────────   ──────────────────────────────────────────────────────────
   journaled, not sent  never left the host                        safe to send (it is a NEW order, same id)  policy: usually DROP — the strategy's intent is stale
   Sent, not acked      lost before the venue | at the venue       NO — a resend may be a DUPLICATE position  ask: order-status / wait for session resync to deliver the ack;
                        working | already filled                                                            if unresolved within budget → cancel by ClOrdID (a cancel of an unknown order is harmless)
   Acked (working)      working | filled since | cancelled since   n/a                                       expect fills/cancels in the resync stream; reconcile with drop-copy
   PartFilled           more fills | done | cancelled              n/a                                       as Acked; leaves from the venue's order status wins over ours
   CancelSent           cancelled | cancel rejected (filled first) NO                                        order-status resolves; a "too late to cancel" is a fill we must book
   Replaced (pending)   old working | new working | both acked     NO                                        the hardest row: two ids live for one order until the venue says which
   Filled/Cancelled/    terminal                                   n/a                                       nothing; the position they produced is in the accumulators and the journal
   Rejected
   IOC / FOK orders     filled, partially filled, or expired       NEVER resend — an IOC's "unknown" is       order-status only; an IOC that was lost is simply gone, which is what IOC means
                        (never "working")                          either a fill to book or nothing
```

The rule that falls out: **never resend an order whose send was journaled** — a journaled send *may* have reached the venue; the only safe actions on an unknown order are *ask* (order status), *wait* (the session resync will deliver what we missed) and *cancel* (idempotent: cancelling a filled or non-existent order is a benign reject). Resending is for orders the journal proves never left (and even then, usually not — the intent is stale). The venue's **duplicate-ClOrdID rejection** (every venue rejects a new order whose ClOrdID it has already seen for the session/day) is the backstop that turns an erroneous resend into a reject instead of a position — it is why the id must be stable across the crash (§91.2.1's generation-from-journal), and it is *not* a substitute for the matrix, because a reject still costs a round trip and a message during the worst moment of the day.

### 91.2.4 Session resynchronization and the two sources of truth

- **FIX** (Appendix J.5): both sides keep `MsgSeqNum`s; on reconnect, a `Logon` with our next expected number lets the venue detect what we missed and we send `ResendRequest(begin, end)` for the gap; the venue replays the application messages (acks, fills — with `PossDupFlag=Y`) or `SequenceReset-GapFill`s the admin ones. Our resend of *our* lost messages is governed by the matrix: we `GapFill` over new orders we choose not to resend.
- **SoupBinTCP / OUCH** (Nasdaq and relatives): the session login names the sequence number we have, the server replays every sequenced message after it — the acks and executions for orders we sent before the crash arrive again, with their tokens; our table correlates them (stale-generation check!) and reaches known state without a query.
- **Drop-copy**: a separate session (often a separate venue system) that streams every execution and order event for the account in near real time — the independent record. It is what the position-drift kill trigger (Ch. 90 §90.4.5) compares against, and what recovery reconciles to when the order-entry session's resync is incomplete.
- **Order-status request** (FIX `OrderStatusRequest (H)`, OUCH has none — use drop-copy): for a specific unknown ClOrdID, the venue states its current state and cumulative quantity; the answer is authoritative and the request is cheap but rate-limited (Appendix K.4).

Time-to-known-state is therefore **journal replay (local, microseconds to milliseconds) + session resync (one RTT plus the replay stream) + per-order queries for the residue + drop-copy catch-up** — §91.3 measures the first and sizes the residue; the rest is venue-dependent and is what Appendix M.4's T-5 gate waits for before `reconciled`.

## 91.3 Measure it: correlation cost, and time-to-known-state after a kill

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -march=native`), run pinned on the 4-vCPU Intel Xeon VM of Ch. 41 §41.3 (TSC 2.70 GHz). Everything here is single-threaded memory-and-integer work and transfers to the reference machine within the memory-latency ratio. Labelled VM-measured.*

**1. Correlating a venue response.** A 65 536-slot table (64 B per order, 4 MiB — L3-resident on the VM, L2+L3 on the reference machine), 20 000 working orders, 10⁶ responses drawn at random over them with 2 % carrying a stale generation; each hit records the ack time (an `rdtsc`, ~10 ns of the figure). Against the same workload on `std::unordered_map<uint64_t, Order*>` and on the FIX-as-string design, `std::unordered_map<std::string, Order*>` keyed by `"ORD" + id`:

```cpp
// ordertab.cpp (the table; full file has the harness, the map baselines and the journal rebuild). Build: g++ -O2 -std=c++20 -march=native ordertab.cpp -o ordertab
struct ClOrdID { u64 v; static ClOrdID make(u32 slot, u32 gen) { return {u64(gen) << 32 | slot}; } u32 slot() const { return u32(v); } u32 gen() const { return u32(v >> 32); } };
struct alignas(64) Order { u32 gen; OState st; std::uint8_t side; std::uint16_t venue; u32 sym; std::int64_t px; std::int32_t qty, leaves; u64 t_sent, t_acked, t_last; u32 next_free; u32 strat; };
template <u32 CAP> struct InFlight {
    Order o[CAP]; u32 free_head = 0; u32 live = 0;
    ClOrdID send(u32 sym, std::int64_t px, std::int32_t qty, u64 now) {                       // allocate a slot: O(1) pop of the free list
        u32 s = free_head; Order& x = o[s]; free_head = x.next_free; x.st = OState::Sent; x.sym = sym; x.px = px; x.qty = x.leaves = qty; x.t_sent = x.t_last = now; ++live;
        return ClOrdID::make(s, x.gen); }
    Order* correlate(ClOrdID id) {                                                               // the whole correlation: index, ONE compare; stale or duplicate → nullptr
        Order& x = o[id.slot() & (CAP - 1)]; return (x.gen == id.gen() && x.st != OState::Free) ? &x : nullptr; }
    void release(u32 s) { Order& x = o[s]; x.st = OState::Free; ++x.gen; if (x.gen == 0) x.gen = 1; x.next_free = free_head; free_head = s; --live; }   // gen bump: the old id is dead forever
};
```

```
                                                ns per response (correlate + state update)    stale/duplicate responses rejected
slot + generation table (65 536 × 64 B)                        21.4   (≈ 10 ns of it is the rdtsc stamp)        20 000 of 10⁶, by one compare
std::unordered_map<uint64_t, Order*>                           33.6                                              (not detected — a map has no generation)
std::unordered_map<std::string, Order*>  ("ORD…" ClOrdIDs)     84.7                                              (not detected)
send → correlate → release (full lifecycle, hot slot)            6.7   per order
```

- **The table is a load and a compare**: ~11 ns net of the timestamp, essentially the L2/L3 miss on a random 4 MiB table (the hot working set of a few hundred active orders is in L1 and correlates in ~2 ns). The `uint64` map is 1.5× slower and the string map 4× — hashing 20 bytes, a probe, a `memcmp` — and neither can *reject* a stale response: the map lookup for a reused id finds the *new* order and books the fill against it. The generation is not an optimization; it is the correctness mechanism, and it happens to be free.
- **A full lifecycle is 6.7 ns**: allocate, correlate once, release — the table's own cost is noise against the venue round trip; everything expensive is memory latency on a cold slot, which the per-symbol live list keeps warm for the active symbols.

**2. Time-to-known-state after a kill.** A 2 M-record journal of send / ack / fill / cancel records (the gateway's own, journal-before-send) is replayed into a fresh table: each `send` re-creates the slot with its *journaled* generation; acks, fills and cancels are correlated as in production. The rebuilt table is then classified:

```
rebuild from journal: 2 000 000 records in 16.8 ms (8.4 ns/record) → 20 000 working orders, of which 13 307 are Sent-but-unacked = UNKNOWN until the session resync / drop-copy / order-status answers
```

- **The local half of recovery is milliseconds**: 8.4 ns per record is the journal's sequential read plus the table's random write; a day's 50 M order events replay in under half a second, and a *snapshot* of the table every few seconds (Ch. 94 §94.4.4) bounds the replay to the records since. This is well inside Appendix M.4's T-5 budget; Ch. 89 §89.3's restart measurement adds the process start and warm-up around it.
- **The residue is what the venue must resolve**: here 13 307 orders were sent and never acknowledged *as far as the journal knows* — in production that count is the orders sent in the last RTT before the crash (tens, not thousands; this workload exaggerates it by never acking two-thirds of its sends), and *each one* is a §91.2.3 row: wait for the resync stream, query, or cancel. The time-to-known-state is therefore **replay (ms) + one session RTT for the replayed responses (hundreds of µs to ms) + max(drop-copy lag, order-status RTT) for the residue** — and the position is not trusted, and `reconciled` is not entered (Appendix M.1), until the residue is zero.

## 91.4 Techniques

### 91.4.1 Allocating ids and sending: journal-before-send

```cpp
// send.cpp — the gateway's send path: allocate (slot, gen), JOURNAL, then arm the guard and send. Order matters.
ClOrdID Gateway::send(const NewOrder& n, std::uint64_t now) {
    Decision d = gate(tables, n, now);                                  // Ch. 90: pure, journaled with the order below
    if (d.reject) { journal.emit(RejectedLocally{n, d}); return ClOrdID{}; }
    ClOrdID id = inflight.send(n.sym, n.px, n.qty, now);                // slot + generation (Ch. 28 §28.4.4)
    journal.emit(SendRecord{id, n, d, now});                           // 1. journal FIRST: a crash after this line has the id and the intent on disk/in shm (Ch. 94)
    inflight.at(id).timer = timers.arm(now_tick + ACK_GUARD, TimerKind::AckGuard, id.slot());   // 2. cancel-if-no-ack (Ch. 47 §47.4.2)
    live.set(id.slot()); per_symbol[n.sym].push_front(id.slot()); per_strategy[n.strategy].push_front(id.slot());   // 3. the indexes the kill and SMP use (Ch. 90)
    tx.push(encode_new_order(session, id, n));                          // 4. onto the wire: FIX tag 11 / OUCH token = hex(id.v), formatted at the edge (Ch. 60 §60.4)
    return id;
}
```

- **Journal before send, not after**: the record that proves "this id may be at the venue" must exist before the bytes leave; a crash between journal and send yields an *unknown* that resolves to "never sent" via order status — safe. A crash between send and journal yields an order at the venue that the rebuilt table has never heard of — the orphan that a kill cannot cancel and a reconciliation can only discover from drop-copy. The journal write is a store into the Ch. 94 ring (tens of ns), not a disk write.
- **Generation from the journal on restart**: the rebuild sets each slot's `gen` to the highest journaled generation for that slot, so no pre-crash id is ever reissued — which is also what makes the venue's duplicate-ClOrdID rejection a backstop rather than a trap.
- **The id's wire form is computed once**, at the edge, as fixed-width hex (`std::to_chars`, 16 chars) — the decode on the way back is `std::from_chars` into the 64-bit id (Ch. 60 §60.4.1), then two shifts; no strings survive past the codec.

### 91.4.2 Correlating responses: acks, fills, rejects, cancels, replaces

```cpp
// correlate.cpp — one venue message in; the table is the single source of truth and the single writer is this thread.
void Gateway::on_venue(const VenueMsg& m, std::uint64_t now) {
    ClOrdID id = decode_id(m.clordid);                                                      // from_chars → u64; two shifts
    Order* o = inflight.correlate(id);
    if (!o) [[unlikely]] { stats.stale_response++; journal.emit(StaleResponse{id, m.kind, now}); return; }   // dead order's response: counted, journaled, ignored
    journal.emit(Transition{id, o->st, m.kind, m.exec_qty, m.leaves, now});                 // every transition journaled first (Ch. 94)
    switch (m.kind) {
    case Ack:       o->st = OState::Acked; o->t_acked = now; timers.cancel(o->timer); update_own_best(o); break;               // guard cancelled: ~3 ns (Ch. 47)
    case Fill:      o->leaves -= m.exec_qty; accum.on_fill(*o, m.exec_qty, m.px);           // Ch. 90's accumulators: position, open qty, open notional, P&L — THIS is their only writer
                    o->st = o->leaves ? OState::PartFilled : OState::Filled; if (!o->leaves) release(id); break;
    case Cancelled: accum.on_cancel(*o); release(id); break;
    case Rejected:  accum.on_reject(*o); stats.reject++; release(id); break;                 // the reject-rate trigger's input (Ch. 90 §90.4.5)
    case ReplaceAck: /* the NEW id was allocated at replace-send; move leaves to it, release the old slot */ on_replace_ack(*o, m, now); break;
    case CancelReject: if (m.reason == TooLateToCancel) { /* a fill is coming or came: leave the order Acked; order-status if no fill within budget */ } break;
    }
}
void Gateway::release(ClOrdID id) { live.clear(id.slot()); per_symbol_unlink(id.slot()); per_strategy_unlink(id.slot()); inflight.release(id.slot()); }
```

- **Replace is two ids.** A cancel/replace allocates a *new* ClOrdID (FIX tag 41 `OrigClOrdID` carries the old); until the venue acks, both slots are live and the exposure check counts the larger (Ch. 90 §90.4.1's worst case); on ack the old slot is released and its generation bumped. The §91.2.3 matrix's hardest row is exactly this window.
- **"Too late to cancel" is a fill.** A `CancelReject` with that reason means the venue filled the order before the cancel arrived; the fill message follows (or arrives via drop-copy); the order stays `Acked` with an ageing timer and is queried if the fill does not appear.
- **Stale responses are telemetry** (Ch. 86): a steady trickle during the day means something is replaying — a session that re-delivers, a drop-copy that lags into reused slots — and the generation is quietly saving the position.

### 91.4.3 Position and exposure accumulators: one writer, from fills only

Ch. 90's `SymState`/`AcctState` are written *only* in `on_venue`'s `Fill`/`Cancel`/`Reject` arms (and `send`'s open-quantity increment): position changes on fills, open quantity on send/ack/cancel/fill, open notional likewise, realized P&L on fills against the running average. A strategy never "books" what it sent; the gate reads what the venue has confirmed plus what is outstanding. The accumulators are rebuilt by the same code from the journal (§91.3), which is why the journal records carry `exec_qty` and `px`, and why their rebuilt values can be compared against drop-copy field by field (§91.4.5).

### 91.4.4 Recovery after a kill: replay, resync, resolve

1. **Replay the journal into a fresh table** (§91.3: ms), from the last table snapshot (Ch. 94 §94.4.4); restore per-slot generations and the accumulators. Everything `Sent`-not-`Acked` is marked `Unknown`; the count is published (Appendix M.1 stays at `syncing`).
2. **Re-establish the session with our true sequence numbers** (FIX `Logon` with `NextExpectedMsgSeqNum`; SoupBinTCP login with our last sequence) and *consume the replayed stream through the normal `on_venue`* — the stale-generation check rejects responses for orders released before the crash, the rest resolve `Unknown`s to `Acked`/`Filled`/`Cancelled`. Our outbound gap is filled with `SequenceReset-GapFill` over new orders (the matrix says never resend journaled sends).
3. **Resolve the residue**: for every order still `Unknown` after the resync, an `OrderStatusRequest` (rate-limited; batch by priority — largest notional first) or, where the venue has none, a wait for drop-copy's view; an `Unknown` IOC is resolved by status only, never by resend. Orders unresolved within the budget are **cancelled by ClOrdID** — idempotent and harmless if the venue never saw them.
4. **Reconcile** (§91.4.5) and only then let Appendix M.1 move to `reconciled`; the Ch. 90 kill stays armed until then (a reconnected gateway with unknown orders must be able to cancel them all).

Measure the whole path as Ch. 89 §89.3 measures a restart: kill → replay done → session up → residue zero → `reconciled`, on a venue test session, daily.

### 91.4.5 Reconciliation against drop-copy and order status; position rebuild

- **Drop-copy as the independent ledger**: its executions carry the venue's order id and our ClOrdID; a reconciler (housekeeping core) folds them into a *shadow* position per symbol and account and compares with the accumulators every interval — the delta is Ch. 90 §90.4.5's position-drift trigger, with a tolerance for drop-copy lag (typically single-digit ms) and a hard limit (any delta older than the lag is a kill).
- **Order status as the authoritative point query**: `OrdStatus`, `CumQty`, `LeavesQty` for one ClOrdID; the reconciler issues them for `Unknown` orders and for any order whose drop-copy view disagrees with the table's (a fill we never saw because the session died mid-message).
- **Position rebuild without a snapshot**: fills from the journal (our view) ∪ fills from drop-copy (the venue's view), de-duplicated by `(venue exec id)`, applied in time order — the union catches the fills that arrived during the outage; the de-duplication is why every journaled fill records the venue's execution id, not just ours.
- **Make the reconciliation itself replayable**: its inputs (drop-copy records, status responses) are journaled like venue messages (Ch. 94) and sequenced like inputs (Ch. 96), so the question "why did we believe we were flat at 09:31:07" has an answer.

### 91.4.6 Type-state for the session and the order

Ch. 19 §19.4.5's type-state makes the illegal transitions unrepresentable: a `Session<Disconnected>` has no `send()`; `Session<LoggedOn>` is obtained only from `logon()`'s result; `Order<Sent>::on_ack()` returns `Order<Acked>` and there is no `Order<Sent>::on_fill()` (a fill before an ack is a protocol violation the venue cannot produce — treat it as `Ack+Fill`). The runtime table stores the state as a byte (it must — states are data), and the type-state wrappers are the *API* through which `on_venue` performs transitions, so the compiler rejects a cancel of a terminal order and the table rejects a stale one.

## 91.5 Pitfalls & anti-patterns

- **ClOrdIDs as strings on the hot path.** Hashing and comparing 20 bytes per response (§91.3: 85 ns vs 21) and no stale detection. Slot + generation, formatted only at the codec.
- **A counter without a generation.** Unique but un-locatable, and a reused slot without a generation books a late fill against the wrong order. The generation check is the mechanism, not a nicety.
- **Send before journal.** An orphan at the venue that the rebuilt table has never heard of; the kill cannot cancel what it does not know. Journal first (§91.4.1).
- **Resending an unacked order.** The Knight failure mode: a journaled send *may* be at the venue; the only safe actions are ask, wait, cancel (§91.2.3). The venue's duplicate-ClOrdID rejection is the backstop, not the plan.
- **Resending an IOC.** An IOC that is unknown is a fill to discover or nothing; a resend is a new, unrelated IOC.
- **Treating "too late to cancel" as a cancel.** It is a fill; book it when it arrives, query if it does not (§91.4.2).
- **Two writers on position.** A strategy pre-booking its own sends races the fill stream (Ch. 90 §90.5); fills are the only writer (§91.4.3).
- **Reusing generations after a restart.** A rebuilt table that starts every slot at generation 1 reissues pre-crash ids — duplicate-ClOrdID rejects at best, misbooked fills at worst. Generations come from the journal (§91.4.1).
- **A kill that walks the table.** 300 µs of cold lines (Ch. 90 §90.3); the live bitmap and lists are part of the table's contract.
- **Trusting one source of truth.** Our journal is our view; the session resync is the venue's view of what it told us; drop-copy is the venue's view of what happened. Reconciliation needs the last two; `reconciled` needs a zero residue.
- **Unbounded `Unknown`.** An order left unknown for minutes is unbounded exposure; the ageing timer (Ch. 47), the status query and the idempotent cancel bound it to a budget (§91.4.4).
- **Skipping the daily rehearsal.** A recovery path exercised only after a real crash has unknown timing; Appendix M.4's T-5 gate runs logon + resync + reconcile on the test session every morning for this reason.

## 91.6 Exercises & checklist

1. Reproduce §91.3 on isolated cores; add a 256-slot "active orders" L1-hot path and show the correlate cost for responses to recent sends vs random old ones.
2. Implement cancel/replace with two live ids (§91.4.2) and verify with a replay that the exposure check (Ch. 90 §90.4.1) counts the larger of the two until the venue's ack, and that the old generation is bumped exactly once.
3. Build the journal-rebuild of §91.3 with table snapshots every 10⁵ records; measure replay time vs snapshot interval and pick the interval that keeps the local recovery under 5 ms at your event rate.
4. Simulate a session kill in the Ch. 102 harness: deliver the acks for the last 50 sends *only* through the resync stream, with 5 % of them for slots that were released and reused before the crash; show the stale-generation counter catching exactly those and the residue resolving to zero.
5. Write the drop-copy reconciler (§91.4.5) against a simulated drop-copy with 3 ms lag; inject one missing fill and measure the detection latency of the position-drift trigger.
6. Encode Appendix J.3's lifecycle as Ch. 19 §19.4.5 type-state and port `on_venue` to it; count the transitions the compiler now rejects.

**Checklist**

- [ ] ClOrdIDs are slot + generation in one 64-bit integer; wire formatting/parsing happens only at the codec.
- [ ] The in-flight table is preallocated, 64 B per order, single-writer; correlation is index + one compare; stale responses are counted and journaled.
- [ ] Live bitmap, per-symbol and per-strategy live lists are maintained on every send/release (the kill's and SMP's inputs).
- [ ] Journal-before-send; generations restored from the journal on restart; the venue's duplicate-ClOrdID rejection is enabled as the backstop.
- [ ] Every transition is journaled with venue fields (exec id, qty, px) before it is applied.
- [ ] Position/exposure accumulators are written only from fills/cancels/rejects/sends in the gateway thread.
- [ ] Recovery = snapshot + journal replay → session resync through `on_venue` → status queries for the residue → idempotent cancels within budget → reconcile → `reconciled`.
- [ ] The unknown-state matrix is written down per order type; no journaled send is ever resent; IOCs are never resent.
- [ ] Drop-copy reconciliation runs continuously with a lag tolerance; the position-drift delta feeds Ch. 90's kill trigger.
- [ ] Logon + resync + reconcile is rehearsed daily on the test session with time-to-known-state recorded.

## 91.7 References

- FIX Trading Community. *FIX 4.4 / 5.0 SP2 Specification* — Session layer (MsgSeqNum, ResendRequest, SequenceReset, PossDupFlag), tags 11/41 (ClOrdID/OrigClOrdID), OrderStatusRequest (H), ExecutionReport (8), OrderCancelReject (9).
- Nasdaq. *OUCH 5.0* and *SoupBinTCP 4.0* specifications — order tokens, sequenced replay on login.
- U.S. SEC. *In the Matter of Knight Capital Americas LLC*, Release No. 34-70694 (2013) — the resend failure mode, in the regulator's words.
- Helland, P. "Idempotence Is Not a Medical Condition." *ACM Queue* 10(4), 2012 — the idempotent-messaging argument.
- Gray, J., Reuter, A. *Transaction Processing: Concepts and Techniques*, Morgan Kaufmann 1993, Ch. 9–10 (write-ahead logging, recovery) — journal-before-action.
- CME Group. *Drop Copy Service* and *iLink 3 — Order Status Request* documentation — the two reconciliation sources.

## 91.8 Additional Reading

- Kleppmann, M. *Designing Data-Intensive Applications*, Ch. 11–12 (exactly-once semantics, idempotence, reconciliation) — the general theory.
- Onyx / Chronicle Software, "Order Management at Low Latency" (blog series) — a production in-flight table.
- FIA. *Best Practices for Automated Trading Risk Controls* §"Order Management and Cancel-on-Disconnect" — operational practice around unknown orders.
- Nasdaq, *Self Match Prevention and Cancel-on-Disconnect* technical notes — the venue-side safety nets.
- Appendix J.3 and J.5 of this book — the lifecycle and field names used throughout.

---

*Next: Ch. 92 — The Order-Entry Gateway & Session Layer.*
