# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 78 — SmartNICs & DPUs

> **Prerequisites:** Ch. 63 (NIC features/offloads — the SmartNIC generalizes them into a programmable processor), Ch. 69 (eBPF/XDP — one of the programming models, now *offloaded* onto the NIC), Ch. 77 (FPGA — some DPUs *are* FPGA-based; the inline-vs-lookaside placement question recurs), Ch. 74 (PCIe — the host–DPU boundary), Ch. 71 (RDMA — DPUs do it on-NIC), Ch. 1 (latency/tail).
>
> **Leads into:** Ch. 89 (process topology — the DPU as a separate fault domain / role offloaded off-host), Ch. 87 (security — DPU-resident isolation/filtering), Ch. 102 (the end-to-end case study). The last chapter of Part XI — the *programmable hop* between wire and host, closing the accelerator tour.

---

## 78.1 Why it matters: a programmable hop between wire and host

A normal NIC (Ch. 63) moves bytes between the wire and host memory and runs a fixed menu of offloads — checksum, segmentation, RSS, hardware timestamping. A **SmartNIC** — and its more capable form, the **DPU (Data Processing Unit)** — is a NIC with a **general-purpose programmable processor on it**: a cluster of ARM cores (NVIDIA BlueField, AMD Pensando) or FPGA fabric (some Intel/Napatech parts) or both, with its own memory, running its own OS (often Linux), sitting **between the wire and the host PCIe bus**. It's a small computer on the NIC. The promise: take work that today runs on host cores — packet filtering, feed handling, pre-trade risk, RDMA, telemetry, security/isolation — and **offload it onto the NIC**, freeing host cores and, in the best case, doing the work *closer to the wire* and *off the host's jittery scheduler* (Ch. 46, 48, 49, 51).

For HFT the appeal is twofold. First, **core liberation and isolation**: the DPU is a separate computer with its own cores, so housekeeping, telemetry (Ch. 69), capture (Ch. 94), and slow-path packet handling can run *there* instead of stealing cycles or polluting caches (Ch. 7) on the isolated hot cores (Ch. 48, 49, 51). Second — the seductive one — **moving the hot path itself onto the NIC**: decode the feed (Ch. 60), run a simple risk check, emit an order, all on the DPU, never touching the host. This is the same dream as the inline FPGA (Ch. 77), and indeed FPGA-based DPUs deliver it the same way. But the *ARM-core* DPUs are the subtle case, and the central question of this chapter is: **does the DPU shorten the path, or just add a hop?**

That question matters because a DPU is **not automatically faster**. Its ARM cores are slower than the host's x86 cores (Appendix A), and if a packet must traverse wire → DPU → *PCIe to host* → back → wire, you have *added* a PCIe crossing and a slower processor — the DPU is a **detour**, not a shortcut. The DPU earns its place only when it does **terminal** work — work that completes *on the NIC* and never round-trips to the host (inline filtering, on-NIC RDMA, a fabric-based inline pipeline) — or when it offloads work that was *never* on the hot path anyway (telemetry, capture, control plane). Get the placement wrong and the "accelerator" is a regression. This chapter builds the DPU mental model (§78.2), measures offload-vs-host latency to expose the added-hop trap (§78.3), surveys the programming models and what to offload (§78.4), and is blunt about when a DPU is the wrong tool (§78.5). It closes Part XI: the programmable hop, the generalization of the FPGA-NIC (Ch. 77) and the NIC offload (Ch. 63) into a full computer on the wire.

## 78.2 Mental model: NIC-resident ARM cores / FPGA fabric (BlueField, IPU, Pensando)

A DPU is **a computer between the wire and the host**. Picture the data path:

```
   wire  ───►  [ DPU: NIC ASIC + ARM cores (+ maybe FPGA) + DRAM + its own Linux ]  ───PCIe───►  host
            ▲                                                                                      ▲
            └─ packets arrive here; the DPU can act on them                       host cores see only what the DPU forwards
```

The DPU has its own NIC silicon (the actual Ethernet MAC/PHY and the fixed offloads of Ch. 63), **plus** a programmable compute complex:

