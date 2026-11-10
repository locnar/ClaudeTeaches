# Part IX — Market Data, NIC & Fabric

# Chapter 76 — Clock Synchronization & Hardware Timestamping (PTP)

> **Prerequisites:** Ch. 20 (on-host TSC/clocks — this is the cross-host sequel), Ch. 73 (NIC hardware timestamping / the PHC — the hardware this builds on), Ch. 67, 69 (the feeds whose latency you measure), Ch. 1 (latency measurement), Ch. 115-preview (end-to-end measurement).
>
> **Leads into:** Ch. 115 (end-to-end / tick-to-trade case study — true wire-to-wire latency needs synced clocks), Ch. 105 (capture — timestamped at the wire), Ch. 73 (the NIC PHC). Closes the timekeeping arc started in Ch. 20. Compliance (MiFID II / Reg NMS timestamping) ties in.

---

## 76.1 Why it matters: comparing timestamps across machines

Chapter 20 gave you a precise *on-host* clock (the TSC) — but it answers only "how long did this take *on this box*." The questions that matter most in trading are **cross-host**: "how long from when the market-data packet crossed *the exchange's* wire to when our order crossed *our* wire?" (true tick-to-trade), "did our quote or theirs arrive first? (answered per order by the post-trade join of Ch. 115 §115.4.5)", "what was the one-way latency from the exchange to us?". Answering any of these requires comparing a timestamp taken on **one machine** with a timestamp taken on **another** — and that only works if the two machines' clocks are **synchronized** to a common time, to nanosecond accuracy. Two unsynchronized clocks (each a perfectly good TSC, Ch. 20) drift relative to each other by *microseconds to milliseconds*, so differencing their timestamps yields garbage. **Clock synchronization is what makes cross-machine latency measurement meaningful** — and it's the foundation of honest tick-to-trade measurement (Ch. 115), capture/replay correlation (Ch. 105), and regulatory timestamping (MiFID II, Reg NMS).

The mechanism is **PTP (Precision Time Protocol, IEEE 1588)**: a protocol that disciplines each host's clock to a **grandmaster** clock (typically GPS-disciplined) across the network, achieving **sub-microsecond — often sub-100-nanosecond — synchronization**, vastly better than NTP's millisecond-level. The key to PTP's accuracy is **hardware timestamping in the NIC** (Ch. 73): PTP messages are timestamped by the NIC's **PTP Hardware Clock (PHC)** at the moment they cross the wire — removing the variable software/OS latency that would otherwise corrupt the measurement (the same reason hardware timestamps beat software ones for *data* packets — Ch. 73). PTP disciplines the PHC to the grandmaster; the PHC then timestamps your market-data and order packets at the wire (Ch. 73), so every packet across your fleet carries a timestamp on a *common, synchronized* time base. That's what lets you say "this packet arrived at 09:30:00.000001234 *and that's comparable to a timestamp taken on another host*."

For HFT this is both a **measurement** tool and a **compliance** requirement. As measurement: with PTP-synced hardware timestamps, you can decompose the tick-to-trade path across the feed handler, strategy, and gateway hosts and measure each hop's true wire-to-wire latency (Ch. 115) — and compare against the exchange's timestamps to know your latency *to the exchange*. As compliance: regulations **mandate** clock accuracy and timestamp granularity on trading events — MiFID II RTS 25: ≤100 µs divergence from UTC with 1 µs timestamp granularity for HFT (1 ms / 1 ms otherwise); in the US, FINRA Rule 4590: within 50 ms of NIST for broker-dealer business clocks, and the CAT NMS Plan (SEC Rule 613): 100 µs for industry members' high-precision systems — which PTP + hardware timestamping delivers and documents. This chapter explains the PTP/PHC mental model (§76.2), measures PTP offset/jitter and true wire-to-wire latency (§76.3), details PTP setup/disciplining and cross-host tick-to-trade measurement (§76.4) — including the advanced extremes (sub-nanosecond White Rabbit, PPS boundary-clock signaling, grandmaster oscillator drift, §76.4.3) — and warns about the pervasive error of using *software* timestamps where the cross-host comparison demands *hardware* ones (§76.5). It closes the timekeeping arc Ch. 20 began: on-host TSC for intervals, PTP-synced hardware timestamps for cross-host truth.

