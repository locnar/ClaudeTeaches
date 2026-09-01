# Appendix J — HFT Market-Structure & Protocol Primer

> **Consolidates the trading framing** the main text assumes but never teaches from scratch — the book targets advanced C++ developers who may be new to electronic trading. It gives the domain vocabulary and mechanics behind the running examples: the order-book case study (Ch. 24–25), zero-copy market-data decoding (Ch. 53 — ITCH/OUCH/FIX/SBE/FAST), reliable multicast and A/B feed arbitration (Ch. 54), and the end-to-end tick-to-trade path (Ch. 76). It complements **Appendix F** (Glossary): F *defines* the terms one line at a time; this appendix explains how the pieces *work together*.

**How to use this:** read top-down for the onramp, or jump to J.5 for the protocol quick reference. This is *background*, not a performance chapter — no benchmarks; it exists so the trading scenarios in the rest of the book are self-contained and a reader never has to guess what "BBO," "IOC," or "A/B feed" means. Nothing here is legal, regulatory, or trading advice, and market structure varies by asset class, venue, and jurisdiction — the model below is the US-equities / listed-derivatives archetype most HFT literature assumes; adapt to your market.

---

## J.1 Market microstructure: exchanges, matching engines, price-time priority

An **exchange** runs a **matching engine**: a single logical process that accepts orders and matches buyers with sellers according to fixed, published rules. The dominant model is the **central limit order book (CLOB)** with **price-time priority** (also called **FIFO**):

