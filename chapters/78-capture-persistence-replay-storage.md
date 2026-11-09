# Part XII — Observability & Operations in Production

# Chapter 78 — Capture, Persistence & Replay Storage

> **Prerequisites:** Ch. 57, 60 (NIC timestamping / PTP — nanosecond, wire-accurate capture times), Ch. 73 (zero-overhead logging — the same off-hot-path-writer, binary-record discipline applied to ticks/orders), Ch. 49 (io_uring — async writes to disk), Ch. 77 (the deterministic state machine — what you replay captures *into*), Ch. 34/37 (SPSC ring / batching — producer→writer hand-off).
>
> **Leads into:** Ch. 81 (the case study — deterministic replay + latency-regression gates in CI). The penultimate chapter: recording everything that happened, losslessly and off the hot path, so the deterministic core (Ch. 77) can be replayed for debugging, research, and compliance.

---

## 78.1 Why it matters: deterministic replay, research, and compliance

Chapter 77 built a deterministic state machine: feed it the same input sequence and it produces the same outputs, bit-for-bit. That property is worthless unless you **captured the input sequence** — every inbound tick, every timer fire, every fill, every config change, in order, with accurate timestamps. Capture is what turns "deterministic in principle" into "replayable in practice," and it serves three demanding masters at once. **Debugging:** a strategy did something wrong at 09:31:42.000123456 — with a capture you *replay the exact session* through the core (Ch. 77) and watch it happen in a debugger, deterministically, as many times as you like, instead of guessing from logs. **Research/backtesting:** a new strategy must be evaluated against *real* historical market data — the capture *is* the backtest input, and replaying it through the strategy gives a faithful "what would we have done" (far better than synthetic or vendor-aggregated data, which lacks the exact timing and microstructure). **Compliance:** regulators require firms to reconstruct what they did and why — a complete, time-stamped, tamper-evident record of market data in and orders out is a legal obligation, not a nicety.

