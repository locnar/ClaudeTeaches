# Part IX — Market Data, NIC & Fabric

# Chapter 59 — Precise & Scheduled Transmission

> **Prerequisites:** Ch. 58 (PTP / hardware timestamping — a scheduled send needs a disciplined clock), Ch. 53 (microbursts — the thing precise pacing avoids creating), Ch. 55 (NIC features — hardware LAUNCHTIME lives here), Ch. 51 (socket options), Ch. 17 (the timestamp counter).
>
> **Leads into:** Ch. 65 (transports that pace their own sends), Ch. 64 (fabric congestion that pacing eases). The send-side dimension the book has ignored: not *what* to send, but *exactly when*.

---

## 59.1 Why it matters: "send now" is not the only option

Every send path in the book so far answers one question — *how fast can I get this packet onto the wire?* — and the answer is always "now, as fast as possible." But there is a second question the hot path often needs and the sockets API historically couldn't answer: *at exactly what time should this packet leave?* Precise, scheduled transmission — telling the NIC "put this packet on the wire at nanosecond T" — is a capability that changes several things a trading system cares about, and it's an area the book hasn't touched at all.

Three motivations. First, **avoiding self-inflicted microbursts** (Ch. 53). When you send a batch of orders or messages "as fast as possible," you create a burst — packets back-to-back at line rate — that can overwhelm a downstream switch buffer (Ch. 60), your own NIC's queue, or the receiver, adding queuing jitter to your *own* traffic. **Pacing** the sends (spreading them over time to a smooth rate) trades a microsecond of deliberate spacing for the removal of burst-induced jitter — often a net latency win at the tail (Ch. 1). Second, **coordinated timing**: sending to multiple venues at a precisely coordinated instant, or releasing an order at an exact time relative to a market event, requires *scheduling* the send, not racing it. Third, **determinism**: a system that sends at scheduled times has a *predictable* egress pattern, which is easier to reason about, measure (Ch. 76), and keep jitter-free than one that bursts whenever work is ready.

The enabling technology arrived in the Linux kernel and NIC hardware as the **`SO_TXTIME` / ETF (Earliest TxTime First) / hardware LAUNCHTIME** stack, built on the **EDT (Earliest Departure Time)** model and the **TSN (Time-Sensitive Networking)** standards. You attach a transmit timestamp to a packet, and the qdisc + NIC hold it until that time, then put it on the wire — with hardware precision disciplined by PTP (Ch. 58). This is a genuinely different send model: the packet's *departure time* is a first-class property. This chapter explains the scheduled-transmission model (§59.2), measures send-time accuracy and pacing precision (§59.3), details `SO_TXTIME`/ETF/LAUNCHTIME, pacing, and TSN (§59.4), and warns about the clock discipline and deadline pitfalls (§59.5). It's the "send at exactly when, not just now" chapter.

## 59.2 Mental model: EDT, `SO_TXTIME`, qdiscs, and hardware LAUNCHTIME

The core shift is that a packet carries a **departure time**, and the egress path honors it:

```
   "send now":         app -> socket -> qdisc -> NIC -> wire   (as fast as possible)
   scheduled send:     app -> [attach TxTime T via SO_TXTIME] -> ETF qdisc (holds until ~T)
                                                              -> NIC hardware LAUNCHTIME (puts on wire AT T)
                              ▲ T is on the PTP-disciplined clock (Ch. 50)
```

The layers, from the EDT model down to the wire:

- **The EDT (Earliest Departure Time) model.** Modern Linux egress is built around each packet having an *earliest departure time* — a timestamp saying "don't send before this." Pacing, rate-limiting, and scheduling all express themselves as setting each packet's EDT. It replaced the older token-bucket mental model: instead of "how many tokens do I have," it's "when is this packet allowed to leave." Both pacing (a smooth rate) and precise scheduling (an exact time) are EDT settings.
- **`SO_TXTIME` — the socket API.** You set `SO_TXTIME` on the socket and attach a per-packet transmit time (a control message with a nanosecond timestamp on a chosen clock, typically the PTP hardware clock — Ch. 58). That timestamp is the packet's requested departure time.
- **ETF (Earliest TxTime First) qdisc — the software scheduler.** The ETF qdisc holds each packet until (near) its TxTime, releasing it to the NIC just in time, in departure-time order. It can operate in *software* (the kernel releases at the right time) or hand off to *hardware* offload. A delta parameter controls how early it hands packets to the NIC.
- **Hardware LAUNCHTIME — the NIC.** NICs with TSN/LAUNCHTIME support take the departure time and put the packet on the wire at *that hardware time* — nanosecond precision, immune to software jitter (Ch. 41) between the qdisc and the wire. This is the precise, deterministic layer: the NIC's own clock (PTP-disciplined, Ch. 58) gates transmission.
- **TSN (Time-Sensitive Networking).** The IEEE 802.1 standards family — the **time-aware shaper (802.1Qbv)** and related — that makes Ethernet deliver on scheduled, deterministic time windows. LAUNCHTIME and the whole scheduled-transmission stack are the endpoint side of TSN; the fabric side (Ch. 60) can honor time-aware scheduling too.