- **ARM-core DPUs (NVIDIA BlueField-2/3, AMD Pensando, Intel IPU):** a cluster of ARM cores (8-16, ~2-3 GHz) running a full Linux, with their own DRAM. You write *software* (C/C++, DPDK/DOCA, eBPF) that runs on those cores, and P4/DOCA Flow rules that program the NIC's hardware match/action pipeline. Flexible, familiar, software-paced — but the cores are slower than host x86 and *software on the DPU still has the software costs* (cache misses, branch mispredicts, jitter — Ch. 7/13/46), just on a smaller, more isolated machine.
- **FPGA DPUs / FPGA-NICs (Napatech, AMD/Xilinx Alveo, NVIDIA Innova-2 Flex — ConnectX-5 + Xilinx KU15P FPGA — and the Intel IPU F2000X-PL on Agilex):** programmable *fabric* (Ch. 77) on the NIC. You describe a *circuit*; packets flow through it in fixed nanosecond cycles. This is the Ch. 77 inline path, packaged as a NIC. Deterministic, lowest latency — but the Ch. 77 development cost.
- **Hybrid:** both — ARM cores for control/complex, FPGA for the inline hot pipeline.

The defining architectural choice is **inline vs lookaside** — the same axis as Ch. 77:

- **Inline (on-path / "bump in the wire"):** the DPU sits *in* the packet path; every packet flows *through* it, and it can act (filter, drop, redirect, decode, modify, emit) **before** the host ever sees it — or instead of the host seeing it. Terminal work done inline never touches the host: this is where latency is *won*. XDP/eBPF offload, FPGA inline pipelines, on-NIC RDMA, inline filtering all live here.
- **Lookaside (off-path / coprocessor):** the host hands the DPU a job (encrypt this, compress that, do this RDMA), the DPU does it and returns — a PCIe round-trip (Ch. 74), exactly like a GPU (Ch. 75). Good for *throughput* offload (crypto, storage, compression) that frees host cores; **bad for hot-path latency** because it *is* the round-trip you were trying to avoid.

The mental error to avoid: assuming "SmartNIC" means "faster." A DPU is a *placement* and a *programming-model* choice. It wins when work is **terminal and inline** (never round-trips) or **off-path and non-hot** (frees cores without touching the hot path). It loses — adds a hop — when hot-path work must still cross PCIe to the host *after* the DPU touches it. Hold that distinction through the rest of the chapter.

### 78.2.1 The two boundaries: wire↔DPU and DPU↔host

A DPU has **two** boundaries, and confusing them is the root of most DPU latency mistakes:

```
   wire  ◄──[boundary 1: Ethernet]──►  DPU  ◄──[boundary 2: PCIe, Ch.74]──►  host
```

- **Boundary 1 (wire ↔ DPU):** Ethernet. The DPU sees packets here *first*, before the host. Work done at boundary 1 and *terminated there* (drop/redirect/reply inline) is the latency win — it never crosses boundary 2.
- **Boundary 2 (DPU ↔ host):** PCIe (Ch. 74), with its round-trip floor and posted-write/non-posted-read asymmetry. Every byte the host must *see* crosses here. A DPU that processes a packet and then forwards it to the host has paid boundary 2 anyway — so it only helped if it *reduced* what crosses (filtered, aggregated) or did something the host can't.

The whole game is keeping hot-path work at **boundary 1, terminal**, and using **boundary 2** only for what genuinely belongs on the host. A DPU that turns one boundary-2 crossing into *two* (host → DPU → host) for hot-path work has made things worse.

## 78.3 Measure it: DPU-offload latency vs host path

*Toolchain and machine: Appendix L.3.*

The measurement that matters is **does the DPU shorten or lengthen the path** for the work you're considering — and it must be **end-to-end wire-to-wire** (Ch. 66's PTP hardware timestamps), never "time on the DPU" in isolation, for the same reason GPU kernel-only timing lies (Ch. 75).

Three scenarios, representative numbers (BlueField-2/3-class ARM DPU + 100 GbE, DOCA 2.x, the reference host of Appendix L.3 on Linux 6.x; wire-to-wire via PTP-timestamped tap; the "forward to host" row is given for both host paths; FPGA-DPU row for contrast; reference figures pending real runs):

| Path | What happens | Wire-to-wire latency | Verdict |
|---|---|---|---|
| **Host kernel stack** (Ch. 54) | wire → NIC → PCIe → kernel → app | ~5-15 µs | baseline |
| **Host kernel bypass** (Ch. 70) | wire → NIC → PCIe → userspace poll | ~0.5-1 µs | the software floor |
| **DPU ARM-core inline filter, terminal** (drop/redirect on NIC) | wire → DPU ARM → reply/drop, host never sees it | **~2-5 µs** | wins *vs host* for the dropped/redirected packets (host work eliminated), but ARM software is slower than host bypass per-packet |
| **eSwitch hardware match/action** (rules installed from the ARM cores) | wire → eSwitch → host or drop, ARM cores untouched | **+~100s of ns** | the ASIC does it — the right way to "filter on the DPU" |
| **DPU ARM-core, then forward to host** | wire → DPU ARM → **PCIe** → host app | ~3-7 µs (host bypass) / ~7-20 µs (host kernel stack) | **added a hop** — slower than host bypass alone; the DPU is a detour |
| **FPGA-DPU inline pipeline, terminal** (Ch. 77) | wire → fabric → emit, wire-to-wire | **~tens-to-low-hundreds of ns** | the latency tier — same as Ch. 77 |

