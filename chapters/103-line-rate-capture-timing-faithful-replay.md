# Part XIII — Observability & Operations in Production

# Chapter 103 — Line-Rate Capture, Timing-Faithful Replay & Passive Wire Measurement

> **Prerequisites:** Ch. 102 (the journal, the storage and the deterministic-core replay — this chapter owns the *wire* side of capture and replay that Ch. 102 §102.4.1 and §102.4.5 point at), Ch. 60 (the kernel receive path — where an `AF_PACKET` tap sits), Ch. 67 §67.2.4 and §67.4.4 (microbursts and drop detection), Ch. 71 and Ch. 74 (NIC hardware timestamps and PTP — the capture time base), Ch. 73 (RSS/flow steering — `PACKET_FANOUT` is the capture-side analogue), Ch. 75 (precise scheduled transmission — `SO_TXTIME`/LaunchTime, the hardware-paced replay), Ch. 76 §76.4.3–§76.4.4 (switch replication, SPAN, taps and the passive-measurement method), Ch. 52 (timer slack — why a sleeping replayer cannot pace a burst), Ch. 77–78 (AF_XDP and DPDK as capture and replay engines), Ch. 108 (the counters that prove a capture lossless).
>
> **Leads into:** Ch. 109 §109.4.4 (golden-master tests cut from captures), Ch. 110 (the nightly replay gate runs this chapter's replayer against the tuned box), Ch. 111 §111.4.4 (shadow runs on a layer-1-replicated feed), Ch. 112 §112.4.5 (post-trade analytics joins wire captures with venue timestamps). Three chapters' exercises say "replay a captured market-open microburst" and one sentence in Ch. 102 names taps, SPAN and `AF_PACKET`; this chapter builds the capture point, the host capture stack that keeps up with the open, the replayer that reproduces its cadence onto the wire with a measured timing error, and the independent wire-level measurement a desk uses to check the application's own numbers.

---

## 103.1 Why it matters: the open is the only realistic test, and you only get one a day

Every receive-path decision in Part IX — ring sizes (Ch. 67), steering (Ch. 73), the batch caps of the hot loop (Ch. 46), conflation (Ch. 47) — is justified by how it behaves under the **market-open microburst**, and the open happens once per day per venue, on production hardware, with real money attached. The engineering answer is to **capture** the open once, losslessly, with timestamps good enough to reproduce its cadence, and **replay** it onto the wire of a test host as often as needed. Both halves are harder than they look. A capture that drops part of the burst (§103.3: a plain UDP socket with a 1 µs consumer lost **50 %** of a 1.1 Mpps burst while an `AF_PACKET` ring beside it lost nothing) is a capture of a *different* open; a replay that flattens 2 µs inter-packet gaps into 12 µs (§103.3: `clock_nanosleep` pacing) tests nothing about bursts. And a third thing rides on the same infrastructure: the firm's **independent measurement**. The application's latency ledger (Ch. 93) measures itself; a passive tap with a PTP-disciplined capture card measures the wire — tick in, order out — and it is the only number a desk, a venue or a regulator (RTS 25) accepts without an argument. Where the tap sits (optical splitter vs switch SPAN) decides whether that number is nanoseconds or microseconds wrong, and §103.2.1's table is the map.

## 103.2 Mental model

### 103.2.1 Capture-point fidelity: what each point loses

```
   capture point                      sees                               timestamp                        loses / adds                                        cost
   ────────────────────────────────   ────────────────────────────────   ──────────────────────────────   ─────────────────────────────────────────────────   ───────────────
   in-app journal (Ch. 102 §102.4.2)    decoded messages the app applied   app clock at apply (rdtsc)       everything the app dropped or never saw; app time    ~5–20 ns/msg on the hot path
   host AF_PACKET / TPACKET_V3        every frame the NIC delivered      kernel software stamp at RX       NIC/ring drops; ~µs stamp jitter (softirq)          one core; 0 on the hot path
     (§103.4.1), PF_RING, rte_pdump      to the host                        (or NIC hw stamp — Ch. 71)        (hw stamp: ~tens of ns)
   NIC port mirror / dual-port NIC    frames on that NIC's link           NIC hw stamp                      nothing on the link; the NIC's own drops            a NIC port
   switch SPAN / ERSPAN               frames on the mirrored ports        capture-side NIC stamp            mirror egress is a shared, oversubscribable queue:  switch config
                                                                                                             SILENT drops under load, µs of queueing jitter,
                                                                                                             ERSPAN adds GRE encapsulation and reordering
   passive optical tap + L1           every bit on the fiber             capture card hw stamp,            nothing (passive); a fixed ~ns propagation delay     tap + card; the
     replicator (Ch. 76 §76.4.3)                                           PTP-disciplined                                                                        reference point
   capture card / appliance           both directions, every link        hw stamp ≤ ~10 ns, PTP/PPS        nothing up to line rate; the appliance's own          Napatech/Endace/
     (timestamped mirror, analytics)                                       (White Rabbit sub-ns, Ch. 74)      drop counters are the proof                          ExaNIC/MetaWatch/
                                                                                                                                                                  Corvil-class
```

Two rules fall out. **Capture below the layer that drops**: the application loses to its own back-pressure, the socket to its buffer, the NIC to its ring; a point *below* each sees what the one above lost, and the tap sees everything. **The timestamp belongs to the lowest point too**: a software stamp is taken when the kernel's RX path got to the packet (on the VM in §103.3 that is ~0.7 µs after the sender's stamp at the median and milliseconds in the tail when the sender was descheduled — the capture is timing the *host*, not the wire); a NIC hardware stamp is the arrival at the MAC; a tap card's stamp is the arrival at the splitter, which is the wire. SPAN is the trap in the middle: it looks like a tap, costs nothing, and under exactly the load you care about its mirror queue drops frames without a counter the trading host can see and delays the rest by a queue's worth — Ch. 76 §76.4.4 gives the method for measuring that error on a live fabric.