The mental model: **a scheduled send is a packet with a departure timestamp that the qdisc and NIC honor, gated by a PTP-disciplined clock — turning "send" from an immediate action into a time-addressed one.** Pacing is the special case where you set a stream of departure times at a steady interval; precise scheduling is setting a specific one. Everything depends on the clock (Ch. 58): a departure time is meaningless without an accurate, disciplined clock to measure it against.

## 59.3 Measure it: send-time accuracy and pacing precision

The measurements are about *accuracy* — how close the actual on-wire time is to the requested time — and require hardware timestamps (Ch. 55, 58) to observe the true wire time.

- **Send-time accuracy.** Request a departure time T (via `SO_TXTIME`), capture the actual on-wire timestamp (NIC hardware TX timestamp, Ch. 55, 58), and measure the error (actual − requested). Compare software ETF vs hardware LAUNCHTIME:

| Mechanism | Send-time accuracy (error vs requested) | Notes |
|---|---|---|
| Best-effort "send now" | N/A (no target); jitter from software + qdisc | the baseline — no time control |
| **ETF qdisc (software)** | ~microseconds | software release timing; subject to some host jitter (Ch. 41) |
| **Hardware LAUNCHTIME** | ~tens of nanoseconds | NIC hardware gates the wire; near-immune to software jitter |

  The lesson: **hardware LAUNCHTIME is the precise option** (tens of ns), software ETF is looser (microseconds) but needs no special NIC. Choose by how precise the schedule must be.
- **Pacing precision and its effect on the tail.** Pace a stream of sends at a target rate (setting evenly-spaced departure times) and measure (a) the inter-packet spacing accuracy on the wire and (b) the effect on downstream jitter — a paced stream should show a *flatter* latency distribution at the receiver/switch (Ch. 60) than a bursted one, because it doesn't overflow buffers (Ch. 53). Quantify the trade: pacing adds a small, deliberate delay per packet but removes burst-induced queuing jitter.
- **The single-message cost.** Measure the latency of *one* scheduled send vs one immediate send — scheduling adds a small fixed cost (attaching the timestamp, the qdisc). For the single latency-critical order where you want it out *now*, that cost is pure overhead (§59.5); scheduling pays off for *coordinated* or *paced* sends, not the race-it-out single order.