Read the table the way Ch. 1 demands — and notice the **two distinct wins and one loss**:

- **The added-hop loss (row 4):** ARM-core DPU that still forwards to the host is *slower* than just using host kernel bypass (Ch. 70). The DPU's slower cores *plus* the unavoidable PCIe crossing (boundary 2) make it a detour. If your design has hot-path packets going wire → DPU → host, you have very likely made latency *worse*. This is the single most common DPU mistake.
- **The terminal-inline win, ARM (row 3):** when the DPU *terminates* the work — drops a packet that fails a filter, replies to an RDMA read, answers without involving the host — the host never runs, so for *those* packets the path is genuinely shorter (no boundary 2). The win is **eliminating host work**, not making each packet faster than the host could.
- **The latency tier, FPGA (row 5):** an FPGA-based inline pipeline is the Ch. 77 result — deterministic nanoseconds, flat tail (p99 ≈ p50). If you need the order path *in hardware on the NIC*, this is it; the ARM-core DPU is not in this league for raw latency.

And measure the **distribution** (Ch. 1, Ch. 3): the ARM-core DPU runs *software*, so its tail has the same enemies as any software path — cache misses (Ch. 7), scheduler jitter on the DPU's own Linux (Ch. 46, 48, 49, 51), contention among the ARM cores. A DPU's median can look fine while its p99.9 is poor *because it is still software*, just on a different chip. Only the FPGA path gives the flat tail. If the pitch is "deterministic low latency," an ARM-core DPU does not automatically deliver it — you must tune the DPU's Linux exactly as you'd tune the host (Appendix C), and even then it's software.

## 78.4 Techniques

The DPU is a *placement* tool: the technique is choosing **what to offload, where (inline vs lookaside), and in which programming model** so the DPU shortens the path or frees cores without adding a hop.

### 78.4.1 Programming models (DOCA, P4, eBPF/XDP offload)

You program a DPU through one (or more) of several models, trading flexibility for latency:

- **DOCA (NVIDIA):** the BlueField SDK — libraries and frameworks (DPDK, flow steering, RDMA, regex, crypto, storage) for writing DPU applications in C/C++ on the ARM cores. The full-software model: most flexible, familiar, but ARM-core-paced and software-jittery. Use it for control-plane, telemetry, capture, and *terminal* slow-path work — not for chasing the FPGA latency tier.
- **Hardware flow steering (the eSwitch).** In DPU/embedded mode the ARM cores *program* the ConnectX embedded switch — flow tables via DOCA Flow, `rte_flow`, or `tc flower` offload (ASAP²), including connection tracking — and the ASIC then executes the match/action at line rate: packets the rules forward or drop **never touch the ARM cores** (the §78.3 eSwitch row). Steering, drop and header rewrite belong here; the ARM software path is for the exceptions the rules miss.
- **P4:** a domain-specific language for *packet processing* — describe match/action pipelines (parse headers, match fields, act: forward/drop/modify) that compile onto the NIC's switching/flow hardware. Declarative, runs in fast fixed-function or fabric, much lower latency than ARM software for the *filtering/steering* it can express. Good for inline filtering, steering, header rewrite — terminal boundary-1 work.
- **eBPF/XDP on the DPU (Ch. 69):** true *hardware* XDP offload (`XDP_FLAGS_HW_MODE`, `xdpoffload`) exists only on Netronome/Corigine NFP NICs. On an ARM DPU the *same* XDP program runs natively in the DPU's own Linux on its ARM cores — software, with the §78.3 ARM-row latency and tail — *in front of* the host; the hardware path is the eSwitch above. The natural HFT use is still the same: drop the feed you don't subscribe to, redirect the A/B feeds (Ch. 60), filter hostile/malformed packets (Ch. 87) — *terminally, on the NIC*, so the host's hot core only ever sees the packets it must process. This is the cleanest "DPU shortens the path" win for an ARM/SmartNIC: less crosses boundary 2.
- **FPGA fabric (Ch. 77):** for the FPGA-DPU, the Ch. 77 RTL/HLS model — the inline-pipeline latency tier. Everything from Ch. 77 applies.

