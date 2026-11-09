# Part IX — Market Data, NIC & Fabric

# Chapter 65 — Network Fabric & Switching

> **Prerequisites:** Appendix K (colocation — the switch is the box next to yours in the rack), Ch. 55 (socket/TCP and multicast), Ch. 57 (microbursts — where switch buffers overflow), Ch. 60 (NIC — the far end of the wire), Ch. 63 (PTP — how you measure per-hop latency), Ch. 1 (the tail).
>
> **Leads into:** Ch. 58 (reliable multicast — the switch fans it out), Appendix E (the switch-hop rows belong beside its NIC RX rows). The hop the software chapters assumed: the physical fabric between your NIC and the exchange, where nanoseconds are decided by hardware you buy, not code you write.

---

## 65.1 Why it matters: the switch is on the tick-to-trade path

Every packet in the tick-to-trade path (Ch. 94) crosses at least one **switch** — usually several — between the exchange's matching engine and your NIC, and between your NIC and the exchange's order gateway. In a colocated setup (Appendix K) the wire is short (a signal travels ~20 cm per ns in fibre or copper — ~5 ns per metre, Appendix K.2 — so a few metres of patching is tens of nanoseconds and a 20–50 m cross-connect 100–250 ns), which means the **switch latency dominates the network hop**. A switch that adds 500 ns per hop across three hops is 1.5 µs — larger than the entire software tick-to-trade budget of a tuned system (Ch. 94's ~640 ns). You can spend months shaving nanoseconds off your decode (Ch. 57) and lose all of it to a store-and-forward switch you didn't think about. The fabric is not "the network department's problem"; it is a latency component of your system, and often the *largest* one you don't control in software.

Worse, the switch is where the **tail** is manufactured under exactly the conditions you care about (Ch. 1). At market open (Ch. 57), the exchange fans out a microburst of market data to every participant simultaneously; that burst hits the switch's buffers, and if the switch queues (store-and-forward, oversubscription, buffer contention) it *adds variable delay* — the flat, deterministic latency you want (Ch. 1) becomes a jittery, load-dependent tail. Two switches with the same "average latency" spec can have wildly different behavior under a burst: one holds a flat tail, the other bufferbloats and spikes. The switch's *distribution under load*, not its datasheet median, is what reaches your strategy.

So the fabric is a first-class latency decision: **which switching technology, how many hops, how the fabric is laid out, and how it behaves under microbursts.** This chapter is deliberately hardware-and-infrastructure-flavored — it's below the C++ layer — but it belongs in a low-latency book because a systems engineer who optimizes the software and ignores the fabric is optimizing the wrong thing. This chapter covers the switching mental model (§65.2), how to measure per-hop and one-way latency (§65.3), the techniques for a low-latency fabric (§65.4), and the fabric-side pitfalls that manufacture tail latency (§65.5). It's the "what's between your NIC and the exchange" chapter.

## 65.2 Mental model: store-and-forward, cut-through, and layer-1 switches

A switch moves a frame from an input port to an output port. *When* it starts forwarding — and how much it buffers — defines its latency:

```
   STORE-AND-FORWARD:  [receive WHOLE frame] -> [check CRC] -> [forward]
                        latency ∝ frame size (serialization of the entire frame) + switching
   CUT-THROUGH:         [read dest header] -> [start forwarding immediately, tail still arriving]
                        latency ≈ header time + fixed switching (independent of frame size)
   LAYER-1 (physical):  [electrical/optical cross-connect, no packet parsing]
                        latency ≈ propagation only (~4-5 ns port-to-port)
```

- **Store-and-forward** — the switch receives the *entire* frame, verifies its CRC, then forwards. Latency includes **serialization of the whole frame** (a 1500-byte frame at 10 Gbps is ~1.2 µs just to clock in), so latency **grows with frame size** and the switch always adds that delay. Standard for enterprise switches; too slow and too variable for the hot path. Its one virtue: it drops corrupt frames (CRC checked before forwarding).
- **Cut-through** — the switch reads only the destination in the header and *starts forwarding while the rest of the frame is still arriving*. Latency is ≈ the header read time + a fixed switching latency, **independent of frame size**. Two classes: ASIC cut-through switches (Arista 7150, Cisco Nexus 3548) at ~200–500 ns, and FPGA layer-2 mux / fan-in devices (Arista 7130 MetaMux, Cisco Nexus 3550-F Fusion Mux) at ~40–100 ns. This is the HFT switch. The trade-off: it may forward a frame that later fails CRC (it started forwarding before seeing the tail), so a corrupt frame propagates one hop — acceptable when links are clean and latency is king.
- **Layer-1 switches** — a *physical-layer* cross-connect (essentially a very fast, software-controlled patch panel / signal replicator) that forwards the electrical/optical signal with **no packet parsing at all**. Latency is a few nanoseconds (~4–5 ns port-to-port on current products — less than the propagation delay of a metre of fibre) — effectively just propagation. Used for **fan-out and replication** (one input to many outputs, e.g. distributing a market-data feed or a tap to many consumers) and for the lowest-latency point-to-point paths. Products: Arista 7130 Connect (the MetaConnect L1 application), Cisco Nexus 3550-F Fusion L1 (ex-Exablaze ExaLINK). They don't "switch" in the routing sense; they cross-connect and replicate at line speed.

Two more concepts:

- **Serialization delay** — the time to clock a frame onto the wire, = frame_bits / link_rate. It's why cut-through beats store-and-forward (which pays full serialization) and why **link speed matters** (a 100 G link serializes a frame 10× faster than 10 G). Mixing speeds (a 10 G segment in a 100 G path) reintroduces serialization delay.
- **Fabric topology** — how switches connect. A **leaf-spine** fabric (access "leaf" switches connected to "spine" switches) bounds hop count; the exchange's distribution fabric and your access to it determine how many switch hops sit on the path. **Fewer hops = less latency and less jitter** — every hop is a switch's latency *and* a place a burst can queue (§65.5).

The mental model: **a switch is a latency component with a *distribution*, and the technology choice (store-and-forward vs cut-through vs layer-1) sets both its median and its behavior under a burst.** For the hot path you want cut-through (or layer-1 where you're just fanning out), the fewest hops, and consistent link speeds.

## 65.3 Measure it: per-hop and one-way latency with hardware timestamps

*Toolchain and machine: Appendix L.3.*

You cannot reason about the fabric from datasheets; you measure it, and measuring the *network* requires **hardware timestamps and synchronized clocks** (Ch. 63), because software timestamps are perturbed by the very host you're trying to exclude from the measurement.

- **Per-hop switch latency** — with a NIC hardware timestamp (Ch. 60, 63) on both ingress and egress of a switch (or a passive tap on each side, Ch. 90), measure the time a frame spends *in* the switch. Compare a store-and-forward vs cut-through switch, and measure how each scales with **frame size** (store-and-forward grows with size; cut-through stays flat) — this single test reveals the switch's class regardless of its spec sheet. Representative (figures pending real runs):

| Switch type | 64-byte frame | 1500-byte frame | Under microburst |
|---|---|---|---|
| **Store-and-forward** (enterprise) | ~1–3 µs | ~2–5 µs | grows; queues under load (jittery) |
| **Cut-through, ASIC** (HFT) | ~200–500 ns | ~250–550 ns | ~flat if not oversubscribed |
| **Cut-through, FPGA L2 mux** (fan-in) | ~40–100 ns | ~40–100 ns | flat (no buffering) |
| **Layer-1** (cross-connect/replicator) | ~3–6 ns | ~3–6 ns | flat (no buffering) |

- **One-way latency (not RTT)** — the number that matters is **wire-to-wire, one direction** (market-data-in to order-out — Ch. 94), and it requires **synchronized clocks across the two measurement points** (PTP, Ch. 63; the ultimate reference is White Rabbit / PPS, Ch. 63). Round-trip time halved is *not* one-way latency when the path is asymmetric (different switches/queues each direction) — measure each direction independently with synchronized hardware timestamps. This is why Ch. 63 exists: to compare timestamps across machines and get true one-way, per-hop fabric latency.
- **Behavior under a burst (the tail).** Replay a market-open microburst (Ch. 57) and measure the switch's latency *distribution* (Ch. 1/3), not just its idle median. A cut-through switch on a non-oversubscribed path holds a flat tail; the same switch oversubscribed, or a store-and-forward switch, spreads under the burst. The distribution under load is the real spec.

The lessons: measure per-hop with hardware timestamps (software timing includes host jitter you're trying to exclude); measure *one-way* with synchronized clocks (Ch. 63), never RTT/2 on an asymmetric path; and measure the *distribution under a burst*, because the fabric's tail — not its median — is what your strategy sees at the open.

## 65.4 Techniques

### 65.4.1 Choosing switching technology and minimizing hops

- **Cut-through on the hot path.** For any switch on the tick-to-trade path, choose **cut-through** (Arista 7130/7150, Cisco Nexus low-latency, Exablaze) over store-and-forward — flat, frame-size-independent, sub-microsecond latency. The CRC-propagation trade-off (§65.2) is acceptable on clean colo links; monitor error counters to confirm the links are clean (FEC/CRC/optics DOM and the switch-side discards — Ch. 93 §93.4.4).
- **Layer-1 for fan-out and replication.** Where you're *distributing* a signal (a market-data feed to multiple strategy hosts, a tap to multiple capture/analytics consumers — Ch. 90, or an order feed to redundant systems), use a **layer-1 replicator** (Arista 7130 Connect, Cisco Nexus 3550-F L1) — a few nanoseconds, no buffering, deterministic. This is how one feed reaches many consumers without a switching-latency penalty per consumer.
- **Minimize hop count.** Every hop is a switch latency *and* a queuing point (§65.5). Lay out the path (and choose your colo cross-connects — Appendix K) to traverse the **fewest switches** between the exchange handoff and your NIC, and between your NIC and the order gateway. One cut-through hop beats three; a direct layer-1 cross-connect beats a switched path where topology allows.
- **Consistent link speeds — but budget FEC and PHY type.** Keep link speeds uniform along the path (a speed change forces a store-and-forward-style buffer at the boundary), and match the exchange's handoff speed — but do not assume faster is lower-latency for the small frames that dominate trading traffic: 25/100 G links normally run RS-FEC, which adds ~250 ns per link at 25 G (~100 ns at 100 G; CL74 FC-FEC ~80 ns; none at 10 G), far more than the serialization saved on a 64-byte frame (51 ns at 10 G vs 20 ns at 25 G) — which is why many HFT fabrics still run 10 G, or 25 G with FEC off where the link is clean enough. Never 10GBASE-T on the hot path (~2.5 µs/link of PHY coding); use SFP+ DAC or fibre.

### 65.4.2 Fabric layout, buffering, and microburst handling

- **Avoid oversubscription on the hot path.** A switch is oversubscribed when more input bandwidth can arrive than an output port can send; under a burst (Ch. 57) that forces queuing and jitter. Provision the hot path so the output port can absorb the worst burst without queuing — the fabric analog of Ch. 57's RX-ring sizing. Non-oversubscribed cut-through is what holds a flat tail.
- **Understand switch buffering and its trade-off.** Deep buffers absorb bursts *at the cost of latency* (bufferbloat — a queued frame waits behind others); shallow buffers keep latency low but drop under a burst (Ch. 57's hardware drops). The low-latency choice is usually **shallow buffers + no oversubscription** (don't queue; don't drop because you didn't overload), not deep buffers (which trade your latency for burst tolerance). Match buffer policy to the traffic: market data (bursty, drop-tolerant with A/B — Ch. 58) differs from order entry (must not drop).
- **Handle head-of-line blocking.** In shared-buffer or input-queued switches, a congested output can stall unrelated traffic behind it (head-of-line blocking). Virtual output queuing (VOQ) and proper switch selection avoid it; on the hot path, isolate market-data and order-entry paths so one can't block the other.
- **Symmetric paths for measurable latency.** Lay out the fabric so the two directions (data-in, order-out) are *symmetric* where possible — it makes one-way latency measurable and predictable (§65.3), and avoids surprises where one direction traverses an extra hop.

### 65.4.3 Switch-based replication, timestamping, and taps

Fabric hardware does more than forward — it can timestamp, replicate, and tap, offloading work from the host:

- **Switch/fabric hardware timestamping (ties Ch. 60, 63).** Low-latency switches and layer-1 devices can **timestamp every packet** as it passes, with nanosecond (or sub-ns) precision, and even append the timestamp to the frame. This gives you a fabric-level, host-independent latency measurement and capture-time source (Ch. 90) — the switch tells you exactly when each packet crossed, disciplined by PTP (Ch. 63).
- **Replication for capture and redundancy.** A layer-1 replicator or a switch's mirror/SPAN (Ch. 90) copies the feed to capture/analytics hosts *without* adding latency to the production path — the compliance-grade capture (Ch. 90) taps the wire at the fabric, below and beside the trading host. This is why capture belongs at the fabric, not in the application (§90.4.1).
- **Multicast replication in the switch (Ch. 58).** The market-data feed is *multicast*: an L2 switch replicates each frame to every port that has **joined** the group — it learns the joins by **IGMP snooping** (with an IGMP querier on the VLAN — without one the snooped entries age out and the feed goes dark on every host ~260 s later; Ch. 59), so a late/missing join means the first packets after startup never arrive (the warm-up concern of Ch. 50) and an *un*snooped switch **floods** the group to every port, swamping unrelated hosts; across L3 the join is PIM. Replication is per output port and costs the egress queue on each — one more reason the A and B lines (Ch. 57–58) should take disjoint switches so a single replication point cannot drop both.
- **Aggregation taps.** A tap-aggregation layer collects taps from many links into capture infrastructure (Ch. 90), timestamped at the fabric — the observability backbone (Ch. 94) for a trading system, built from switch/tap hardware rather than host CPU.

## 65.5 Pitfalls & anti-patterns: store-and-forward, oversubscription, and hidden hops

- **A store-and-forward switch on the hot path (the cardinal fabric error).** It pays full frame serialization on every hop and queues under load — microseconds of latency that grows with frame size and spikes under a burst (§65.3), dwarfing your software budget (Ch. 94). Use cut-through (or layer-1) on any path that matters.
- **Oversubscription manufacturing tail latency.** An oversubscribed switch queues under the market-open burst (Ch. 57), turning a flat cut-through latency into a jittery, load-dependent tail (§65.3). Provision the hot path non-oversubscribed; the tail you see at the open is often the switch, not your code.
- **Hidden extra hops.** A path that looks direct but traverses an extra aggregation switch, a firewall, or a mismatched cross-connect — each hop is latency and a queuing point. Trace the *actual* physical path (Appendix K — colo cross-connects) and count hops; the fewest wins (§65.4.1).
- **Measuring RTT/2 as one-way latency on an asymmetric path.** If the two directions traverse different switches/queues (common), RTT/2 is not the one-way latency — you'll misattribute where the time goes. Measure each direction with synchronized hardware timestamps (PTP, Ch. 63; §65.3).
- **Software timestamps for fabric measurement.** Timing packets with host software clocks includes the host's own jitter (Ch. 43) — the thing you're trying to exclude when measuring the *fabric*. Use NIC/switch hardware timestamps (Ch. 60, 63).
- **Deep buffers "for reliability."** Choosing a deep-buffer switch to avoid drops trades your latency for burst tolerance (bufferbloat) — a queued frame is a slow frame. For drop-tolerant market data (A/B recovery handles loss — Ch. 58), shallow buffers + no oversubscription keep the tail flat; reserve deep buffering for paths that truly can't drop and aren't latency-critical (§65.4.2).
- **Mismatched link speeds.** A slower link segment in the path reintroduces serialization delay (§65.2) and a speed-mismatch buffering point. Keep the path uniform and high-speed.
- **Ignoring the fabric entirely.** Optimizing the host to the nanosecond while the fabric adds microseconds of variable latency — the classic mis-optimization (§65.1). The fabric is a measured component of tick-to-trade (Ch. 94), not an externality.

## 65.6 Exercises & checklist

**Exercises:**

1. **Store-and-forward vs cut-through vs frame size.** Measure a switch's per-hop latency (hardware timestamps, Ch. 63) at 64, 512, and 1500 bytes. Store-and-forward grows with size; cut-through stays flat — use the test to classify a switch regardless of its datasheet (§65.3).
2. **One-way, not RTT.** Set up synchronized clocks (PTP, Ch. 63) at two points and measure one-way latency in each direction; construct an asymmetric path and show RTT/2 misleads (§65.3, §65.5).
3. **Burst behavior.** Replay a market-open microburst (Ch. 57) through a cut-through switch, non-oversubscribed vs oversubscribed; measure the latency *distribution* (Ch. 3) and show oversubscription manufactures a tail (§65.3, §65.5).
4. **Hop count.** Trace the actual physical path from the exchange handoff to your NIC; count switches and their types; identify a hop you can remove or convert to cut-through/layer-1 (§65.4.1).
5. **Layer-1 fan-out.** Distribute one feed to multiple consumers through a layer-1 replicator vs a switched path; measure the added latency per consumer (near zero for layer-1) (§65.4.1, §65.4.3).

**Checklist:**

- [ ] Every switch on the tick-to-trade path is **cut-through** (or **layer-1** for fan-out) — no store-and-forward on the hot path (§65.4.1, §65.5).
- [ ] The path traverses the **fewest hops** possible, with **uniform high link speeds** (minimal serialization) (§65.4.1).
- [ ] The hot path is **not oversubscribed**; buffer policy suits the traffic (shallow + no oversubscription for market data) (§65.4.2, §65.5).
- [ ] Per-hop and **one-way** latency are measured with **hardware timestamps and synchronized clocks** (Ch. 63) — never RTT/2 on an asymmetric path (§65.3).
- [ ] The fabric's **latency distribution under a market-open burst** (Ch. 57) is measured, not just the idle median (§65.3).
- [ ] **Fan-out / replication** uses layer-1 devices; **capture** taps the wire at the fabric (Ch. 90) without adding hot-path latency (§65.4.3).
- [ ] **Fabric/switch hardware timestamping** (Ch. 63) feeds latency monitoring and capture (Ch. 90–94) (§65.4.3).
- [ ] The **actual physical path** (Appendix K — colocation) is traced — no hidden hops, mismatched speeds, or asymmetric detours (§65.5).

## 65.7 References

- Low-latency switch vendor documentation — **Arista 7130/7150** (MetaMux L2 mux / MetaConnect layer-1 applications), **Cisco Nexus 3550/low-latency**, **Exablaze/Cisco** — cut-through and layer-1 latency specs and timestamping (§65.2, §65.4).
- IEEE 802.3 (Ethernet) and switching-architecture references — store-and-forward vs cut-through, serialization, head-of-line blocking, VOQ (§65.2).
- Ch. 63 (PTP / hardware timestamping — how to measure the fabric), Ch. 60 (NIC timestamping), Ch. 57 (microbursts — where the fabric queues), Appendix K (colocation — the physical path), Ch. 90 (capture — fabric taps).
- Exchange colocation and connectivity guides — the handoff speed, cross-connects, and fabric between the matching engine and the participant (§65.1, §65.4).

## 65.8 Additional Reading

- Vendor and practitioner material on HFT network fabrics, cut-through switching, and layer-1 devices — real low-latency fabric designs and their measured latencies (§65.2, §65.4).
- Papers and talks on datacenter fabric latency, microburst behavior, and buffer sizing — the tail-under-load behavior of §65.3/§65.5.
- Ch. 58 (*Reliable Multicast*) — the feed the fabric fans out and how loss is recovered; Ch. 63 (*PTP*) — the clock discipline behind one-way measurement; **Appendix K** (*Exchange Connectivity*) — colocation and the physical path; Ch. 90 (*Capture*) — fabric-level tapping; **Appendix E** — the NIC RX floor the switch hop sits in front of; **Appendix F** — cut-through/layer-1/serialization glossary.

---

*Next: Ch. 66 — eBPF, bpftrace & XDP / AF_XDP, opening Part X: programmable kernel observability and fast packet paths — the eBPF VM and verifier, low-overhead production tracing of latency and syscalls, and XDP/AF_XDP for in-driver packet processing between the kernel stack and full bypass.*