### 103.2.2 The host capture stack: rings, blocks and fan-out

A `recvfrom()` per packet is a syscall per packet (Ch. 60) and a socket buffer that overflows silently; a line-rate capture on the host is a **memory-mapped ring the kernel fills and the capturer walks**:

```
   AF_PACKET + PACKET_RX_RING (TPACKET_V3)     kernel writes frames into mmap'd BLOCKS (e.g. 64 × 4 MiB); a block is handed to user space when full
   ─────────────────────────────────────────   or after tp_retire_blk_tov ms; the capturer walks tpacket3_hdr records (tp_mac → frame, tp_sec/tp_nsec,
                                               sll_pkttype) and returns the block. One syscall (poll) per BLOCK, not per packet; PACKET_STATISTICS counts
                                               the kernel's drops (ring full). PACKET_FANOUT spreads flows over N rings/cores (hash, CPU, queue — Ch. 73).
   PF_RING ZC / DPDK rte_pdump / AF_XDP        bypass the kernel's RX path: the NIC DMAs into the capturer's buffers (Ch. 77–78); hardware timestamps
                                               in the descriptor; the only options past ~5–10 Mpps per core, and the only ones that do not perturb a
                                               busy-polling trading process on the same host (an AF_PACKET tap still runs in the softirq — Ch. 60).
   capture card / appliance                    the NIC IS the capture engine: every frame timestamped in hardware, to pcap/pcapng or a proprietary
                                               ring, with per-port drop counters; the host only writes to disk (Ch. 102 §102.4.3).
```

`TPACKET_V3`'s block design is the latency/efficiency knob made explicit: a full 4 MiB block is ~20 000 small frames of batching (§103.3 saw up to 12 800 datagrams per block during the burst) and the retire timer bounds how long a quiet period keeps a partial block from the consumer. For capture that is ideal; for *live* use (a feed handler on `AF_PACKET`) the retire timeout is a latency floor, which is why the live path uses AF_XDP or bypass (Ch. 77–78) and leaves `AF_PACKET` to the capture box.

### 103.2.3 Timestamps and formats

