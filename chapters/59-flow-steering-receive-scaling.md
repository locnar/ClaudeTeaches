# Part IX — Market Data, NIC & Fabric

# Chapter 59 — Flow Steering & Receive Scaling

> **Prerequisites:** Ch. 42 (core pinning — steering is its receive-side counterpart), Ch. 57 (NIC features — RSS, queues, filters), Ch. 58 (DDIO — steering to a core on the NIC's socket keeps data L3-hot in the right socket's L3), Ch. 44 (CAT — the isolated core the flow steers to), Ch. 48 (native I/O — the receive path), Ch. 16 (NUMA).
>
> **Leads into:** Ch. 62 (the fabric that delivers the flows). The receive-side deep-dive: getting the right packet to the right core with no cross-core handoff, and scaling receive across cores without scattering a feed.

---

## 59.1 Why it matters: the right packet must reach the right core

Core pinning (Ch. 42) put the hot thread on an isolated core, DDIO (Ch. 58) lands received packets in that core's socket-shared L3, and CAT (Ch. 44) protects its cache. But all of that assumes the packet the hot thread wants actually *arrives on the queue that core polls*. By default it doesn't. A modern NIC has many receive queues and spreads incoming packets across them using **RSS (Receive Side Scaling)** — a hash of the packet's flow tuple picks a queue, pseudo-randomly. So the market-data multicast group your strategy needs might land on queue 7 (consumed by core 12) while your hot thread polls queue 0 on core 3 — and now every packet crosses cores: a cross-core handoff (Ch. 33's cache-line bounce on the queue and descriptor lines, a wakeup, a queue transfer) and — if the consuming core sits on the other socket — the data in the *wrong* L3 (Ch. 58's cross-socket penalty; within one socket the L3 is shared, so the intra-socket cost is the handoff itself), adding latency and jitter to the receive path you worked so hard to shorten.

**Flow steering** is the receive-side discipline that fixes this: directing a *specific* flow (a multicast group, an order session, a feed) to a *specific* queue, consumed by a *specific* pinned core — so the packet the hot thread wants arrives *on the queue that thread polls*, lands in *its socket's* L3 (DDIO — Ch. 58), and is decoded (Ch. 54) with no cross-core handoff. It's the missing half of the isolation story: Ch. 42 pinned the *thread*, Ch. 59 steers the *packets* to it. Without it, all the core/cache isolation is undermined by packets arriving on the wrong core.

The second problem is **scaling receive across cores**: a single core can't process all the traffic at high rates, so you want to spread it — but *deliberately* (each feed to its owning core), not RSS's hash placement, which puts each flow on a queue you did not choose and spreads a multi-channel feed (several groups/ports, the A and B lines) across cores while leaving them unbalanced. The tools — RSS and RSS contexts, exact-match flow steering (Flow Director / `rte_flow`), aRFS, `SO_REUSEPORT`, and batched receive (`recvmmsg`) — are how you control *which* core gets *which* traffic. This chapter explains the RX-to-core-to-cache chain (§59.2), measures the cross-core handoff cost and steering benefit (§59.3), details the steering and scaling techniques (§59.4), and warns about scattering a feed and steering to the wrong core (§59.5). It's the "get the packet to the core that owns it" chapter.

## 59.2 Mental model: RSS, flow steering, and the RX-to-core-to-cache chain

Receiving a packet is a chain from wire to queue to core to cache, and each link can be controlled:

```
   wire -> NIC -> [which RX queue?] -> [which core polls it?] -> [which L3? (DDIO)] -> decode
                        │                      │                      │
                     RSS (hash)          queue<->core map        NIC-local socket (Ch.16,68)
                     or flow steering     (IRQ affinity /         + CAT ways (Ch.44)
                     (exact match)         poll assignment)
```

The mechanisms, from coarse to precise:

- **RSS (Receive Side Scaling).** The NIC hashes each packet's flow tuple (src/dst IP + port — a Toeplitz hash) and uses the hash to pick one of N receive queues — spreading load across queues/cores pseudo-randomly *per flow*. The hash is deterministic: every packet of one tuple lands on one queue, in order (RSS never reorders a flow). Good for *bulk* server load-balancing (spread many connections across cores); *bad* for the hot path, because the queue is one you did not choose — your feed lands wherever the hash sends it, split across queues only when it spans several tuples (a multi-group channel, the A/B lines), and not on the core you want. **RSS contexts / indirection tables** let you shape *which* queues a group of flows can hash to, a coarse steering knob.
- **Exact-match flow steering (the precise tool).** Instead of hashing, the NIC matches specific packet fields (n-tuple: dst IP/port, multicast group, VLAN) against **filter rules** and directs matching packets to a *specific* queue. Interfaces: **ethtool n-tuple filters**, **Intel Flow Director**, and **DPDK `rte_flow`** (the rich, programmable flow API — match on many fields, actions including queue, drop, mark, RSS-to-a-subset). This is how you say "multicast group X → queue 5" deterministically, overriding RSS for that flow.
- **The queue↔core mapping.** Each RX queue's interrupts (or poll assignment, for busy-poll/bypass — Ch. 48/64) map to a core (IRQ affinity — Ch. 42/57). Steer the flow to a queue whose core is your *isolated hot core* (Ch. 42) so the packet arrives where it's consumed.
- **aRFS (accelerated Receive Flow Steering).** A *dynamic*, kernel-driven version: the kernel observes which core a flow's socket is consumed on and programs the NIC to steer that flow there automatically — keeping RX on the consuming core without manual filter rules. The automatic counterpart to static flow steering.
- **`SO_REUSEPORT` (socket-level scaling).** Multiple sockets bound to the *same* port, with the kernel (or a BPF program) distributing incoming flows across them — so N threads each own a socket and the kernel load-balances (or you steer with BPF). Scales accept/receive across cores at the *socket* layer for **TCP accept and unicast UDP** — order-entry sessions, the unicast recovery/snapshot channels (Ch. 55) — complementary to NIC-level RSS/steering. It does **not** shard multicast: Linux delivers every multicast datagram to *every* socket bound to the group/port (`__udp4_lib_mcast_deliver` clones it to each listener), so a multicast feed is partitioned by *group* (one group per socket/core, steered at the NIC — §59.4.2), never by reuseport selection.
- **RPS/RFS, aRFS and XPS (the software layer).** **RPS** (Receive Packet Steering) is RSS in software — the kernel hashes and re-queues packets to other cores' backlogs (adds an IPI and a handoff; never on a hot core); **RFS** steers to the core where the flow's socket was last consumed; **aRFS** is RFS pushed into NIC hardware (needs `ethtool -K <if> ntuple on` and `rps_flow_cnt`); **XPS** picks the *transmit* queue per core so sends do not contend on a shared TX ring. A thread learns where its flow arrives with `SO_INCOMING_CPU`/`SO_INCOMING_NAPI_ID` — the bridge to NAPI busy-poll (Ch. 48).

The mental model: **RSS places each flow deterministically but on a queue you did not choose (good for bulk, bad for the hot path); flow steering places a specific flow on a specific queue/core (what the hot path needs); and the goal is the full chain — the feed's packets on the queue the hot core polls, landing in its socket's shared L3 (DDIO — Ch. 58), protected by its CAT ways (Ch. 44).** Steering is core-pinning (Ch. 42) extended to the packets.

## 59.3 Measure it: cross-core handoff cost and steering benefit

*Toolchain and machine: Appendix L.3.*

- **The cross-core handoff cost.** Measure receive-to-decode latency when the packet arrives on the *right* core (steered) vs the *wrong* core (RSS placed it there, requiring a cross-core handoff to the consuming thread). Procedure: a two-core receiver — (a) the consumer busy-polls the steered queue directly, (b) an RX core drains the queue and hands each packet over an SPSC ring (Ch. 34) to the consumer; per packet, NIC hardware RX timestamp (Ch. 57) → `rdtscp` at decode-complete (Ch. 17), HdrHistogram (Ch. 3); `perf stat -e mem_load_l3_hit_retired.xsnp_hitm -C <consumer-core>` for the HITM count; `ethtool -S <if> | grep rx_queue_` to prove which queue the packets took. Representative (Xeon Gold 6326, Linux 6.x, 25 GbE NIC; figures pending real runs):

| Arrival | Receive-to-decode | Why |
|---|---|---|
| **Steered to the consuming core** | baseline (fast) | packet in the right L3 (DDIO — Ch. 58), no handoff, decode starts hot |
| **RSS to a different core** (handoff) | + cross-core latency + cache miss | cache-line bounce (Ch. 33) + data in wrong L3 only if cross-socket (Ch. 16/58) + wakeup |

  The cross-core handoff adds the cache-line-transfer / HITM cost (Ch. 33, ~40–100 ns) *plus*, when the consuming core sits on the other socket, the data being in the wrong socket's L3 (within a socket the L3 is shared — the packet itself is an L3 hit; it is the queue/descriptor lines that bounce) *plus* any scheduling wakeup — turning a hot receive into a cold, jittery one. Steering removes all three.
- **RSS distribution and feed splitting.** Measure where RSS puts a feed (`ethtool -S` per-queue counters while subscribed). A single tuple always stays on one queue, in order; a *logical* feed that spans several tuples — several multicast groups/ports, the A and B lines, a sender varying its source port — lands on several queues/cores, so the channels of one instrument are decoded on different cores and A/B arbitration (Ch. 54) becomes a cross-core merge. Measure how many queues your feed occupies under RSS and on which cores — motivating exact per-group steering.
- **Per-core scaling.** For multi-feed / multi-strategy scaling, measure throughput and tail as you steer each feed to its own isolated core vs letting RSS balance — deliberate steering should give better locality (each feed's data stays in its core's private L1/L2 and its socket's L3) and better tail than random balancing.
- **DDIO-hit confirmation (Ch. 58).** With steering + NIC-local placement (Ch. 16), confirm the first read of a steered packet is an L3 hit (Ch. 58's measurement) — the payoff of the full RX-to-core-to-cache chain. Steering to a core on the wrong socket (Ch. 16) still lands data in the wrong L3, so steering and NUMA placement must agree.

The lessons: a cross-core handoff (RSS to the wrong core) adds cache-transfer + wrong-L3-miss + wakeup latency and jitter — the exact costs steering removes; RSS often *splits* an ordered feed across cores (bad); deliberate per-feed steering beats random balancing for locality and tail; and steering must agree with NUMA placement (Ch. 16) so the data lands in the *right* L3 (Ch. 58). Measure the handoff cost — it's the hidden tax of not steering.

## 59.4 Techniques

### 59.4.1 RSS and RSS contexts for deliberate spreading

- **Shape RSS with the indirection table / contexts.** Where you *do* want to spread bulk traffic across cores (many connections, a high-rate feed you process in parallel), use RSS but *shape* it: configure the RSS indirection table so flows hash only to the queues/cores you intend (not all cores, avoiding your isolated hot cores — Ch. 42). **RSS contexts** let different flow groups use different queue sets. This turns RSS from random scatter into *bounded* spreading over chosen cores.
- **Keep RSS off the isolated hot cores.** Configure RSS to hash only onto housekeeping/general cores, never the isolated hot cores (Ch. 42) — so background/bulk traffic never lands on a hot core and steals its cycles/cache. The hot cores receive *only* their steered flows (§59.4.2).
- **Tune the hash for feed integrity.** If a feed must stay on one queue, ensure the RSS hash keys don't split it (or steer it exactly, §59.4.2). Configure the hash fields (`ethtool -N ... rx-flow-hash`) so related packets share a queue where ordering/locality matters.

### 59.4.2 Exact flow steering: n-tuple, Flow Director, and `rte_flow`

- **Steer the hot feed to the hot core's queue.** The NIC-side mechanism (queues, RSS, filters, IRQ per queue) is §57.4.1; this section is the policy and the complete commands. Use exact-match filters to direct the specific flow — the market-data multicast group, the order-entry session, the drop-copy feed — to the queue polled by its *owning isolated core* (Ch. 42):

  ```
  $ ethtool -K eth0 ntuple on                                   # enable the exact-match filter table
  $ ethtool -N eth0 flow-type udp4 dst-ip 233.54.12.1 dst-port 26477 action 5   # group A -> queue 5
  $ ethtool -N eth0 flow-type udp4 dst-ip 233.54.12.2 dst-port 26477 action 6   # group B -> queue 6
  $ ethtool -n eth0                                             # list installed rules (+ their IDs)
  $ ethtool -X eth0 equal 4                                     # bound RSS to queues 0-3 (housekeeping cores)
  $ ethtool -N eth0 rx-flow-hash udp4 sdfn                      # hash UDP on src/dst IP + ports
  $ ethtool -S eth0 | grep -E 'rx_queue_(5|6)_packets'          # verify the packets took the steered queues
  ```

  (Queue → core is the IRQ-affinity/`irqbalance` setup of Ch. 42; on an i40e/ice part these rules program **Intel Flow Director**.) For bypass paths (Ch. 64) the same rule is a **DPDK `rte_flow`** (ETH/IPV4/UDP pattern → QUEUE action — the programmable flow API that matches many fields). This is the core technique: deterministic feed → queue → core.
- **`rte_flow` for rich steering (DPDK / bypass).** On a DPDK path (Ch. 64), `rte_flow` expresses sophisticated rules — match on multicast group, VLAN, protocol; actions of queue, RSS-to-a-subset, drop (filter unwanted traffic in NIC hardware — Ch. 57; the hardware analogue of Ch. 63's XDP drop, and defence in depth in front of the parser, Ch. 79), mark (tag for the app). Steer each feed to its core and drop everything you don't subscribe to *at the NIC*, before it costs a cycle.
- **Combine steering with the full chain (Ch. 44, 58).** Steer the feed to the isolated core's queue (Ch. 42), on the NIC's NUMA node (Ch. 16) so DDIO lands it in that socket's L3 (Ch. 58), in CAT-protected ways (Ch. 44). Steering, pinning, NUMA, DDIO, and CAT together are the complete RX isolation: the right packet, on the right core, in the right cache, protected. Any one missing undermines the rest (§59.5).

### 59.4.3 aRFS, `SO_REUSEPORT`, and batched receive

- **aRFS for automatic steering.** Where manual filter rules are impractical (many dynamic flows), enable **aRFS** — the kernel programs the NIC to steer each flow to the core its socket is consumed on, automatically keeping RX on the consuming core (§59.2). Good for dynamic connection sets (many order sessions) where static rules don't scale; the hot single-feed path usually still wants explicit steering (§59.4.2) for determinism.
- **`SO_REUSEPORT` for socket-level scaling.** Bind N sockets (one per thread/core) to the same port; the kernel distributes flows across them (or a **BPF program** picks the socket deterministically — steer a flow to a specific thread). This scales accept/receive across cores at the socket layer — each thread owns a socket, no shared-socket contention (Ch. 33). Combine with NIC steering so the flow lands on the right queue *and* the right socket.
- **Batched receive (`recvmmsg`/`sendmmsg`) to amortize the syscall.** For high-rate UDP/multicast on a kernel path, `recvmmsg` receives *many* datagrams in one syscall (Ch. 41) — amortizing the per-message syscall cost that caps small-message rate. Combine with steering (the batch is all from the right feed on the right core) and busy-poll (Ch. 48). On a bypass path (Ch. 64) the poll loop already batches; `recvmmsg` is the kernel-path equivalent.

## 59.5 Pitfalls & anti-patterns: scattering a feed and steering to the wrong core

- **Leaving the hot feed to RSS (the cardinal error).** Default RSS puts each flow on a queue you did not choose — a core that doesn't consume it, forcing a cross-core handoff (§59.3) — and spreads a multi-group feed or the A/B lines across cores (it never reorders a single flow, §59.2). Explicitly steer the hot feed to its owning core's queue (§59.4.2); don't leave the hot path to RSS's random hash.
- **Steering to a non-isolated core.** Directing a flow to a queue whose core isn't isolated (Ch. 42) — the packet arrives on a core sharing cycles/cache with housekeeping, undermining the isolation. Steer to the *isolated* hot core (Ch. 42), and keep RSS/bulk traffic *off* that core (§59.4.1).
- **Steering right but NUMA wrong (Ch. 16).** Steering the flow to a core on the *wrong socket* relative to the NIC — DDIO lands the data in the wrong L3 (Ch. 58), a cross-socket miss despite correct steering. Steering and NUMA placement must agree: the core must be on the NIC's node (§59.4.2, Ch. 16).
- **RSS on the isolated core.** Leaving RSS free to hash background traffic onto the isolated hot core — bulk/unwanted packets steal the hot core's cycles and pollute its cache (Ch. 58). Bound RSS to housekeeping cores (§59.4.1); the hot core sees only its steered flow.
- **Hash collisions / feed splitting.** An RSS hash configuration that spreads a multi-tuple feed across queues (different groups/ports → different queues) — a cross-core merge for A/B arbitration and locality loss (§59.3). Steer the feed exactly (§59.4.2) or configure the hash so it stays on one queue.
- **`SO_REUSEPORT` rebalancing surprises.** The kernel's `SO_REUSEPORT` distribution can move flows when the socket set changes (a thread joins/leaves), disrupting flow-to-core stability. Use a BPF steering program for deterministic assignment where stability matters (§59.4.3).
- **Filter-rule limits and precedence.** NICs have a bounded number of flow-steering rules and a precedence order (exact filters vs RSS); exceeding the limit or mis-ordering rules silently falls back to RSS for some flows. Verify rules are installed and taking effect (count steered packets per queue), don't assume (§59.4.2).
- **Steering without the rest of the chain.** Steering the packet to the right core but not pinning the thread (Ch. 42), or not NUMA-placing (Ch. 16), or not CAT-protecting (Ch. 44) — each missing link undermines the others (§59.4.2). The RX isolation is the *whole* chain (steer + pin + NUMA + DDIO + CAT), not steering alone.

## 59.6 Exercises & checklist

**Exercises:**

1. **Cross-core handoff cost.** Receive a feed on the consuming core (steered) vs a different core (RSS), and measure receive-to-decode latency and cache misses (Ch. 2) — reproduce the handoff tax (§59.3).
2. **Feed splitting under RSS.** Send a multicast feed and observe (per-queue packet counts) whether RSS splits it across queues; then steer it exactly to one queue and confirm it stays (§59.3, §59.4.2).
3. **Full RX chain.** Steer a feed to an isolated core's queue (Ch. 42), on the NIC's NUMA node (Ch. 16), and confirm (Ch. 58) the first read is an L3 hit — the complete steer+pin+NUMA+DDIO chain (§59.4.2).
4. **`rte_flow` steer + drop.** On a DPDK path (Ch. 64), use `rte_flow` to steer a subscribed feed to a queue and *drop* an unsubscribed one at the NIC; measure the cycles saved by not receiving the dropped traffic (§59.4.2).
5. **`SO_REUSEPORT` + BPF.** Scale receive across N threads with `SO_REUSEPORT`; use a BPF program to steer a specific flow to a specific thread deterministically (§59.4.3).

**Checklist:**

- [ ] The **hot feed is explicitly steered** to the queue polled by its **owning isolated core** (Ch. 42) — not left to RSS (§59.4.2, §59.5).
- [ ] The steered-to core is on the **NIC's NUMA node** (Ch. 16) so DDIO lands data in its **local L3** (Ch. 58), in **CAT-protected ways** (Ch. 44) — the full chain (§59.4.2).
- [ ] **RSS is bounded** to housekeeping cores (indirection table / contexts) and kept **off the isolated hot cores** (§59.4.1, §59.5).
- [ ] An **ordered feed's channels stay on the owning core's queue(s)** (exact per-group steering or hash config) — not spread across cores (§59.3, §59.5).
- [ ] Steering rules are **verified installed and effective** (per-queue packet counts), within the NIC's rule limits and precedence (§59.5).
- [ ] **aRFS** is used for dynamic many-flow steering; **`SO_REUSEPORT` (+ BPF)** scales TCP/unicast-UDP receive with deterministic assignment where needed — never relied on to shard multicast (§59.2, §59.4.3).
- [ ] High-rate kernel-path UDP uses **`recvmmsg`** batching (Ch. 41) with busy-poll (Ch. 48); bypass paths (Ch. 64) batch in the poll loop (§59.4.3).
- [ ] Unsubscribed/unwanted traffic is **dropped at the NIC** (`rte_flow`/filters — Ch. 57; the hardware analogue of Ch. 63's XDP drop) before it costs a cycle (§59.4.2).

## 59.7 References

- The Linux **RSS / RFS / aRFS / RPS/XPS** documentation (`Documentation/networking/scaling.rst`) — receive scaling and steering (§59.2, §59.4).
- **ethtool** n-tuple / `rx-flow-hash` / RSS-context documentation, **Intel Flow Director** guides, and the **DPDK `rte_flow`** API documentation — exact flow steering (§59.4.2).
- The **`SO_REUSEPORT`** and BPF socket-selection documentation — socket-level receive scaling (§59.4.3).
- Ch. 42 (core pinning — steering's counterpart), Ch. 57 (NIC queues/filters), Ch. 44, 58 (CAT/DDIO — the cache half of the chain), Ch. 16 (NUMA), Ch. 48 (native I/O / `recvmmsg`), Ch. 64 (bypass / `rte_flow`).

## 59.8 Additional Reading

- NIC-vendor and DPDK material on flow steering and `rte_flow` for low-latency receive — steering feeds to cores in practice (§59.4.2).
- Netdev talks on RSS/RFS/aRFS and receive scaling — the kernel steering mechanisms (§59.2).
- Ch. 58 (*DDIO*) — where a steered packet lands; Ch. 44 (*CAT*) — protecting the steered-to core's cache; Ch. 42 (*Pinning*) — the thread the flow steers to; Ch. 62 (*Network Fabric*) — the fabric delivering the flows (Ch. 66 for its lossless-Ethernet/RoCE side); **Appendix C** — the IRQ-affinity / RPS-XPS setup; **Appendix F** — RSS / flow-steering / aRFS glossary.

---

*Next: Ch. 60 — Clock Synchronization & Hardware Timestamping (PTP), which takes the NIC's hardware-timestamp unit and the on-host TSC (Ch. 17) and solves the cross-host problem: synchronizing clocks across machines to measure true wire-to-wire and tick-to-trade latency.*