The selection rule mirrors §78.2's inline/lookaside axis: **filtering/steering/dropping** → P4 or XDP-offload, inline, terminal, low-latency; **complex/control/slow-path** → DOCA software on ARM, off the hot path; **the order pipeline in hardware** → FPGA fabric (Ch. 77). Reach for the *most declarative, most inline* model the work allows — software on the ARM cores is the fallback for what can't be expressed in fixed-function/fabric, not the default.

### 78.4.2 On-NIC RDMA and storage/security offload

The strongest *non-order-path* DPU wins are the offloads that are **terminal on the NIC by construction** — they complete at boundary 1 and never burden a host hot core:

- **On-NIC RDMA (Ch. 71):** a plain RDMA NIC already terminates one-sided `READ`/`WRITE` without the host CPU (Ch. 71's one-sided semantics — that is not the DPU's contribution). What the DPU adds is an on-NIC *initiator/consumer*: its ARM cores can run the RDMA-driven capture/replication or market-data fan-out pipeline (Ch. 94) themselves — issuing the WRITEs, reaping the completions, managing the buffers — so the host is not merely uninvolved in the data movement but in the *orchestration* too. A genuine win that doesn't touch the order path.
- **Storage offload:** NVMe-oF, compression, and the capture/journal write path (Ch. 94) can run on the DPU — getting captured ticks/orders to storage **entirely off the host** (the Ch. 94 "off the hot path" goal, taken one chip further: off the host *machine*). The host's isolated cores never see the storage I/O.
- **Security/isolation offload (Ch. 87):** inline encryption/decryption (TLS/IPsec), firewalling, and DDoS/malformed-packet filtering at boundary 1 — hostile traffic is dropped *on the NIC* before it reaches the host stack (Ch. 87's "treat the wire as untrusted," now enforced before the host). The DPU as an isolated security perimeter is one of its strongest production cases, and it's *off* the hot path by design.

The pattern across all three: **terminal at boundary 1, host cores untouched.** These are the DPU wins that are real *regardless* of whether you ever put the order path on the NIC — they free and protect the host without adding a hop, because the work *ends on the NIC*.

### 78.4.3 Partitioning work between DPU and host

The master technique is the **partition** — deciding which work goes on the DPU and which stays on the host — governed by one test: **does it terminate on the NIC, or must it round-trip?**

```
   ON THE DPU (boundary-1 terminal — wins):
     • inline filter/drop/redirect (XDP-offload, P4)         — host never sees the dropped packets
     • on-NIC RDMA, storage, capture (Ch.71, 94)             — terminal, zero host-core cost
     • security/isolation, crypto (Ch.87)                    — hostile traffic dies on the NIC
     • the order pipeline IN FABRIC (FPGA-DPU, Ch.77)        — deterministic ns, terminal
     • housekeeping/telemetry/control (Ch.69)                — off the host's hot cores entirely

   ON THE HOST (keep here):
     • complex/evolving strategy logic                       — too big/changeable for fabric, faster on x86
     • anything the DPU would only FORWARD after touching    — DPU would just add a hop
     • work whose latency is already fine on host bypass     — don't "offload" a non-problem

   THE TRAP (avoid):
     • hot-path packet: wire → DPU(ARM) → PCIe → host        — slower than plain host bypass (added hop)
```

Concretely, in a trading system: put **inline feed filtering/arbitration** (Ch. 60 A/B), **on-NIC RDMA/capture** (Ch. 71/94), **security filtering** (Ch. 87), and **telemetry** (Ch. 69) on the DPU — all terminal or off-path, all freeing/protecting the host. Keep the **strategy** on the host (x86, fast, evolving). Put the **order pipeline on the DPU only if it's an FPGA-DPU and stays in fabric** (Ch. 77); if it's an ARM-core DPU, the order path almost certainly stays on host bypass (Ch. 70), because routing it through ARM-then-host is the trap. The partition is the same fault-domain thinking as Ch. 89 — the DPU is a separate machine and a separate fault domain (a DPU crash shouldn't take the host with it, and vice versa) — applied at the wire.

The discipline, stated once: **offload what terminates on the NIC or was never hot; keep on the host what must round-trip or is already fast enough.** A DPU that obeys this frees cores and shortens paths; a DPU that violates it is an expensive detour.

## 78.5 Pitfalls & anti-patterns: when the DPU just adds a hop

- **The added hop (the cardinal sin).** Routing hot-path packets wire → DPU(ARM) → PCIe → host → wire. You added a PCIe crossing (boundary 2, Ch. 74) and a *slower* processor to a path you were trying to shorten. This is *slower* than plain host kernel bypass (Ch. 70). If hot-path packets cross boundary 2 *after* the DPU touches them, the DPU is a detour. Measure wire-to-wire (§78.3) and you'll see it.
- **Assuming "SmartNIC" means "faster."** A DPU is a placement and programming-model choice, not a speed button. ARM cores are *slower* than host x86 (Appendix A); software on the DPU has all the software costs (cache, branches, jitter — Ch. 7/13/46), just on a smaller machine. It wins only by **terminal/inline placement** or **freeing host cores**, never automatically.
- **Confusing inline with lookaside.** Lookaside offload (host hands DPU a job, gets it back) *is* a PCIe round-trip (Ch. 74) — fine for *throughput* (crypto/compression/storage that frees cores), useless or harmful for *hot-path latency*. Don't put latency-critical work in a lookaside model and expect a latency win; you bought the round-trip you were avoiding.
- **Expecting FPGA determinism from ARM cores.** The flat-tail, deterministic-nanosecond result (Ch. 77, §78.3 row 5) comes from *fabric*, not from software on ARM cores. An ARM-core DPU runs software with a software tail — cache misses, scheduler jitter on the DPU's own Linux (Ch. 46, 48, 49, 51), inter-core contention. If you need determinism, you need fabric (FPGA-DPU), and you must tune the DPU's OS like any hot box (Appendix C).
- **Ignoring the DPU's own jitter.** The DPU runs its own Linux with its own scheduler, IRQs, and housekeeping. Untuned, it has the same jitter problems as an untuned host (Ch. 46, 48, 49, 51) — you've just moved the problem to a chip you can't see as easily. Treat the DPU as a host to be tuned (isolate cores, pin, quiet it — Appendix C), not a magic appliance.
- **Underestimating the development and operational cost.** A DPU is another computer to provision, image, secure, monitor, update, and debug — with a less familiar toolchain (DOCA/P4/XDP-offload), harder observability (it's *on the NIC*), and FPGA development costs (Ch. 77) if it's fabric-based. The TCO is real; justify it with a measured win, not a datasheet.
- **Single point of failure / fault-domain confusion.** The DPU is inline on the wire — if it hangs or crashes, traffic stops. It's a separate fault domain (Ch. 89): a DPU crash and a host crash are different events with different blast radii. Design the kill-switch/safe-state (Ch. 87/89) accounting for the DPU as its own failure mode, and don't let DPU and host take each other down.
- **Offloading a non-problem.** If host kernel bypass (Ch. 70) already meets your latency budget for some work, "offloading" it to a DPU adds cost and risk for no win. Offload to *free contended host cores* or to do *terminal/inline* work the host can't — not to move work that was already fast enough.
- **Not measuring wire-to-wire.** "Time spent on the DPU" is the GPU-kernel-only lie again (Ch. 75): it omits both PCIe crossings and the queuing. Measure end-to-end with PTP hardware timestamps (Ch. 66) on a tap, distribution and all (Ch. 1/3), or you'll ship an added-hop regression believing it's an accelerator.

## 78.6 Exercises & checklist

**Exercises**

1. **Expose the added hop.** Build two paths for the same filter-then-process work: (a) host kernel bypass (Ch. 70) doing everything on host; (b) ARM-core DPU that filters then *forwards survivors to the host*. Measure both wire-to-wire (Ch. 66 PTP tap). Confirm (b) is *slower* for the forwarded packets (added boundary-2 crossing + slower cores) and quantify the penalty. Then make the DPU *terminal* for the dropped packets and show the host-work elimination win for those.
2. **XDP-offload feed filter.** Take an XDP drop/redirect program (Ch. 69) and run it (a) in the host driver vs (b) offloaded onto the DPU. Measure how much *less* crosses PCIe to the host in case (b) and the host-core cycles freed. This is the clean "DPU shortens the path" win.
3. **DPU tail under load.** Run a software filter on the ARM-core DPU and measure its latency *distribution* (Ch. 3, HdrHistogram) under microburst load (Ch. 60). Observe the software tail (p99.9 ≫ p50). Then tune the DPU's Linux (isolate/pin cores, quiet IRQs — Appendix C) and re-measure. Quantify how much the tail is *software*, not hardware.
4. **On-NIC RDMA capture.** Move the capture/journal write path (Ch. 94) onto the DPU via on-NIC RDMA/storage offload. Confirm host hot-core cost drops to ~zero for capture and that the order path's tail is unaffected. This is a terminal, off-hot-path DPU win.
5. **Partition decision.** For your tick-to-trade path, write the §78.4.3 partition explicitly: list each piece of work and classify it *terminal-on-NIC* / *off-hot-path* / *keep-on-host* / *trap*. Justify each with the boundary it crosses.

**Checklist — SmartNICs & DPUs**

- [ ] Every candidate offload is classified **inline-terminal** / **lookaside** / **off-hot-path** — and no *hot-path* work is in a lookaside (round-trip) model (§78.2, §78.5).
- [ ] No hot-path packet goes wire → DPU(ARM) → **PCIe** → host (the added hop) — hot-path work is either terminal on the NIC or stays on host bypass (§78.5).
- [ ] Latency is measured **wire-to-wire** with PTP hardware timestamps (Ch. 66), distribution and all (Ch. 1/3) — never "time on the DPU" alone (§78.3).
- [ ] Determinism claims are backed by **fabric** (FPGA-DPU, Ch. 77), not expected from ARM software; the DPU's own Linux is **tuned** (Appendix C) if it carries any latency-sensitive work (§78.5).
- [ ] DPU wins are **terminal-on-NIC** (XDP-offload filter, on-NIC RDMA, capture, security — §78.4.2) or **core-freeing/control-plane**, not "offloading a non-problem" already fast on host bypass (§78.5).
- [ ] The DPU is treated as a **separate machine and fault domain** (Ch. 89): provisioned, secured, monitored, kill-switched, and prevented from taking the host down (or vice versa) (§78.5).
- [ ] The TCO (extra computer, unfamiliar toolchain, FPGA cost if fabric) is justified by a **measured** wire-to-wire win, not a datasheet (§78.5).

## 78.7 References

- NVIDIA BlueField DPU and **DOCA** SDK documentation — ARM-core DPU architecture, programming model, on-NIC RDMA/storage/security offload (§78.2, §78.4).
- AMD Pensando and Intel IPU documentation — alternative DPU architectures and offload models (§78.2).
- The **P4** language specification (p4.org) and P4-on-NIC toolchains — declarative match/action packet processing (§78.4.1).
- The XDP/eBPF *offload* documentation (Ch. 69 references, plus NIC-offload specifics) — running XDP on the NIC (§78.4.1).
- Ch. 63 references (NIC offloads — the DPU generalizes them), Ch. 71 (RDMA — done on-NIC), Ch. 74 (PCIe — the host–DPU boundary), Ch. 77 (FPGA — the fabric DPU).

## 78.8 Additional Reading

- Vendor case studies and talks on DPU offload in finance/HFT — feed filtering, capture, security, and on-NIC RDMA in production (§78.4).
- The "is a DPU worth it?" analyses and benchmarks from the systems/networking community — the added-hop trap and where offload actually pays (§78.3, §78.5).
- Ch. 77 (*FPGA*) — the fabric path and its determinism, which FPGA-DPUs inherit; Ch. 69 (*eBPF/XDP*) — the offloaded programming model; Ch. 63 (*NIC offloads*) — the fixed-function baseline; Ch. 70 (*Kernel bypass*) — the host software floor the DPU must beat to be worth it; Ch. 89 (*Process topology*) — the DPU as a fault domain; Ch. 87 (*Security*) — DPU-resident isolation; Ch. 102 (*Case study*) — where the DPU fits the wire-to-wire path.
- **Appendix A** (ARM/Graviton) — the DPU's ARM cores and their memory model/performance vs x86; **Appendix C** — tuning the DPU's own Linux; **Appendix E** — wire-to-wire numbers; **Appendix F** — DPU/SmartNIC/inline/lookaside glossary.

---

*Next: Ch. 79 — Hand-Written & Inline Assembly, opening Part XII (Beyond C++: Languages at the Latency Floor): the writing-side sequel to Ch. 4 — GCC/Clang extended-asm anatomy (operands, constraints, clobbers, `volatile`, `asm goto`), what the optimizer may do around an asm statement, the intrinsics-first rule measured, the irreducible cases (`rdpmc`, serializing timestamps, `clwb`, `movdir64b` doorbells), a hand-written NASM decoder against the compiler's, and the one-line clobber bug that turns a loop infinite under both compilers.*
