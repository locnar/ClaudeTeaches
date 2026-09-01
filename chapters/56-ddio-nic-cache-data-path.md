# Part IX — Market Data, NIC & Fabric

# Chapter 56 — DDIO & the NIC-to-Cache Data Path

> **Prerequisites:** Ch. 7 (the cache hierarchy — DDIO targets L3), Ch. 66 (PCIe and DMA — how the NIC writes to the host), Ch. 55 (NIC features — descriptors, RX rings), Ch. 16 (NUMA — DDIO is socket-local), Ch. 44 (CAT — the tool for keeping DDIO and the hot working set apart), Ch. 62 (kernel bypass — the path DDIO most helps).
>
> **Leads into:** Ch. 60 (the fabric that delivers the packets DDIO catches), Appendix E (add the DDIO-hit vs DRAM RX numbers). The NIC-to-cache deep-dive behind the receive path: where a received packet physically lands, and why that decides the first few nanoseconds of tick-to-trade — building on the cache partitioning of Ch. 44 and the NIC of Ch. 55.

---

## 56.1 Why it matters: where a received packet lands decides the RX latency floor

Kernel bypass (Ch. 62) gets a packet from the NIC to your userspace poll loop without the kernel — but it doesn't answer the more basic question: **when the NIC DMAs the packet into host memory, where does it physically go?** The answer sets a hard floor under receive latency. If the packet lands in **DRAM**, your poll loop's first read of it is a cache miss to main memory (~80–100 ns, Ch. 7) — you pay DRAM latency to even *look* at the bytes you just received. If instead the packet lands directly in the **L3 cache**, that first read is an L3 hit (~15–20 ns) — a 4–5× difference on the very first access, on the hottest path there is (Ch. 53's decode reads the packet immediately). Multiply by every field the decoder touches and it is a real chunk of the tick-to-trade budget (Ch. 76).

**Intel DDIO (Data Direct I/O)** is the feature that makes NIC data land in L3 instead of DRAM. On DDIO-capable server parts it is on by default and mostly invisible — which is exactly the problem: because it's invisible, its *downside* is invisible too. DDIO writes into a **limited region of L3**, and under high packet rates that region behaves like a leaky bucket — incoming packets stream through a small set of ways, and if that region overlaps your hot working set (the order book, strategy state, decode tables — Ch. 25), the flood of received packets **evicts your hot data**, or your hot data evicts the packets before you read them. The latency win (RX lands in cache) and a latency hazard (RX pollutes the cache) come from the same mechanism. On an untuned box you get the win and the hazard together and never see either in a profiler that only looks at your code.

So DDIO is the receive-side companion to Ch. 44: Ch. 44 partitioned the L3 to keep *neighbors* out of the hot data; this chapter is about keeping the *NIC's own DMA traffic* from fighting the hot data in that same L3 — and, done right, using DDIO to keep received packets hot so the decoder never misses to DRAM. This chapter explains the DDIO mechanism and the NIC-to-cache path (§56.2), measures DDIO-hit vs DRAM RX and DDIO cache pollution (§56.3), shows how to tune the DDIO region and combine it with CAT and NUMA (§56.4), and warns about DDIO thrashing and the cross-socket trap (§56.5). It's the "where does the packet land" chapter the networking Part assumed but never stated.

## 56.2 Mental model: DMA into L3, the DDIO region, and allocating writes

Normally a device DMA writes to **DRAM**, and the CPU later pulls the data into cache on demand (a cold miss). **DDIO changes the target of I/O DMA to the L3 directly:**

```
   WITHOUT DDIO:  NIC --DMA--> DRAM ......... CPU reads --> L3 miss --> DRAM (~100 ns)   [cold]
   WITH DDIO:     NIC --DMA--> L3 (directly) . CPU reads --> L3 hit (~15-20 ns)          [warm]
                              ▲
                         a limited set of L3 "ways" reserved for inbound I/O writes
```

The mechanics that matter:

- **DDIO writes land in a limited portion of L3.** DDIO does not get the whole cache; it uses a bounded number of L3 ways (historically ~2 ways on many Xeons) for **write-allocate** of inbound DMA. Think of it as a small, fixed window through which *all* inbound I/O streams. This bound is deliberate — it stops I/O from evicting the entire cache — but it means high packet rates churn that small window fast (the "leaky bucket").
- **Allocating vs non-allocating writes.** DDIO does a **write-allocate** into L3 for inbound data (so the CPU finds it hot). Outbound DMA (TX — the NIC *reading* your descriptors/buffers) is served from L3 if present. The read of a device register is still a full PCIe round-trip (Ch. 66); DDIO is about the *bulk data* path.
- **It's socket-local (NUMA — Ch. 16).** DDIO targets the L3 of the socket the **NIC is attached to** (via its PCIe root complex). If your hot thread runs on a *different* socket than the NIC, the packet lands in the *wrong* socket's L3 and your read is a cross-socket access (~130–180 ns, Ch. 16) — worse than local DRAM. NIC-to-core NUMA locality (Ch. 16, 42) is a *precondition* for DDIO to help.
- **Descriptors and buffers both flow through it.** The NIC writes RX descriptors and packet buffers (Ch. 55) via DMA; with DDIO both can be L3-resident, so the poll loop's descriptor check *and* the packet read are hits — provided the working set of RX buffers fits the DDIO window.

The mental model: **DDIO is a small, always-on cache region that inbound I/O streams through.** Used well (RX buffers sized to the window, NIC-local socket, region kept disjoint from hot data), it makes received packets hot for free. Used blindly, it's a firehose pointed at a corner of your L3 that can wash out whatever hot data shares those ways.

## 56.3 Measure it: DDIO hit vs DRAM, and DDIO cache pollution

Two things to measure: the **RX latency benefit** (does the packet arrive hot?) and the **pollution cost** (does RX evict the hot working set, or vice versa?).

**RX latency** — time from "packet DMA'd" to "decoder's first read completes," for DDIO-hit vs DRAM. Measure the decode's first-access latency (Ch. 3) and confirm with `perf` L3-vs-DRAM counters (Ch. 2). Representative (Xeon Gold 6326, NIC on the local socket, kernel bypass — Ch. 62; figures pending real runs):

| Condition | First-read of packet | Notes |
|---|---|---|
| **DDIO hit** (packet in L3, NIC-local) | ~15–20 ns | the intended path — decode starts on a warm packet (Ch. 53) |
| **DDIO disabled / evicted** (packet in DRAM) | ~80–100 ns | cold miss to main memory on the first byte |
| **Cross-socket** (NIC on other socket, Ch. 16) | ~130–180 ns | packet landed in the *wrong* L3 — worse than local DRAM |

**DDIO pollution** — the reverse effect: does heavy RX evict hot data? Run the hot lookup loop (working set in L3, Ch. 44) while driving high packet rates, and measure the hot path's L3 miss rate and tail (Ch. 3) as packet rate climbs:

| RX packet rate | Hot-path L3 miss rate | Hot-path p99.9 |
|---|---|---|
| idle | ~0.2% | ~1.1 µs |
| moderate | ~1% | ~1.4 µs |
| **market-open burst (Ch. 53)** | ~6–12% | ~**3–4 µs** |

The lessons:

- **DDIO is a real RX win *when the packet is L3-resident and NIC-local*** — 4–5× faster first access than DRAM. This is why kernel bypass (Ch. 62) on a NIC-local core is fast: not just skipping the kernel, but the packet being *hot* when you read it.
- **Cross-socket destroys the win** (Ch. 16). If the NIC and the hot thread are on different sockets, DDIO lands the packet in the wrong L3 and you pay cross-socket latency — worse than if DDIO were off. NIC-local placement (Ch. 42, `numactl`) is mandatory, not optional.
- **At high packet rates DDIO's small region churns and spills into your hot data** — the pollution effect. The tail climbs with packet rate not because decode got slower but because the *strategy's* working set is being evicted by the RX firehose sharing L3 ways. This is the market-open failure mode (Ch. 53 microburst) that a code profiler never explains.
- **The fix is separation, not disabling** (§56.4): keep the DDIO region and the hot data region in *disjoint* L3 ways (via CAT, Ch. 44), so RX stays hot *and* the strategy stays hot. Disabling DDIO removes the pollution but also the win (back to DRAM RX) — usually the wrong trade.

## 56.4 Techniques

### 56.4.1 Keeping RX buffers DDIO-resident and NIC-local

Get the win first: make sure received packets actually land hot and stay hot until you read them.

- **Put the hot RX thread on the NIC's socket (Ch. 16, 42).** Discover the NIC's NUMA node (`/sys/class/net/<if>/device/numa_node`) and pin the poll thread — and allocate its RX buffers — on that node (`numactl --membind`, Ch. 16). This is the precondition for DDIO to land packets in *your* L3. Cross-socket RX is the single biggest DDIO mistake (§56.5).
- **Size the RX buffer working set to the DDIO window.** The set of RX descriptors + buffers the NIC cycles through should fit the DDIO region so they stay resident between DMA and your read. Too many in-flight RX buffers (huge rings — Ch. 53) churn the DDIO window and packets get evicted to DRAM before you read them. Balance ring size for microburst absorption (Ch. 53) against DDIO residency — bigger isn't always better here.
- **Read promptly (poll, don't batch too deep).** DDIO makes the packet hot *now*; the longer you wait to read it (deep batching, a slow poll cadence), the more likely later DMA evicts it from the small window. Tight poll loops (Ch. 47, 62) read packets while they're still hot.
- **Reuse buffers to stay warm.** Recycling a small pool of RX buffers (Ch. 24) keeps the same lines in the DDIO region, versus cycling through a large buffer set that never revisits a line — the pool pattern (Ch. 24) helps DDIO residency too.

### 56.4.2 Separating the DDIO region from the hot working set with CAT

The key combined technique (with Ch. 44): stop the NIC's DMA and the strategy's data from evicting each other.

- **Give the DDIO region and the hot data disjoint L3 ways.** By default the DDIO ways and your hot working set share the same L3 pool and can evict each other (§56.3). Using CAT (Ch. 44), reserve a CLOS for the hot core's data with ways that **do not overlap** the ways DDIO writes into. On parts that expose DDIO way control (`IIO LLC WAYS` MSR / BIOS knob), you can set the DDIO region explicitly; combined with a complementary CAT mask for the hot path, RX streams through *its* ways and the strategy lives in *its* ways — neither evicts the other.
- **Tune the DDIO way count if the platform allows.** Some platforms let you widen or narrow the DDIO region (via MSR or BIOS). Widen it if you have many RX queues / high packet rates and packets are spilling to DRAM before you read them; keep it narrow (and CAT-isolated) if RX is modest and you want to protect hot data. Measure both the RX first-read latency (§56.3) *and* the hot-path tail — you're balancing DDIO residency against hot-data protection.
- **Monitor with CMT (Ch. 44).** Watch the occupancy of the DDIO/RX region vs the hot region. If the hot region's occupancy dips under RX load, the regions are still fighting — the masks overlap or the DDIO region is too wide. CMT makes the fight visible (Ch. 76).
- **This is the resolution of §56.3's pollution.** Separation gives you *both* halves: DDIO keeps packets hot (fast decode) *and* CAT keeps the strategy hot (no eviction). It's why Ch. 44 and Ch. 56 are adjacent — cache partitioning is what makes DDIO safe on a busy hot path.

### 56.4.3 When to disable DDIO, and cross-socket/peer-to-peer cases

The edge cases:

- **When to disable DDIO.** Rarely — but if a workload does huge inbound DMA that you *don't* read promptly (bulk capture to storage — Ch. 75, where the bytes go straight to disk and the CPU barely touches them), DDIO just pollutes L3 for no benefit. Disabling DDIO (or CAT-isolating the capture NIC's region to a tiny slice) keeps that traffic out of the hot cache. Decide per traffic class: DDIO helps traffic you *read soon*, hurts traffic you *only forward*.
- **Cross-socket and the NIC's placement.** If you *must* run across sockets (Ch. 16), understand that DDIO lands data on the NIC's socket — route the processing there, or accept the cross-socket cost. Multi-NIC boxes should place each NIC's hot consumer on that NIC's socket. This is the NUMA discipline of Ch. 16/42 applied to the DDIO target.
- **Peer-to-peer DMA (ties Ch. 66, 69).** NIC-to-FPGA or NIC-to-GPU peer-to-peer transfers (Ch. 66's P2P, Ch. 69's inline FPGA) bypass host memory entirely — no DDIO involved, the data never touches host L3. That's the ultimate "where does it land" answer for the inline-hardware path (Ch. 69): it lands in the accelerator, not the host cache at all.

## 56.5 Pitfalls & anti-patterns: DDIO thrashing and the cross-socket trap

- **The cross-socket trap (the cardinal DDIO error).** Running the hot RX thread on a different socket than the NIC — DDIO lands the packet in the NIC's socket's L3, and your read is a cross-socket access (~130–180 ns, Ch. 16), *worse* than if DDIO were off. Always pin the RX consumer (and its buffers) to the NIC's NUMA node (§56.4.1). This silently negates every other optimization.
- **DDIO thrashing under load (the leaky bucket).** Blindly trusting DDIO's default small region under a market-open packet flood (Ch. 53) — the region churns, packets spill to DRAM before you read them, *and* the spillover evicts hot data. The symptom is a tail that climbs with packet rate (§56.3). Fix by separating regions (CAT, §56.4.2) and sizing RX buffers to the window (§56.4.1).
- **Huge RX rings defeating DDIO.** Oversizing RX rings for microburst absorption (Ch. 53) without regard for the DDIO window — the NIC cycles through more buffers than the DDIO region holds, so packets are evicted to DRAM before the poll loop reaches them. Balance ring size against DDIO residency; measure RX first-read latency (§56.3), not just drop rate.
- **Disabling DDIO to "fix" pollution.** Turning DDIO off removes the pollution but also the RX win — now *every* packet read is a DRAM miss (§56.3). The right fix is *separation* (CAT), not disabling; disable only for traffic you don't read promptly (§56.4.3).
- **Assuming DDIO is on / configured.** DDIO availability and default region size vary by platform, BIOS setting, and generation; some parts expose it, some don't, AMD's data-path behaves differently. Don't assume — verify on your hardware, and measure RX first-read latency to confirm packets are actually arriving hot (§56.3).
- **Ignoring DDIO when reasoning about the RX path.** Treating "kernel bypass = fast RX" as the whole story (Ch. 62) and being surprised by DRAM-latency first reads. The packet's *cache residency* is as important as skipping the kernel; DDIO (and its NUMA locality) is why a NIC-local bypass path is fast, and forgetting it leaves latency on the table.
- **DDIO and capture traffic sharing the hot cache.** A capture NIC (Ch. 75) DMAing full line-rate traffic through DDIO into the same L3 as the hot strategy — pure pollution, since capture data goes to disk, not the strategy. Isolate or disable DDIO for the capture path (§56.4.3).

## 56.6 Exercises & checklist

**Exercises:**

1. **DDIO hit vs DRAM.** With kernel bypass (Ch. 62) on a NIC-local core, measure the decoder's first-read latency of a received packet; then force the packet cold (evict it, or disable DDIO) and re-measure — reproduce the ~15 ns vs ~100 ns gap (§56.3).
2. **Cross-socket penalty.** Pin the RX consumer to the *wrong* socket (Ch. 16) and measure first-read latency — show it exceeds even local DRAM, quantifying the cross-socket trap (§56.5).
3. **DDIO pollution under load.** Run a hot lookup loop (working set in L3) while ramping packet rate; measure the hot path's L3 miss rate and p99.9 climbing with RX rate (§56.3) — the leaky-bucket effect.
4. **Separate the regions.** Using CAT (Ch. 44), give the hot data ways disjoint from the DDIO/RX region; re-run the pollution test and show the hot tail stays flat *and* RX stays hot (§56.4.2) — the combined technique.
5. **RX buffer sizing.** Vary the RX ring/buffer-pool size and measure RX first-read latency; find the point where too many buffers churn the DDIO window and packets spill to DRAM (§56.4.1).

**Checklist:**

- [ ] The hot RX thread **and its buffers are on the NIC's NUMA node** (Ch. 16, 42) — packets land in the *local* L3, never cross-socket (§56.4.1, §56.5).
- [ ] RX first-read latency is **measured** and confirms packets arrive **L3-hot** (~15–20 ns), not DRAM-cold (§56.3).
- [ ] The **DDIO region and the hot working set use disjoint L3 ways** (CAT, Ch. 44) so RX and the strategy don't evict each other (§56.4.2).
- [ ] RX ring / buffer-pool size is **balanced against the DDIO window** (residency) as well as microburst absorption (Ch. 53) — not blindly oversized (§56.4.1, §56.5).
- [ ] The hot path **reads packets promptly** (tight poll, shallow batching) so DDIO-resident packets aren't evicted before use (§56.4.1).
- [ ] **CMT monitoring** (Ch. 44) confirms the DDIO/RX region and hot region aren't fighting under load (§56.4.2).
- [ ] **Capture / forward-only NIC traffic** (Ch. 75) is **DDIO-isolated or disabled** so it doesn't pollute the hot cache (§56.4.3); DDIO is left *on* for traffic read promptly.
- [ ] DDIO availability/behavior is **verified on the actual hardware** (BIOS, generation, Intel/AMD), not assumed (§56.5).

## 56.7 References

- Intel **Data Direct I/O Technology** briefs and the *Intel Xeon Uncore Performance Monitoring* / platform guides — the DDIO mechanism, region, and MSR/BIOS controls (§56.2, §56.4).
- The *Intel 64 and IA-32 SDM* and platform tuning guides — IIO LLC ways, DDIO way control, and the DMA-to-L3 path (§56.2, §56.4.2).
- Research on **DDIO leaky-bucket / cache pollution** (e.g. "Understanding DDIO" and net-DMA cache-contention papers) — the pollution effect and its mitigation with CAT (§56.3, §56.4.2).
- Ch. 66 (PCIe/DMA — the transport DDIO rides), Ch. 55 (NIC descriptors/rings), Ch. 16 (NUMA — DDIO locality), Ch. 44 (CAT — region separation), Ch. 62 (kernel bypass — the path DDIO accelerates), Ch. 53 (microbursts — when pollution bites).

## 56.8 Additional Reading

- Solarflare/Onload, DPDK, and NIC-vendor guidance on DDIO and cache-aware RX buffer placement — practical tuning for the bypass path (§56.4).
- Talks and papers on cache contention between I/O and compute (the "DDIO is a double-edged sword" line of work) — measuring and taming RX-induced eviction (§56.3).
- Ch. 44 (*CAT/RDT*) — the partitioning that makes DDIO safe; Ch. 60 (*Fabric*) — what delivers the packets; Ch. 69 (*FPGA*) — the peer-to-peer path that skips host cache entirely; Ch. 75 (*Capture*) — forward-only traffic to keep out of the hot cache; **Appendix E** — the L3-vs-DRAM-vs-cross-socket numbers that drive every decision here; **Appendix F** — DDIO/DMA/NUMA glossary.

---

*Next: Ch. 57 — Flow Steering & Receive Scaling, getting the right packet to the right core with no cross-core handoff: RSS, hardware flow steering (Flow Director / rte_flow), aRFS, SO_REUSEPORT, and batched receive — so the packet lands on the core, and in the L3, that owns it.*