## 76.2 Mental model: PTP distribution; NIC hardware timestamps

**The problem: independent clocks drift.** Each host has its own oscillator (the TSC's source — Ch. 20); two oscillators run at *slightly* different rates, so two clocks set identically *now* diverge by microseconds within seconds, milliseconds within minutes. To compare timestamps across hosts, you must continuously **discipline** each host's clock to a common reference so they stay aligned to nanoseconds.

**PTP (IEEE 1588) — disciplining clocks across the network:**

```
   GPS / atomic ──► GRANDMASTER clock (the authoritative time source)
                        │  PTP messages (Sync, Follow_Up, Delay_Req, Delay_Resp)
                        ▼      ... timestamped IN HARDWARE by each NIC's PHC (Ch.73) ...
   network (PTP-aware switches = BOUNDARY/TRANSPARENT clocks correct for switch delay)
                        │
                        ▼
   each HOST: ptp4l disciplines the NIC's PHC to the grandmaster (sub-µs / sub-100ns)
              phc2sys disciplines the SYSTEM clock to the PHC (so software clocks agree too)
```

- **PTP exchanges timestamped messages** between the grandmaster and each slave to compute the **offset** (how far the slave's clock is from the master) and the **path delay** (network propagation), then adjusts the slave's clock. Done continuously, it tracks out drift.
- **The accuracy comes from hardware timestamping (Ch. 73).** PTP messages are timestamped by the **NIC's PHC** at the *wire* — so the measurement excludes the variable software/OS/queueing latency that would otherwise dominate (software PTP timestamps give only ~µs-ms accuracy; hardware gives ~tens of ns). The PHC is the precise hardware clock PTP disciplines.
- **PTP-aware network (boundary/transparent clocks).** Switches introduce variable delay; **boundary clocks** (the switch is a PTP node, re-distributing time) and **transparent clocks** (the switch measures and corrects for its own forwarding delay in the PTP message) keep accuracy across the network. A non-PTP-aware switch adds unmeasured jitter — HFT networks use PTP-aware switches.
- **Two disciplining steps (`ptp4l` + `phc2sys`).** **`ptp4l`** (from `linuxptp`) disciplines the **NIC PHC** to the grandmaster. **`phc2sys`** disciplines the **host system clock** (`CLOCK_REALTIME`) to the PHC — so software timestamps (`clock_gettime` — Ch. 20) are *also* aligned to the synced time, not just hardware ones. Both run for a fully-synced host.

**Hardware timestamping of *your* packets (Ch. 73).** Once the PHC is disciplined to the grandmaster, the NIC timestamps **your market-data and order packets** (`SO_TIMESTAMPING` hardware RX/TX — Ch. 73) on the *same synced time base*. So a market-data packet's hardware RX timestamp on host A and an order packet's hardware TX timestamp on host B are **directly comparable** (both on grandmaster time) — that's the foundation of cross-host wire-to-wire latency (§76.4.2).

**The clock hierarchy, recapped (ties Ch. 20):**

```
   on-host intervals (Ch.20):   rdtsc / CLOCK_MONOTONIC  — fast, monotonic, NOT cross-host comparable
   cross-host comparison (this chapter): PTP-disciplined PHC + hardware timestamps — synced, comparable
   wall-clock / compliance:     PTP-synced CLOCK_REALTIME (via phc2sys) — traceable to UTC
```

The model: **independent host clocks drift, so cross-host timestamp comparison requires PTP — which disciplines each NIC's PHC (and via phc2sys the system clock) to a GPS-grandmaster across a PTP-aware network, using NIC hardware timestamps for ns accuracy. Your packets, hardware-timestamped on the synced PHC, then carry cross-host-comparable times — enabling true wire-to-wire latency and compliant timestamps. On-host intervals stay on the TSC (Ch. 20); cross-host truth needs PTP.**

## 76.3 Measure it: PTP offset/jitter; true wire-to-wire latency

Two measurements: the **PTP synchronization quality** (how well-synced are the clocks — the offset/jitter to the grandmaster), and the **true wire-to-wire latency** it unlocks (comparing hardware timestamps across hosts). The first validates the sync; the second is what you bought it for. The *independent* version of the second — a passive tap feeding a PTP-disciplined capture card, so the wire is timed without the host's cooperation — is Ch. 106 §106.4.5 and Ch. 78 §78.4.4 (where a switch SPAN port's timestamp error is measured against the tap's).

```
   # PTP sync quality (linuxptp):
   $ ptp4l -i <iface> -m            # runs the PTP slave; logs offset-from-master each interval
       ... "master offset    -23 s2 freq  +1200 path delay   310" ...   (offset in NANOSECONDS)
   $ pmc -u -b 0 'GET CURRENT_DATA_SET'   # query current offset/path-delay
   $ phc2sys -s <iface> -w -m       # discipline system clock to PHC; -w: take the TAI-UTC offset from ptp4l (PTP time is TAI)

   # True wire-to-wire latency (cross-host, hardware timestamps — Ch.73):
   #   exchange-side or upstream packet hw-timestamp (T_in)  vs  our order packet hw-timestamp (T_out)
   #   tick-to-trade = T_out - T_in   — VALID because both are on PTP-synced grandmaster time
```

Representative results (toolchain and machine: Appendix L.3) — HFT-grade NIC (Solarflare/ExaNIC) + PTP-aware network + GPS grandmaster, `linuxptp` (illustrative):

```
   PTP sync quality:
     master offset (well-tuned, hw timestamping)     ~±20-100 ns        sub-µs — comparable across hosts
     master offset (software timestamping, no PHC)   ~±10-100 µs        100-1000x worse — NOT ns-comparable
     phc2sys PHC↔system offset                        ~±50 ns            system clock tracks the PHC

   true wire-to-wire (cross-host, with synced hw timestamps):
     tick-to-trade (feed-in hw-ts → order-out hw-ts)  measurable to ~100 ns (bounded by the two hosts' PTP offsets)   ← the number you actually want
     (without PTP: cross-host difference is dominated by ~µs-ms clock offset → meaningless)
```

Read it: **with hardware-timestamped PTP**, the host clocks track the grandmaster to **~±20-100 ns** — so a timestamp on host A and one on host B are comparable to ~100 ns, *less than* the latencies you're measuring, making cross-host latency meaningful. **Without** hardware timestamping (software PTP, or just NTP), the sync is ~µs-ms — *larger* than the latencies you care about, so cross-host differences are dominated by clock error and are **meaningless**. That's the whole point: the *quality of the sync must be finer than the latency you're measuring*, and only hardware-timestamped PTP achieves that for HFT latencies. The second block is the payoff: with both the inbound feed packet and the outbound order packet **hardware-timestamped on the synced PHC** (Ch. 73), `T_out − T_in` is the **true wire-to-wire tick-to-trade latency** — the headline number of the whole book (Ch. 1, 115), now *measurable* and *correct* because both timestamps are on the same grandmaster time base. The measurement discipline: **validate PTP offset is sub-µs (ideally sub-100ns) continuously** (`ptp4l` offset, alert if it drifts), and **measure latency with hardware timestamps on the synced clock** — never software timestamps for cross-host (§76.5).

## 76.4 Techniques

### 76.4.1 PTP setup and disciplining

Building the synchronized fleet (the `linuxptp` toolchain):

- **A GPS-disciplined grandmaster.** The authoritative time source — a hardware grandmaster clock (GPS/GNSS-disciplined, often with a holdover oscillator for GPS outages) on the network. Everything syncs to it; its quality bounds the whole fleet's.
- **PTP-aware network (boundary/transparent clocks).** Switches between the grandmaster and hosts should be PTP boundary or transparent clocks (§76.2) so switch delay is corrected. A non-PTP switch adds unmeasured jitter — HFT colos use PTP-grade switches. Topology matters (fewer hops, symmetric paths — asymmetry biases the offset, §76.5).
- **`ptp4l` — discipline the NIC PHC.** Run `ptp4l` (linuxptp) on each host, bound to the NIC with PHC + hardware timestamping (Ch. 73 — verify with `ethtool -T`); it exchanges PTP messages and disciplines the **PHC** to the grandmaster. Configure for hardware timestamping (`-H`) and tune (delay mechanism E2E/P2P, sync interval).
- **`phc2sys` — discipline the system clock.** Run `phc2sys` to slave the host **system clock** (`CLOCK_REALTIME`) to the PHC — so software timestamps (`clock_gettime`) are also on synced time (for logging/compliance — though hardware timestamps remain the gold standard for latency). **PTP time is TAI**, not UTC (currently 37 s ahead): `phc2sys` must learn `currentUtcOffset` from `ptp4l` (`-w`, or `-a -r`) or be told it (`-O -37`), otherwise it steps `CLOCK_REALTIME` to TAI and every wall-clock/compliance stamp is off by whole seconds; `CLOCK_TAI` (which Ch. 77's scheduled transmission uses) is the offset-free view. Leap seconds arrive as a `currentUtcOffset` change announced by the grandmaster — the kernel applies them to `CLOCK_REALTIME`, `CLOCK_TAI` and the PHC never jump.
- **Profile, transport and timestamping mode.** `ptp4l` runs the **BMCA** (best-master clock algorithm) to pick the grandmaster from the announced clock qualities — fix the hierarchy with `priority1`/`priority2` so a lab box can never win; choose **L2 vs UDP/IPv4 transport** and the **one-step vs two-step** mode (one-step NICs stamp the Sync on the wire and skip Follow_Up) to match the switch and exchange profile.
- **Monitor the sync continuously.** Watch the `ptp4l` offset (and `phc2sys` offset) — alert if it exceeds a threshold (sync degraded → your cross-host measurements and compliance are invalid). The offset is a first-class operational metric (Ch. 115): a drifting/lost grandmaster (GPS outage, network issue) silently corrupts every latency number until fixed.
- **Relate the PHC to the TSC (Ch. 20).** To correlate a software TSC timestamp (Ch. 20) with a hardware PHC timestamp, use **cross-timestamping** (`PTP_SYS_OFFSET`/`PTP_SYS_OFFSET_PRECISE` ioctls — read PHC and system clock near-simultaneously). This bridges the on-host TSC world (Ch. 20) and the synced PHC world.

### 76.4.2 Tick-to-trade measurement across hosts

Using the synced clocks to measure the real latency (the Ch. 115 payoff):

- **Hardware-timestamp at the wire, both ends.** Capture the **NIC hardware RX timestamp** of the inbound market-data packet (Ch. 73) and the **hardware TX timestamp** of the outbound order packet — both on the PTP-synced PHC. Their difference is the **true wire-to-wire tick-to-trade** latency (§76.3) — the most honest measurement, excluding nothing and including nothing extra (no software-stack-latency confound — Ch. 73). If both stamps come from the *same* NIC/PHC (the usual single-box path), no PTP is needed for the interval itself — a free-running PHC measures it fine; PTP becomes necessary as soon as T_in and T_out are taken by *different* clocks (two NICs, two hosts, or a fabric tap/capture box — Ch. 78, 105).
- **Decompose across hops/hosts.** With synced clocks across the feed handler, strategy, and gateway hosts (or processes — Ch. 100), hardware-timestamp at each boundary and difference adjacent timestamps to get **per-hop** latency — locating where the time goes (Ch. 115's breakdown). Each hop's timestamps are comparable because all hosts are PTP-synced.
- **Compare to the exchange's timestamps — against the PHC, never the TSC.** Exchange feeds and order-entry responses carry the venue's own PTP-disciplined UTC timestamps — CME MDP 3.0 `SendingTime`/`TransactTime` and iLink 3 `SendingTimeEpoch`/`RequestTime`, Eurex EOBI `TransactTime`/`TrdRegTSTimePriority` and ETI `TrdRegTSTimeIn`/`TrdRegTSTimeOut`, Nasdaq ITCH/OUCH nanoseconds-since-midnight. Because your NIC's hardware RX/TX stamps are on the same UTC (this chapter), subtracting them gives one-way feed latency, order wire-out, the matching engine's own time and your queue position — the three-way attribution between *you*, *the wire* and *them* that Ch. 96 §96.4.5 develops field by field (Appendix J.5 tabulates the fields). The PTP-side rules are this chapter's: compare venue stamps to PHC stamps (both UTC), bridge any TSC stamp through `PTP_SYS_OFFSET_PRECISE` (§76.4.1) with its error bound, remember that the venue's clock is only guaranteed within its regulatory tolerance (MiFID II RTS 25: 100 µs for HFT-class venues, usually far better), and verify with the symmetry check — `(TrdRegTSTimeIn − t_tx) − (t_rx_ack − TrdRegTSTimeOut)` should be near zero on a symmetric path; a persistent offset is a clock error, not asymmetry (the same argument §76.5 makes about PTP's own path).
- **Capture with timestamps (Ch. 105).** Record every inbound/outbound packet *with its hardware timestamp* (Ch. 105) — so latency analysis, replay (Ch. 115), and compliance reporting all use the true wire times. The capture's timestamps are the synced PHC times.
- **Build it into continuous monitoring (Ch. 115).** Tick-to-trade latency (and its per-hop breakdown), measured via synced hardware timestamps, is the production performance metric (Ch. 115) — track its distribution (p50/p99/p99.9 — Ch. 1), alert on regression, and tie regressions to changes. This is the measurement the whole book optimizes toward.

### 76.4.3 Advanced synchronization: sub-nanosecond White Rabbit, PPS signaling for boundary clocks, grandmaster oscillator drift

The extremes of synchronization, where standard PTP isn't enough:

- **White Rabbit — sub-nanosecond synchronization.** **White Rabbit** (developed at CERN, now PTP's High Accuracy profile, IEEE 1588-2019) extends PTP with **Synchronous Ethernet** (syntonization — frequency locking over the physical layer) plus precise phase measurement to achieve **sub-nanosecond** (and picosecond-class) synchronization. Used where standard PTP's ~tens of ns isn't enough — the most latency-obsessed trading setups, and scientific timing. It requires White Rabbit-capable hardware (switches, NICs) — a specialized, expensive tier above standard PTP.
- **PPS (Pulse Per Second) for boundary clocks.** A **PPS** signal — a precise electrical pulse once per second from a GPS receiver/grandmaster — distributes a hardware reference edge to discipline a device's clock with very low jitter (the *phase* reference, complementing PTP's *time* messages). Boundary clocks and grandmasters use PPS (often with a 10 MHz frequency reference) to align to GPS precisely; on a host, linuxptp's **`ts2phc`** disciplines a NIC PHC from a PPS input (`ts2phc -c <iface> -s generic` with the pulse on the NIC's SMA/PPS pin) — the operational face of this bullet. PPS in/out lets you chain reference clocks and verify sync at the hardware level (scope the pulse alignment).
- **Grandmaster oscillator drift and holdover.** The grandmaster is only as good as its oscillator *when GPS is available* — and during a **GPS outage** (jamming, antenna issue, signal loss), it runs in **holdover** on its internal oscillator, which **drifts**. The oscillator grade (TCXO → OCXO → Rubidium → Cesium) determines holdover stability (a TCXO drifts ~ms/day — hundreds of µs/hour; an OCXO ~µs/hour; a Rubidium clock ~0.1–1 µs/day — ns to tens of ns/hour). HFT grandmasters use high-grade oscillators (OCXO/Rubidium) so a GPS outage doesn't immediately desync the fleet — and **monitor holdover** (alert when GPS is lost and holdover begins, because accuracy degrades over time). Account for the oscillator's drift/temperature behavior in the sync budget.
- **The accuracy budget.** Total sync error = grandmaster accuracy (GPS + oscillator) + network (switch boundary-clock errors, path asymmetry — §76.5) + host (PHC disciplining, timestamping precision). Each contributes; the weakest link bounds the fleet. For sub-100ns sync, every link must be tight (hardware timestamping, PTP-aware symmetric network, good grandmaster) — and for sub-ns, White Rabbit.

## 76.5 Pitfalls & anti-patterns: software-timestamp error

- **Using software timestamps for cross-host comparison (the cardinal error).** A software timestamp (`clock_gettime` in your recv loop) includes all the variable software/OS/queueing latency (Ch. 73) — so it's *not* the wire time, and differencing software timestamps across hosts compounds that error with clock offset → meaningless latency (§76.3). For cross-host/wire-to-wire, use **NIC hardware timestamps** on the **PTP-synced PHC** (Ch. 73, §76.2). Software timestamps are for coarse/on-host use only.
- **Relying on NTP for HFT timing.** NTP syncs to ~milliseconds — *larger* than HFT latencies, so cross-host comparison is meaningless and it fails compliance (µs requirements). PTP + hardware timestamping (~tens of ns) is required (§76.2). NTP is fine for log wall-clock, not latency.
- **PTP without hardware timestamping.** Software-timestamped PTP gives only ~µs-ms sync (§76.3) — defeating the purpose. The accuracy *comes from* the NIC PHC hardware timestamps; verify the NIC supports it (`ethtool -T`) and `ptp4l` uses it (`-H`).
- **Non-PTP-aware network (uncorrected switch jitter).** Switches add variable forwarding delay; without boundary/transparent clocks, that jitter corrupts the sync (§76.2). Use PTP-aware switches; mind **path asymmetry** (different send/receive path lengths bias the offset — PTP assumes symmetry).
- **TAI-UTC offset unset — a 37-second error that passes every jitter check.** `phc2sys` run without `-w`/`-a`/`-O` (and `ptp4l` without `utc_offset`/a grandmaster that announces it) disciplines `CLOCK_REALTIME` to TAI: offsets and jitter look perfect, yet every wall-clock timestamp, log line and compliance record is 37 s ahead of UTC, and cross-venue comparisons against exchange (UTC) stamps are nonsense. Verify `date` against a known-UTC source once sync is up (§76.4.1).
- **Not monitoring sync quality / undetected desync.** A drifting offset, a lost grandmaster (GPS outage → holdover drift — §76.4.3), or a `ptp4l`/`phc2sys` failure silently invalidates *every* cross-host latency number and compliance timestamp. **Monitor the offset continuously** and alert (§76.4.1) — desync is an outage, not a degradation.
- **Forgetting `phc2sys` (system clock unsynced).** Disciplining the PHC but not the system clock leaves software timestamps (`clock_gettime`/logs) on a *different*, drifting time — confusing correlation between hardware-timestamped packets and software-timestamped logs/events. Run `phc2sys` so the system clock tracks the PHC.
- **Path asymmetry and antenna/cabling errors.** PTP computes path delay assuming the send and receive paths are *symmetric*; asymmetry (different cable lengths, asymmetric routing) introduces a *fixed offset* error. Calibrate for known asymmetries; for sub-ns, account for cable delays explicitly (White Rabbit does — §76.4.3).
- **Assuming sync is set-and-forget.** Oscillators drift with temperature/age, GPS can fail, network changes; sync must be *maintained and monitored* (§76.4.1, §76.4.3). Temperature changes in the data center can shift oscillator frequency — a real effect at ns precision.
- **Mixing time bases without bridging.** Comparing an on-host TSC timestamp (Ch. 20) to a PHC hardware timestamp without **cross-timestamping** (§76.4.1) compares different clocks. Bridge them (`PTP_SYS_OFFSET`) or keep each measurement within one time base.

## 76.6 Exercises & checklist

**Exercises**

1. **PTP sync quality.** Set up `ptp4l` (with a grandmaster, or a software test setup) and watch the master offset converge; record the offset distribution (ns) with hardware timestamping vs software timestamping. Confirm hardware is ~ns and software is ~µs-ms (§76.3). Validate with `pmc`.
2. **Hardware vs software wire-to-wire.** Send a packet between two PTP-synced hosts; measure the latency using (a) software timestamps and (b) NIC hardware timestamps on the synced PHC (Ch. 73). Quantify the software-stack error the hardware timestamp removes (§76.5).
3. **`phc2sys` and the system clock.** With only `ptp4l` running (PHC synced, system clock not), compare a software `clock_gettime` timestamp to a hardware one — observe the offset. Add `phc2sys`; confirm the system clock now tracks the PHC (§76.4.1).
4. **Holdover drift.** Disconnect the grandmaster's GPS (or simulate) and watch the offset drift over time in holdover; relate the drift rate to the oscillator grade (§76.4.3). How long until sync exceeds your threshold?
5. **Tick-to-trade across hosts.** Hardware-timestamp an inbound feed packet on one host and an outbound order packet on another (both PTP-synced); compute the cross-host wire-to-wire latency. Confirm it's meaningful (and that without PTP it would be dominated by clock offset) — the Ch. 115 measurement (§76.4.2).

**Checklist — clock synchronization & hardware timestamping**

- [ ] Cross-host / wire-to-wire latency uses **NIC hardware timestamps** (Ch. 73) on a **PTP-disciplined PHC** — **never software timestamps** for cross-host comparison (§76.5).
- [ ] **PTP (IEEE 1588) with hardware timestamping** syncs the fleet to a **GPS-grandmaster** to **sub-µs (ideally sub-100ns)** over a **PTP-aware (boundary/transparent-clock) network** — not NTP (§76.2).
- [ ] **`ptp4l`** disciplines the **PHC** and **`phc2sys`** disciplines the **system clock** (so software timestamps/logs are synced too); both run on every host (§76.4.1).
- [ ] **Sync quality (offset) is monitored continuously and alerted** — a drift/grandmaster-loss/holdover event is treated as an outage that invalidates latency + compliance data (§76.4.1, §76.5).
- [ ] **True tick-to-trade** is measured as `T_out − T_in` of **hardware timestamps on the synced clock**, decomposed **per hop/host** (Ch. 115), and captured with the wire data (Ch. 105) (§76.4.2).
- [ ] **Grandmaster oscillator grade and holdover** are appropriate (OCXO/Rubidium for GPS-outage stability), and **holdover is monitored** (§76.4.3).
- [ ] **Path asymmetry / cable delays** are accounted for (PTP assumes symmetry); sub-ns needs are met with **White Rabbit** + Synchronous Ethernet (§76.4.3).
- [ ] On-host TSC (Ch. 20) and PHC times are **bridged via cross-timestamping** (`PTP_SYS_OFFSET`) when correlated; **compliance** timestamp requirements (MiFID II/Reg NMS) are met and documented.

## 76.7 References

- IEEE 1588 (Precision Time Protocol), including the 2019 revision (High Accuracy / White Rabbit profile) — the protocol (§76.2, §76.4.3).
- The **`linuxptp`** project documentation — `ptp4l`, `phc2sys`, `pmc`, and the `Documentation/networking/timestamping.rst` / `ptp` kernel docs (`PTP_SYS_OFFSET`) — the toolchain of §76.4.
- The CERN **White Rabbit** project documentation and papers — sub-nanosecond synchronization, Synchronous Ethernet, and PPS distribution (§76.4.3).
- The regulatory timestamping requirements — **MiFID II RTS 25** (clock synchronization, traceability to UTC) and **Reg NMS / CAT** timestamp rules — the compliance driver (§76.1).
- Ch. 20 references (Intel SDM TSC, `clock_gettime`/`SO_TIMESTAMPING`) and Ch. 73 (NIC PHC / hardware timestamping) — the on-host and NIC foundations.

## 76.8 Additional Reading

- The Solarflare/ExaNIC and grandmaster-vendor (Meinberg, Orolia/Safran) PTP/timing documentation — HFT-grade synchronization and timestamping in practice.
- Talks on exchange/HFT timestamping, MiFID II compliance, and tick-to-trade measurement — real-world cross-host timing.
- Ch. 20 (*Timekeeping: TSC*) — on-host clocks, the prequel; Ch. 73 (*NIC Features*) — the PHC / hardware timestamps; Ch. 105 (*Capture*) — timestamped capture; Ch. 115 (*End-to-End*) — tick-to-trade measurement using synced clocks; Ch. 67, 69 (*Sockets/Decoding*) — the feeds measured.
- Ch. 77 (*Precise & Scheduled Transmission*) — the disciplined clock put to work on the send side: `SO_TXTIME`/LAUNCHTIME to transmit at an exact nanosecond and pace egress.
- **Appendix E** — wire-to-wire latency numbers and the timing chain; **Appendix F** — PTP/PHC/grandmaster/White-Rabbit glossary.

---

*Next: Ch. 77 — Precise & Scheduled Transmission, putting the disciplined clock to work on the send side: `SO_TXTIME`, hardware LAUNCHTIME, and time-aware shaping to transmit at an exact nanosecond, pace egress, and stop creating your own microbursts.*