The wire format for a capture is **pcapng** (Ch. 102 §102.4.1): nanosecond timestamps with the resolution declared per interface, an *Interface Statistics Block* carrying the capture's own drop counts, and comments for the capture point and clock source. `pcap` has microsecond stamps unless the nanosecond-precision variant is used — a 2 µs burst gap is *two* µs ticks, and a replayer fed microsecond stamps reproduces a different open. Hardware stamps reach the file three ways: the NIC's `SOF_TIMESTAMPING_RX_HARDWARE` into the ring record (Ch. 71), a **timestamp trailer** appended to the frame by a switch or tap (Arista 7130/MetaWatch, Cisco Nexus ERSPAN headers) that the reader must strip and interpret, or the capture card's native format. Whatever the source, the file must record *which clock* stamped it and how it was disciplined (Ch. 74); the post-trade join in Ch. 112 §112.4.5 compares these stamps with the venue's.

### 103.2.4 Replay is a timing problem, not a copying problem

Replaying a capture onto the wire has an obvious half — rewrite the destination MAC/IP, send the bytes — and a hard half: **send each packet at the captured instant**, relative to the first. The captured gaps at the open are 1–3 µs inside a burst and hundreds of µs between bursts (§103.3's file: p50 2.0 µs, p99 350 µs); a replayer that sleeps between packets inherits the kernel timer's overshoot (Ch. 52: tens of µs on a tuned box, more on a VM), so bursts come out stretched and the receiver under test never sees the queue depth the real open produced. The options, in increasing fidelity: `tcpreplay -t` (as fast as possible — tests throughput, not timing), `tcpreplay -p`/`--mbps` (rate shaping — average rate, no burst structure), a **busy-spinning software pacer** on a dedicated core (§103.3: median gap error 40 ns, p99 2 µs, a ~1.5 ms tail when preempted), **hardware-timed transmission** (`SO_TXTIME` with the ETF qdisc or NIC LaunchTime — Ch. 75 — sub-µs per packet, no spinning), and **DPDK-based replayers** (MoonGen's timestamped pcap replay, TRex's stateless mode) that pace from the capture's stamps at line rate with hardware timestamps on the way out. The validation is the same in every case: capture the *replayed* stream with the tap and compare its gaps with the original's.

## 103.3 Measure it: the host capture stack under a burst, and the replayer's timing error

*Toolchain and machine: Appendix L.3. Built with GCC 13.3 (`-O2 -std=c++20 -pthread`), run on the 4-vCPU Intel Xeon VM of Ch. 46 §46.3 (TSC 2.70 GHz, Linux 6.18, no isolation, no NIC with hardware timestamps — everything here crosses `lo`, so the timestamps are the kernel's software stamps and the burst is bounded by the loopback stack at ~1.1 Mpps). Ratios and mechanisms transfer; the absolute pps and the tail outliers (hypervisor steal) do not. Labelled VM-measured.*

**Capture harness** (`pktcap.cpp`): two or three sender threads blast 10⁶ 200-byte datagrams at a UDP port as fast as the stack allows (an open compressed onto loopback); the **application** receives them with `recvmsg` + `SO_TIMESTAMPNS` and does a configurable amount of work per packet; beside it an **`AF_PACKET` `TPACKET_V3` ring** (64 × 4 MiB blocks, 1 ms retire) on `lo` captures the same stream, skipping the TX copies loopback shows. Each datagram carries the sender's `CLOCK_REALTIME` stamp, so both capture points report *their stamp − send stamp*.

```
burst: 1 000 000 × 200 B in 883–926 ms = 1.08–1.13 Mpps (2–3 senders)

application (UDP socket, recvmsg + SO_TIMESTAMPNS)                        received            lost      stamp − send stamp  p50 / p99 / max
  default SO_RCVBUF (212 KB), no work per packet                            994 627 / 10⁶       0.5 %     0.9 / 2.1 µs / 3.9 ms
  default SO_RCVBUF, 1 µs of work per packet                                493 092 / 10⁶      50.7 %     0.7 / 1.6 µs / 0.6 ms
  SO_RCVBUF 4 MiB,    1 µs of work per packet                               466 401 / 10⁶      53.4 %     0.9 / 2.4 µs / 6.1 ms
AF_PACKET TPACKET_V3 ring beside it (every run)                           1 000 000 / 10⁶       0         0.7–0.9 / 1.5–2.3 µs / same max as the app
  kernel PACKET_STATISTICS: 2 000 000 frames seen (RX + TX copies on lo), 0 drops, 0 queue freezes;  883–927 blocks consumed, up to 2 699–12 787 datagrams per block
  with 8 × 4 MiB blocks instead of 64: still 0 drops at this rate
```

**Replay harness** (`replay.cpp`): a synthetic open — 20 000 records, bursts of 5–60 packets at 1–3 µs gaps separated by 50–500 µs lulls, 203 ms in all (gap p50 2.0 µs, p99 351 µs, max 500 µs) — replayed onto a loopback UDP socket four ways; the receiver stamps arrivals with `SO_TIMESTAMPNS`; the error is *arrival gap − captured gap* per packet.

```
pacer                                               |gap error| p50 / p90 / p99 / max          whole-file span (captured 203.44 ms)   sender lateness vs schedule p50 / p99
no pacing (tcpreplay -t style)                      0.94 / 1.78 / 349 / 499 µs                  22.2 ms   (−89 %: the open compressed 9×)   —
clock_nanosleep(TIMER_ABSTIME) per packet            10.3 / 11.1 / 338 / 488 µs                 247.5 ms  (+22 %)                          23.5 ms / 44 ms (accumulates inside bursts)
  …with PR_SET_TIMERSLACK = 1 µs                     10.7 / 13.0 / 339 / 511 µs                 261.0 ms  (+28 %)                          26.9 ms / 57 ms  (slack is not the problem here)
busy-spin on CLOCK_MONOTONIC, one core               0.04 / 0.46 / 2.0 / 1 520 µs               203.42 ms (−0.0 %)                        0.05 µs / 59 µs (max 1.5 ms: preempted)
busy-spin at 10× speed (gaps 0.1–0.3 µs)             1.09 / 1.33 / 33.7 / 48.8 µs               26.5 ms vs 20.3 (+30 %)                   3.3 ms / 6.1 ms  (send() ≈ 1 µs/pkt is the floor)
```

Reading it:

- **The capture point decides what the capture contains.** The same burst, the same host, the same instant: the application lost half of it once it had 1 µs of work per packet (the burst arrived every 0.9 µs), and a bigger socket buffer did not help — the consumer was slower than the arrival rate for longer than any buffer; the `AF_PACKET` ring beside it kept every frame with zero kernel-reported drops, because its consumer does one `poll()` per *block* and the block is 4 MiB of batching. This is Ch. 102 §102.3's "capture below the lossy layer" with a number on it, and the reason an in-app journal is a record of what the application *applied*, not of what arrived.
- **The kernel's stamp is the host's time, not the wire's.** Both points stamp in the same place on `lo` (`netif_rx`), so they agree — ~0.7–0.9 µs after the sender's stamp at the median, 1.5–2.4 µs at p99, and milliseconds in the tail *because the sender was descheduled between stamping and sending*. On a real NIC the hardware stamp (Ch. 71) removes the host from the measurement; a software-stamped capture inherits softirq scheduling and should be labelled as such in the pcapng.
- **Blocks are the batching knob.** Up to 12 787 datagrams per block during the burst means the capturer handled the open in ~80 wake-ups; 8 blocks were enough here, 64 is the margin for a slower disk writer (Ch. 102 §102.4.3) — the ring must absorb `(peak − writer rate) × burst length`, the same arithmetic as the journal ring.
- **Only a spinning pacer reproduces the cadence.** Unpaced replay compressed the open 9× — a throughput test. `clock_nanosleep` stretched every in-burst gap by ~10 µs (the oversleep on this VM — Ch. 52 §52.3 measured the same hrtimer floor) and the lateness accumulated to 44 ms inside bursts before the lulls absorbed it; shrinking timer slack changed nothing, because slack is not what limits a 2 µs sleep here. The busy-spin pacer hit the schedule to 50 ns at the median and 2 µs at p99, with a 1.5 ms tail when the hypervisor took the core — on an isolated core (Ch. 56) that tail disappears, and `SO_TXTIME`/LaunchTime (Ch. 75) removes the spinning. At 10× speed the pacer fell behind: a loopback `send()` is ~1 µs, so the stack, not the pacer, sets the floor — faster-than-real-time wire replay is a DPDK job.

## 103.4 Techniques

### 103.4.1 A `TPACKET_V3` capture ring

```cpp
// pktcap.cpp (abridged) — the AF_PACKET mmap ring: one poll() per block, kernel-side drop counting, software or hardware timestamps per frame.
struct V3Ring {
    int fd; std::uint8_t* map; std::uint32_t block_size = 1 << 22, block_nr = 64, frame_size = 2048; std::vector<iovec> blocks;
    void open(const char* ifname, int fanout_group) {
        fd = ::socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL)); int v = TPACKET_V3; ::setsockopt(fd, SOL_PACKET, PACKET_VERSION, &v, sizeof v);
        tpacket_req3 req{}; req.tp_block_size = block_size; req.tp_block_nr = block_nr; req.tp_frame_size = frame_size; req.tp_frame_nr = (block_size / frame_size) * block_nr;
        req.tp_retire_blk_tov = 1;                                       // ms: hand over a partial block after 1 ms — the latency/efficiency knob
        ::setsockopt(fd, SOL_PACKET, PACKET_RX_RING, &req, sizeof req);
        map = static_cast<std::uint8_t*>(::mmap(nullptr, std::size_t(block_size) * block_nr, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_LOCKED, fd, 0));   // locked: no faults mid-burst (Ch. 33)
        for (std::uint32_t i = 0; i < block_nr; ++i) blocks.push_back({map + std::size_t(i) * block_size, block_size});
        sockaddr_ll ll{}; ll.sll_family = AF_PACKET; ll.sll_protocol = htons(ETH_P_ALL); ll.sll_ifindex = int(if_nametoindex(ifname)); ::bind(fd, reinterpret_cast<sockaddr*>(&ll), sizeof ll);
        if (fanout_group) { int fo = fanout_group | (PACKET_FANOUT_HASH << 16); ::setsockopt(fd, SOL_PACKET, PACKET_FANOUT, &fo, sizeof fo); }   // N rings, N cores, flows kept together (Ch. 73)
        // hardware stamps: setsockopt(fd, SOL_PACKET, PACKET_TIMESTAMP, SOF_TIMESTAMPING_RAW_HARDWARE) on a NIC that supports them (Ch. 71) — tp_sec/tp_nsec then carry the NIC's clock
    }
};
// consumer: walk the block the kernel handed over, return it, move on. On lo every frame appears twice (TX and RX): skip PACKET_OUTGOING.
auto* bd = static_cast<tpacket_block_desc*>(ring.blocks[cur].iov_base);
if (!(bd->hdr.bh1.block_status & TP_STATUS_USER)) { pollfd pf{ring.fd, POLLIN | POLLERR, 0}; ::poll(&pf, 1, 1); continue; }
auto* ph = reinterpret_cast<tpacket3_hdr*>(reinterpret_cast<std::uint8_t*>(bd) + bd->hdr.bh1.offset_to_first_pkt);
for (std::uint32_t i = 0; i < bd->hdr.bh1.num_pkts; ++i) {
    auto* sll = reinterpret_cast<sockaddr_ll*>(reinterpret_cast<std::uint8_t*>(ph) + TPACKET_ALIGN(sizeof(tpacket3_hdr)));
    if (sll->sll_pkttype != PACKET_OUTGOING) { std::uint8_t* frame = reinterpret_cast<std::uint8_t*>(ph) + ph->tp_mac; std::uint64_t t = std::uint64_t(ph->tp_sec) * 1'000'000'000ull + ph->tp_nsec; write_pcapng(frame, ph->tp_snaplen, t); }
    ph = reinterpret_cast<tpacket3_hdr*>(reinterpret_cast<std::uint8_t*>(ph) + ph->tp_next_offset);
}
bd->hdr.bh1.block_status = TP_STATUS_KERNEL; __sync_synchronize(); cur = (cur + 1) % ring.block_nr;
```

- **Pin the capturer and its ring** to a housekeeping core on the NIC's NUMA node (Ch. 17, 53); `MAP_LOCKED` the ring; size blocks for the writer's lag, not the burst's peak alone.
- **`PACKET_FANOUT`** for more than one core: `PACKET_FANOUT_HASH` keeps a flow on one ring (ordering within a feed preserved), `PACKET_FANOUT_CPU` follows the NIC queue's CPU (one ring per RSS queue — Ch. 73). Each ring reports its own `PACKET_STATISTICS`.
- **BPF filter on the socket** (`SO_ATTACH_FILTER`, classic BPF; or an XDP program redirecting to AF_XDP — Ch. 77) to capture only the feed's groups and the order session's ports, so the ring holds market data rather than management traffic.
- **Beyond the kernel's rate**: PF_RING ZC, DPDK `rte_pdump` (a secondary process mirroring the primary's queues into a pcap — the way to capture *what the bypass stack saw*, Ch. 78), AF_XDP with hardware stamps where the driver supports them. The structure (ring, batch, timestamp, drop counter, pcapng) is the same.

### 103.4.2 Proving the capture lossless

A capture is lossless when every layer below the capturer says so, *for that session*: the NIC's `ethtool -S` drop counters unchanged across the open (Ch. 108 §108.2.1's per-layer map: `rx_missed`/`rx_no_buffer` is the host ring, `rx_fifo`/`discards_phy` the port), `PACKET_STATISTICS.tp_drops == 0` (the AF_PACKET ring never filled), the capturer's own ring-to-disk queue never wrapped (Ch. 102 §102.5), and — the only check that spans everything — **the feed's sequence numbers have no gaps** in the capture that the A/B arbitration (Ch. 69) could not explain. Write all four into the pcapng's Interface Statistics Block and the capture's manifest (Appendix N); a capture without them is an anecdote. On a tap/appliance the proof is the card's per-port counters, read before and after.

