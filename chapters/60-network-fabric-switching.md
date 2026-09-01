# Part IX — Market Data, NIC & Fabric

# Chapter 60 — Network Fabric & Switching

> **Prerequisites:** Ch. 6 (colocation — the switch is the box next to yours in the rack), Ch. 51 (socket/TCP and multicast), Ch. 53 (microbursts — where switch buffers overflow), Ch. 55 (NIC — the far end of the wire), Ch. 58 (PTP — how you measure per-hop latency), Ch. 1 (the tail).
>
> **Leads into:** Ch. 54 (reliable multicast — the switch fans it out), Appendix E (add the switch-latency numbers). The hop the software chapters assumed: the physical fabric between your NIC and the exchange, where nanoseconds are decided by hardware you buy, not code you write.

---

## 60.1 Why it matters: the switch is on the tick-to-trade path

Every packet in the tick-to-trade path (Ch. 76) crosses at least one **switch** — usually several — between the exchange's matching engine and your NIC, and between your NIC and the exchange's order gateway. In a colocated setup (Ch. 6) the wire is short (light travels ~1 ns per 30 cm, so the cable itself is tens of nanoseconds), which means the **switch latency dominates the network hop**. A switch that adds 500 ns per hop across three hops is 1.5 µs — larger than the entire software tick-to-trade budget of a tuned system (Ch. 76's ~640 ns). You can spend months shaving nanoseconds off your decode (Ch. 53) and lose all of it to a store-and-forward switch you didn't think about. The fabric is not "the network department's problem"; it is a latency component of your system, and often the *largest* one you don't control in software.

Worse, the switch is where the **tail** is manufactured under exactly the conditions you care about (Ch. 1). At market open (Ch. 53), the exchange fans out a microburst of market data to every participant simultaneously; that burst hits the switch's buffers, and if the switch queues (store-and-forward, oversubscription, buffer contention) it *adds variable delay* — the flat, deterministic latency you want (Ch. 1) becomes a jittery, load-dependent tail. Two switches with the same "average latency" spec can have wildly different behavior under a burst: one holds a flat tail, the other bufferbloats and spikes. The switch's *distribution under load*, not its datasheet median, is what reaches your strategy.

So the fabric is a first-class latency decision: **which switching technology, how many hops, how the fabric is laid out, and how it behaves under microbursts.** This chapter is deliberately hardware-and-infrastructure-flavored — it's below the C++ layer — but it belongs in a low-latency book because a systems engineer who optimizes the software and ignores the fabric is optimizing the wrong thing. This chapter covers the switching mental model (§60.2), how to measure per-hop and one-way latency (§60.3), the techniques for a low-latency fabric (§60.4), and the fabric-side pitfalls that manufacture tail latency (§60.5). It's the "what's between your NIC and the exchange" chapter.

## 60.2 Mental model: store-and-forward, cut-through, and layer-1 switches

A switch moves a frame from an input port to an output port. *When* it starts forwarding — and how much it buffers — defines its latency:

```
   STORE-AND-FORWARD:  [receive WHOLE frame] -> [check CRC] -> [forward]
                        latency ∝ frame size (serialization of the entire frame) + switching
   CUT-THROUGH:         [read dest header] -> [start forwarding immediately, tail still arriving]
                        latency ≈ header time + fixed switching (independent of frame size)
   LAYER-1 (physical):  [electrical/optical cross-connect, no packet parsing]
                        latency ≈ propagation only (sub-nanosecond to a few ns)
```

- **Store-and-forward** — the switch receives the *entire* frame, verifies its CRC, then forwards. Latency includes **serialization of the whole frame** (a 1500-byte frame at 10 Gbps is ~1.2 µs just to clock in), so latency **grows with frame size** and the switch always adds that delay. Standard for enterprise switches; too slow and too variable for the hot path. Its one virtue: it drops corrupt frames (CRC checked before forwarding).
- **Cut-through** — the switch reads only the destination in the header and *starts forwarding while the rest of the frame is still arriving*. Latency is ≈ the header read time + a fixed switching latency, **independent of frame size** — often tens to low-hundreds of nanoseconds. This is the HFT switch (Arista 7130/Metamux, Cisco Nexus low-latency, Exablaze/Cisco Nexus 3550, Metamako). The trade-off: it may forward a frame that later fails CRC (it started forwarding before seeing the tail), so a corrupt frame propagates one hop — acceptable when links are clean and latency is king.
- **Layer-1 switches** — a *physical-layer* cross-connect (essentially a very fast, software-controlled patch panel / signal replicator) that forwards the electrical/optical signal with **no packet parsing at all**. Latency is sub-nanosecond to a few nanoseconds — effectively just propagation. Used for **fan-out and replication** (one input to many outputs, e.g. distributing a market-data feed or a tap to many consumers) and for the lowest-latency point-to-point paths. Products like Arista 7130 (Metamux/Metaconnect) and Exablaze/Cisco layer-1 devices. They don't "switch" in the routing sense; they cross-connect and replicate at line speed.

Two more concepts:

- **Serialization delay** — the time to clock a frame onto the wire, = frame_bits / link_rate. It's why cut-through beats store-and-forward (which pays full serialization) and why **link speed matters** (a 100 G link serializes a frame 10× faster than 10 G). Mixing speeds (a 10 G segment in a 100 G path) reintroduces serialization delay.
- **Fabric topology** — how switches connect. A **leaf-spine** fabric (access "leaf" switches connected to "spine" switches) bounds hop count; the exchange's distribution fabric and your access to it determine how many switch hops sit on the path. **Fewer hops = less latency and less jitter** — every hop is a switch's latency *and* a place a burst can queue (§60.5).

The mental model: **a switch is a latency component with a *distribution*, and the technology choice (store-and-forward vs cut-through vs layer-1) sets both its median and its behavior under a burst.** For the hot path you want cut-through (or layer-1 where you're just fanning out), the fewest hops, and consistent link speeds.

## 60.3 Measure it: per-hop and one-way latency with hardware timestamps

You cannot reason about the fabric from datasheets; you measure it, and measuring the *network* requires **hardware timestamps and synchronized clocks** (Ch. 58), because software timestamps are perturbed by the very host you're trying to exclude from the measurement.

- **Per-hop switch latency** — with a NIC hardware timestamp (Ch. 55, 58) on both ingress and egress of a switch (or a passive tap on each side, Ch. 75), measure the time a frame spends *in* the switch. Compare a store-and-forward vs cut-through switch, and measure how each scales with **frame size** (store-and-forward grows with size; cut-through stays flat) — this single test reveals the switch's class regardless of its spec sheet. Representative (figures pending real runs):

| Switch type | 64-byte frame | 1500-byte frame | Under microburst |
|---|---|---|---|
| **Store-and-forward** (enterprise) | ~1–3 µs | ~2–5 µs | grows; queues under load (jittery) |
| **Cut-through** (HFT) | ~300–500 ns | ~350–550 ns | ~flat if not oversubscribed |
| **Layer-1** (cross-connect/replicator) | ~1–5 ns | ~1–5 ns | flat (no buffering) |

- **One-way latency (not RTT)** — the number that matters is **wire-to-wire, one direction** (market-data-in to order-out — Ch. 76), and it requires **synchronized clocks across the two measurement points** (PTP, Ch. 58; the ultimate reference is White Rabbit / PPS, Ch. 58). Round-trip time halved is *not* one-way latency when the path is asymmetric (different switches/queues each direction) — measure each direction independently with synchronized hardware timestamps. This is why Ch. 58 exists: to compare timestamps across machines and get true one-way, per-hop fabric latency.
- **Behavior under a burst (the tail).** Replay a market-open microburst (Ch. 53) and measure the switch's latency *distribution* (Ch. 1/3), not just its idle median. A cut-through switch on a non-oversubscribed path holds a flat tail; the same switch oversubscribed, or a store-and-forward switch, spreads under the burst. The distribution under load is the real spec.

The lessons: measure per-hop with hardware timestamps (software timing includes host jitter you're trying to exclude); measure *one-way* with synchronized clocks (Ch. 58), never RTT/2 on an asymmetric path; and measure the *distribution under a burst*, because the fabric's tail — not its median — is what your strategy sees at the open.

## 60.4 Techniques

### 60.4.1 Choosing switching technology and minimizing hops

- **Cut-through on the hot path.** For any switch on the tick-to-trade path, choose **cut-through** (Arista 7130/7150, Cisco Nexus low-latency, Exablaze) over store-and-forward — flat, frame-size-independent, sub-microsecond latency. The CRC-propagation trade-off (§60.2) is acceptable on clean colo links; monitor error counters to confirm the links are clean.
- **Layer-1 for fan-out and replication.** Where you're *distributing* a signal (a market-data feed to multiple strategy hosts, a tap to multiple capture/analytics consumers — Ch. 75, or an order feed to redundant systems), use a **layer-1 replicator** (Arista 7130 Metamux/Metaconnect, Exablaze) — sub-nanosecond, no buffering, deterministic. This is how one feed reaches many consumers without a switching-latency penalty per consumer.
- **Minimize hop count.** Every hop is a switch latency *and* a queuing point (§60.5). Lay out the path (and choose your colo cross-connects — Ch. 6) to traverse the **fewest switches** between the exchange handoff and your NIC, and between your NIC and the order gateway. One cut-through hop beats three; a direct layer-1 cross-connect beats a switched path where topology allows.
- **Consistent, high link speeds.** Keep link speeds uniform and high (25/100 G) along the path to minimize serialization delay (§60.2); a single slower segment reintroduces serialization latency. Match the exchange's handoff speed.

### 60.4.2 Fabric layout, buffering, and microburst handling

- **Avoid oversubscription on the hot path.** A switch is oversubscribed when more input bandwidth can arrive than an output port can send; under a burst (Ch. 53) that forces queuing and jitter. Provision the hot path so the output port can absorb the worst burst without queuing — the fabric analog of Ch. 53's RX-ring sizing. Non-oversubscribed cut-through is what holds a flat tail.
- **Understand switch buffering and its trade-off.** Deep buffers absorb bursts *at the cost of latency* (bufferbloat — a queued frame waits behind others); shallow buffers keep latency low but drop under a burst (Ch. 53's hardware drops). The low-latency choice is usually **shallow buffers + no oversubscription** (don't queue; don't drop because you didn't overload), not deep buffers (which trade your latency for burst tolerance). Match buffer policy to the traffic: market data (bursty, drop-tolerant with A/B — Ch. 54) differs from order entry (must not drop).
- **Handle head-of-line blocking.** In shared-buffer or input-queued switches, a congested output can stall unrelated traffic behind it (head-of-line blocking). Virtual output queuing (VOQ) and proper switch selection avoid it; on the hot path, isolate market-data and order-entry paths so one can't block the other.
- **Symmetric paths for measurable latency.** Lay out the fabric so the two directions (data-in, order-out) are *symmetric* where possible — it makes one-way latency measurable and predictable (§60.3), and avoids surprises where one direction traverses an extra hop.

### 60.4.3 Switch-based replication, timestamping, and taps

Fabric hardware does more than forward — it can timestamp, replicate, and tap, offloading work from the host:

- **Switch/fabric hardware timestamping (ties Ch. 55, 58).** Low-latency switches and layer-1 devices can **timestamp every packet** as it passes, with nanosecond (or sub-ns) precision, and even append the timestamp to the frame. This gives you a fabric-level, host-independent latency measurement and capture-time source (Ch. 75) — the switch tells you exactly when each packet crossed, disciplined by PTP (Ch. 58).
- **Replication for capture and redundancy.** A layer-1 replicator or a switch's mirror/SPAN (Ch. 75) copies the feed to capture/analytics hosts *without* adding latency to the production path — the compliance-grade capture (Ch. 75) taps the wire at the fabric, below and beside the trading host. This is why capture belongs at the fabric, not in the application (Ch. 75.4.1).
- **Aggregation taps.** A tap-aggregation layer collects taps from many links into capture infrastructure (Ch. 75), timestamped at the fabric — the observability backbone (Ch. 76) for a trading system, built from switch/tap hardware rather than host CPU.

## 60.5 Pitfalls & anti-patterns: store-and-forward, oversubscription, and hidden hops

- **A store-and-forward switch on the hot path (the cardinal fabric error).** It pays full frame serialization on every hop and queues under load — microseconds of latency that grows with frame size and spikes under a burst (§60.3), dwarfing your software budget (Ch. 76). Use cut-through (or layer-1) on any path that matters.
- **Oversubscription manufacturing tail latency.** An oversubscribed switch queues under the market-open burst (Ch. 53), turning a flat cut-through latency into a jittery, load-dependent tail (§60.3). Provision the hot path non-oversubscribed; the tail you see at the open is often the switch, not your code.
- **Hidden extra hops.** A path that looks direct but traverses an extra aggregation switch, a firewall, or a mismatched cross-connect — each hop is latency and a queuing point. Trace the *actual* physical path (Ch. 6 colo cross-connects) and count hops; the fewest wins (§60.4.1).
- **Measuring RTT/2 as one-way latency on an asymmetric path.** If the two directions traverse different switches/queues (common), RTT/2 is not the one-way latency — you'll misattribute where the time goes. Measure each direction with synchronized hardware timestamps (PTP, Ch. 58; §60.3).
- **Software timestamps for fabric measurement.** Timing packets with host software clocks includes the host's own jitter (Ch. 41) — the thing you're trying to exclude when measuring the *fabric*. Use NIC/switch hardware timestamps (Ch. 55, 58).
- **Deep buffers "for reliability."** Choosing a deep-buffer switch to avoid drops trades your latency for burst tolerance (bufferbloat) — a queued frame is a slow frame. For drop-tolerant market data (A/B recovery handles loss — Ch. 54), shallow buffers + no oversubscription keep the tail flat; reserve deep buffering for paths that truly can't drop and aren't latency-critical (§60.4.2).
- **Mismatched link speeds.** A slower link segment in the path reintroduces serialization delay (§60.2) and a speed-mismatch buffering point. Keep the path uniform and high-speed.
- **Ignoring the fabric entirely.** Optimizing the host to the nanosecond while the fabric adds microseconds of variable latency — the classic mis-optimization (§60.1). The fabric is a measured component of tick-to-trade (Ch. 76), not an externality.

## 60.6 Exercises & checklist

**Exercises:**

1. **Store-and-forward vs cut-through vs frame size.** Measure a switch's per-hop latency (hardware timestamps, Ch. 58) at 64, 512, and 1500 bytes. Store-and-forward grows with size; cut-through stays flat — use the test to classify a switch regardless of its datasheet (§60.3).
2. **One-way, not RTT.** Set up synchronized clocks (PTP, Ch. 58) at two points and measure one-way latency in each direction; construct an asymmetric path and show RTT/2 misleads (§60.3, §60.5).
3. **Burst behavior.** Replay a market-open microburst (Ch. 53) through a cut-through switch, non-oversubscribed vs oversubscribed; measure the latency *distribution* (Ch. 3) and show oversubscription manufactures a tail (§60.3, §60.5).
4. **Hop count.** Trace the actual physical path from the exchange handoff to your NIC; count switches and their types; identify a hop you can remove or convert to cut-through/layer-1 (§60.4.1).
5. **Layer-1 fan-out.** Distribute one feed to multiple consumers through a layer-1 replicator vs a switched path; measure the added latency per consumer (near zero for layer-1) (§60.4.1, §60.4.3).

**Checklist:**

- [ ] Every switch on the tick-to-trade path is **cut-through** (or **layer-1** for fan-out) — no store-and-forward on the hot path (§60.4.1, §60.5).
- [ ] The path traverses the **fewest hops** possible, with **uniform high link speeds** (minimal serialization) (§60.4.1).
- [ ] The hot path is **not oversubscribed**; buffer policy suits the traffic (shallow + no oversubscription for market data) (§60.4.2, §60.5).
- [ ] Per-hop and **one-way** latency are measured with **hardware timestamps and synchronized clocks** (Ch. 58) — never RTT/2 on an asymmetric path (§60.3).
- [ ] The fabric's **latency distribution under a market-open burst** (Ch. 53) is measured, not just the idle median (§60.3).
- [ ] **Fan-out / replication** uses layer-1 devices; **capture** taps the wire at the fabric (Ch. 75) without adding hot-path latency (§60.4.3).
- [ ] **Fabric/switch hardware timestamping** (Ch. 58) feeds latency monitoring and capture (Ch. 75–76) (§60.4.3).
- [ ] The **actual physical path** (Ch. 6 colo) is traced — no hidden hops, mismatched speeds, or asymmetric detours (§60.5).

## 60.7 References

- Low-latency switch vendor documentation — **Arista 7130/7150** (and Metamux/Metaconnect layer-1), **Cisco Nexus 3550/low-latency**, **Exablaze/Cisco** — cut-through and layer-1 latency specs and timestamping (§60.2, §60.4).
- IEEE 802.3 (Ethernet) and switching-architecture references — store-and-forward vs cut-through, serialization, head-of-line blocking, VOQ (§60.2).
- Ch. 58 (PTP / hardware timestamping — how to measure the fabric), Ch. 55 (NIC timestamping), Ch. 53 (microbursts — where the fabric queues), Ch. 6 (colocation — the physical path), Ch. 75 (capture — fabric taps).
- Exchange colocation and connectivity guides — the handoff speed, cross-connects, and fabric between the matching engine and the participant (§60.1, §60.4).

## 60.8 Additional Reading

- Vendor and practitioner material on HFT network fabrics, cut-through switching, and layer-1 devices — real low-latency fabric designs and their measured latencies (§60.2, §60.4).
- Papers and talks on datacenter fabric latency, microburst behavior, and buffer sizing — the tail-under-load behavior of §60.3/§60.5.
- Ch. 54 (*Reliable Multicast*) — the feed the fabric fans out and how loss is recovered; Ch. 58 (*PTP*) — the clock discipline behind one-way measurement; Ch. 6 (*System Setup*) — colocation and the physical path; Ch. 75 (*Capture*) — fabric-level tapping; **Appendix E** — switch-latency numbers in the latency table; **Appendix F** — cut-through/layer-1/serialization glossary.

---

*Next: Ch. 61 — eBPF, bpftrace & XDP / AF_XDP, opening Part X: programmable kernel observability and fast packet paths — the eBPF VM and verifier, low-overhead production tracing of latency and syscalls, and XDP/AF_XDP for in-driver packet processing between the kernel stack and full bypass.*
