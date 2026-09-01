# Part XII — Observability & Operations in Production

# Chapter 75 — Capture, Persistence & Replay Storage

> **Prerequisites:** Ch. 55, 58 (NIC timestamping / PTP — nanosecond, wire-accurate capture times), Ch. 71 (zero-overhead logging — the same off-hot-path-writer, binary-record discipline applied to ticks/orders), Ch. 48 (io_uring — async writes to disk), Ch. 74 (the deterministic state machine — what you replay captures *into*), Ch. 34/37 (SPSC ring / batching — producer→writer hand-off).
>
> **Leads into:** Ch. 76 (the case study — deterministic replay + latency-regression gates in CI). The penultimate chapter: recording everything that happened, losslessly and off the hot path, so the deterministic core (Ch. 74) can be replayed for debugging, research, and compliance.

---

## 75.1 Why it matters: deterministic replay, research, and compliance

Chapter 74 built a deterministic state machine: feed it the same input sequence and it produces the same outputs, bit-for-bit. That property is worthless unless you **captured the input sequence** — every inbound tick, every timer fire, every fill, every config change, in order, with accurate timestamps. Capture is what turns "deterministic in principle" into "replayable in practice," and it serves three demanding masters at once. **Debugging:** a strategy did something wrong at 09:31:42.000123456 — with a capture you *replay the exact session* through the core (Ch. 74) and watch it happen in a debugger, deterministically, as many times as you like, instead of guessing from logs. **Research/backtesting:** a new strategy must be evaluated against *real* historical market data — the capture *is* the backtest input, and replaying it through the strategy gives a faithful "what would we have done" (far better than synthetic or vendor-aggregated data, which lacks the exact timing and microstructure). **Compliance:** regulators require firms to reconstruct what they did and why — a complete, time-stamped, tamper-evident record of market data in and orders out is a legal obligation, not a nicety.