The hard constraint is the one this whole book has hammered: **capture must not slow the hot path.** You are recording *everything* — a market-data feed at open can be millions of messages per second (Ch. 54 microbursts) — and writing it to disk, which costs microseconds per syscall and tens of microseconds to milliseconds when the device or writeback stalls — syscall-heavy (Ch. 48) and with exactly the fat tail Ch. 1 warns about — the antithesis of a microsecond hot path. If capture writes to disk synchronously on the path that trades, you've destroyed the latency you spent the book building. So capture obeys the exact same discipline as logging (Ch. 73): **the hot path does the cheap part — copy the raw bytes into a lock-free buffer and move on; a separate writer thread/process does the expensive part — getting bytes to disk — off the path.** Capture is zero-overhead logging's bigger sibling: same producer→ring→async-writer shape, but lossless (you can drop a log line; you cannot drop a tick you're legally required to keep) and higher-volume.

And it must be **lossless and gap-free**: a capture with holes is useless for replay (the deterministic core diverges at the gap), wrong for backtesting (missing data = wrong P&L), and non-compliant (an incomplete record). This is in tension with "don't slow the hot path" — under a microburst, the writer may not keep up, and you must *never* block the hot path to wait for it, yet you must *not* lose data. Resolving that tension (large buffers, fast storage, back-pressure that doesn't reach the hot path, and capturing *off the NIC* below the application — §78.4.1) is the engineering of this chapter. It covers the append-only/WAL journal model (§78.2), measuring capture throughput and hot-path impact (§78.3), the lossless-capture / binary-format / off-path-write / gap-free / replay techniques (§78.4), and the ways storage stalls and drops bleed into the hot path (§78.5).

## 78.2 Mental model: append-only/WAL journals; time-indexed storage

Capture is an **append-only journal**: events are written once, in arrival order, never modified — the same write-ahead-log (WAL) discipline databases use for durability and recovery, applied to the market/order stream:

```
   HOT PATH                         CAPTURE RING (Ch.34)        WRITER (off-path)              STORAGE
   ────────                         ───────────────────         ────────────────              ───────
   on each event:                   ┌──────────────────┐        drain (batched, Ch.37)         append-only journal
     • timestamp (NIC/PTP, Ch.60)   │ ev ev ev ev ev …│  ───►   • write sequentially            ┌─[seq, ts, bytes]─┐
     • copy raw bytes + seq to ring │ (lock-free SPSC) │        • io_uring/O_DIRECT (Ch.49)      │ append, append,  │
     • bump producer seq            └──────────────────┘        • fsync/barrier for durability   │ append … (WAL)   │
     └─ return (no disk, no syscall)                            └─ never touches the hot path     └─time-indexed────┘
```

The model's properties:

- **Append-only / write-ahead.** Events are appended in order; nothing is updated in place. Append-only writes are *sequential* (the fastest disk access pattern, even on NVMe — §78.4.3), recovery-friendly (replay the log forward), and tamper-evident *if* you hash-chain the records and periodically sign the chain head (or store to WORM media) — append-only is the precondition; the chain/signature is what detects an edit. This is the write-ahead-log idea ("persist before you act", generalized): the journal *is* the durable truth.
- **Each record is self-describing and sequenced:** a monotonic **sequence number** assigned by the Ch. 77 shell — the *sequencer* — to every input before the core sees it (for gap detection — §78.4.4; it is this sequenced stream, not the raw sources, that replay feeds back), an accurate **timestamp** (NIC hardware / PTP — Ch. 57, 60, so the time is *wire-accurate*, not when software happened to look), the **raw bytes** (captured losslessly — §78.4.1), and enough framing to parse it back. Compact binary (§78.4.2), not text.
- **Time-indexed.** The journal is indexed by time (and sequence) so replay and research can seek to "09:31:42" or "sequence N" without scanning from the start — periodic index checkpoints / snapshots (§78.4.4) bound the seek and the replay-from cost (ties Ch. 77's snapshot-bounded recovery).
- **Two capture altitudes.** You can capture at the **line/wire level** (every packet off the NIC, below the application — §78.4.1, the most faithful and the compliance gold standard) and/or at the **application level** (normalized events, the inputs the Ch. 77 core actually consumed). Wire capture replays the *whole stack*; application capture replays the *core*. Production systems often do both: wire for compliance/forensics, application for fast deterministic core replay.

The mental shift mirrors Ch. 73: **capturing an event is "append a record to a journal," and the journal-writing is somebody else's job, off the path.** The hot path timestamps and enqueues; the journal is built off-path, durably, losslessly, in order.

## 78.3 Measure it: capture throughput and hot-path impact

*Toolchain and machine: Appendix L.3.*

Two things to measure, and they pull against each other: **hot-path impact** (capture must add ~nothing to tick-to-trade) and **sustained lossless throughput** (the writer must keep up with the worst burst without dropping). Measure both, under a market-open-style microburst (Ch. 54).

**Hot-path impact** — the cost the trading thread pays to capture an event (identical in spirit to Ch. 73's §73.3):

| Capture approach | Hot-path cost per event | Lossless under burst? | Verdict |
|---|---|---|---|
| **Synchronous write to disk** (`write()`/`fwrite` on the path) | **µs–ms** (syscall + I/O stall, Ch. 48) | only if disk keeps up (it won't) | ✗ destroys the hot path |
| **App-level: copy raw bytes + seq to SPSC ring** (§78.4.1-2) | **~5–20 ns** for a ≤64 B message record (a few stores, Ch. 34); ~50–150 ns for a full MTU-sized datagram — the copy scales with bytes, so capture per message where the hot path pays it | depends on ring size + writer speed (§78.4.3) | ✓ the in-process capture path |
| **Off-NIC / off-host capture** (tap, mirror port, SmartNIC — §78.4.1, Ch. 72) | **~0 on the application** (capture happens *below/beside* the app) | depends on the capture appliance | ✓ best for compliance/lossless wire capture |

**Sustained throughput & loss** — drive the worst burst and measure (a) whether the ring ever fills (→ drop or back-pressure, §78.5), (b) the writer's sustained MB/s vs the burst's peak, (c) end-to-end durability latency (event → durably on disk). Harness: a producer thread enqueueing fixed-size records at a controlled burst rate into the SPSC ring, the §78.4.3 writer draining with `io_uring` (`io_uring_prep_write_fixed`, registered buffers, one `io_uring_submit` per batch) onto an `O_DIRECT`, preallocated file; record the ring's high-water mark and drop count per burst, the writer's MB/s from the completion offsets, and event → completion latency (`rdtscp` in the record vs the CQE time) as a histogram (Ch. 3); `iostat -x 1` for device queue depth. Representative shape (NVMe, Linux 6.x, liburing 2.5, io_uring, batched — figures pending real runs): sequential batched writes sustain **multiple GB/s**, enough for a full normalized feed, *if* the ring is large enough to absorb the burst peak above the disk's sustained rate while the writer catches up in the troughs.

The lessons:

- **Synchronous capture is the same catastrophe as synchronous logging** (Ch. 73) — a `write()` on the path is µs-to-ms of syscall + stall. Never.
- **In-process capture is the Ch. 73 ring-write cost (~ns)**; the engineering is all in the *writer* keeping up (§78.4.3) and the *buffer* absorbing the burst (§78.5).
- **Off-NIC / off-host capture has ~zero application cost** because it happens *below* the app (a tap, a switch mirror port, a SmartNIC/DPU — Ch. 72) — the gold standard for *lossless wire capture* and compliance, since it can't be slowed by or lost to application back-pressure (it's not in the application at all). Many firms capture at the wire *and* in the app for the two replay altitudes (§78.2).
- **"Lossless" is a measured claim under the worst burst**, not a hope — size the ring for `(peak_rate − sustained_disk_rate) × burst_duration` and prove no drops at open (§78.5).

## 78.4 Techniques

### 78.4.1 Lossless line capture and nanosecond packet timestamps

The most faithful capture is **at the wire**, below the application, with hardware timestamps:

- **Capture off the NIC / off the host.** A network **tap**, a switch **mirror/SPAN port**, NIC **RX capture**, or a **SmartNIC/DPU** (Ch. 72) records every packet *without* the application doing the writing — so capture can't be slowed by, or lost to, application back-pressure (§78.3). This is the compliance gold standard: a complete, independent record of exactly what was on the wire. Tools/stacks: kernel-bypass capture (Ch. 64), `AF_PACKET`/`PF_RING`, or a dedicated capture appliance. Formats: `pcapng` (per-packet nanosecond timestamps, interface and drop-count blocks — the interchange format every tool reads; `pcap` only microseconds unless the nanosecond link type is used) for the wire capture, and the chapter's own compact journal (§78.4.2) for the normalized stream; `tcpdump -j adapter_unsynced`/`--time-stamp-precision=nano` and `tshark` read the hardware stamps back.
- **Nanosecond hardware timestamps (Ch. 57, 60).** Stamp each packet with the **NIC hardware timestamp**, disciplined by **PTP** (Ch. 60) so times are accurate *and comparable across hosts* (wire-to-wire, tick-to-trade reconstruction — Ch. 60). Software timestamps (when the app looked) are both less accurate and perturbed by load; hardware/PTP timestamps are the truth you replay and audit against.
- **Lossless = capture below the lossy layer.** The reason wire capture is lossless is that it sits *below* the place drops happen (application back-pressure, ring-full). Combined with adequate NIC RX rings and capture buffers (Ch. 54's microburst tuning), off-NIC capture records the burst the application itself might shed.
- **Capture the redundant feeds (Ch. 54 §54.2.3, Ch. 55).** Record *both* A/B lines — replay and gap-recovery (§78.4.4) need both, and a gap on one line is filled from the other.

### 78.4.2 Compact binary log formats

The record format is **compact binary**, for the same reasons and with the same machinery as Ch. 73's binary logging:

- **Raw bytes + minimal framing.** Store the original wire bytes (lossless — you can re-decode with any future parser, important for compliance and research) plus a small fixed header: sequence number, hardware timestamp (Ch. 60), length, source/feed ID, and a record type. No text, no per-field formatting on the path (Ch. 73) — just the bytes and a header. The record and the writer that gets it to disk (§78.4.3), in one listing:

```cpp
// journal_writer.cpp — the capture writer: drain the SPSC ring into 4 KiB-aligned batches, submit with io_uring to an
// O_DIRECT, preallocated file. Build: g++ -O2 -std=c++20 journal_writer.cpp -o journal_writer -luring   (liburing 2.5)
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <liburing.h>

struct RecHdr { std::uint64_t seq, hw_ts_ns; std::uint32_t len; std::uint16_t source; std::uint8_t type, ver; };   // 24 B
struct Ring { static constexpr std::size_t N = 1 << 24; alignas(64) std::atomic<std::uint64_t> head{0}; alignas(64) std::atomic<std::uint64_t> tail{0}; alignas(64) std::uint8_t buf[N]; };
extern Ring* g_capture;                                           // the hot path appends RecHdr + raw bytes (Ch. 73's producer)

int main(int argc, char** argv) {
    constexpr std::size_t kBlock = 4096, kBatch = 1 << 20, kDepth = 8;    // O_DIRECT: addr, offset AND length multiples of
    int fd = open(argc > 1 ? argv[1] : "capture.bin", O_WRONLY | O_CREAT | O_DIRECT, 0600);   //  the logical block (BLKSSZGET)
    posix_fallocate(fd, 0, 64ull << 30);                          // preallocate: no block allocation on the write path
    io_uring ring; io_uring_queue_init(kDepth, &ring, 0);
    io_uring_register_files(&ring, &fd, 1);
    void* batches[kDepth]; iovec iov[kDepth];
    for (auto i = 0u; i < kDepth; ++i) { batches[i] = std::aligned_alloc(kBlock, kBatch); iov[i] = {batches[i], kBatch}; }
    io_uring_register_buffers(&ring, iov, kDepth);                // registered buffers: pinned once, no per-write map
    std::uint64_t file_off = 0, inflight = 0; unsigned slot = 0;
    for (;;) {
        io_uring_cqe* cqe;                                            // reap first: durability watermark = highest contiguous
        while (inflight && io_uring_peek_cqe(&ring, &cqe) == 0) {    //   completed offset (publish it for §78.4.4 readers)
            if (cqe->res < 0) { /* count + alert: a failed write is a gap in the journal (§78.5) */ }
            io_uring_cqe_seen(&ring, cqe); --inflight;
        }
        std::uint64_t t = g_capture->tail.load(std::memory_order_relaxed), h = g_capture->head.load(std::memory_order_acquire);
        if (h - t < kBatch || inflight == kDepth) { usleep(50); continue; }   // or a time-based flush for a quiet feed
        std::size_t n = kBatch & ~(kBlock - 1);                      // whole blocks only; a partial tail block waits for the next batch
        for (std::size_t i = 0; i < n; ++i) static_cast<std::uint8_t*>(batches[slot])[i] = g_capture->buf[(t + i) & (Ring::N - 1)];
        g_capture->tail.store(t + n, std::memory_order_release);    // ring space is free once COPIED, not once durable
        io_uring_sqe* sqe = io_uring_get_sqe(&ring);
        io_uring_prep_write_fixed(sqe, 0, batches[slot], unsigned(n), file_off, int(slot));
        io_uring_sqe_set_flags(sqe, IOSQE_FIXED_FILE); io_uring_sqe_set_data64(sqe, file_off);
        io_uring_submit(&ring); file_off += n; ++inflight; slot = (slot + 1) % kDepth;
    }
}
```

- **Self-describing and versioned.** The format carries a version and enough framing to parse it back years later (compliance retention is long). Pair the journal with the *schema*/decoder (and the strategy/config versions that consumed it — §76.4.2) so a replay is fully reproducible.
- **Compact = cheaper everywhere.** Binary records are small → less disk bandwidth (helps the writer keep up, §78.4.3), less storage (retention is expensive at feed volumes), faster to read back for replay. This is the Ch. 73 win at capture scale.
- **Optionally compress off-path.** Compression (for retention) happens on the writer/archival side (off the hot path), never on the capture path — and often *after* the capture is durable, during compaction (§78.4.4).

### 78.4.3 Getting bytes to disk off the hot path (async, io_uring, `O_DIRECT`, NVMe, batching)

The writer's job: drain the ring and get bytes durably to storage **fast enough to keep up**, entirely off the hot path. The toolkit:

- **A dedicated writer thread/process** on a housekeeping core (Ch. 42), draining the capture ring (Ch. 34), **batched** (Ch. 37 — accumulate many records, write them in one large sequential I/O to amortize syscall and seek cost). Batching is what turns millions of tiny events into a few big efficient writes.
- **io_uring (Ch. 49)** for asynchronous, batched, low-overhead submission — submit many writes without a syscall each, reap completions asynchronously. The modern way to sustain capture bandwidth without the writer stalling.
- **`O_DIRECT`** to bypass the page cache for large sequential capture writes — avoids double-buffering: the extra copy into the page cache streams through the shared L3 (Ch. 7, 58) and burns memory bandwidth the hot cores share, and dirty-page writeback throttling can stall the writer; `O_DIRECT` DMA's straight from the aligned user buffer. The capture stream is write-once, so caching it is pure pollution. `O_DIRECT` requires buffer address, file offset *and* length to be multiples of the device's logical block size (512 B / 4 KiB — `ioctl(BLKSSZGET)` or `statx`), hence the 4 KiB-aligned batch buffers in the §78.4.2 listing; `posix_fallocate` the file up front so no block allocation happens on the write path, and choose the sync mode deliberately — `O_DSYNC` per batch for a durability watermark, or periodic `fdatasync` with the watermark published to readers (§78.4.4).
- **NVMe / fast sequential storage.** Append-only writes are *sequential* (§78.2), the pattern NVMe and SSDs handle best (GB/s, §78.3). Provision storage for the *peak sustained* feed rate, and put capture on its own device so it never contends with anything the hot path touches (§78.5).
- **Durability vs latency of the journal.** Decide how durable each record must be before you "trust" it: an `fsync`/write barrier per batch (durable but slower) vs relying on the OS/device to flush (faster, small loss window on power failure). For compliance you need durability; tune the batch/fsync granularity so durability latency is bounded *without* the writer falling behind. None of this is on the hot path — but it bounds how much capture sits in volatile buffers at any instant.

### 78.4.4 Sequencing and gap-free persistence; retention and compaction

A capture is only useful if it's **complete and ordered** — and manageable over long retention:

- **Sequence every record; detect gaps.** A monotonic sequence number per record (and the *feed's own* sequence numbers from the protocol — Ch. 54) lets you *prove* the capture is gap-free, and detect/locate any hole. On a gap in one feed line, fill from the redundant line (Ch. 54–55 A/B and recovery, §78.4.1); an unrecoverable gap is flagged (replay can't be trusted across it — §78.5).
- **Gap-free persistence under burst.** The lossless guarantee (§78.3) plus sequence-checking: size buffers for the burst, capture off-NIC where possible, and *verify* no sequence holes in the persisted journal. "We think it's complete" isn't good enough for replay or compliance — *check* the sequence.
- **Time-indexing and snapshots.** Periodically write an **index checkpoint** and a **state snapshot** (the Ch. 77 core's state at a sequence point) so replay/seek starts from a recent checkpoint, not session-start — bounding both seek time and replay-from cost (ties Ch. 77.3's snapshot-bounded recovery).
- **Retention and compaction.** Feed-volume capture is large; manage it with tiered retention (hot recent capture on fast NVMe, older archived/compressed off to cheaper storage), compaction (merge/compress sealed journal segments off-path), and a retention policy that meets the *compliance* minimum (often years) while controlling cost. Compaction runs off the hot path and off the capture device.

### 78.4.5 Feeding captures into replay, simulation and backtests

Capture's payoff is replaying it through the deterministic core (Ch. 77) — for debugging (Ch. 5), regression gating, backtesting and compliance — and the *consumers* of a capture are **Ch. 81's** subject: deterministic replay and performance bisection (§81.4.2), simulation harnesses that model the venue's reactions where an open-loop replay cannot (§81.4.3), and the CI latency-regression gate (§81.4.1). What the capture side owes them is this section's content. **Seekability:** a file layout that can seek by *time* and by *sequence* (§78.2's time index), so a replay can start at 09:31:42 or at sequence 1 048 576 without scanning the day. **The replay clock:** captured timestamps (Ch. 60) drive the replayer's notion of time — never the wall clock — so timing-dependent paths (timeouts, warming, the reorder window of Ch. 54) behave as they did live, and the replayer can run faster than real time without changing the outcome. **Two altitudes, labelled:** a wire-level capture (§78.4.1) replays through the whole stack (decode → decision) while an application-level capture replays only the core, and the file header must say which it is so a replayer never feeds one to the other. **Completeness:** the gap-free, sequenced persistence of §78.4.4 is what makes bit-for-bit replay (Ch. 77.2.2) possible at all — a capture with a hole is a replay that diverges.

## 78.5 Pitfalls & anti-patterns: dropping under load; storage stalls bleeding into the hot path

- **Synchronous capture on the hot path (the cardinal sin).** A `write()`/`fsync` on the trading thread is µs-to-ms (Ch. 48, §78.3) — capture, like logging (Ch. 73), is *always* a ring-enqueue on the path and an off-path writer. Never write to disk from the hot path.
- **Storage stall bleeding into the hot path.** Even with an async writer, shared resources leak the stall back: the capture device shared with something the hot path touches, page-cache pressure from buffered capture evicting the hot working set (Ch. 7/15 — use `O_DIRECT`, §78.4.3), the writer thread on a hot core (Ch. 42), or an `fsync` that stalls a shared I/O queue. Isolate capture storage and the writer completely (§78.4.3).
- **Dropping under load and calling it lossless.** The ring fills during the open burst (Ch. 54), the writer can't keep up, and capture silently drops — a hole that breaks replay (the core diverges, Ch. 77), corrupts backtests, and violates compliance. Size the ring for the burst above sustained disk rate, capture off-NIC where you can (§78.4.1), and **verify gap-free by sequence** (§78.4.4) — don't assume.
- **Back-pressure reaching the hot path.** "Don't drop" must *never* become "block the hot path until the writer drains" (that's the §78.3 catastrophe inverted). The hot path never waits on capture; you absorb bursts with buffer + fast storage + off-NIC capture, and if truly overwhelmed you drop-and-flag (or shed at the capture layer, not the trading layer) — but you never stall the trade to capture it.
- **Software timestamps for a wire record.** Stamping capture with when *software looked* (not the NIC/PTP hardware time, Ch. 57, 60) gives inaccurate, load-perturbed times — useless for wire-to-wire reconstruction and weak for compliance. Use hardware/PTP timestamps (§78.4.1).
- **Lossy/aggregated capture for backtesting.** Capturing only top-of-book, or vendor-aggregated data, or sampling — then backtesting on it and trusting the result. The microstructure and exact timing are gone; the backtest lies. Capture the *full, raw, timestamped* feed (§78.4.1-2).
- **A capture you can't decode later.** Storing bytes without the schema/version, or in a format tied to a since-changed parser — years later (compliance retention) you can't read it. Version the format and archive the decoder + strategy/config versions (§78.4.2, Ch. 76).
- **No snapshots → unbounded replay/seek.** Replaying from session-start every time because there are no checkpoints (§78.4.4) — replay and recovery (Ch. 77.3) become unbounded. Snapshot periodically.
- **Non-deterministic replay.** Replay diverging from production because the core isn't actually deterministic (Ch. 77.5 — a hidden `clock_gettime`, unordered inputs) or the capture didn't record *all* inputs (a timer, a config change — Ch. 76). Capture *every* input the core consumes, in order; assert replay matches the golden record (§78.4.5).

## 78.6 Exercises & checklist

**Exercises:**

1. **Capture-cost measurement.** Compare hot-path cost of (a) synchronous `write()` capture and (b) ring-enqueue + async io_uring writer (§78.4.3) under a microburst (Ch. 54). Reproduce the §78.3 spread; confirm (b) is ~ns on the path.
2. **Prove lossless under burst.** Drive a burst whose peak exceeds sustained disk rate; size the ring for `(peak − sustained) × duration` and verify the persisted journal is **gap-free by sequence** (§78.4.4). Then shrink the ring and show drops appear — and that your gap-check *detects* them (not assumes).
3. **Wire vs app capture replay.** Capture both at the wire (tap/`AF_PACKET`, §78.4.1) and at the application level; replay each through the deterministic core (Ch. 77) and confirm both reproduce the session — wire capture exercising decode+decision, app capture exercising the core (§78.4.5).
4. **Hardware timestamps.** Capture with software timestamps and with NIC/PTP hardware timestamps (Ch. 57, 60); compare their accuracy and load-sensitivity. Reconstruct a wire-to-wire latency from the hardware timestamps (Ch. 60).
5. **Deterministic-replay regression gate.** Build a golden capture + golden output (Ch. 77); in CI, replay the capture through the current build and assert bit-identical outputs and no per-stage latency regression (§78.4.5, Ch. 81). Break the core's determinism (add a `now()`) and watch the gate catch it.
6. **`O_DIRECT` and page-cache pollution.** Capture with buffered I/O vs `O_DIRECT` (§78.4.3) and measure the hot path's cache/TLB miss rate (Ch. 2) during heavy capture — show buffered capture pollutes the hot working set and `O_DIRECT` doesn't.

**Checklist:**

- [ ] The hot path **captures by ring-enqueue only** (~ns, Ch. 34/73) — **never** a disk write/`fsync` on the path (§78.3, §78.5).
- [ ] Capture is **lossless and gap-free**, *verified by sequence number* under the worst burst; ring sized for `(peak − sustained) × burst` and/or captured **off-NIC** (tap/mirror/DPU, Ch. 72) (§78.4.1, §78.4.4).
- [ ] Back-pressure **never reaches the hot path** — bursts absorbed by buffer + fast storage + off-NIC capture; overwhelmed → drop-and-flag, never stall the trade (§78.5).
- [ ] Records are **compact binary** (raw bytes + seq + **hardware/PTP timestamp**, Ch. 57, 60), **versioned/self-describing**, archived with their decoder + strategy/config versions (§78.4.2, Ch. 76).
- [ ] The writer is a **dedicated off-core thread/process** (Ch. 42) using **batched io_uring** (Ch. 49) + **`O_DIRECT`** + **NVMe**, on **isolated storage**; durability (`fsync` granularity) is tuned without falling behind (§78.4.3, §78.5).
- [ ] The journal is **append-only/WAL**, **time-indexed**, with periodic **snapshots/checkpoints** bounding replay/seek; retention + off-path compaction meet the **compliance** minimum (§78.2, §78.4.4).
- [ ] Captures feed **deterministic replay** (Ch. 77), **simulation**, **backtests**, and a **CI regression gate** (outputs match golden + no latency regression) (§78.4.5, Ch. 81); **every** core input (incl. timers/config) is captured in order (§78.5).

## 78.7 References

- The write-ahead logging / journaling literature (databases, filesystems) — append-only durability and recovery, the model for the capture journal (§78.2).
- `liburing`/io_uring documentation (Ch. 49 references) and `O_DIRECT`/NVMe performance guidance — async, high-throughput, off-path persistence (§78.4.3).
- PTP / NIC hardware timestamping references (Ch. 57, 60) — accurate, cross-host capture times (§78.4.1).
- The LMAX/event-sourcing and deterministic-replay literature (Ch. 37, 77 references) — capture-and-replay through a deterministic core (§78.4.5).
- Ch. 73 (binary logging — the same off-path-writer discipline), Ch. 54 (feed sequencing/A-B/gap recovery), Ch. 77 (the deterministic state machine), Ch. 49 (io_uring).
- Regulatory record-keeping requirements (e.g. MiFID II / SEC/CAT order-and-market-data reconstruction) — why complete, timestamped, long-retention capture is mandatory (§78.1).

## 78.8 Additional Reading

- Talks and write-ups on market-data capture, packet capture at line rate, and trading-system journaling — lossless capture and replay in production (§78.4).
- The event-sourcing / log-as-source-of-truth literature (Kafka, the "turning the database inside out" line of thinking) adapted to a low-latency capture journal (§78.2).
- Ch. 81 (*Production Profiling & Case Study*) — deterministic replay + latency-regression gates in CI, the direct consumer of capture; Ch. 77 (*Process Topology / State Machine*) — what replay runs through; Ch. 60 (*PTP*) — the timestamps; Ch. 49 (*io_uring*) — the write path; Ch. 73 (*Logging*) — the sibling off-path writer.
- **Appendix E** — the syscall and page-fault rows (the floor under why capture can't be synchronous; NVMe write ~10–30 µs and `fdatasync` ~20–100 µs are measured in §78.3); **Appendix F** — WAL/journal/capture/replay glossary.

---

*Next: Ch. 79 — Host Health Telemetry: Proving the Quiet Box Stayed Quiet, the first of two chapters that watch the platform the way Ch. 74 watches the application: the isolation invariants of Ch. 42–46 (involuntary switches, migrations, faults, the IPI rows, the tick) sampled every second from a housekeeping core, plus frequency/thermal throttling, SMIs, THP/compaction, the clocksource and the hardware's own error counters — with the fingerprint of every way a tuned box drifts, and the proof that the sampler itself never touches the hot core.*