- Orders rest in the book at a **price**; at each price they are queued by **arrival time**.
- An incoming **aggressive** order matches against the **best** opposite-side price first, and within that price against the **oldest** resting order first. Better price wins; at equal price, earlier wins. This is why *latency* is *edge* — being earlier in the queue at a price is a real, monetizable advantage (the motivation for the entire book).
- Some venues use **pro-rata** matching (fills allocated proportionally to resting size rather than strictly by time) or a **maker-taker** fee model (the resting "maker" is rebated, the aggressing "taker" pays) — these change the incentives but not the core data structures.
- Trading is usually **continuous** during the session, bracketed by **auctions** — an **opening** and **closing auction** that cross accumulated orders at a single price. The **open** is the highest-stress moment for a feed handler (Ch. 53's microburst discussion): a burst of activity as the auction resolves and continuous trading begins.

The two sides of the market: the **sell-side** (brokers, exchanges, market makers providing liquidity) and the **buy-side** (asset managers, hedge funds taking liquidity). An HFT firm is typically a **market maker** (quoting both sides, earning the spread/rebate) and/or a **taker** (crossing the spread to capture a signal).

## J.2 The order book: levels, BBO, NBBO, depth

The **order book** is the aggregated set of resting buy orders (**bids**) and sell orders (**asks**/**offers**), organized by price:

- A **price level** is all resting size at one price on one side. The book is two sorted sequences of levels — bids descending, asks ascending.
- The **best bid** (highest buy price) and **best ask** (lowest sell price) together are the **top of book** or **BBO** (Best Bid and Offer). The gap between them is the **spread**. The **mid** is their average.
- **Depth** is the size available beyond the top — level 2, level 3, and deeper. A "deep book" update touches a level away from the top; a "top-of-book" update moves the BBO. This distinction drives data-structure choice (Ch. 24–25: array-of-pointers vs. intrusive lists for levels; BBO fast path vs. deep-book path).
- The **NBBO** (National Best Bid and Offer) is the best bid/ask *across all venues* for a US-equity symbol — a consolidated view a strategy compares its local book against.

**Building the book** from a feed is the feed handler's core job (Ch. 25, 53): apply each incremental message (add / modify / delete / execute) to the right price level, maintaining the sorted levels and the BBO, so downstream strategy code can read a coherent snapshot cheaply (ties Ch. 34's seqlock publication).

## J.3 The order lifecycle

An order is a small state machine the **order gateway** / **OMS** (Order Management System) tracks from submission to terminal state:

- **Order types:** a **market** order executes immediately at the best available price(s); a **limit** order rests until it can trade at its price or better; a **stop** order activates when the market reaches a trigger. **Pegged** orders track the BBO; **iceberg**/reserve orders show only part of their size.
- **Time-in-force (TIF):** **Day** (rests until the close), **IOC** (Immediate-Or-Cancel — take what's available now, cancel the rest), **FOK** (Fill-Or-Kill — all now or nothing), **GTC** (Good-Til-Cancelled). IOC is the workhorse of aggressive HFT strategies.
- **The state machine:** you send **New** → the venue replies **Ack** (accepted, now working) or **Reject** → the order may receive **partial fills** and then a **full fill** (**Execution** reports), or you send **Cancel** / **Cancel-Replace** (amend price/size) → terminal states are **Filled**, **Cancelled**, **Rejected**, **Expired**. Every transition is a message on the order-entry protocol (J.5).
- **Self-trade prevention (STP)** stops your own resting order from matching your own aggressing order. **Pre-trade risk** checks (position limits, max order size, price collars — mandated by rules like SEC 15c3-5, Appendix K.4) sit *on* the order path and add latency you must budget (Ch. 76).

This state machine is exactly the kind of pure, replayable logic Ch. 74 isolates into a deterministic core, decoupled from I/O.

## J.4 Market data: feeds, A/B lines, snapshots vs. increments

Strategies see the market through a **market-data feed** — a stream of messages describing every book change and trade:

- **Incremental (delta) feeds** send one message per event (add/modify/delete/execute). They are compact and low-latency but **stateful**: you must apply every message in order to keep a correct book, and a single gap corrupts your book until repaired.
- **Snapshots** periodically send the full book (or top-N levels) so a late-joining or recovering consumer can *rebuild* state without replaying all history. A feed handler typically boots from a snapshot, then applies increments.
- **Sequence numbers** on every message let you detect **gaps** (a missing sequence → you dropped a packet). Detection triggers **recovery**: request a retransmission, or rebuild from the next snapshot (Ch. 54).
- **Multicast and redundant A/B lines:** exchanges disseminate market data over UDP **multicast** (one send, many subscribers — Ch. 51) on **two independent lines, A and B**, carrying identical data over diverse paths. The consumer performs **A/B arbitration**: take whichever line's copy of each sequence number arrives first, using the other to fill gaps. This is the reliability mechanism over unreliable UDP (Ch. 54), and the reason feed handlers are built around sequence-gap detection rather than TCP-style retransmission on the hot path.

## J.5 Protocols: ITCH, OUCH, FIX, SBE, FAST (field-level quick reference)

The wire formats the decoder chapters parse (Ch. 53). Two axes: **market data vs. order entry**, and **binary fixed-layout vs. tag-value text**.

- **ITCH** (Nasdaq, market data) — a compact **binary** protocol; fixed-layout messages, each a leading type byte then packed big-endian fields (`AddOrder`, `OrderExecuted`, `OrderDelete`, `Trade`, …). Ideal for zero-copy parsing: cast the buffer to a packed struct after a bounds check (Ch. 53, 60). The archetype the book's decoding examples use.
- **OUCH** (Nasdaq, order entry) — ITCH's order-entry sibling; binary fixed-layout order messages (`EnterOrder`, `ReplaceOrder`, `CancelOrder`) and responses (`Accepted`, `Executed`, `Canceled`, `Rejected`). Same zero-copy treatment.
- **FIX** (Financial Information eXchange) — the ubiquitous **tag-value text** protocol: `8=FIX.4.2\x019=...\x0135=D\x01...` — `tag=value` pairs separated by SOH (`0x01`). Human-readable and flexible but **parse-heavy** (string scanning, integer/decimal conversion — Ch. 53's `from_chars`/branch-free-parse material) — too slow in raw form for the hot path, which is why binary encodings exist.
- **SBE** (Simple Binary Encoding) — the FIX community's **binary** encoding: a schema (XML) generates fixed-layout message structs with explicit offsets, so decoding is a bounds-checked struct overlay (like ITCH) with none of FIX's string parsing. The modern low-latency choice where FIX semantics are required (Ch. 53).
- **FAST** (FIX Adapted for STreaming) — a **compression**-oriented FIX encoding (implicit fields, delta/copy operators, bit-mapped presence). Bandwidth-efficient for multicast but requires stateful, branchy decoding — a different trade-off from SBE's flat layout.

**The through-line (Ch. 53):** fixed-layout binary (ITCH/OUCH/SBE) → parse by bounds-checked struct overlay, mind endianness (`std::byteswap`, Appendix H.5) and packing (Ch. 9); tag-value/compressed (FIX/FAST) → branch-light scanning and careful number conversion. All of it treats the wire bytes as **untrusted input** (Ch. 72, 60) — validate lengths before you read.

## J.6 Roles in a trading system

The functions the process-topology chapter (Ch. 74) maps onto separate processes:

- **Feed handler** — receives market data (J.4), performs A/B arbitration and gap recovery, decodes messages (J.5).
- **Book builder** — applies decoded messages to maintain order books and the BBO (J.2), publishing snapshots to strategies (Ch. 34).
- **Strategy / signal** — reads the book, computes a trading decision (the alpha logic — outside this book's scope, but the thing everything else serves).
- **Order gateway / OMS** — encodes and sends orders (J.3), tracks their state machine, handles acks/fills/cancels.
- **Pre-trade risk** — enforces limits on the order path (J.3) before anything reaches the wire.
- **Sequencer** — in some designs, a single process that stamps a total order on events for deterministic replay (Ch. 74, 75).

## J.7 The tick-to-trade path, end to end

One market event, from wire to wire — the narrative Ch. 76 measures:

1. A packet arrives at the NIC (Ch. 55) — a market-data multicast datagram on line A or B.
2. The feed handler receives it (kernel stack, io_uring, or bypass — Ch. 47–52, 61–62), performs A/B arbitration and gap check (J.4), and **decodes** the message (J.5, Ch. 53).
3. The book builder **applies** it, updating the affected price level and the BBO (J.2, Ch. 25).
4. The strategy sees the changed book, evaluates its signal, and **decides** to act.
5. Pre-trade **risk** validates the intended order (J.3).
6. The order gateway **encodes** the order (OUCH/SBE — J.5) and writes it to the wire (Ch. 52, 55).

Each hop has a latency budget; the sum, measured wire-to-wire with hardware timestamps (Ch. 56, Appendix K.5), is the **tick-to-trade** number the whole book exists to minimize (Ch. 1, 76). The physical floor beneath it — colocation, cabling, the exchange link — is Appendix K.

## J.8 References

- Larry Harris, *Trading and Exchanges: Market Microstructure for Practitioners* — the standard reference for J.1–J.3 (matching, order types, market structure).
- Exchange protocol specifications: **Nasdaq TotalView-ITCH** and **OUCH** specs, the **FIX Trading Community** documents (FIX, **SBE**, **FAST** encoding specs) — J.5.
- Regulatory market-structure primers — the SEC's market-structure overviews (Reg NMS, the NBBO), and equivalent ESMA/MiFID II material for Europe.

## J.9 Additional Reading

- **Appendix F** (Glossary) — one-line definitions of every term used here (tick-to-trade, NBBO, IOC, maker/taker, A/B arbitration, …).
- Ch. 24–25 (order book), Ch. 53 (decoding), Ch. 54 (reliable multicast), Ch. 74 (process topology / deterministic state machine), Ch. 76 (tick-to-trade) — where these mechanics become C++.
- **Appendix K** (Exchange Connectivity) — the physical and commercial layer beneath the protocols (colocation, cross-connects, access, entitlements).
- The exchange operators' own developer portals (Nasdaq, CME, Cboe, NYSE) — the current, authoritative message specs, which supersede any summary here.

---

*Next: Appendix K — Exchange Connectivity & the Physical Layer: colocation and cross-connects, latency-equalized cabling, microwave/millimeter-wave vs. fiber, exchange access and entitlements, and venue-side time distribution (grandmaster clocks, PPS, PTP boundary clocks).*
