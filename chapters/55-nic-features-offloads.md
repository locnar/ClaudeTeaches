# Part IX — Market Data, NIC & Fabric

# Chapter 55 — NIC Features & Offloads

> **Prerequisites:** Ch. 47 (the kernel I/O path the NIC feeds), Ch. 51, 53 (multicast feeds / decoding the packets), Ch. 42 (IRQ affinity / pinning — RSS steers to cores), Ch. 17 (timekeeping — hardware timestamps), Ch. 41 (busy-poll vs interrupts), Ch. 62-preview (kernel bypass — the NIC's user-space fast path).
>
> **Leads into:** Ch. 58 (PTP & cross-host hardware timestamping — built on the NIC's timestamp unit), Ch. 61 (XDP/AF_XDP — in-driver/near-NIC processing), Ch. 62 (kernel bypass — ef_vi/Onload, the NIC's bypass path), Ch. 70 (SmartNICs/DPUs — programmable NICs). The receive-side hardware under Ch. 53's decoder.

---

## 55.1 Why it matters: push work into the card

The NIC (Network Interface Card) is not a dumb wire-to-memory pipe — a modern server/HFT NIC is a sophisticated processor that can **offload** work from the CPU, **steer** packets to specific cores, **timestamp** packets in hardware, and — most importantly for HFT — provide a **kernel-bypass** path that delivers packets directly to user space (Ch. 62). The packets the feed handler decodes (Ch. 53) and the orders it sends all pass through the NIC, and *how the NIC is configured* determines the latency floor of the whole I/O path. This chapter is about using the NIC's features to get packets to the decoder (Ch. 53) — and orders onto the wire — with the lowest possible latency and CPU cost, and about the features that *matter* for latency (RSS, hardware timestamping, busy-polling, vendor bypass) versus those tuned for *throughput* (the segmentation/checksum offloads) that can actually *hurt* latency (§55.5).

The framing that runs through the chapter is **latency vs throughput** (Ch. 1), restated at the hardware level. Most NIC offloads — **LRO/GRO** (coalescing received packets), **TSO/GSO** (segmenting large sends), **interrupt coalescing** — are designed to maximize *throughput* and minimize *CPU* by **batching**: hold packets and process them in groups. That batching is exactly wrong for latency: it *adds delay* (a packet waits to be coalesced/an interrupt waits to be batched). So a latency-tuned NIC config often **disables** the throughput offloads (LRO/GRO off, interrupt coalescing off or minimal) and instead **busy-polls** (Ch. 41 — no interrupts at all). The HFT NIC is configured opposite to a throughput server: minimize batching, poll instead of interrupt, and push the *latency-relevant* work (timestamping, flow steering, bypass) into the card.

And the features that genuinely matter for HFT are specific: **RSS / flow steering** (direct the market-data feed to the right CPU/queue so the right core processes it, with IRQ affinity — Ch. 42), **hardware timestamping** (the NIC stamps each packet with a precise hardware clock at wire-arrival — the foundation of honest latency measurement, Ch. 17, and PTP cross-host sync, Ch. 58), **busy-polling** (poll the NIC's RX ring instead of waiting for interrupts — Ch. 41, lower and more deterministic latency), and the **vendor kernel-bypass stacks** (Solarflare/AMD **Onload** and **ef_vi**, Exablaze/Cisco **ExaNIC** — Ch. 62) that let user-space read the NIC's rings directly with no kernel. This chapter explains the RSS/offload/timestamping mental model (§55.2), measures offload-on/off CPU and latency (§55.3), details RSS/flow-steering, busy-polling, and Solarflare/Onload (§55.4), and warns about the throughput offloads that add latency to a low-latency path (§55.5) — the hardware foundation under the receive path of Ch. 53 and the bypass of Ch. 62.

## 55.2 Mental model: RSS, offloads, hardware timestamping

**The NIC's job and its rings.** The NIC receives packets off the wire and **DMA**s them into **RX descriptor rings** in host memory (Ch. 53.2.4), then signals the host (an **interrupt**, or the host **polls**). On send, the host puts packets in **TX rings** and the NIC transmits them. The rings, the steering of packets *to* rings, the interrupt-vs-poll choice, and the offloads are the NIC features that matter:

- **RSS (Receive Side Scaling) — spread/steer packets across queues/cores.** A NIC has *multiple* RX queues; **RSS** hashes each packet's flow (src/dst IP/port) and assigns it to a queue, each queue's interrupt pinned to a specific core (Ch. 42). This (a) spreads load across cores (throughput) and (b) — with **flow steering** (directing a *specific* flow to a *specific* queue/core, via `ntuple`/Flow Director/aRFS) — lets you put the **market-data feed on the dedicated decoder core** (Ch. 42, 53) so the right core processes it with warm caches (Ch. 46) and no cross-core handoff. RSS+steering is how you get the feed to the right place.
- **Hardware timestamping — the NIC stamps packets in hardware.** The NIC has a **PTP Hardware Clock (PHC)** and can record a precise timestamp at the moment a packet **crosses the wire** (RX) or is **sent** (TX) — far more accurate than a software timestamp taken when your code *gets around to* reading the packet (which includes all the stack/scheduling latency — Ch. 17). Exposed via `SO_TIMESTAMPING` (`SOF_TIMESTAMPING_RX_HARDWARE`/`TX_HARDWARE`). This is the basis of *honest* wire-to-wire latency measurement (Ch. 17, 76) and of PTP cross-host clock sync (Ch. 58). For HFT, hardware RX timestamps tell you *exactly* when a market-data packet arrived, independent of how long your software took to process it.
- **Offloads — work the NIC does instead of the CPU:**
  - **Checksum offload (RX/TX):** the NIC computes/verifies IP/TCP/UDP checksums — *generally good* (saves CPU, no latency cost, keep on).
  - **TSO/GSO (TCP/Generic Segmentation Offload):** the host hands the NIC a large buffer; the NIC segments it into packets — *throughput* feature; little harm for latency on the send side but irrelevant for small orders.
  - **LRO/GRO (Large/Generic Receive Offload):** the NIC/stack **coalesces** multiple received packets into one large one before handing up — *adds latency* (it *waits* to coalesce — §55.5). **Disable for latency.**
  - **Interrupt coalescing (`rx-usecs`/`rx-frames`):** the NIC **batches** interrupts (waits to accumulate packets before interrupting) — *throughput/CPU* feature that **adds latency** (a packet waits for the coalesce timer — §55.5). **Minimize or disable; busy-poll instead.**
- **Busy-polling vs interrupts (Ch. 41).** Instead of the NIC **interrupting** the CPU when a packet arrives (interrupt latency + the handler + a possible context switch — Ch. 41), the CPU can **busy-poll** the RX ring (check for new descriptors in a tight loop on a dedicated core — Ch. 41). Polling is lower-latency and more *deterministic* (no interrupt jitter) — the HFT default on the hot RX path (via `SO_BUSY_POLL`/NAPI busy-poll — Ch. 47, or kernel bypass — Ch. 62).

**Kernel bypass (Ch. 62) — the NIC's user-space path.** The vendor stacks (Solarflare **Onload**/**ef_vi**, **ExaNIC libexanic**) let user-space map the NIC's rings *directly* and receive/send with **no kernel stack** — the ultimate low-latency NIC path (Ch. 62). The NIC features here (RSS steering, hardware timestamping, busy-poll) are used *within* the bypass path too.

The model: **the NIC DMAs packets into RX rings and signals via interrupt or poll; for latency you steer the feed to the right core (RSS/flow steering + IRQ affinity — Ch. 42), busy-poll instead of interrupt (Ch. 41), capture hardware timestamps (Ch. 17, 58), keep the cheap offloads (checksum) and disable the batching ones (LRO/GRO/interrupt-coalescing — they add latency), and ultimately bypass the kernel (Ch. 62). NIC config is latency-vs-throughput at the hardware level.**

## 55.3 Measure it: offload on/off CPU cost

Measure two things: the **latency** effect of the batching offloads (interrupt coalescing, GRO) — they *add* delay — and the **CPU/latency** of interrupt-driven vs busy-poll receive. The headline: disabling the throughput offloads and busy-polling *lowers and flattens* latency (at a CPU cost).

```
   # Inspect and toggle offloads (ethtool):
   $ ethtool -k <iface>                          # show offload settings (gro, lro, tso, rx/tx checksum)
   $ ethtool -K <iface> gro off lro off          # disable receive coalescing (latency)
   $ ethtool -C <iface> rx-usecs 0 rx-frames 1   # disable interrupt coalescing (interrupt per packet)
   $ ethtool -G <iface> rx 4096                   # size the RX ring for bursts (Ch.48)
   $ ethtool -l/-L <iface>                         # show/set RSS queue count
   $ ethtool -T <iface>                            # show hardware-timestamping capabilities

   # Measure RX latency (wire-to-app) under each config, ideally with HARDWARE timestamps (Ch.16,50):
   #   app_recv_time - nic_hw_rx_timestamp  = the software-stack latency the NIC config affects
```

Representative results — reference machine **Xeon Gold 6326** (Ice Lake-SP) + a low-latency NIC, small UDP multicast packets, pinned, turbo off (illustrative):

```
   RX config                                   wire-to-app latency (p50 / p99)   CPU      note
   default (GRO on, interrupt coalescing on)   ~8 µs / ~30 µs                    low      coalescing ADDS latency
   GRO/LRO off, coalescing off (IRQ/packet)    ~5 µs / ~12 µs                    higher   no batching delay, more IRQs
   + busy-poll (SO_BUSY_POLL / NAPI)           ~3 µs / ~6 µs                     high     no interrupt latency/jitter (Ch.39)
   + kernel bypass (ef_vi/Onload — Ch.52)      ~1 µs / ~2 µs                     100%     no kernel stack at all

   software timestamp vs HARDWARE timestamp:   sw includes all the above latency; hw = true wire arrival (Ch.16,50)
```

Read it: the **batching offloads add latency** — GRO coalescing and interrupt coalescing *hold* packets to process them in groups (throughput/CPU efficiency), which is the *opposite* of what a latency path wants; turning them off drops the latency (and tightens the tail — less batching jitter) at the cost of more interrupts/CPU. **Busy-polling** (Ch. 41) removes the interrupt latency *and jitter* entirely (no waiting for an interrupt, no handler, no context switch) — lower and more *deterministic* — at the cost of a core pegged at 100%. And **kernel bypass** (Ch. 62) removes the stack, reaching ~1 µs. The other crucial point is the **hardware timestamp** row: a *software* receive timestamp (when your code read the packet) *includes* all the stack/config latency above — so it's not the true arrival time; the **NIC hardware timestamp** (Ch. 17, 58) is the *actual* wire-arrival moment, which is what you need for honest latency measurement (Ch. 76) and PTP (Ch. 58). The configuration lesson: **for a latency RX path, disable the batching offloads, busy-poll, size the RX ring for bursts (Ch. 53), steer the feed to the right core (RSS), capture hardware timestamps — and bypass the kernel for the ultra-hot feed (Ch. 62).** Keep the cheap offloads (checksum); kill the batching ones.

## 55.4 Techniques

### 55.4.1 RSS and flow steering

Getting the right packets to the right core (ties Ch. 42, 53):

- **RSS spreads RX across queues/cores.** Enable multiple RX queues (`ethtool -L <iface> combined N`), each with its interrupt pinned to a specific core (`/proc/irq/*/smp_affinity` — Ch. 42). The NIC hashes flows across queues. For a *throughput* server this load-balances; for HFT the point is *control* — putting specific flows on specific cores.
- **Flow steering — direct a specific flow to a specific queue/core.** `ethtool -N <iface> flow-type ... action <queue>` (ntuple / Intel Flow Director), or aRFS (accelerated Receive Flow Steering). Steer the **market-data multicast feed** to the queue whose IRQ is on the **dedicated decoder core** (Ch. 42, 53) — so the feed lands on the right core, with warm caches (Ch. 46), no cross-core handoff, and a predictable IRQ. This is how you place the feed in the topology you designed (Ch. 31, 42).
- **Per-feed/per-symbol steering.** With enough queues, steer different feeds (or symbol shards — Ch. 31) to different cores, mapping the network topology onto the shared-nothing core topology (Ch. 31). Each shard's feed → its core → its book.
- **Coordinate with IRQ affinity (Ch. 42).** RSS/steering decides which *queue*; IRQ affinity decides which *core* handles that queue's interrupt. They must agree (the feed's queue's IRQ on the decoder core, *not* a housekeeping core if you're interrupt-driven — though busy-poll avoids interrupts entirely). Disable `irqbalance` (Ch. 42) so your steering sticks.

### 55.4.2 Busy-polling

Eliminating interrupt latency on the RX path (Ch. 41, 47):

- **Poll the RX ring instead of waiting for interrupts.** On a dedicated core (Ch. 42, 45), busy-poll the NIC's RX descriptor ring in a tight loop — process a packet the instant the NIC DMAs it, with **no interrupt** (no interrupt latency, no handler, no context switch — Ch. 41), and **no interrupt jitter** (deterministic). This is the HFT RX model on the hot feed.
- **`SO_BUSY_POLL` / NAPI busy-poll (kernel path — Ch. 47).** Within the kernel stack, `SO_BUSY_POLL` (per-socket) or the NAPI busy-poll sysctls make the kernel busy-poll the NIC for the socket before sleeping — lower latency than interrupt-driven, still using the stack. The middle ground (Ch. 47) before full bypass.
- **Kernel-bypass polling (ef_vi/DPDK — Ch. 62).** The lowest-latency: user-space polls the NIC's rings directly (no kernel), the ultimate busy-poll. §55.4.3 and Ch. 62.
- **The CPU/power trade (Ch. 41).** Busy-polling pegs a core at 100% (and the power/heat — Ch. 6, and the SMT sibling — Ch. 43). Accepted for a dedicated RX core; not for a shared/idle system. Match to the deployment (dedicated cores → poll; else interrupt-driven).
- **Disable interrupt coalescing if interrupt-driven.** If you *do* use interrupts (not polling), minimize coalescing (`ethtool -C rx-usecs 0`) so packets aren't held — but polling is better for the hot path (§55.3).

### 55.4.3 Solarflare/Onload

The vendor kernel-bypass stacks — the production HFT NIC path (leads into Ch. 62):

- **Onload (Solarflare/AMD) — transparent bypass.** Onload is a user-space network stack that **transparently** accelerates existing **sockets** applications (`LD_PRELOAD` — no code change): standard socket calls are intercepted and serviced by a user-space stack reading the NIC's rings directly, **bypassing the kernel**. You get most of the bypass latency win *without rewriting* to a bypass API — a major practical advantage. Tune via Onload environment variables (spinning, polling). The common "easy bypass" path.
- **ef_vi (Solarflare) — the raw bypass API.** For the absolute lowest latency, **ef_vi** is Solarflare's *raw* layer-2 API: the application manages the NIC's RX/TX rings directly (post receive buffers, poll for completions, send) with no stack at all — explicit, lowest-latency, more work (Ch. 62). Used for the ultra-hot feed/order path.
- **ExaNIC libexanic (Exablaze/Cisco).** The equivalent raw bypass API for ExaNIC cards, with extremely low latency and precise hardware timestamping; some ExaNICs offer FPGA-based inline processing (Ch. 69). Another vendor in the same niche (Ch. 62).
- **Hardware timestamping in bypass.** The bypass APIs expose the NIC's **hardware timestamps** (ef_vi/libexanic give per-packet hardware RX timestamps) — combining the lowest-latency receive with the most accurate timing (Ch. 17, 58). The HFT receive path is bypass + hardware timestamps.
- **The decision (Ch. 62).** Onload (transparent, socket-compatible, easy) vs ef_vi/libexanic (raw, lowest-latency, more work) vs DPDK (vendor-neutral bypass — Ch. 62). Covered fully in Ch. 62; the point here is that these are *NIC* features (vendor stacks for specific cards) — the NIC choice and the bypass stack are coupled.

## 55.5 Pitfalls & anti-patterns: offloads that add latency

- **Leaving the batching offloads on (the headline NIC-latency bug).** **GRO/LRO** (receive coalescing) and **interrupt coalescing** *hold* packets to batch them — adding latency (§55.3) — and they're *on by default* (tuned for throughput). For a latency path, **disable GRO/LRO** (`ethtool -K gro off lro off`) and **minimize/disable interrupt coalescing** (`ethtool -C rx-usecs 0`) or busy-poll. The default NIC config is a throughput config; retune it for latency.
- **Interrupt-driven RX on the hot path.** Interrupts add latency *and jitter* (the interrupt, the handler, a possible context switch — Ch. 41) vs busy-polling. On a dedicated core, **busy-poll** (§55.4.2). And if interrupt-driven, route the IRQ correctly (Ch. 42) — an un-steered NIC IRQ on the wrong core (or a hot core) is the Ch. 42 problem.
- **Undersized RX ring → market-open drops (Ch. 53).** A small RX descriptor ring overflows during a microburst → dropped packets (§53.2.4) → gaps/recovery. Size it (`ethtool -G rx <N>`) for the worst burst; monitor `ethtool -S` (rx_missed/rx_no_buffer). A top market-open drop cause.
- **Software timestamps mistaken for wire-arrival time (Ch. 17, 58).** A software receive timestamp includes all the stack/scheduling latency — it's *not* when the packet arrived. For honest latency measurement (Ch. 76) and sequencing, use **NIC hardware timestamps** (§55.2, Ch. 17, 58). Confusing the two gives wrong latency numbers.
- **RSS spreading a single feed across cores (losing locality).** If a single market-data feed gets hashed across multiple queues/cores by RSS, it's processed by different cores with cold caches and reordering (Ch. 53). **Steer** the feed to *one* (the decoder) core (§55.4.1) — RSS *load-balancing* is a throughput goal; HFT wants *deterministic steering*.
- **`irqbalance` undoing your steering (Ch. 42).** Leaving `irqbalance` running re-shuffles the NIC IRQs, breaking your flow steering. Disable it (Ch. 42); pin IRQs manually.
- **Disabling checksum offload (over-correction).** Checksum offload is *cheap and good* (saves CPU, no latency cost) — disabling it (in a blanket "turn off all offloads") wastes CPU for no latency benefit. Disable the *batching* offloads (GRO/LRO/coalescing), **keep** checksum/the non-batching ones.
- **Bypass without the rest of the tuning.** Kernel bypass (Onload/ef_vi — §55.4.3, Ch. 62) is the big win, but it still needs the RX ring sized, the core dedicated/isolated (Ch. 42, 45), busy-polling, and steering — bypass on an un-tuned core/ring still drops/jitters. Bypass is part of a tuned system, not a standalone fix.
- **Vendor lock-in / portability.** Onload/ef_vi/libexanic tie you to specific NIC vendors (Solarflare/AMD, ExaNIC); DPDK (Ch. 62) is more vendor-neutral. A consideration in the bypass choice (Ch. 62), not a reason to avoid bypass — just plan for it.

## 55.6 Exercises & checklist

**Exercises**

1. **Offload latency.** Measure UDP RX latency (ideally with hardware timestamps — §55.2) with GRO/coalescing **on** (default) vs **off** (`ethtool -K gro off lro off`, `-C rx-usecs 0`). Confirm the batching offloads *add* latency and tail (§55.3). Watch the CPU/interrupt-rate trade.
2. **Busy-poll vs interrupt.** Compare interrupt-driven RX vs `SO_BUSY_POLL` (Ch. 47) vs (if available) ef_vi bypass (§55.4.3). Measure latency p50/p99 and CPU. Quantify the interrupt latency/jitter removed (§55.4.2, Ch. 41).
3. **Flow steering.** Use `ethtool -N` (ntuple) to steer a specific UDP flow to a specific RX queue whose IRQ is on a chosen core (Ch. 42). Verify (`/proc/interrupts`, `ethtool -S` per-queue) the feed lands on that core. Then mis-steer it across cores and observe the locality/reorder cost (§55.4.1, §55.5).
4. **Hardware timestamps.** Enable `SO_TIMESTAMPING` hardware RX timestamps; compare the hardware timestamp to a software timestamp taken in your recv loop. Quantify the software-stack latency the difference reveals (§55.2, ties Ch. 17, 58).
5. **RX ring sizing.** Replay a microburst (Ch. 53, 75); with a small RX ring, observe `ethtool -S` rx_missed drops; increase `ethtool -G rx` until drops stop. Find the ring size that survives your worst burst (§55.5, Ch. 53).

**Checklist — NIC features & offloads**

- [ ] **Batching offloads disabled** for the latency RX path: **GRO/LRO off** (`ethtool -K`), **interrupt coalescing minimized/off** (`ethtool -C rx-usecs 0`) — the **cheap offloads (checksum) kept** (§55.5).
- [ ] The hot RX path **busy-polls** (`SO_BUSY_POLL`/NAPI — Ch. 47, or kernel bypass — Ch. 62) on a **dedicated/isolated core** (Ch. 42, 45) — no interrupt latency/jitter (Ch. 41).
- [ ] The **market-data feed is flow-steered to the dedicated decoder core** (RSS + `ethtool -N` + IRQ affinity — Ch. 42, 53), **not** RSS-spread across cores; **`irqbalance` disabled** (§55.4.1).
- [ ] The **NIC RX ring is sized for the worst microburst** (`ethtool -G rx <N>` — Ch. 53), and **drops are monitored** (`ethtool -S` rx_missed/rx_no_buffer — §55.5).
- [ ] **NIC hardware timestamping** is enabled (`SO_TIMESTAMPING` — §55.2) and used for honest wire-arrival timing (Ch. 17) and PTP (Ch. 58) — software timestamps are **not** mistaken for arrival time.
- [ ] The ultra-hot feed/order path uses **kernel bypass** (Onload/ef_vi/libexanic — §55.4.3, Ch. 62) with the rest of the tuning (ring, core, poll, steer) in place.
- [ ] The bypass-stack / NIC-vendor choice (Onload transparent vs ef_vi raw vs DPDK — Ch. 62) is deliberate (latency vs portability vs effort).
- [ ] The whole RX path is **tested under replayed market-open burst load** (Ch. 75) — drops, latency, and steering verified at the open, not just in calm markets.

## 55.7 References

- The `ethtool(8)` man page and NIC-vendor (Intel, Mellanox/NVIDIA, Solarflare/AMD) tuning guides — RSS/queues (`-L`), flow steering (`-N`), offloads (`-K`), coalescing (`-C`), ring sizing (`-G`), timestamping (`-T`) — the mechanisms of this chapter.
- The Linux `Documentation/networking/` — `scaling.rst` (RSS/RPS/RFS/aRFS), `timestamping.rst` (`SO_TIMESTAMPING`/hardware timestamps), and NAPI busy-poll (§55.2, §55.4).
- The Solarflare/AMD **Onload** and **ef_vi** documentation, and the Exablaze/Cisco **ExaNIC libexanic** docs — the vendor bypass stacks and their hardware timestamping (§55.4.3, ties Ch. 62).
- The IEEE 1588 (PTP) and `SO_TIMESTAMPING` / `linuxptp` documentation — NIC hardware clocks and timestamps (ties Ch. 17, 58).
- Cloudflare / netdev / "Mechanical Sympathy" NIC-tuning writeups — measured offload/busy-poll latency effects behind §55.3.

## 55.8 Additional Reading

- The DPDK documentation (Ch. 62) — poll-mode drivers and NIC features in the bypass context.
- HFT NIC-tuning talks (Solarflare/Onload, ExaNIC) — production low-latency NIC configuration.
- Ch. 47 (*Linux Native I/O*) — the kernel path the NIC feeds; Ch. 51, 53 (*Sockets / Decoding*) — the feeds and packets; Ch. 42 (*Pinning*) — IRQ affinity/steering; Ch. 17 / Ch. 58 (*Timekeeping / PTP*) — hardware timestamps; Ch. 61 (*XDP/AF_XDP*) — in-driver/near-NIC processing; Ch. 62 (*Kernel Bypass*) — ef_vi/Onload/DPDK; Ch. 70 (*SmartNICs/DPUs*) — programmable NICs.
- Ch. 56 (*DDIO & the NIC-to-Cache Data Path*) — where the received packet physically lands (L3 vs DRAM) and how DDIO decides the RX latency floor; Ch. 60 (*Network Fabric & Switching*) — the hop before the NIC.
- **Appendix C** (System Tuning Checklist) — the consolidated NIC/`ethtool` tuning; **Appendix E** — kernel-stack vs busy-poll vs bypass RX latency numbers.

---

*Next: Ch. 56 — DDIO & the NIC-to-Cache Data Path, where the received packet physically lands: Intel Data Direct I/O writes NIC data straight into L3 (skipping DRAM) — a latency win that can also pollute the hot working set, and how to separate the two with the CAT partitioning of Ch. 44.*