### 103.4.3 Timing-faithful replay onto the wire

```cpp
// replay.cpp (abridged) — pace from the capture's timestamps: t_i = base + (cap[i].t − cap[0].t) / speed. A spinning pacer on an isolated core;
// SO_TXTIME (Ch. 75) replaces the spin on hardware that supports LaunchTime.
std::uint64_t base = mono_ns() + 2'000'000;                                           // start 2 ms out so the first packets are not late
for (std::uint32_t i = 0; i < n; ++i) {
    std::uint64_t due = base + std::uint64_t(double(cap[i].t_ns - cap[0].t_ns) / speed);
    while (mono_ns() < due) _mm_pause();                                              // the only pacer that held 2 µs gaps on the VM (§103.3)
    rewrite_l2_l3(cap[i].bytes, dst_mac, dst_ip);                                    // the replay target's addresses; keep UDP payloads byte-identical
    ::send(tx, cap[i].bytes, cap[i].len, 0);                                          // or a DPDK/AF_XDP TX ring with per-packet launch times
}
// validation: capture the REPLAYED stream (tap, or the AF_PACKET ring of §103.4.1) and report |arrival gap − captured gap| per packet and the span drift
```

- **Keep the payload byte-identical** — sequence numbers, timestamps inside the messages, everything: the feed handler under test must see the venue's bytes, including the gaps and the out-of-order packets the capture recorded (Ch. 67 §67.4.4). Rewrite only the L2/L3 envelope. Replaying *both* A and B lines with their relative timing is what tests arbitration.
- **Speed is a parameter, fidelity is a measurement**: replay at 1× for the receive path, at 0.1× to isolate the decode cost from the queueing, at 10× only with a DPDK/hardware pacer (§103.3's loopback floor). Report the gap error distribution and span drift with every run — a replay whose error is not measured is a different open.
- **`SO_TXTIME` / LaunchTime (Ch. 75)**: hand the kernel or NIC a per-packet transmit time; the ETF qdisc or the NIC's launch-time engine sends at that instant with sub-µs accuracy and no spinning core. The capture's stamps become transmit deadlines directly.
- **Off-the-shelf**: `tcpreplay` (`-t` flood, `-p`/`--mbps` shaping, `--timer=nano` for a best-effort nanosleep pacer — §103.3 shows what that buys), MoonGen (Lua over DPDK; pcap replay with timestamps and hardware TX stamps), TRex (stateless replay at line rate). Measure their error the same way before trusting one.

### 103.4.4 Validating replay fidelity with the tap

The replay target's host sees the replayed stream the same way it sees the real one, so the same capture point validates both: tap the replay link, capture with hardware timestamps, and compare the replayed gaps with the original's as §103.3 does — per-packet error distribution, cumulative drift, and burst *shape* (the queue depth implied by consecutive gaps below the receiver's service time). A desk-grade replay rig is therefore a replayer on one host, a tap, and a capture card — the same three things the production capture uses, and often the same hardware on a weekend (Ch. 111 §111.4.6).

### 103.4.5 Passive wire measurement: what to compute from the tap

With the tap on the feed link and another on the order link (or one appliance on both), the wire-level numbers are: **wire-to-wire tick-to-trade** (feed packet arrival → order packet departure, both from the appliance's PTP clock — the number Ch. 112 §112.3 decomposes and the one a venue's own timestamps can be checked against), **per-hop latency** through each switch (Ch. 76 §76.3), **gap and loss on the feed** independent of the host, and **order-path RTT to the venue** from the order's departure to the ack's arrival. Ch. 76 §76.4.4 gives the SPAN-vs-tap error measurement and the appliance landscape; Ch. 112 §112.4.5 joins the tap's stamps with the venue's to decide races. The rule for the trading host is that *its* numbers (Ch. 93) must agree with the tap's to within the tap's resolution — a drift between them is a clock problem (Ch. 74) or an instrumentation bug, and the tap is the one that is right.

### 103.4.6 What this chapter takes from Ch. 102

Ch. 102 keeps the journal formats, the off-hot-path writer, storage and indexing, the deterministic-core replay and the columnar research tier; its §102.4.1 is now a paragraph pointing here for the capture point and the host stack, and its §102.4.5 for replay *onto the wire*. Ch. 112 keeps the CI gate and the state-machine replay. The three exercises that asked for a replayed open (Ch. 62, 67, 76) are satisfied by §103.4.3's replayer and §103.4.4's validation.

## 103.5 Pitfalls & anti-patterns

- **Capturing in the application and calling it the wire.** The in-app journal is what the application applied — §103.3's 50 % loss never appears in it. Capture below the layer that drops; label the altitude in the file.
- **SPAN as a measurement point.** The mirror queue is shared and oversubscribable; its drops are silent and its delay is load-dependent. Use a passive tap for anything whose timestamps or completeness matter (Ch. 76 §76.4.4).
- **Microsecond pcap for a microsecond burst.** `pcap` stamps are µs unless the nanosecond variant is used; a replayer fed them rounds every in-burst gap. Use pcapng with ns resolution declared, and record the clock source.
- **Sleeping replayers.** `nanosleep` pacing stretched every in-burst gap by ~10 µs on the VM and tens of µs on tuned hosts (Ch. 52). Spin on an isolated core or use `SO_TXTIME`/DPDK; always report the measured gap error.
- **Trusting the capture box's silence.** `PACKET_STATISTICS`, `ethtool -S`, the writer's ring and the feed's sequence numbers are the proof (§103.4.2); a capture without them is assumed lossy.
- **Rewriting payloads to "make the replay work."** Patched sequence numbers or timestamps inside the messages change what the receiver's arbitration and staleness logic see; rewrite only the L2/L3 envelope.
- **A capturer on the trading core.** An `AF_PACKET` ring still runs its copy in the softirq of the CPU that received the frame; steer the capture queue and the capturer to housekeeping cores (Ch. 53, 73) or capture at the fabric.

## 103.6 Exercises & checklist

1. **Fidelity on real hardware.** Run `pktcap` with hardware timestamps (`PACKET_TIMESTAMP` + `SOF_TIMESTAMPING_RAW_HARDWARE`) on a NIC that supports them and compare *hw stamp − software stamp* per packet under the burst; relate the tail to `softirq` scheduling (Ch. 60).
2. **Prove it lossless.** Capture a real feed's open with §103.4.1; produce the four-part proof of §103.4.2 and an Interface Statistics Block that carries it.
3. **Pace it three ways.** Replay your capture with `tcpreplay --timer=nano`, the spinning pacer on an isolated core, and `SO_TXTIME`; report gap-error distributions and span drift for each.
4. **Both lines.** Replay A and B with their captured relative timing into a feed handler and verify that its arbitration statistics (Ch. 69) match the live day's.
5. **SPAN vs tap (with Ch. 76 §76.4.4).** On a fabric you control, mirror a link to one capture port and tap it to another; capture both during a replayed burst and measure the SPAN stream's extra delay and drop count.

Checklist:

- [ ] The capture point is **below the layer that drops** and its altitude is labelled in the file (§103.2.1); compliance capture is at a tap or appliance, never only in-app.
- [ ] Host capture uses a **memory-mapped ring** (`TPACKET_V3`/PF_RING/`rte_pdump`/AF_XDP) on a housekeeping core, with `PACKET_FANOUT` or RSS as needed (§103.4.1).
- [ ] Losslessness is **proved** per session: NIC counters, ring drops, writer queue, feed sequence numbers (§103.4.2), written into the pcapng and the manifest.
- [ ] Timestamps are nanosecond, hardware where available, with the clock source and discipline recorded (§103.2.3).
- [ ] Replay paces from the capture's stamps with a spinning or hardware pacer, rewrites only L2/L3, and **reports its gap error** every run (§103.4.3).
- [ ] The application's latency numbers are reconciled against the tap's (§103.4.5); the tap wins.

## 103.7 References

- `packet(7)` and `Documentation/networking/packet_mmap.rst` (Linux kernel) — `PACKET_RX_RING`, `TPACKET_V3` block descriptors and `tp_retire_blk_tov`, `PACKET_FANOUT`, `PACKET_STATISTICS`, `PACKET_TIMESTAMP`.
- `Documentation/networking/timestamping.rst` — `SOF_TIMESTAMPING_*`, hardware RX/TX stamps on `AF_PACKET` and UDP sockets; `SO_TIMESTAMPNS`.
- The pcapng specification (IETF `draft-ietf-opsawg-pcapng`) — Interface Description/Statistics Blocks, timestamp resolution.
- `tcpreplay` documentation (`tcpreplay(1)`, `--timer`, `-p`, `-t`); MoonGen (Emmerich et al., IMC 2015) and TRex documentation — DPDK-based paced replay with hardware timestamps.
- `tc-etf(8)` and `SO_TXTIME` (Ch. 75) — time-based transmission.
- ntop PF_RING ZC and DPDK `rte_pdump`/`dpdk-pdump` documentation — bypass-side capture.
- Ch. 76 §76.4.4 (taps vs SPAN), Ch. 102 (journals and storage), Ch. 108 (drop counters), Ch. 112 §112.4.5 (the post-trade join).

## 103.8 Additional Reading

- Vendor documentation for capture cards and timestamped mirrors — Napatech, Endace (DAG), Cisco/Exablaze ExaNIC, Arista 7130 MetaWatch, and the Corvil-class latency-analytics appliances — for what "lossless at line rate" and "timestamp accuracy" mean in each product's terms.
- Ch. 112 §112.3 and §112.4.5 — how the wire-to-wire number from the tap is decomposed and joined with order outcomes.
- ESMA RTS 25 (clock synchronisation and timestamp granularity for trading records) — why the tap's PTP discipline is a compliance matter as well as an engineering one.

---

*Next: Ch. 104 — The Sequencer: Total-Order Broadcast & Replicated Deterministic Consumers: the component that produces the single ordered input stream Ch. 102 journals and this chapter's captures are reconciled against — one process stamping a global sequence on ticks, acks, timer fires, commands and strategy decisions, fanned out over shm and reliable multicast to replicas that hold identical state and prove it with a digest.*