The hard constraint is the one this whole book has hammered: **capture must not slow the hot path.** You are recording *everything* — a market-data feed at open can be millions of messages per second (Ch. 53 microbursts) — and writing it to disk, which is *milliseconds*-slow and syscall-heavy (Ch. 47), the antithesis of a microsecond hot path. If capture writes to disk synchronously on the path that trades, you've destroyed the latency you spent the book building. So capture obeys the exact same discipline as logging (Ch. 71): **the hot path does the cheap part — copy the raw bytes into a lock-free buffer and move on; a separate writer thread/process does the expensive part — getting bytes to disk — off the path.** Capture is zero-overhead logging's bigger sibling: same producer→ring→async-writer shape, but lossless (you can drop a log line; you cannot drop a tick you're legally required to keep) and higher-volume.

And it must be **lossless and gap-free**: a capture with holes is useless for replay (the deterministic core diverges at the gap), wrong for backtesting (missing data = wrong P&L), and non-compliant (an incomplete record). This is in tension with "don't slow the hot path" — under a microburst, the writer may not keep up, and you must *never* block the hot path to wait for it, yet you must *not* lose data. Resolving that tension (large buffers, fast storage, back-pressure that doesn't reach the hot path, and capturing *off the NIC* below the application — §75.4.1) is the engineering of this chapter. It covers the append-only/WAL journal model (§75.2), measuring capture throughput and hot-path impact (§75.3), the lossless-capture / binary-format / off-path-write / gap-free / replay techniques (§75.4), and the ways storage stalls and drops bleed into the hot path (§75.5).

## 75.2 Mental model: append-only/WAL journals; time-indexed storage

Capture is an **append-only journal**: events are written once, in arrival order, never modified — the same write-ahead-log (WAL) discipline databases use for durability and recovery, applied to the market/order stream:

```
   HOT PATH                         CAPTURE RING (Ch.33)        WRITER (off-path)              STORAGE
   ────────                         ───────────────────         ────────────────              ───────
   on each event:                   ┌──────────────────┐        drain (batched, Ch.36)         append-only journal
     • timestamp (NIC/PTP, Ch.50)   │ ev ev ev ev ev …│  ───►   • write sequentially            ┌─[seq, ts, bytes]─┐
     • copy raw bytes + seq to ring │ (lock-free SPSC) │        • io_uring/O_DIRECT (Ch.45)      │ append, append,  │
     • bump producer seq            └──────────────────┘        • fsync/barrier for durability   │ append … (WAL)   │
     └─ return (no disk, no syscall)                            └─ never touches the hot path     └─time-indexed────┘
```

The model's properties:

- **Append-only / write-ahead.** Events are appended in order; nothing is updated in place. Append-only writes are *sequential* (the fastest disk access pattern, even on NVMe — §75.4.3), recovery-friendly (replay the log forward), and naturally tamper-evident (you detect edits to an append-only signed log). This is the WAL idea (Ch. 23's "persist before you act" generalized): the journal *is* the durable truth.
- **Each record is self-describing and sequenced:** a monotonic **sequence number** (for gap detection — §75.4.4), an accurate **timestamp** (NIC hardware / PTP — Ch. 55, 58, so the time is *wire-accurate*, not when software happened to look), the **raw bytes** (captured losslessly — §75.4.1), and enough framing to parse it back. Compact binary (§75.4.2), not text.
- **Time-indexed.** The journal is indexed by time (and sequence) so replay and research can seek to "09:31:42" or "sequence N" without scanning from the start — periodic index checkpoints / snapshots (§75.4.4) bound the seek and the replay-from cost (ties Ch. 74's snapshot-bounded recovery).
- **Two capture altitudes.** You can capture at the **line/wire level** (every packet off the NIC, below the application — §75.4.1, the most faithful and the compliance gold standard) and/or at the **application level** (normalized events, the inputs the Ch. 74 core actually consumed). Wire capture replays the *whole stack*; application capture replays the *core*. Production systems often do both: wire for compliance/forensics, application for fast deterministic core replay.

The mental shift mirrors Ch. 71: **capturing an event is "append a record to a journal," and the journal-writing is somebody else's job, off the path.** The hot path timestamps and enqueues; the journal is built off-path, durably, losslessly, in order.

## 75.3 Measure it: capture throughput and hot-path impact

Two things to measure, and they pull against each other: **hot-path impact** (capture must add ~nothing to tick-to-trade) and **sustained lossless throughput** (the writer must keep up with the worst burst without dropping). Measure both, under a market-open-style microburst (Ch. 53).

**Hot-path impact** — the cost the trading thread pays to capture an event (identical in spirit to Ch. 71's §71.3):

| Capture approach | Hot-path cost per event | Lossless under burst? | Verdict |
|---|---|---|---|
| **Synchronous write to disk** (`write()`/`fwrite` on the path) | **µs–ms** (syscall + I/O stall, Ch. 47) | only if disk keeps up (it won't) | ✗ destroys the hot path |
| **App-level: copy raw bytes + seq to SPSC ring** (§75.4.1-2) | **~5–20 ns** (a few stores, Ch. 34) | depends on ring size + writer speed (§75.4.3) | ✓ the in-process capture path |
| **Off-NIC / off-host capture** (tap, mirror port, SmartNIC — §75.4.1, Ch. 70) | **~0 on the application** (capture happens *below/beside* the app) | depends on the capture appliance | ✓ best for compliance/lossless wire capture |

**Sustained throughput & loss** — drive the worst burst and measure (a) whether the ring ever fills (→ drop or back-pressure, §75.5), (b) the writer's sustained MB/s vs the burst's peak, (c) end-to-end durability latency (event → durably on disk). Representative shape (NVMe, io_uring, batched — figures pending real runs): sequential batched writes sustain **multiple GB/s**, enough for a full normalized feed, *if* the ring is large enough to absorb the burst peak above the disk's sustained rate while the writer catches up in the troughs.

The lessons:

- **Synchronous capture is the same catastrophe as synchronous logging** (Ch. 71) — a `write()` on the path is µs-to-ms of syscall + stall. Never.
- **In-process capture is the Ch. 71 ring-write cost (~ns)**; the engineering is all in the *writer* keeping up (§75.4.3) and the *buffer* absorbing the burst (§75.5).
- **Off-NIC / off-host capture has ~zero application cost** because it happens *below* the app (a tap, a switch mirror port, a SmartNIC/DPU — Ch. 70) — the gold standard for *lossless wire capture* and compliance, since it can't be slowed by or lost to application back-pressure (it's not in the application at all). Many firms capture at the wire *and* in the app for the two replay altitudes (§75.2).
- **"Lossless" is a measured claim under the worst burst**, not a hope — size the ring for `(peak_rate − sustained_disk_rate) × burst_duration` and prove no drops at open (§75.5).

## 75.4 Techniques

### 75.4.1 Lossless line capture and nanosecond packet timestamps

The most faithful capture is **at the wire**, below the application, with hardware timestamps:

- **Capture off the NIC / off the host.** A network **tap**, a switch **mirror/SPAN port**, NIC **RX capture**, or a **SmartNIC/DPU** (Ch. 70) records every packet *without* the application doing the writing — so capture can't be slowed by, or lost to, application back-pressure (§75.3). This is the compliance gold standard: a complete, independent record of exactly what was on the wire. Tools/stacks: kernel-bypass capture (Ch. 62), `AF_PACKET`/`PF_RING`, or a dedicated capture appliance.
- **Nanosecond hardware timestamps (Ch. 55, 58).** Stamp each packet with the **NIC hardware timestamp**, disciplined by **PTP** (Ch. 58) so times are accurate *and comparable across hosts* (wire-to-wire, tick-to-trade reconstruction — Ch. 58). Software timestamps (when the app looked) are both less accurate and perturbed by load; hardware/PTP timestamps are the truth you replay and audit against.
- **Lossless = capture below the lossy layer.** The reason wire capture is lossless is that it sits *below* the place drops happen (application back-pressure, ring-full). Combined with adequate NIC RX rings and capture buffers (Ch. 53's microburst tuning), off-NIC capture records the burst the application itself might shed.
- **Capture the redundant feeds (Ch. 53).** Record *both* A/B lines — replay and gap-recovery (§75.4.4) need both, and a gap on one line is filled from the other.

### 75.4.2 Compact binary log formats

The record format is **compact binary**, for the same reasons and with the same machinery as Ch. 71's binary logging:

- **Raw bytes + minimal framing.** Store the original wire bytes (lossless — you can re-decode with any future parser, important for compliance and research) plus a small fixed header: sequence number, hardware timestamp (Ch. 58), length, source/feed ID, and a record type. No text, no per-field formatting on the path (Ch. 71) — just the bytes and a header.
- **Self-describing and versioned.** The format carries a version and enough framing to parse it back years later (compliance retention is long). Pair the journal with the *schema*/decoder (and the strategy/config versions that consumed it — Ch. 73.4.2) so a replay is fully reproducible.
- **Compact = cheaper everywhere.** Binary records are small → less disk bandwidth (helps the writer keep up, §75.4.3), less storage (retention is expensive at feed volumes), faster to read back for replay. This is the Ch. 71 win at capture scale.
- **Optionally compress off-path.** Compression (for retention) happens on the writer/archival side (off the hot path), never on the capture path — and often *after* the capture is durable, during compaction (§75.4.4).

### 75.4.3 Getting bytes to disk off the hot path (async, io_uring, `O_DIRECT`, NVMe, batching)

The writer's job: drain the ring and get bytes durably to storage **fast enough to keep up**, entirely off the hot path. The toolkit:

- **A dedicated writer thread/process** on a housekeeping core (Ch. 42), draining the capture ring (Ch. 34), **batched** (Ch. 37 — accumulate many records, write them in one large sequential I/O to amortize syscall and seek cost). Batching is what turns millions of tiny events into a few big efficient writes.
- **io_uring (Ch. 48)** for asynchronous, batched, low-overhead submission — submit many writes without a syscall each, reap completions asynchronously. The modern way to sustain capture bandwidth without the writer stalling.
- **`O_DIRECT`** to bypass the page cache for large sequential capture writes — avoids double-buffering and page-cache pressure that would evict the hot path's working set (Ch. 7/15); the capture stream is write-once, so caching it is pure pollution. (Mind `O_DIRECT`'s alignment requirements.)
- **NVMe / fast sequential storage.** Append-only writes are *sequential* (§75.2), the pattern NVMe and SSDs handle best (GB/s, §75.3). Provision storage for the *peak sustained* feed rate, and put capture on its own device so it never contends with anything the hot path touches (§75.5).
- **Durability vs latency of the journal.** Decide how durable each record must be before you "trust" it: an `fsync`/write barrier per batch (durable but slower) vs relying on the OS/device to flush (faster, small loss window on power failure). For compliance you need durability; tune the batch/fsync granularity so durability latency is bounded *without* the writer falling behind. None of this is on the hot path — but it bounds how much capture sits in volatile buffers at any instant.

### 75.4.4 Sequencing and gap-free persistence; retention and compaction

A capture is only useful if it's **complete and ordered** — and manageable over long retention:

- **Sequence every record; detect gaps.** A monotonic sequence number per record (and the *feed's own* sequence numbers from the protocol — Ch. 53) lets you *prove* the capture is gap-free, and detect/locate any hole. On a gap in one feed line, fill from the redundant line (Ch. 53 A/B, §75.4.1); an unrecoverable gap is flagged (replay can't be trusted across it — §75.5).
- **Gap-free persistence under burst.** The lossless guarantee (§75.3) plus sequence-checking: size buffers for the burst, capture off-NIC where possible, and *verify* no sequence holes in the persisted journal. "We think it's complete" isn't good enough for replay or compliance — *check* the sequence.
- **Time-indexing and snapshots.** Periodically write an **index checkpoint** and a **state snapshot** (the Ch. 74 core's state at a sequence point) so replay/seek starts from a recent checkpoint, not session-start — bounding both seek time and replay-from cost (ties Ch. 74.3's snapshot-bounded recovery).
- **Retention and compaction.** Feed-volume capture is large; manage it with tiered retention (hot recent capture on fast NVMe, older archived/compressed off to cheaper storage), compaction (merge/compress sealed journal segments off-path), and a retention policy that meets the *compliance* minimum (often years) while controlling cost. Compaction runs off the hot path and off the capture device.

### 75.4.5 Feeding captures into replay, simulation and backtests

Capture's payoff — replaying it through the deterministic core (Ch. 74) for the three use cases of §75.1:

- **Deterministic replay (debugging).** Feed the captured input stream into the Ch. 74 state machine and reproduce the exact session — same inputs, same outputs, bit-for-bit (Ch. 74.2.2). Replay a production incident in a debugger as many times as needed; the determinism guarantees you see the *same* behavior. Replaying *application-level* capture exercises the core; replaying *wire-level* capture (§75.4.1) exercises the whole stack (decode → decision).
- **Simulation harness.** Replay through the core under a simulator that models the exchange's responses (fills, rejects, queue position) so you can test the strategy's *behavior*, not just recompute its outputs — useful where the strategy's orders would have changed the market (a limitation of naive backtests).
- **Backtesting / research.** The captured market data is the backtest input; replay new strategies against real historical microstructure and timing (§75.1). Because the data is the *real captured feed* with real timestamps (Ch. 58), the backtest is faithful in a way aggregated/synthetic data isn't.
- **Latency-regression gates in CI (ties Ch. 76).** Replay a captured session through the build in CI and assert (a) outputs match the golden record (correctness/determinism regression — Ch. 74) and (b) per-stage latency hasn't regressed (Ch. 3/76). Capture + deterministic replay is what makes a *latency-regression gate* possible — the subject of the final chapter.

## 75.5 Pitfalls & anti-patterns: dropping under load; storage stalls bleeding into the hot path

- **Synchronous capture on the hot path (the cardinal sin).** A `write()`/`fsync` on the trading thread is µs-to-ms (Ch. 47, §75.3) — capture, like logging (Ch. 71), is *always* a ring-enqueue on the path and an off-path writer. Never write to disk from the hot path.
- **Storage stall bleeding into the hot path.** Even with an async writer, shared resources leak the stall back: the capture device shared with something the hot path touches, page-cache pressure from buffered capture evicting the hot working set (Ch. 7/15 — use `O_DIRECT`, §75.4.3), the writer thread on a hot core (Ch. 42), or an `fsync` that stalls a shared I/O queue. Isolate capture storage and the writer completely (§75.4.3).
- **Dropping under load and calling it lossless.** The ring fills during the open burst (Ch. 53), the writer can't keep up, and capture silently drops — a hole that breaks replay (the core diverges, Ch. 74), corrupts backtests, and violates compliance. Size the ring for the burst above sustained disk rate, capture off-NIC where you can (§75.4.1), and **verify gap-free by sequence** (§75.4.4) — don't assume.
- **Back-pressure reaching the hot path.** "Don't drop" must *never* become "block the hot path until the writer drains" (that's the §75.3 catastrophe inverted). The hot path never waits on capture; you absorb bursts with buffer + fast storage + off-NIC capture, and if truly overwhelmed you drop-and-flag (or shed at the capture layer, not the trading layer) — but you never stall the trade to capture it.
- **Software timestamps for a wire record.** Stamping capture with when *software looked* (not the NIC/PTP hardware time, Ch. 55, 58) gives inaccurate, load-perturbed times — useless for wire-to-wire reconstruction and weak for compliance. Use hardware/PTP timestamps (§75.4.1).
- **Lossy/aggregated capture for backtesting.** Capturing only top-of-book, or vendor-aggregated data, or sampling — then backtesting on it and trusting the result. The microstructure and exact timing are gone; the backtest lies. Capture the *full, raw, timestamped* feed (§75.4.1-2).
- **A capture you can't decode later.** Storing bytes without the schema/version, or in a format tied to a since-changed parser — years later (compliance retention) you can't read it. Version the format and archive the decoder + strategy/config versions (§75.4.2, Ch. 73).
- **No snapshots → unbounded replay/seek.** Replaying from session-start every time because there are no checkpoints (§75.4.4) — replay and recovery (Ch. 74.3) become unbounded. Snapshot periodically.
- **Non-deterministic replay.** Replay diverging from production because the core isn't actually deterministic (Ch. 74.5 — a hidden `clock_gettime`, unordered inputs) or the capture didn't record *all* inputs (a timer, a config change — Ch. 73). Capture *every* input the core consumes, in order; assert replay matches the golden record (§75.4.5).

## 75.6 Exercises & checklist

**Exercises:**

1. **Capture-cost measurement.** Compare hot-path cost of (a) synchronous `write()` capture and (b) ring-enqueue + async io_uring writer (§75.4.3) under a microburst (Ch. 53). Reproduce the §75.3 spread; confirm (b) is ~ns on the path.
2. **Prove lossless under burst.** Drive a burst whose peak exceeds sustained disk rate; size the ring for `(peak − sustained) × duration` and verify the persisted journal is **gap-free by sequence** (§75.4.4). Then shrink the ring and show drops appear — and that your gap-check *detects* them (not assumes).
3. **Wire vs app capture replay.** Capture both at the wire (tap/`AF_PACKET`, §75.4.1) and at the application level; replay each through the deterministic core (Ch. 74) and confirm both reproduce the session — wire capture exercising decode+decision, app capture exercising the core (§75.4.5).
4. **Hardware timestamps.** Capture with software timestamps and with NIC/PTP hardware timestamps (Ch. 55, 58); compare their accuracy and load-sensitivity. Reconstruct a wire-to-wire latency from the hardware timestamps (Ch. 58).
5. **Deterministic-replay regression gate.** Build a golden capture + golden output (Ch. 74); in CI, replay the capture through the current build and assert bit-identical outputs and no per-stage latency regression (§75.4.5, Ch. 76). Break the core's determinism (add a `now()`) and watch the gate catch it.
6. **`O_DIRECT` and page-cache pollution.** Capture with buffered I/O vs `O_DIRECT` (§75.4.3) and measure the hot path's cache/TLB miss rate (Ch. 2) during heavy capture — show buffered capture pollutes the hot working set and `O_DIRECT` doesn't.

**Checklist:**

- [ ] The hot path **captures by ring-enqueue only** (~ns, Ch. 34/71) — **never** a disk write/`fsync` on the path (§75.3, §75.5).
- [ ] Capture is **lossless and gap-free**, *verified by sequence number* under the worst burst; ring sized for `(peak − sustained) × burst` and/or captured **off-NIC** (tap/mirror/DPU, Ch. 70) (§75.4.1, §75.4.4).
- [ ] Back-pressure **never reaches the hot path** — bursts absorbed by buffer + fast storage + off-NIC capture; overwhelmed → drop-and-flag, never stall the trade (§75.5).
- [ ] Records are **compact binary** (raw bytes + seq + **hardware/PTP timestamp**, Ch. 55, 58), **versioned/self-describing**, archived with their decoder + strategy/config versions (§75.4.2, Ch. 73).
- [ ] The writer is a **dedicated off-core thread/process** (Ch. 42) using **batched io_uring** (Ch. 48) + **`O_DIRECT`** + **NVMe**, on **isolated storage**; durability (`fsync` granularity) is tuned without falling behind (§75.4.3, §75.5).
- [ ] The journal is **append-only/WAL**, **time-indexed**, with periodic **snapshots/checkpoints** bounding replay/seek; retention + off-path compaction meet the **compliance** minimum (§75.2, §75.4.4).
- [ ] Captures feed **deterministic replay** (Ch. 74), **simulation**, **backtests**, and a **CI regression gate** (outputs match golden + no latency regression) (§75.4.5, Ch. 76); **every** core input (incl. timers/config) is captured in order (§75.5).

## 75.7 References

- The write-ahead logging / journaling literature (databases, filesystems) — append-only durability and recovery, the model for the capture journal (§75.2).
- `liburing`/io_uring documentation (Ch. 48 references) and `O_DIRECT`/NVMe performance guidance — async, high-throughput, off-path persistence (§75.4.3).
- PTP / NIC hardware timestamping references (Ch. 55, 58) — accurate, cross-host capture times (§75.4.1).
- The LMAX/event-sourcing and deterministic-replay literature (Ch. 37, 74 references) — capture-and-replay through a deterministic core (§75.4.5).
- Ch. 71 (binary logging — the same off-path-writer discipline), Ch. 53 (feed sequencing/A-B/gap recovery), Ch. 74 (the deterministic state machine), Ch. 48 (io_uring).
- Regulatory record-keeping requirements (e.g. MiFID II / SEC/CAT order-and-market-data reconstruction) — why complete, timestamped, long-retention capture is mandatory (§75.1).

## 75.8 Additional Reading

- Talks and write-ups on market-data capture, packet capture at line rate, and trading-system journaling — lossless capture and replay in production (§75.4).
- The event-sourcing / log-as-source-of-truth literature (Kafka, the "turning the database inside out" line of thinking) adapted to a low-latency capture journal (§75.2).
- Ch. 76 (*Production Profiling & Case Study*) — deterministic replay + latency-regression gates in CI, the direct consumer of capture; Ch. 74 (*Process Topology / State Machine*) — what replay runs through; Ch. 58 (*PTP*) — the timestamps; Ch. 48 (*io_uring*) — the write path; Ch. 71 (*Logging*) — the sibling off-path writer.
- **Appendix E** — disk/`fsync`/NVMe latency numbers (why capture can't be synchronous); **Appendix F** — WAL/journal/capture/replay glossary.

---

*Next: Ch. 76 — Production Profiling & End-to-End Case Study, the capstone tying the book together: continuous performance monitoring and regression detection in production, a full tick-to-trade walkthrough touching every Part, and deterministic replay of captured market data (Ch. 74–75) through the core with latency-regression gates in CI.*
