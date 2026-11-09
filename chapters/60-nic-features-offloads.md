# Part IX — Market Data, NIC & Fabric

# Chapter 60 — NIC Features & Offloads

> **Prerequisites:** Ch. 51 (the kernel I/O path the NIC feeds), Ch. 55, 57, 59 (multicast feeds / decoding the packets / membership — the group has to be joined on *this* NIC), Ch. 45 (IRQ affinity / pinning — RSS steers to cores), Ch. 17 (timekeeping — hardware timestamps), Ch. 43 (busy-poll vs interrupts), Ch. 67-preview (kernel bypass — the NIC's user-space fast path).
>
> **Leads into:** Ch. 61 (where the DMA'd packet lands — DDIO), Ch. 62 (flow steering & receive scaling — the deep-dive of §60.4.1), Ch. 63 (PTP & cross-host hardware timestamping — built on the NIC's timestamp unit), Ch. 66 (XDP/AF_XDP — in-driver/near-NIC processing), Ch. 67 (kernel bypass — ef_vi/Onload, the NIC's bypass path), Ch. 75 (SmartNICs/DPUs — programmable NICs). The receive-side hardware under Ch. 57's decoder.

---

## 60.1 Why it matters: push work into the card

The NIC (Network Interface Card) is not a dumb wire-to-memory pipe — a modern server/HFT NIC is a sophisticated processor that can **offload** work from the CPU, **steer** packets to specific cores, **timestamp** packets in hardware, and — most importantly for HFT — provide a **kernel-bypass** path that delivers packets directly to user space (Ch. 67). The packets the feed handler decodes (Ch. 57) and the orders it sends all pass through the NIC, and *how the NIC is configured* determines the latency floor of the whole I/O path. This chapter is about using the NIC's features to get packets to the decoder (Ch. 57) — and orders onto the wire — with the lowest possible latency and CPU cost, and about the features that *matter* for latency (RSS, hardware timestamping, busy-polling, vendor bypass) versus those tuned for *throughput* (the segmentation/checksum offloads) that can actually *hurt* latency (§60.5).

The framing that runs through the chapter is **latency vs throughput** (Ch. 1), restated at the hardware level. Most NIC offloads — **LRO/GRO** (coalescing received packets), **TSO/GSO** (segmenting large sends), **interrupt coalescing** — are designed to maximize *throughput* and minimize *CPU* by **batching**: hold packets and process them in groups. That batching is exactly wrong for latency: it *adds delay* (a packet waits to be coalesced/an interrupt waits to be batched). So a latency-tuned NIC config often **disables** the throughput offloads (LRO/GRO off, interrupt coalescing off or minimal) and instead **busy-polls** (Ch. 43 — no interrupts at all). The HFT NIC is configured opposite to a throughput server: minimize batching, poll instead of interrupt, and push the *latency-relevant* work (timestamping, flow steering, bypass) into the card.

And the features that genuinely matter for HFT are specific: **RSS / flow steering** (direct the market-data feed to the right CPU/queue so the right core processes it, with IRQ affinity — Ch. 45), **hardware timestamping** (the NIC stamps each packet with a precise hardware clock at wire-arrival — the foundation of honest latency measurement, Ch. 17, and PTP cross-host sync, Ch. 63), **busy-polling** (poll the NIC's RX ring instead of waiting for interrupts — Ch. 43, lower and more deterministic latency), and the **vendor kernel-bypass stacks** (Solarflare/AMD **Onload** and **ef_vi**, Exablaze/Cisco **ExaNIC** — Ch. 67) that let user-space read the NIC's rings directly with no kernel. This chapter explains the RSS/offload/timestamping mental model (§60.2), measures offload-on/off CPU and latency (§60.3), details RSS/flow-steering, busy-polling, and Solarflare/Onload (§60.4), and warns about the throughput offloads that add latency to a low-latency path (§60.5) — the hardware foundation under the receive path of Ch. 57 and the bypass of Ch. 67.

## 60.2 Mental model: RSS, offloads, hardware timestamping

**The NIC's job and its rings.** The NIC receives packets off the wire and **DMA**s them into **RX descriptor rings** in host memory (Ch. 57.2.4), then signals the host (an **interrupt**, or the host **polls**). On send, the host puts packets in **TX rings** and the NIC transmits them. The rings, the steering of packets *to* rings, the interrupt-vs-poll choice, and the offloads are the NIC features that matter:

- **RSS (Receive Side Scaling) — spread/steer packets across queues/cores.** A NIC has *multiple* RX queues; **RSS** hashes each packet's flow (src/dst IP/port) and assigns it to a queue, each queue's interrupt pinned to a specific core (Ch. 45). This (a) spreads load across cores (throughput) and (b) — with **flow steering** (directing a *specific* flow to a *specific* queue/core, via `ntuple`/Flow Director/aRFS) — lets you put the **market-data feed on the dedicated decoder core** (Ch. 45, 57) so the right core processes it with warm caches (Ch. 50) and no cross-core handoff. RSS+steering is how you get the feed to the right place.
- **Hardware timestamping — the NIC stamps packets in hardware.** The NIC has a **PTP Hardware Clock (PHC)** and can record a precise timestamp at the moment a packet **crosses the wire** (RX) or is **sent** (TX) — far more accurate than a software timestamp taken when your code *gets around to* reading the packet (which includes all the stack/scheduling latency — Ch. 17). Exposed via `SO_TIMESTAMPING`: enable timestamping on the interface first (`SIOCSHWTSTAMP` / `hwstamp_ctl -i <if> -r 1`), then set `SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RAW_HARDWARE` (plus `TX_HARDWARE` for sends) — the `RX_HARDWARE`/`TX_HARDWARE` flags only *request generation*; without the `RAW_HARDWARE` *reporting* flag no hardware timestamp reaches the `SCM_TIMESTAMPING` cmsg. This is the basis of *honest* wire-to-wire latency measurement (Ch. 17, 94) and of PTP cross-host clock sync (Ch. 63). For HFT, hardware RX timestamps tell you *exactly* when a market-data packet arrived, independent of how long your software took to process it.
- **Offloads — work the NIC does instead of the CPU:**
  - **Checksum offload (RX/TX):** the NIC computes/verifies IP/TCP/UDP checksums — *generally good* (saves CPU, no latency cost, keep on).
  - **TSO / GSO (TCP / Generic Segmentation Offload):** the stack hands down one large buffer; with TSO the NIC segments it into packets in hardware, with GSO the kernel segments it in software just before the driver (the fallback when the NIC can't) — a *throughput* feature; little harm for latency on the send side but irrelevant for small orders.
  - **LRO/GRO (Large/Generic Receive Offload):** the NIC (LRO) or the stack (GRO) **coalesces** TCP segments within a NAPI poll batch into one large one before handing up — relevant to the TCP recovery/order sockets (Ch. 56); GRO never waits on its own timer (it flushes at the end of the poll — unless `gro_flush_timeout` is set), and for small UDP multicast datagrams it is a no-op unless `UDP_GRO`/`rx-gro-list` is enabled. The cost is the batch-sized hold and the merge work (§60.5). **Disable for latency** — cheap insurance, not a large win on a UDP feed.
  - **Interrupt coalescing (`rx-usecs`/`rx-frames`):** the NIC **batches** interrupts (waits to accumulate packets before interrupting) — *throughput/CPU* feature that **adds latency** (a packet waits for the coalesce timer — §60.5). **Minimize or disable; busy-poll instead.**
- **Busy-polling vs interrupts (Ch. 43).** Instead of the NIC **interrupting** the CPU when a packet arrives (interrupt latency + the handler + a possible context switch — Ch. 43), the CPU can **busy-poll** the RX ring (check for new descriptors in a tight loop on a dedicated core — Ch. 43). Polling is lower-latency and more *deterministic* (no interrupt jitter) — the HFT default on the hot RX path (via `SO_BUSY_POLL`/NAPI busy-poll — Ch. 51, or kernel bypass — Ch. 67).

**Kernel bypass (Ch. 67) — the NIC's user-space path.** The vendor stacks (Solarflare **Onload**/**ef_vi**, **ExaNIC libexanic**) let user-space map the NIC's rings *directly* and receive/send with **no kernel stack** — the ultimate low-latency NIC path (Ch. 67). The NIC features here (RSS steering, hardware timestamping, busy-poll) are used *within* the bypass path too.

The model: **the NIC DMAs packets into RX rings and signals via interrupt or poll; for latency you steer the feed to the right core (RSS/flow steering + IRQ affinity — Ch. 45), busy-poll instead of interrupt (Ch. 43), capture hardware timestamps (Ch. 17, 63), keep the cheap offloads (checksum) and disable the batching ones (LRO/GRO/interrupt-coalescing — they add latency), and ultimately bypass the kernel (Ch. 67). NIC config is latency-vs-throughput at the hardware level.**

## 60.3 Measure it: offload on/off CPU cost

Measure two things: the **latency** effect of the batching offloads (interrupt coalescing, GRO) — they *add* delay — and the **CPU/latency** of interrupt-driven vs busy-poll receive. The headline: disabling the throughput offloads and busy-polling *lowers and flattens* latency (at a CPU cost).

```
   # Inspect and toggle offloads (ethtool):
   $ ethtool -k <iface>                          # show offload settings (gro, lro, tso, rx/tx checksum)
   $ ethtool -K <iface> gro off lro off          # disable receive coalescing (latency)
   $ ethtool -C <iface> adaptive-rx off adaptive-tx off rx-usecs 0 tx-usecs 0   # disable coalescing (+ rx-frames 1 where the driver supports it);
   $ ethtool -c <iface>                                  #   verify: with adaptive moderation left on, mlx5/ice silently ignore rx-usecs
   $ ethtool -G <iface> rx 4096                   # size the RX ring for bursts (Ch.57)
   $ ethtool -l/-L <iface>                         # show/set RSS queue count
   $ ethtool -T <iface>                            # show hardware-timestamping capabilities

   # Measure RX latency (wire-to-app) under each config, ideally with HARDWARE timestamps (Ch.17,63):
   #   app_recv_time - nic_hw_rx_timestamp  = the software-stack latency the NIC config affects
   #   receiver: SO_TIMESTAMPING (RX_HARDWARE|RAW_HARDWARE|SOFTWARE) on the multicast socket, recvmsg(),
   #   read the SCM_TIMESTAMPING cmsg (struct scm_timestamping: ts[0]=sw, ts[2]=hw raw) and subtract from
   #   clock_gettime(CLOCK_REALTIME) taken right after recvmsg (PHC synced to system time via phc2sys, Ch. 63).
   # CPU per config:  mpstat -P <rx-core> 1   and   perf stat -e irq:irq_handler_entry -C <rx-core> sleep 10
```

Representative results (toolchain and machine: Appendix L.3) — reference machine **Xeon Gold 6326** (Ice Lake-SP), Linux 6.x, a low-latency 10/25 GbE NIC (Solarflare/AMD X2522 under `sfc`, or Intel E810 under `ice`; the bypass row needs the Solarflare part), small UDP multicast packets, pinned, turbo off (illustrative — the CPU column is the RX core's busy fraction from `mpstat`):

```
   RX config                                   wire-to-app latency (p50 / p99)   CPU      note
   default (GRO on, interrupt coalescing on)   ~8 µs / ~30 µs                    ~5%      coalescing ADDS latency
   GRO/LRO off, coalescing off (IRQ/packet)    ~5 µs / ~12 µs                    ~15%     no batching delay, more IRQs
   + busy-poll (SO_BUSY_POLL / NAPI)           ~3 µs / ~6 µs                     ~100%    no interrupt latency/jitter (Ch.43)
   + kernel bypass (ef_vi/Onload — Ch.67)      ~0.5-1 µs / ~1-2 µs               100%     no kernel stack at all

   software timestamp vs HARDWARE timestamp:   sw includes all the above latency; hw = true wire arrival (Ch.17,63)
```

Read it: the **batching offloads add latency** — GRO coalescing and interrupt coalescing *hold* packets to process them in groups (throughput/CPU efficiency), which is the *opposite* of what a latency path wants; turning them off drops the latency (and tightens the tail — less batching jitter) at the cost of more interrupts/CPU. **Busy-polling** (Ch. 43) removes the interrupt latency *and jitter* entirely (no waiting for an interrupt, no handler, no context switch) — lower and more *deterministic* — at the cost of a core pegged at 100%. And **kernel bypass** (Ch. 67) removes the stack, reaching ~1 µs. The other crucial point is the **hardware timestamp** row: a *software* receive timestamp (when your code read the packet) *includes* all the stack/config latency above — so it's not the true arrival time; the **NIC hardware timestamp** (Ch. 17, 63) is the *actual* wire-arrival moment, which is what you need for honest latency measurement (Ch. 94) and PTP (Ch. 63). The configuration lesson: **for a latency RX path, disable the batching offloads, busy-poll, size the RX ring for bursts (Ch. 57), steer the feed to the right core (RSS), capture hardware timestamps — and bypass the kernel for the ultra-hot feed (Ch. 67).** Keep the cheap offloads (checksum); kill the batching ones.

## 60.4 Techniques

### 60.4.1 RSS and flow steering

The NIC side of the mechanism, so the rest of this chapter makes sense; the *policy* — which feed lands on which queue, core and L3 slice, and the measurements that justify it — is **Ch. 62**:

- **What the card exposes.** Multiple RX queues (`ethtool -L <iface> combined N`), each with its own interrupt vector that IRQ affinity pins to a core (Ch. 45); a hash-based spreader — **RSS**, with an indirection table you can reshape — that assigns flows to queues; **exact-match filters** (`ethtool -N <iface> flow-type … action <queue>` — n-tuple / Intel Flow Director, `rte_flow` on a DPDK path) and **aRFS** that override the hash for a specific flow; and per-queue counters (`ethtool -S`) to verify where packets actually landed.
- **What you do with them is Ch. 62.** Steering the market-data multicast group to the queue the dedicated decoder core polls, per-shard steering onto the shared-nothing core topology (Ch. 31), RSS contexts for deliberate spreading, `SO_REUSEPORT`, keeping IRQ affinity and the steering rules in agreement (and `irqbalance` off so they stay that way), and the cross-core-handoff measurement that shows why it matters — Ch. 62 §62.2–§62.4. The one-line HFT summary: RSS *spreading* one feed across cores is the anti-pattern (§60.5); exact steering to the owning core is the technique.

### 60.4.2 Busy-polling

Eliminating interrupt latency on the RX path (Ch. 43, 51):

- **Poll the RX ring instead of waiting for interrupts.** On a dedicated core (Ch. 45, 48), busy-poll the NIC's RX descriptor ring in a tight loop — process a packet the instant the NIC DMAs it, with **no interrupt** (no interrupt latency, no handler, no context switch — Ch. 43), and **no interrupt jitter** (deterministic). This is the HFT RX model on the hot feed.
- **`SO_BUSY_POLL` / NAPI busy-poll (kernel path — Ch. 51).** Within the kernel stack, `SO_BUSY_POLL` (per-socket) or the NAPI busy-poll sysctls make the kernel busy-poll the NIC for the socket before sleeping — lower latency than interrupt-driven, still using the stack. The middle ground (Ch. 51) before full bypass.
- **Kernel-bypass polling (ef_vi/DPDK — Ch. 67).** The lowest-latency: user-space polls the NIC's rings directly (no kernel), the ultimate busy-poll. §60.4.3 and Ch. 67.
- **The CPU/power trade (Ch. 43).** Busy-polling pegs a core at 100% (and the power/heat — Ch. 6, and the SMT sibling — Ch. 46). Accepted for a dedicated RX core; not for a shared/idle system. Match to the deployment (dedicated cores → poll; else interrupt-driven).
- **Disable interrupt coalescing if interrupt-driven.** If you *do* use interrupts (not polling), minimize coalescing (`ethtool -C rx-usecs 0`) so packets aren't held — but polling is better for the hot path (§60.3).

### 60.4.3 Solarflare/Onload

The vendor kernel-bypass stacks — **Onload** (transparent `LD_PRELOAD` socket acceleration), **ef_vi** (Solarflare/AMD's raw ring API) and **libexanic** (ExaNIC/Cisco's) — are the production HFT NIC path, and **Ch. 67 §67.4.4 is their owner**: the ring-access mechanics, the transparent-vs-explicit latency floors, the DPDK/AF_XDP alternatives and the decision between them. What belongs *here* is the coupling between the stack and the card:

- **The stack and the NIC are chosen together.** Each vendor API runs only on that vendor's silicon, so choosing Onload/ef_vi or libexanic *is* a NIC purchase — and the card must expose what a bypass path needs: user-mappable RX/TX rings with a protected doorbell (Ch. 71), per-packet **hardware timestamps** in the descriptor (Ch. 63), and filters programmable from userspace (§60.4.1, Ch. 62) so the bypass stack can claim its flows without the kernel's help.
- **This chapter's features still apply under bypass.** Steering (Ch. 62), hardware timestamping and busy-polling (§60.4.2) are used *inside* the bypass path, not replaced by it; the batching offloads mostly are not (a raw ring delivers frames as the wire carried them, which is the point). Measure a vendor path with the same offload-on/off and hardware-timestamp methodology as §60.3 before trusting a datasheet floor — Ch. 67 §67.3 does exactly that.

## 60.5 Pitfalls & anti-patterns: offloads that add latency

- **Leaving the batching offloads on (the headline NIC-latency bug).** **GRO/LRO** (receive coalescing) and **interrupt coalescing** *hold* packets to batch them — adding latency (§60.3) — and they're *on by default* (tuned for throughput). For a latency path, **disable GRO/LRO** (`ethtool -K gro off lro off`) and **minimize/disable interrupt coalescing** (`ethtool -C rx-usecs 0`) or busy-poll. The default NIC config is a throughput config; retune it for latency.
- **Interrupt-driven RX on the hot path.** Interrupts add latency *and jitter* (the interrupt, the handler, a possible context switch — Ch. 43) vs busy-polling. On a dedicated core, **busy-poll** (§60.4.2). And if interrupt-driven, route the IRQ correctly (Ch. 45) — an un-steered NIC IRQ on the wrong core (or a hot core) is the Ch. 45 problem.
- **Undersized RX ring → market-open drops (Ch. 57).** A small RX descriptor ring overflows during a microburst → dropped packets (§57.2.4) → gaps/recovery. Size it (`ethtool -G rx <N>`) for the worst burst; monitor `ethtool -S` (rx_missed/rx_no_buffer — and read Ch. 93 §93.2.2 for what each driver's counter actually means: ring-full vs port-FIFO vs link). A top market-open drop cause.
- **Software timestamps mistaken for wire-arrival time (Ch. 17, 63).** A software receive timestamp includes all the stack/scheduling latency — it's *not* when the packet arrived. For honest latency measurement (Ch. 94) and sequencing, use **NIC hardware timestamps** (§60.2, Ch. 17, 63). Confusing the two gives wrong latency numbers.
- **RSS putting the feed on the wrong core (losing locality).** RSS is a deterministic hash of the flow tuple, so one feed (one source → one group:port) lands on *one* queue, in order — but an arbitrary one, likely a core other than the decoder's (cross-core handoff, cold caches); a channel spread over several multicast groups may land on several queues. **Steer** each group explicitly to the decoder core (§60.4.1, Ch. 62) — RSS *load-balancing* is a throughput goal; HFT wants *deterministic steering*.
- **`irqbalance` undoing your steering (Ch. 45).** Leaving `irqbalance` running re-shuffles the NIC IRQs, breaking your flow steering. Disable it (Ch. 45); pin IRQs manually.
- **Disabling checksum offload (over-correction).** Checksum offload is *cheap and good* (saves CPU, no latency cost) — disabling it (in a blanket "turn off all offloads") wastes CPU for no latency benefit. Disable the *batching* offloads (GRO/LRO/coalescing), **keep** checksum/the non-batching ones.
- **Bypass without the rest of the tuning.** Kernel bypass (Onload/ef_vi — §60.4.3, Ch. 67) is the big win, but it still needs the RX ring sized, the core dedicated/isolated (Ch. 45, 48), busy-polling, and steering — bypass on an un-tuned core/ring still drops/jitters. Bypass is part of a tuned system, not a standalone fix.
- **Vendor lock-in / portability.** Onload/ef_vi/libexanic tie you to specific NIC vendors (Solarflare/AMD, ExaNIC); DPDK (Ch. 67) is more vendor-neutral. A consideration in the bypass choice (Ch. 67), not a reason to avoid bypass — just plan for it.

## 60.6 Exercises & checklist

**Exercises**

1. **Offload latency.** Measure UDP RX latency (ideally with hardware timestamps — §60.2) with GRO/coalescing **on** (default) vs **off** (`ethtool -K gro off lro off`, `-C rx-usecs 0`). Confirm the batching offloads *add* latency and tail (§60.3). Watch the CPU/interrupt-rate trade.
2. **Busy-poll vs interrupt.** Compare interrupt-driven RX vs `SO_BUSY_POLL` (Ch. 51) vs (if available) ef_vi bypass (§60.4.3). Measure latency p50/p99 and CPU. Quantify the interrupt latency/jitter removed (§60.4.2, Ch. 43).
3. **Flow steering.** Use `ethtool -N` (ntuple) to steer a specific UDP flow to a specific RX queue whose IRQ is on a chosen core (Ch. 45). Verify (`/proc/interrupts`, `ethtool -S` per-queue) the feed lands on that core. Then mis-steer it across cores and observe the locality/reorder cost (§60.4.1, §60.5).
4. **Hardware timestamps.** Enable `SO_TIMESTAMPING` hardware RX timestamps; compare the hardware timestamp to a software timestamp taken in your recv loop. Quantify the software-stack latency the difference reveals (§60.2, ties Ch. 17, 63).
5. **RX ring sizing.** Replay a microburst (Ch. 57, 90); with a small RX ring, observe `ethtool -S` rx_missed drops; increase `ethtool -G rx` until drops stop. Find the ring size that survives your worst burst (§60.5, Ch. 57).

**Checklist — NIC features & offloads**

- [ ] **Batching offloads disabled** for the latency RX path: **GRO/LRO off** (`ethtool -K`), **interrupt coalescing minimized/off** (`ethtool -C rx-usecs 0`) — the **cheap offloads (checksum) kept** (§60.5).
- [ ] The hot RX path **busy-polls** (`SO_BUSY_POLL`/NAPI — Ch. 51, or kernel bypass — Ch. 67) on a **dedicated/isolated core** (Ch. 45, 48) — no interrupt latency/jitter (Ch. 43).
- [ ] The **market-data feed is flow-steered to the dedicated decoder core** (RSS + `ethtool -N` + IRQ affinity — Ch. 45, 57), **not** RSS-spread across cores; **`irqbalance` disabled** (§60.4.1).
- [ ] The **NIC RX ring is sized for the worst microburst** (`ethtool -G rx <N>` — Ch. 57), and **drops are monitored** (`ethtool -S` rx_missed/rx_no_buffer — §60.5).
- [ ] **NIC hardware timestamping** is enabled (`SO_TIMESTAMPING` — §60.2) and used for honest wire-arrival timing (Ch. 17) and PTP (Ch. 63) — software timestamps are **not** mistaken for arrival time.
- [ ] The ultra-hot feed/order path uses **kernel bypass** (Onload/ef_vi/libexanic — §60.4.3, Ch. 67) with the rest of the tuning (ring, core, poll, steer) in place.
- [ ] The bypass-stack / NIC-vendor choice (Onload transparent vs ef_vi raw vs DPDK — Ch. 67) is deliberate (latency vs portability vs effort).
- [ ] The whole RX path is **tested under replayed market-open burst load** (Ch. 90) — drops, latency, and steering verified at the open, not just in calm markets.

## 60.7 References

- The `ethtool(8)` man page and NIC-vendor (Intel, Mellanox/NVIDIA, Solarflare/AMD) tuning guides — RSS/queues (`-L`), flow steering (`-N`), offloads (`-K`), coalescing (`-C`), ring sizing (`-G`), timestamping (`-T`) — the mechanisms of this chapter.
- The Linux `Documentation/networking/` — `scaling.rst` (RSS/RPS/RFS/aRFS), `timestamping.rst` (`SO_TIMESTAMPING`/hardware timestamps), and NAPI busy-poll (§60.2, §60.4).
- The Solarflare/AMD **Onload** and **ef_vi** documentation, and the Exablaze/Cisco **ExaNIC libexanic** docs — the vendor bypass stacks and their hardware timestamping (§60.4.3, ties Ch. 67).
- The IEEE 1588 (PTP) and `SO_TIMESTAMPING` / `linuxptp` documentation — NIC hardware clocks and timestamps (ties Ch. 17, 63).
- Cloudflare / netdev / "Mechanical Sympathy" NIC-tuning writeups — measured offload/busy-poll latency effects behind §60.3.

## 60.8 Additional Reading

- The DPDK documentation (Ch. 67) — poll-mode drivers and NIC features in the bypass context.
- HFT NIC-tuning talks (Solarflare/Onload, ExaNIC) — production low-latency NIC configuration.
- Ch. 51 (*Linux Native I/O*) — the kernel path the NIC feeds; Ch. 55, 57 (*Sockets / Decoding*) — the feeds and packets; Ch. 45 (*Pinning*) — IRQ affinity/steering; Ch. 17 / Ch. 63 (*Timekeeping / PTP*) — hardware timestamps; Ch. 66 (*XDP/AF_XDP*) — in-driver/near-NIC processing; Ch. 67 (*Kernel Bypass*) — ef_vi/Onload/DPDK; Ch. 75 (*SmartNICs/DPUs*) — programmable NICs.
- Ch. 61 (*DDIO & the NIC-to-Cache Data Path*) — where the received packet physically lands (L3 vs DRAM) and how DDIO decides the RX latency floor; Ch. 65 (*Network Fabric & Switching*) — the hop before the NIC.
- **Appendix C** (System Tuning Checklist) — the consolidated NIC/`ethtool` tuning; **Appendix E** — kernel-stack vs bypass RX latency numbers.

---

*Next: Ch. 61 — DDIO & the NIC-to-Cache Data Path, where the received packet physically lands: Intel Data Direct I/O writes NIC data straight into L3 (skipping DRAM) — a latency win that can also pollute the hot working set, and how to separate the two with the CAT partitioning of Ch. 47.*