The lessons: measure accuracy with hardware timestamps (software timing can't see the true wire time); hardware LAUNCHTIME gives nanosecond scheduling, software ETF microsecond; pacing flattens the downstream tail by not bursting; and scheduling has a per-send cost that makes it a *coordination/pacing* tool, not something to put on the race-it-out single-order path (§59.5). All of it depends on a disciplined clock (Ch. 58) — the accuracy is only as good as the clock the times are measured against.

## 59.4 Techniques

### 59.4.1 `SO_TXTIME` + ETF + hardware LAUNCHTIME

- **Set up the ETF qdisc and clock.** Configure the ETF qdisc on the transmit queue (`tc qdisc ... etf clockid CLOCK_TAI delta ... offload`), tied to the PTP-disciplined clock (Ch. 58 — typically `CLOCK_TAI` synced to the PHC). The `delta` sets how early the qdisc hands packets to the NIC; `offload` enables hardware LAUNCHTIME where the NIC supports it. This is a one-time setup (Appendix C territory).
- **Attach a departure time per packet.** Set `SO_TXTIME` on the socket; for each send, attach a `SCM_TXTIME` control message with the target nanosecond timestamp. The qdisc/NIC hold the packet until (near) that time. The timestamp must be on the *same clock* the qdisc is configured for (Ch. 58) — a mismatch sends at the wrong time (§59.5).
- **Choose software vs hardware.** Hardware LAUNCHTIME (offload) for nanosecond precision on a supporting NIC; software ETF where the NIC lacks it and microsecond precision suffices (§59.3). Verify the NIC's LAUNCHTIME support and PTP discipline before relying on hardware precision.
- **Handle the deadline.** If a packet's departure time has already passed when it reaches the qdisc (you scheduled too tight, or the host was late — Ch. 41), the ETF qdisc *drops* it (by default) rather than sending late. Schedule with enough lead time, and handle the drop/late case explicitly (§59.5).

### 59.4.2 Pacing: `SO_MAX_PACING_RATE`, FQ, and the EDT model

- **Rate pacing with `SO_MAX_PACING_RATE`.** For a smooth egress rate (rather than exact times), `SO_MAX_PACING_RATE` on the socket, honored by the **FQ (Fair Queue) qdisc** via the EDT model, spaces packets to the target rate — no bursting. Use it to smooth bulk egress (capture upload — Ch. 75, snapshot/replay streams — Ch. 54) so they don't create microbursts that jitter the hot path's own traffic.
- **Pace to avoid self-inflicted bursts (Ch. 53).** When you have a batch to send (many orders, a snapshot), pacing it to a rate the downstream fabric (Ch. 60) and receiver can absorb smoothly *removes* the burst-induced queuing jitter (§59.3) — often a tail-latency win even though each individual packet leaves slightly later. This is the counter-intuitive lesson: sending *slower* (paced) can give a *flatter tail* than bursting.
- **EDT as the unifying model.** Both pacing (steady departure times) and scheduling (specific departure times) are EDT settings (§59.2) — the same mechanism, different time patterns. Understanding egress as "set each packet's earliest departure time" unifies rate-limiting, pacing, and precise scheduling into one model.

### 59.4.3 TSN, time-aware shaping, and coordinated sends

- **Coordinated multi-venue / multi-order sends.** To release orders to several venues at a precisely coordinated instant (or in a precise relative order), schedule each send's departure time (§59.4.1) rather than racing them out sequentially — the NIC releases them at the planned times, giving deterministic, coordinated egress that "send now" can't. Useful for strategies that must hit multiple venues simultaneously or in a controlled sequence.
- **Time-aware shaping (802.1Qbv, Ch. 60).** In a TSN fabric, the network can reserve *time windows* for classes of traffic (the time-aware shaper), so scheduled traffic gets deterministic, contention-free transmission windows end-to-end. The endpoint's LAUNCHTIME (§59.4.1) and the fabric's time-aware shaping (Ch. 60) together give scheduled, deterministic delivery across the network — the full TSN picture.
- **Deterministic egress for measurability.** A scheduled-send system has a *predictable* egress pattern, which makes the tick-to-trade measurement (Ch. 76) cleaner (you know when packets left) and the system's behavior easier to reason about and keep jitter-free. Determinism (Ch. 1, 74) extends to *when you transmit*, not just what you compute.

## 59.5 Pitfalls & anti-patterns: clock discipline and missed deadlines

- **Scheduling without a disciplined clock (the cardinal error).** A departure time is meaningless without an accurate, PTP-disciplined clock (Ch. 58) to measure it against — an undisciplined or drifting clock sends at the wrong wall-clock time, defeating coordination and making "nanosecond precision" a fiction. Scheduled transmission *requires* the Ch. 58 clock infrastructure; it's a precondition, not an add-on (§59.2).
- **Clock-ID mismatch.** The `SO_TXTIME` timestamp's clock must match the ETF qdisc's configured clock (`CLOCK_TAI` typically) and the NIC's PHC (Ch. 58). A mismatch (e.g. scheduling on `CLOCK_MONOTONIC` while the qdisc uses `CLOCK_TAI`) sends at a wildly wrong time. Align all three (§59.4.1).
- **Missed deadlines / scheduling too tight.** If a packet's departure time has passed by the time it reaches the qdisc (scheduled with too little lead, or host jitter — Ch. 41 — delayed it), ETF drops it (or sends late). Schedule with adequate lead time and handle the late/drop case; don't schedule departure times so tight that normal host jitter blows them (§59.4.1).
- **Pacing the single latency-critical order.** Applying pacing/scheduling to the *one* order you want out *now* — adding deliberate delay to the thing you're racing (§59.3). Scheduling/pacing is for *coordination* and *bulk smoothing*, not the single race-it-out order; the hot order path still sends immediately (bypass — Ch. 62, or inline — Ch. 69).
- **Assuming software ETF is nanosecond-precise.** Software ETF release timing is subject to host scheduling jitter (Ch. 41) — microsecond, not nanosecond, precision (§59.3). For nanosecond scheduling you need hardware LAUNCHTIME; don't rely on software ETF for tight coordination.
- **NIC/driver support assumed.** Hardware LAUNCHTIME, ETF offload, and TSN features vary by NIC and driver; verify support (and PTP discipline) on your hardware before designing around nanosecond scheduling (§59.4.1) — the Appendix A portability discipline applied to TSN.
- **Pacing rate mismatched to the fabric.** Pacing to a rate the downstream fabric (Ch. 60) or receiver *still* can't absorb doesn't remove the burst problem — it just moves it. Pace to a rate matched to the bottleneck's absorption (§59.4.2), measured (Ch. 60).
- **Forgetting pacing is a tail trade.** Pacing adds per-packet delay to remove burst jitter — a *tail* win but a per-packet-latency cost. On a path where per-message latency dominates and there's no burst problem, pacing is pure overhead. Apply it where bursts actually cause downstream jitter (§59.4.2).

## 59.6 Exercises & checklist

**Exercises:**

1. **Send-time accuracy.** Schedule sends at target times via `SO_TXTIME` + ETF (software) and via hardware LAUNCHTIME; measure the error against the actual NIC TX timestamp (Ch. 58) — reproduce the microsecond-vs-nanosecond gap (§59.3).
2. **Pacing flattens the tail.** Send a batch bursted vs paced (`SO_MAX_PACING_RATE`/FQ); measure the downstream latency distribution (at a switch/receiver, Ch. 60) — show pacing removes burst-induced jitter (§59.3, §59.4.2).
3. **Missed deadline.** Schedule a departure time too tight (less than host jitter) and observe ETF dropping/late-sending; add lead time and handle the case (§59.4.1, §59.5).
4. **Clock mismatch.** Schedule on the wrong clock ID and observe the send at the wrong time; align `SO_TXTIME`, qdisc, and PHC clocks (Ch. 58) and confirm accuracy (§59.5).
5. **Coordinated sends.** Schedule sends to two destinations at a coordinated instant; verify (hardware timestamps) they leave at the planned relative times, vs racing them sequentially (§59.4.3).

**Checklist:**

- [ ] Scheduled transmission runs on a **PTP-disciplined clock** (Ch. 58); the `SO_TXTIME`, ETF qdisc, and NIC PHC **clock IDs all match** (§59.5).
- [ ] **Hardware LAUNCHTIME** is used where nanosecond precision is required; **software ETF** only where microsecond suffices (§59.3, §59.4.1).
- [ ] Departure times are scheduled with **adequate lead time**; missed-deadline (drop/late) behavior is **handled explicitly** (§59.4.1, §59.5).
- [ ] **Pacing** (`SO_MAX_PACING_RATE`/FQ, EDT) smooths **bulk egress** (capture/replay — Ch. 75/54) to avoid self-inflicted microbursts (Ch. 53); paced to a rate the **fabric can absorb** (§59.4.2).
- [ ] Scheduling/pacing is **not applied to the single race-it-out order** — that still sends immediately (bypass — Ch. 62) (§59.5).
- [ ] Pacing is applied where **bursts cause measured downstream jitter** (a tail trade), not blanket (§59.4.2, §59.5).
- [ ] **NIC/driver LAUNCHTIME/ETF/TSN support** is verified on the actual hardware (§59.4.1, §59.5).
- [ ] Where end-to-end determinism is needed, endpoint LAUNCHTIME is combined with **fabric time-aware shaping** (802.1Qbv, Ch. 60) (§59.4.3).

## 59.7 References

- The Linux kernel documentation on **`SO_TXTIME`**, the **ETF qdisc** (`tc-etf(8)`), the **FQ qdisc**, and the **EDT model** (Van Jacobson's "EDT" netdev talks) — the mechanisms of §59.4.
- The **TSN / IEEE 802.1Qbv** (time-aware shaper) and 802.1AS (timing) standards — the deterministic-networking framework (§59.2, §59.4.3).
- NIC/driver documentation for **hardware LAUNCHTIME** (Intel igb/igc, and TSN-capable NICs) — hardware scheduled transmission (§59.4.1).
- Ch. 58 (PTP — the clock discipline scheduled transmission requires), Ch. 53 (microbursts — what pacing avoids), Ch. 55 (NIC TX timestamping), Ch. 60 (fabric time-aware shaping), Ch. 17 (timestamp counter).

## 59.8 Additional Reading

- Netdev and TSN community talks on `SO_TXTIME`, EDT, and time-aware shaping — the design and deployment of scheduled transmission (§59.2, §59.4).
- Van Jacobson's EDT / "bufferbloat and pacing" material — why the departure-time model replaced token buckets (§59.2, §59.4.2).
- Ch. 65 (*Transport Beyond TCP*) — transports that pace their own sends; Ch. 64 (*Lossless Ethernet*) — congestion that pacing eases; Ch. 58 (*PTP*) — the clock; Ch. 60 (*Fabric*) — end-to-end time-aware delivery; **Appendix C** — the qdisc/clock setup; **Appendix F** — EDT / LAUNCHTIME / TSN glossary.

---

*Next: Ch. 60 — Network Fabric & Switching, the hop between your NIC and the exchange: store-and-forward vs cut-through vs layer-1 switches, per-hop latency in nanoseconds, and why the switch on the tick-to-trade path is as much a latency decision as any line of code.*
