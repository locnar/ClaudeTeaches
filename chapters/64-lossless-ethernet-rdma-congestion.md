# Part X — Kernel Bypass, RDMA & Transport

# Chapter 64 — Lossless Ethernet & RDMA Congestion

> **Prerequisites:** Ch. 63 (RDMA / InfiniBand verbs — the transport this fabric carries), Ch. 60 (network fabric & switching — the switches that enforce PFC/ECN), Ch. 16 (NUMA — RDMA is memory-to-memory), Ch. 68 (MPI — a big RDMA user with incast), Ch. 1 (the tail — congestion is a tail event).
>
> **Leads into:** Ch. 65 (transport beyond TCP — the messaging layer above this fabric). The congestion companion to the RDMA of Ch. 63: what RDMA assumes underneath it — a lossless fabric — and the machinery (PFC, ECN, DCQCN) that provides it, along with the hazards that machinery introduces.

---

## 64.1 Why it matters: RDMA assumes a lossless fabric that Ethernet isn't

Ch. 63 built RDMA — one-sided, kernel-bypass, ultra-low-latency remote memory access — and it delivers microsecond messaging *when the fabric cooperates*. But RDMA carries a hidden assumption that Ch. 63 could take for granted on native InfiniBand and cannot on Ethernet: **the fabric is lossless.** RDMA transports (especially the common **RoCEv2** — RDMA over Converged Ethernet) were designed assuming packets are not dropped due to congestion; their reliability and go-back-N retransmission are built for a fabric that doesn't lose packets under load. Native **InfiniBand** provides this by design (credit-based link-level flow control — a sender only transmits when the receiver has buffer). But plain **Ethernet drops packets when a switch buffer fills** (Ch. 60) — that's normal, expected Ethernet behavior — and RDMA over a lossy Ethernet fabric performs *terribly*: a single congestion drop triggers go-back-N retransmission of a whole window, collapsing throughput and spiking latency (Ch. 1's tail, manufactured by the fabric).

So running RDMA over Ethernet (RoCEv2 — the economical choice, reusing Ethernet infrastructure instead of a separate InfiniBand fabric) requires *making Ethernet lossless enough*, and that is a real engineering discipline with its own mechanisms and its own failure modes. The tools are **PFC (Priority Flow Control)** — link-level pause frames that tell an upstream switch "stop sending this traffic class, my buffer is full" (creating losslessness by *pausing* instead of *dropping*) — and **ECN (Explicit Congestion Notification)** plus **DCQCN** (the congestion-control algorithm) — which mark and react to congestion *before* buffers fill, reducing how often PFC has to pause. Together, PFC + ECN + DCQCN + DCB (Data Center Bridging) make Ethernet carry RDMA.

But these mechanisms are double-edged. PFC pausing can cause **head-of-line blocking** (a pause stops *all* traffic in a class, including unrelated flows) and, worst, **PFC deadlock** (a cycle of switches each pausing the next — a fabric-wide freeze). And the classic RDMA workload — **incast**, many senders hitting one receiver (a scatter-gather — Ch. 68, a matching-engine fan-in) — is exactly what congests the fabric and triggers all of this. This chapter is the fabric-congestion companion to Ch. 63: it explains lossless Ethernet and RDMA congestion control (§64.2), how to measure RDMA under congestion (§64.3), the techniques to configure PFC/ECN/DCQCN and avoid the hazards (§64.4), and the pitfalls — deadlock, incast collapse, and assuming Ethernet is lossless (§64.5). It's where the networking stack bottoms out: the physical fabric's congestion behavior under RDMA.

## 64.2 Mental model: PFC, ECN, DCQCN, and the RoCEv2 congestion problem

The problem and the layered solution:

```
   RDMA (RoCEv2) assumes: NO congestion drops. But Ethernet drops when a switch buffer fills.
   Two-layer fix:
     1. PFC (link-level, reactive):  buffer nearly full -> PAUSE upstream (per traffic class)
                                      -> lossless, but coarse: pauses a whole class (HOL blocking, deadlock risk)
     2. ECN + DCQCN (end-to-end, proactive):  congestion building -> MARK packets (ECN)
                                      -> receiver signals sender (CNP) -> sender REDUCES rate (DCQCN)
                                      -> keeps buffers from filling, so PFC rarely fires
```

The pieces:

- **PFC (Priority Flow Control, 802.1Qbb).** Link-level flow control *per traffic class* (priority): when a switch's ingress buffer for a class nears full, it sends a **PAUSE** frame upstream telling the sender to stop that class briefly. The upstream holds the traffic (in its buffer) instead of the downstream dropping it — **lossless by pausing**. Coarse (pauses a whole priority class, not a flow) and reactive (fires when nearly full), so it's the *safety net*, not the primary mechanism.
- **DCB (Data Center Bridging).** The umbrella of standards (PFC + **ETS** — Enhanced Transmission Selection for bandwidth allocation per class + DCBX for negotiation) that partitions the Ethernet link into traffic classes with guarantees — so RDMA gets its own lossless class separate from lossy TCP traffic. You put RDMA in a dedicated, PFC-enabled, no-drop class.
- **ECN (Explicit Congestion Notification).** Instead of dropping a congested packet, a switch **marks** it (sets ECN bits) — a signal "congestion is building." The receiver sees the mark and notifies the sender (a **CNP** — Congestion Notification Packet in RoCEv2). ECN acts *before* buffers fill, so it's proactive.
- **DCQCN (Data Center Quantized Congestion Notification).** The RoCEv2 congestion-control algorithm that reacts to ECN marks: on receiving CNPs, the sender **reduces its rate** (like a congestion controller — Ch. 52, but for RDMA), then recovers. DCQCN's job is to keep buffers from filling so **PFC rarely has to fire** — because PFC's pausing has the nasty side effects (HOL, deadlock). ECN/DCQCN is the *primary* congestion control; PFC is the last-resort losslessness guarantee. (Alternatives/successors: **TIMELY** — delay-based, **HPCC** — using in-network telemetry for precise control.)
- **The RoCEv2 congestion problem, stated.** Make Ethernet lossless *enough* that RDMA's drop-intolerant transport works, using ECN/DCQCN to avoid congestion and PFC as the backstop — *without* letting PFC's pausing cause HOL blocking or deadlock. That balance is the whole discipline.

The mental model: **lossless Ethernet for RDMA is a two-layer system — ECN/DCQCN proactively slows senders to avoid congestion, and PFC reactively pauses to guarantee no drops — and the art is tuning ECN/DCQCN so PFC almost never fires, because PFC's cure (pausing whole classes) has worse side effects than the disease.** Native InfiniBand (Ch. 63) sidesteps most of this with credit-based flow control built into the fabric; RoCEv2 trades that for cheaper Ethernet at the cost of this configuration burden.

## 64.3 Measure it: RDMA under congestion and PFC behavior

- **RDMA latency/throughput under congestion.** Run RDMA (Ch. 63) on an uncongested fabric (baseline: microsecond latency, line-rate throughput) and then introduce congestion (competing flows, incast — below). Representative (figures pending real runs): on a *properly configured* lossless fabric, latency degrades gracefully under load (ECN/DCQCN slows senders, tail stays bounded); on a *lossy* or misconfigured fabric, a congestion drop triggers go-back-N and throughput collapses / latency spikes (the tail explodes — Ch. 1). The difference between graceful degradation and collapse is the configuration.
- **Incast collapse (the decisive test).** The canonical RDMA congestion scenario: **many senders → one receiver** simultaneously (a scatter-gather — Ch. 68, a many-to-one fan-in). Measure goodput and tail as the number of concurrent senders (fan-in) rises. Without congestion control: buffers fill, PFC storms or drops happen, goodput collapses (**incast collapse**). With ECN/DCQCN tuned: senders back off, goodput holds, tail stays bounded. This is where lossless-fabric configuration is proven or disproven — measure at high fan-in, not with two nodes.
- **PFC behavior — how often it fires, and HOL.** Measure PFC PAUSE frame counts (switch/NIC counters) under load — if PFC is firing frequently, ECN/DCQCN isn't doing its job (buffers are reaching the PFC threshold), and you're exposed to HOL blocking and deadlock risk (§64.5). Well-tuned, PFC fires rarely (the backstop); frequently-firing PFC is a warning sign. Also measure whether PFC pausing one class blocks *unrelated* traffic (HOL blocking, §64.5).
- **ECN marking rate and DCQCN response.** Measure the ECN marking rate and whether senders are reacting (rate reduction on CNP). Tune the ECN marking thresholds (when the switch starts marking) so senders slow *before* buffers reach the PFC threshold — the marking should lead, PFC should trail.

The lessons: on a properly configured lossless fabric RDMA degrades gracefully; misconfigured, it collapses under congestion (go-back-N) — the config is the difference. Incast (high fan-in) is the test that matters; two-node tests hide the problem. Frequently-firing PFC means ECN/DCQCN is failing its job and you're exposed to PFC's hazards. Tune ECN to *lead* (slow senders early) so PFC only *trails* as a rare backstop.

## 64.4 Techniques

### 64.4.1 Configuring PFC and DCB for lossless RoCEv2

- **Put RDMA in a dedicated, PFC-enabled, no-drop class.** Use DCB (ETS + PFC) to give RoCEv2 traffic its own priority class with PFC enabled (lossless) and a bandwidth guarantee (ETS), separate from lossy TCP traffic. TCP shares the link but in a *different* class that *can* drop (TCP handles drops — Ch. 52); RDMA gets the lossless class. This isolation is the foundation — don't run RDMA and bursty TCP in the same class.
- **Configure PFC end-to-end and consistently.** PFC must be enabled and configured *consistently on every switch and NIC* along the RDMA path (Ch. 60) — a single hop with PFC disabled or misconfigured breaks losslessness (that hop drops, and RDMA collapses). Verify the whole path; PFC is only as lossless as its weakest link.
- **Set buffer thresholds carefully.** PFC pauses when a class's buffer hits a threshold (with headroom for in-flight packets after the pause is sent). Set thresholds and headroom correctly for the link speed and distance (Ch. 60) — too little headroom drops despite PFC; too much wastes buffer. This is switch-specific tuning (vendor guidance).
- **Prefer InfiniBand where the fabric can be dedicated (Ch. 63).** If you can run a dedicated fabric, native InfiniBand (Ch. 63) gives losslessness by design (credit-based flow control) without the PFC/DCQCN configuration burden and its hazards — often the right choice for a dedicated intra-datacenter RDMA fabric (§64.5). RoCEv2 wins when reusing Ethernet infrastructure is the priority.

### 64.4.2 ECN and DCQCN congestion control

- **Enable ECN marking on the switches and DCQCN on the NICs.** Configure switches to ECN-mark the RDMA class when congestion builds (WRED/ECN thresholds), and the RDMA NICs to run DCQCN (react to CNPs by reducing rate, then recover). This is the *proactive* layer that keeps buffers below the PFC threshold (§64.2) — the primary congestion control.
- **Tune ECN thresholds to lead PFC.** The critical tuning: set the ECN marking threshold *below* the PFC pause threshold, so senders start slowing (ECN/DCQCN) *before* buffers reach the point where PFC must pause (§64.3). If PFC fires before ECN has slowed senders, you get PFC's hazards without ECN's benefit — the marking must lead, PFC trail. This ordering is the heart of the tuning.
- **Consider DCQCN alternatives for demanding fabrics.** For fabrics needing tighter control, **TIMELY** (delay-based, using RTT gradients) or **HPCC** (High Precision Congestion Control, using in-network telemetry — INT — for precise rate setting) can outperform DCQCN. These are more advanced/newer; DCQCN is the widely-deployed default. Know they exist for fabrics where DCQCN's tuning is insufficient.

### 64.4.3 Handling incast and avoiding PFC deadlock

- **Design for incast (Ch. 68, 54).** The many-to-one pattern (scatter-gather, fan-in) is the congestion generator. Mitigate at the application level too: stagger/pace senders (Ch. 59's scheduled transmission), use receiver-driven flow control (Ch. 65's Homa ideas — the receiver grants transmission), or limit fan-in concurrency. The fabric (ECN/DCQCN) handles congestion, but application-level incast awareness (don't have 1000 nodes hit one node simultaneously unthrottled) reduces the pressure. Ties Ch. 54 (recovery-request storms are an incast) and Ch. 65 (receiver-driven transports).
- **Avoid PFC deadlock — the worst failure.** PFC deadlock is a cycle of switches each pausing the next (A pauses B because B pauses C because ... because A) — a fabric-wide freeze that doesn't self-resolve. It arises from *cyclic buffer dependencies*, often from routing that creates loops in the pause-dependency graph. Avoid it with careful topology/routing (no cyclic dependencies — leaf-spine done right, Ch. 60), deadlock-avoidance features in modern switches, and by keeping ECN/DCQCN effective so PFC rarely fires (§64.4.2). Deadlock is why you want PFC as a *rare backstop*, not a frequently-active mechanism.
- **Monitor PFC and losslessness continuously (Ch. 76).** Watch PFC PAUSE counts, ECN marking rates, RDMA retransmissions, and buffer occupancy across the fabric (Ch. 76's production monitoring, extended to the fabric). Rising PFC counts or RDMA retransmits signal the losslessness is degrading (a misconfigured hop, an incast, a threshold drift) *before* it becomes a collapse. The fabric's congestion state is a production metric, not a set-and-forget config.

## 64.5 Pitfalls & anti-patterns: PFC deadlock and assuming Ethernet is lossless

- **Assuming Ethernet is lossless (the cardinal error).** Running RoCEv2 over plain Ethernet without PFC/DCB configured — Ethernet drops under congestion (Ch. 60), RDMA's go-back-N collapses, and you get microsecond latency in the lab and catastrophic collapse under production load. RoCEv2 *requires* a configured lossless fabric (§64.4.1); it is not optional (§64.1).
- **PFC deadlock (the worst failure mode).** A cycle of pause dependencies freezes the whole fabric, unrecoverable without intervention (§64.4.3). Avoid with acyclic routing/topology (Ch. 60), switch deadlock-avoidance, and — crucially — keeping ECN/DCQCN effective so PFC rarely fires. A fabric where PFC fires constantly is a deadlock waiting to happen.
- **Frequently-firing PFC (HOL blocking).** PFC pauses a *whole traffic class*, so a pause for one congested flow blocks *all* flows in that class (head-of-line blocking) — unrelated RDMA traffic stalls behind the congested flow. If PFC fires often (ECN/DCQCN not keeping up, §64.3), you get pervasive HOL blocking. Tune ECN to lead so PFC is rare (§64.4.2).
- **ECN threshold above PFC threshold.** Misconfiguring so PFC pauses *before* ECN marks — you get PFC's hazards (HOL, deadlock risk) without ECN's proactive benefit. ECN marking must lead the PFC threshold (§64.4.2); this ordering is the most important tuning parameter.
- **Inconsistent PFC across the path.** One hop (a switch, a NIC) with PFC disabled/misconfigured — that hop drops, breaking losslessness for the whole path (§64.4.1). PFC must be consistent end-to-end (Ch. 60); verify every hop.
- **Ignoring incast.** Deploying RDMA scatter-gather (Ch. 68) or many-to-one fan-in without incast mitigation — the fabric congests, PFC storms, goodput collapses (§64.3, §64.4.3). Design for incast (pace senders — Ch. 59, receiver-driven flow — Ch. 65, limit fan-in) as well as configuring the fabric.
- **RoCEv2 where InfiniBand would be simpler.** Enduring the full PFC/ECN/DCQCN configuration burden and hazards on a fabric you *could* have made dedicated InfiniBand (Ch. 63 — lossless by design). If the fabric can be dedicated, InfiniBand avoids most of this chapter's pain (§64.4.1); RoCEv2's benefit (reuse Ethernet) must be worth the configuration burden.
- **Set-and-forget.** Configuring PFC/ECN/DCQCN once and not monitoring (§64.4.3) — thresholds drift, a new workload changes the incast pattern, a hop's config is lost on a firmware update, and losslessness silently degrades until a collapse. Monitor PFC/ECN/retransmit counters continuously (Ch. 76).

## 64.6 Exercises & checklist

**Exercises:**

1. **Lossless vs lossy RDMA.** Run RDMA (Ch. 63) throughput/latency on a fabric with PFC/DCB configured vs not, under congestion; reproduce graceful degradation vs go-back-N collapse (§64.3).
2. **Incast collapse.** Drive increasing fan-in (many senders → one receiver) and measure goodput/tail with and without ECN/DCQCN tuned — find the collapse point and show congestion control moving it (§64.3, §64.4.3).
3. **ECN leads PFC.** Configure ECN marking below vs above the PFC threshold; measure PFC PAUSE counts and tail in each — show that ECN-leads keeps PFC rare and the tail bounded (§64.4.2, §64.5).
4. **PFC HOL blocking.** Create congestion on one flow and measure the latency of an *unrelated* flow in the same PFC class — demonstrate head-of-line blocking, then separate classes (DCB) to isolate them (§64.4.1, §64.5).
5. **Monitor the fabric.** Instrument PFC PAUSE counts, ECN marks, and RDMA retransmits (Ch. 76); induce a misconfigured hop and show the monitoring catches the losslessness degradation before collapse (§64.4.3).

**Checklist:**

- [ ] RoCEv2 runs in a **dedicated PFC-enabled, no-drop DCB class** (ETS bandwidth guarantee), separate from lossy TCP — never on unconfigured Ethernet (§64.4.1, §64.5).
- [ ] **PFC is configured consistently end-to-end** (every switch and NIC on the path — Ch. 60); verified, not assumed (§64.4.1, §64.5).
- [ ] **ECN marking + DCQCN** are enabled as the **primary** congestion control, with the **ECN threshold set below the PFC threshold** so marking leads and PFC is a rare backstop (§64.4.2, §64.5).
- [ ] **Incast is designed for** — sender pacing (Ch. 59), receiver-driven flow (Ch. 65), or fan-in limits — not left to the fabric alone (§64.4.3, §64.5).
- [ ] **PFC deadlock is avoided** — acyclic routing/topology (Ch. 60), switch deadlock-avoidance, and PFC kept rare via effective ECN/DCQCN (§64.4.3, §64.5).
- [ ] **PFC PAUSE counts, ECN marks, and RDMA retransmits are monitored** continuously (Ch. 76); rising counts flagged before collapse (§64.4.3, §64.5).
- [ ] **Native InfiniBand** (Ch. 63) is chosen where a **dedicated fabric** is possible and its lossless-by-design simplicity outweighs RoCEv2's Ethernet reuse (§64.4.1, §64.5).

## 64.7 References

- The **RoCEv2** specification and the **DCQCN** paper (Zhu et al., "Congestion Control for Large-Scale RDMA Deployments", SIGCOMM 2015) — the RDMA-over-Ethernet congestion problem and its solution (§64.2).
- The **IEEE DCB** standards — **802.1Qbb (PFC)**, **802.1Qaz (ETS/DCBX)** — and switch-vendor lossless-RoCE configuration guides (§64.4.1).
- **TIMELY** (Mittal et al., SIGCOMM 2015) and **HPCC** (Li et al., SIGCOMM 2019) — advanced RDMA congestion control beyond DCQCN (§64.4.2).
- The PFC-deadlock literature (e.g. "Deadlocks in Datacenter Networks" and Tagger/other deadlock-avoidance work) — the deadlock hazard and its avoidance (§64.4.3, §64.5).
- Ch. 63 (RDMA/InfiniBand — the transport this fabric carries), Ch. 60 (fabric/switching — the switches enforcing PFC/ECN), Ch. 68 (MPI — an incast-heavy RDMA user), Ch. 16 (NUMA — RDMA memory placement), Ch. 76 (production monitoring — extended to the fabric).

## 64.8 Additional Reading

- Vendor (NVIDIA/Mellanox, Cisco, Arista) lossless-RoCE deployment guides — practical PFC/ECN/DCQCN configuration and monitoring (§64.4).
- The datacenter-congestion-control research line (DCQCN, TIMELY, HPCC, Swift — and the incast/pFabric work) — the state of the art beyond DCQCN (§64.4.2).
- Ch. 63 (*RDMA*) — the transport that depends on this fabric; Ch. 65 (*Transport Beyond TCP*) — receiver-driven flow control for incast; Ch. 59 (*Precise Transmission*) — pacing to ease congestion; Ch. 60 (*Fabric*) — the switches; **Appendix E** — RDMA latency numbers; **Appendix F** — PFC / ECN / DCQCN / incast glossary; **Appendix G** — the DCQCN/TIMELY/HPCC bibliography.

---

*Next: Ch. 65 — Transport Beyond TCP: Aeron, eRPC, Homa & Custom Reliable UDP, closing Part X: the purpose-built low-latency messaging transports that beat general-purpose TCP for short messages — message semantics, no head-of-line blocking, receiver-driven flow, and microsecond RPC.*
