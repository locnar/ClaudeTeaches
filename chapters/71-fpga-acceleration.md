# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 71 — FPGA Acceleration

> **Prerequisites:** Ch. 68 (PCIe — the round-trip FPGAs avoid by going inline on the NIC), Ch. 54 (feed decoding — what an inline FPGA does in hardware), Ch. 57/64 (NIC / bypass — the FPGA sits on/replaces the NIC path), Ch. 1 (latency/determinism), Ch. 69–70 (GPU/MPI — the *throughput* accelerators; FPGA is the *latency* one).
>
> **Leads into:** Ch. 72 (SmartNICs/DPUs — programmable NICs, some FPGA-based), Ch. 84 (the wire-to-wire case study). Part XI's *latency*-tier accelerator — the one that *can* go on the tick-to-trade path.

---

## 71.1 Why it matters: deterministic nanosecond pipelines inline on the wire

GPUs (Ch. 69) and MPI clusters (Ch. 70) are *throughput* accelerators, kept off the order path by the PCIe round-trip (Ch. 68) and network latency. The **FPGA (Field-Programmable Gate Array)** is the accelerator that breaks that rule — the one that *can* go on the tick-to-trade path — and it does so by a fundamentally different placement: instead of sitting *behind* PCIe as a coprocessor the CPU calls (paying the round-trip), the FPGA is placed **inline on the NIC, directly on the wire**, so packets flow *through* it as they arrive. An FPGA is **programmable hardware** — you configure its logic gates to implement *your* circuit — so you can build a **feed handler or order-entry pipeline as a hardware circuit** that processes a packet *in the FPGA, at the wire, in nanoseconds*, with **no software, no CPU, no PCIe round-trip, no operating system** in the path. This is the lowest-latency tier in all of computing for this kind of work: **tens to low-hundreds of nanoseconds wire-to-wire**, an order of magnitude below even kernel bypass (Ch. 64's ~1 µs).

The two properties that make FPGAs uniquely suited to HFT are **latency** and **determinism**. Latency: a hardware pipeline processes a packet in a fixed, tiny number of clock cycles — there's no instruction fetch, no cache miss (Ch. 7), no branch misprediction (Ch. 13), no OS jitter (Ch. 41, 42, 43, 45) — just signals propagating through gates. Determinism: a hardware pipeline takes **exactly the same number of cycles every time** — the latency isn't just *low*, it's *constant* (no tail — Ch. 1), because there's nothing to vary it (no cache, no scheduler, no contention). For a tick-to-trade path where the *tail* is the product (Ch. 1) and the open is a microburst (Ch. 54), a deterministic-nanosecond hardware pipeline is the ultimate answer. The canonical HFT FPGA use: a **NIC-integrated FPGA** that decodes the market-data feed (Ch. 54), runs a (simple) trading/risk decision, and emits an order — **all in fabric, wire-to-wire**, with the CPU only configuring it and handling the complex/rare cases.

The cost — and why FPGAs are a specialized, high-investment tier — is **development complexity**. You don't *program* an FPGA in the software sense; you *describe hardware* — in **RTL** (Verilog/VHDL — a cycle-exact description of clocked registers and the logic between them, which synthesis maps to gates; hard, slow to develop, lowest latency) or **HLS** (High-Level Synthesis — C/C++ compiled to hardware, easier, higher-level, often higher latency). The toolchains are slow (synthesis/place-and-route takes *hours*), the debugging is hard (it's hardware), the engineers are specialized and scarce, and the logic you can fit is *limited* (an FPGA has finite gates — you can't run a huge complex strategy in fabric). So the discipline is **what belongs in fabric vs software** (§71.4.3): the *simplest, hottest, most latency-critical* part (decode, a fast pre-trade risk check, a simple signal → order) goes in the FPGA; the *complex, rare, evolving* logic stays in software (CPU). This chapter explains the HLS/RTL and host-FPGA-boundary mental model (§71.2), measures wire-to-wire hardware latency (§71.3), details NIC-integrated inline pipelines, partial reconfiguration, and the fabric-vs-software decision (§71.4), and warns about host-boundary stalls and toolchain complexity (§71.5). It's the latency accelerator — the hardware floor under the tick-to-trade path.

## 71.2 Mental model: HLS vs RTL; the host–FPGA boundary

**An FPGA is programmable hardware — a circuit you configure, not a processor you program.** It's a sea of configurable logic blocks (LUTs — lookup tables), flip-flops, DSP blocks, block RAM, and I/O, connected by configurable routing. You *describe a circuit* and the toolchain *synthesizes* it onto the fabric:

```
   CPU/software (Ch.7-14):  fetch instruction → decode → execute → ...  (sequential, cached, branchy, JITTERY)
   FPGA:                    your CIRCUIT — packet flows THROUGH the gates, pipelined, in N fixed cycles
                            (PARALLEL by construction, NO instruction fetch/cache/branch, DETERMINISTIC)

   inline placement (the HFT win):
     wire → [FPGA: parse → decide → emit order] → wire     ← nanoseconds, no CPU, no PCIe round-trip (Ch.68)
                    ↕ (PCIe, Ch.68)
                   host CPU (config, complex/rare cases, monitoring) — OFF the hot path
```

- **Why it's fast and deterministic.** A hardware pipeline does many things *in parallel* (every stage every cycle) and takes a *fixed* number of cycles — no instruction fetch (Ch. 12), no cache (Ch. 7), no branch prediction (Ch. 13), no OS (Ch. 41, 42, 43, 45). Latency = pipeline depth × clock period — *constant*. This is the latency *and* determinism (no tail — Ch. 1) FPGAs uniquely deliver.
- **HLS vs RTL — how you describe the circuit:**
  - **RTL (Register Transfer Level — Verilog/VHDL):** describe the hardware as clocked registers and the combinational logic between them (synthesis then produces the gate-level netlist) — *full control*, *lowest latency*, but *hard* (you're designing a circuit cycle-by-cycle), slow to develop, specialized skill. The choice for the ultra-hot, latency-critical pipeline.
  - **HLS (High-Level Synthesis):** write C/C++ (with pragmas) and a tool compiles it to RTL — *easier*, *faster development*, accessible to software engineers, but typically *higher latency / less efficient* than hand-written RTL (the tool's circuit isn't as tight). Good for less-ultra-critical logic, prototyping, complex algorithms. The trade: HLS productivity vs RTL latency.
- **The host–FPGA boundary (Ch. 68).** The FPGA connects to the host CPU via **PCIe** (Ch. 68) — for *configuration* (loading the bitstream), *control* (parameters, the strategy's tunable state), and *exceptions* (the complex/rare cases the FPGA hands up to software). **Crossing this boundary costs the PCIe round-trip (Ch. 68)** — so the hot path *stays in fabric* (inline on the wire), and the host boundary is only for off-hot-path config/control/exceptions. Anything that crosses to the host per-packet defeats the purpose (§71.5).
- **Inline (on the NIC) vs lookaside (behind PCIe).** Two placements: **inline** — the FPGA is *on the NIC*, on the wire path (packets flow through it) → nanosecond wire-to-wire, the HFT model (§71.4.1); **lookaside** — the FPGA is a PCIe coprocessor the CPU sends data to and gets results back (paying the round-trip — Ch. 68) → for *throughput* offload (like a GPU), not the latency win. **Inline is the tick-to-trade FPGA; lookaside is a throughput coprocessor.**

The model: **an FPGA is a configurable circuit (described in RTL — lowest latency, hard, or HLS — easier, higher latency) — parallel, no instruction-fetch/cache/branch/OS, so it's nanosecond-fast and *deterministic* (no tail). Placed *inline on the NIC* (on the wire), it processes packets in fabric with no CPU/PCIe-round-trip — the lowest-latency, most deterministic tick-to-trade path. The host (over PCIe) handles config/control/exceptions off the hot path. The cost is hardware-development complexity, so only the simplest-hottest logic goes in fabric.**

## 71.3 Measure it: wire-to-wire latency of a hardware path

The FPGA's metric is **wire-to-wire latency** — from a packet arriving on the FPGA's input to the response leaving its output — measured with hardware timestamps (Ch. 57, 60). The headline: tens-to-hundreds of nanoseconds, *deterministic* (a flat distribution, the key property), an order of magnitude below software bypass (Ch. 64).

```
   # Measure with hardware timestamps (Ch.57, 60) at the FPGA's wire I/O, or an external timing tap:
   #   t_out (first bit of the order out) - t_in (LAST bit of the triggering field in) = wire-to-wire latency
   #   (the STAC-T0 "actionable latency" convention; a first-bit-in convention adds the inbound serialization,
   #    ~54 ns for a 68-byte frame at 10GbE, ~0.4 us for 507 bytes — comparable to the FPGA itself)
   # Compare: FPGA inline vs software kernel bypass (Ch.64) vs kernel stack (Ch.48).
   # The KEY metric is not just the median — it's the DISTRIBUTION (determinism / tail — Ch.1).
```

Representative results (toolchain and machine: Appendix L.3 for the software rows) — a 10 GbE NIC-integrated FPGA (UltraScale+ class, ~300 MHz fabric clock) measured last-bit-in → first-bit-out at a PTP-timestamped tap (Ch. 60), vs software on the reference host (illustrative; the *tier* and the *determinism* are the point):

```
   tick-to-trade path                  wire-to-wire latency (p50)     p99 / tail        determinism
   kernel stack (Ch.48)                ~5 µs                          ~15-20 µs         jittery (OS, Ch.41-45)
   software kernel bypass (Ch.64)      ~1 µs                          ~2 µs             good (tuned)
   FPGA inline (HLS)                   ~hundreds of ns                ~tight            very deterministic
   FPGA inline (RTL, optimized)        ~tens to ~100 ns               p99 ≈ p50         ESSENTIALLY CONSTANT
                                                                       ↑ the tail is FLAT — no cache/branch/OS jitter

   the FPGA win: ~10x lower than software bypass AND deterministic (no tail) — the order-path floor.
```

Read it: the FPGA inline path is **~10× lower latency than software kernel bypass** (Ch. 64's ~1 µs → ~tens-to-hundreds of ns) — but the *more important* number is the **determinism**: the FPGA's **p99 ≈ p50** (the distribution is *flat*), because a hardware pipeline takes *exactly* the same cycles every time — no cache miss (Ch. 7), no branch mispredict (Ch. 13), no OS jitter (Ch. 41, 42, 43, 45), no contention (Ch. 31). For a tick-to-trade path where the *tail* is what you trade against (Ch. 1) and the open is a microburst (Ch. 54), a **deterministic** ~tens-of-nanoseconds path is the ultimate edge — not just fast on average, but *always* fast. **RTL** gives the lowest latency (tens of ns), **HLS** is higher but easier (§71.2). The measurement discipline: **measure wire-to-wire with hardware timestamps (Ch. 57, 60), and measure the *distribution* (the determinism is the FPGA's signature property), not just the median.** This is the order-path floor — and the reason HFT firms invest in FPGA: not just the ~10× over software, but the *flat tail*. (Beyond the FPGA, the only lower tier is... a tighter FPGA, or moving more into fabric — §71.4.)

## 71.4 Techniques

### 71.4.1 NIC-integrated FPGAs and inline feed handling/order entry

The canonical HFT FPGA — the trading pipeline *in fabric on the NIC*:

- **The FPGA *is* (part of) the NIC.** A NIC-integrated FPGA (or an FPGA SmartNIC — Ch. 72) puts the fabric *on the wire path* — packets arrive at the FPGA's MAC/PHY, flow through your circuit, and orders leave from the FPGA — *inline*, nanoseconds, no host (§71.2). Vendors: AMD/Xilinx (the Alveo UL3524 trading accelerator; Alveo X3522/X3522PV — an UltraScale+ FPGA NIC with user logic via hybrid mode / the PV variant; note the Solarflare X2 series is an ASIC NIC with no FPGA), Cisco/Exablaze ExaNIC (FDK-programmable FPGA), Napatech, Intel/Altera-based cards.
- **Feed handling in fabric (Ch. 54).** The market-data decode (Ch. 54 — parse the fixed-binary message, extract price/qty/symbol) is a *fixed-layout, deterministic* operation — ideal for hardware. The FPGA parses the feed at the wire (a hardware version of Ch. 54's overlay), maintains a (simple) book or top-of-book in block RAM, and detects the trigger conditions — all in nanoseconds.
- **Pre-trade risk and order entry in fabric.** A **fast pre-trade risk check** (price/size limits, position checks — Ch. 27's integer math in hardware) and **order generation** (build the order message — Ch. 54's encoding in reverse) in fabric → the FPGA can decide and *emit an order* wire-to-wire without the CPU. Risk checks in hardware are also a *safety* mechanism (a hardware kill-switch — Ch. 78).
- **Order-entry transport: who owns the TCP session?** Most venues take orders over **TCP** sessions (OUCH, FIX, binary gateways), and TCP in fabric means a **TCP offload engine** — hard, and a major vendor differentiator. The standard HFT hybrid avoids it: the **CPU keeps the session** (handshake, keep-alives, retransmission, sequence numbers) and **pre-arms** complete order frames — headers, sequence numbers and checksums precomputed — that the FPGA **fires** on a trigger, then hands back to the host to reconcile the stream. This "CPU arms / FPGA fires" split is why the partition of §71.4.3 puts the *decision* in fabric but the *session* in software.
- **The CPU handles the complex/rare (§71.4.3).** The FPGA does the *simple, hot* path; complex strategy logic, the full book, rare cases, and reconfiguration are in *software* on the host (over PCIe — Ch. 68, off the hot path). The FPGA and CPU split the work by latency-criticality (§71.4.3). A common pattern: the FPGA handles the simplest "obvious" trades at nanosecond latency, and passes everything else to the software strategy.
- **Hardware timestamping (Ch. 57, 60).** FPGA NICs provide extremely precise hardware timestamps (Ch. 57, 60) — the FPGA stamps packets at the wire with sub-nanosecond precision — for measurement (Ch. 84) and sequencing. Often the *best* timestamps available.

### 71.4.2 Partial reconfiguration

Updating the FPGA without taking it offline:

- **The problem.** Reconfiguring an FPGA (loading a new bitstream) normally *halts* it — unacceptable for a trading system that must stay live. And synthesis (compiling new logic) takes *hours* (§71.5) — you can't recompile per parameter change.
- **Partial reconfiguration (PR).** PR lets you reconfigure *part* of the FPGA (a defined region) while the *rest keeps running* — so you can swap out one module (a strategy, a parameter set) without halting the feed-handling/risk pipeline. Update the changing part live; keep the stable infrastructure running.
- **Parameters vs logic.** Most *runtime* changes are *parameters* (risk limits, thresholds, enabled symbols) — these are written to FPGA registers over PCIe (Ch. 68, off the hot path) *without* reconfiguration (just updating values the running circuit reads). Reserve PR (and full re-synthesis) for *logic* changes (a new strategy circuit, a protocol change). Design the circuit so the tunable behavior is *parameters* (fast, live) and only structural changes need PR/re-synthesis.
- **Ties to hot reload (Ch. 77).** Live parameter updates and PR are the FPGA version of hot reload (Ch. 77) — change behavior without dropping a tick. The host pushes new parameters (validated — Ch. 77) to FPGA registers; the running circuit picks them up.

### 71.4.3 Deciding what belongs in fabric vs software

The central FPGA engineering decision — partition by latency-criticality and complexity:

- **Fabric: the simplest, hottest, most latency-critical, most deterministic-benefiting.** Feed decode (Ch. 54 — fixed, hot, deterministic), fast pre-trade risk (Ch. 27 — simple integer checks, safety-critical), simple signal→order triggers (the "obvious" trades), hardware timestamping (Ch. 57, 60), and kill-switch/safety logic (Ch. 78). These are *hot every packet*, *simple enough to fit*, and *benefit hugely from determinism*.
- **Software (CPU): the complex, rare, evolving.** Full strategy logic, the complete order book (Ch. 25), complex/multi-leg decisions, gap recovery (Ch. 54), the slow/rare paths, reconfiguration, monitoring, and anything that *changes often* (software iterates in minutes; FPGA in hours — §71.5). Complex logic doesn't *fit* in fabric anyway (finite gates), and putting evolving logic in hardware is too slow to iterate.
- **The partition principle.** Put in fabric *only* what is (1) on the *hottest* path, (2) *simple* enough to implement and fit, and (3) *stable* enough not to need frequent change — and where the *determinism* matters. Everything else is software. The FPGA handles the *common, simple, fast* case; the CPU handles the *rare, complex* case (the hot/cold split — Ch. 1 — at the hardware/software boundary).
- **The FPGA-software handoff (§71.5).** When the FPGA hands a case to software (a complex decision), it crosses the PCIe boundary (Ch. 68) — so that handoff is *not* on the nanosecond path. Design so the *fabric* path is self-contained (decode→risk→order in fabric for the common case), and the software handoff is for the *rare* case (where the extra latency is acceptable). A per-packet FPGA→software round-trip defeats the FPGA (§71.5).
- **Evolve the partition.** As strategies stabilize, move more into fabric (lower latency); as they need flexibility, keep them in software. The partition is a living decision balancing latency (fabric) vs agility (software).

## 71.5 Pitfalls & anti-patterns: host-boundary stalls; toolchain complexity

- **Per-packet FPGA→host round-trips (the cardinal FPGA sin).** If the FPGA crosses the PCIe boundary (Ch. 68) to the host *per packet* (e.g. to consult software for every decision), you pay the PCIe round-trip on every packet — *defeating* the inline-nanosecond win (§71.2-3). The *fabric* path must be **self-contained** for the common case (decode→decide→order in fabric); the host boundary is for *config/control/rare exceptions only*, off the hot path (§71.4.3).
- **Toolchain complexity / slow iteration.** FPGA synthesis + place-and-route takes **hours** (vs software's seconds) — so the develop-test-iterate loop is *brutally slow* compared to software (Ch. 3's iterate loop, at hardware scale). Debugging is hardware-level (signal taps, simulation). This makes FPGA development *expensive* and *slow* — a reason to keep evolving logic in software (§71.4.3) and put only *stable* logic in fabric. Budget for the toolchain reality.
- **Putting too much (or evolving) logic in fabric.** An FPGA has *finite* gates — a complex strategy may not *fit*, and evolving logic iterates too slowly in hardware (§71.4.3). Don't try to put the whole system in fabric; put the *simple, hot, stable* core, and keep complexity/agility in software. Over-ambitious fabric scope is a common, costly mistake.
- **RTL vs HLS mismatch.** Using HLS (easier, higher latency) for the ultra-hot path where RTL's tighter latency is needed — or hand-writing RTL (hard, slow) for logic where HLS would suffice. Match the tool to the latency requirement: RTL for the critical pipeline, HLS for the rest/prototyping (§71.2).
- **Reconfiguration halting the system.** Reconfiguring the FPGA (new bitstream) halts it (§71.4.2) — doing this naively drops ticks. Use **partial reconfiguration** for live logic changes, and **register parameters** for live tuning (no reconfiguration) — design the changeable behavior as parameters (§71.4.2, ties Ch. 77).
- **Underestimating cost/specialization.** FPGA development needs specialized (scarce, expensive) hardware engineers, long timelines, and significant hardware cost. It's a *high-investment* tier — justified for the latency edge at the top, but not a casual optimization. Weigh the ROI (the latency/determinism win vs the cost).
- **Inadequate verification.** Hardware bugs are hard to fix (re-synthesis, possibly re-spin) and a bug in the fabric trading path is a financial risk (a wrong order at nanosecond speed). Verify rigorously (simulation, formal verification, hardware-in-the-loop testing — the hardware version of Ch. 40) — *especially* the risk/safety logic (a hardware kill-switch that fails is catastrophic — Ch. 78).
- **Lookaside FPGA expecting the inline win.** Using the FPGA as a *lookaside* PCIe coprocessor (CPU sends data, FPGA computes, returns — Ch. 68) and expecting nanosecond latency — that pays the PCIe round-trip (like a GPU — Ch. 69), so it's a *throughput* offload, not the latency win. The latency win requires *inline* placement (§71.2).
- **Not measuring the distribution.** The FPGA's value is *determinism* (flat tail — §71.3); measuring only the median misses the point. Measure the *distribution* (the constant-latency property is the edge — Ch. 1).

## 71.6 Exercises & checklist

**Exercises**

1. **Wire-to-wire + determinism.** (If you have an FPGA NIC) Measure wire-to-wire latency of an inline FPGA path (e.g. a simple feed-decode → echo) with hardware timestamps (Ch. 57, 60); compare the *distribution* to software kernel bypass (Ch. 64). Confirm the FPGA is ~10× lower *and* deterministic (p99 ≈ p50 — §71.3). The flat tail is the lesson.
2. **HLS vs RTL.** Implement a simple parser (Ch. 54) in HLS (C++) and (if able) RTL; compare latency, resource usage, and development effort. Quantify the HLS-productivity vs RTL-latency trade (§71.2).
3. **Fabric/software partition.** For a simple strategy, decide what goes in fabric (decode, fast risk, simple trigger) vs software (complex logic, full book, rare cases — §71.4.3); sketch the FPGA→software handoff and confirm the *fabric* path is self-contained for the common case (no per-packet host round-trip — §71.5).
4. **Parameters vs reconfiguration.** Design a circuit where risk limits are *register parameters* (live-updatable over PCIe, no reconfiguration) vs logic that needs re-synthesis. Demonstrate updating a parameter live (the FPGA hot-reload — §71.4.2, Ch. 77).
5. **Pre-trade risk in hardware.** Implement a pre-trade risk check (price/size limits — Ch. 27) as a hardware circuit; verify it (simulation) including edge cases. Treat it as a safety-critical kill-switch (Ch. 78) — what happens if it fails? (§71.5)

**Checklist — FPGA acceleration**

- [ ] The FPGA is placed **inline on the NIC / on the wire** (nanosecond wire-to-wire, no CPU/PCIe-round-trip) for the tick-to-trade path — **not lookaside** (which pays the PCIe round-trip — a throughput offload — §71.2, §71.5).
- [ ] The **fabric path is self-contained** for the common case (decode→risk→order — Ch. 54, 27) — **no per-packet FPGA→host round-trip** (§71.5); the host boundary is config/control/rare-exceptions only, off the hot path (§71.4.3).
- [ ] The **fabric/software partition** puts only the **simplest, hottest, most stable, determinism-benefiting** logic in fabric; complex/rare/evolving logic stays in software (§71.4.3).
- [ ] **RTL** is used for the ultra-hot critical pipeline (lowest latency); **HLS** for the rest/prototyping (the productivity-vs-latency trade — §71.2).
- [ ] Latency is measured **wire-to-wire with hardware timestamps** (Ch. 57, 60) and as a **distribution** — the **determinism (flat tail)** is the verified property (§71.3, Ch. 1).
- [ ] Live changes are **register parameters** (no reconfiguration) where possible; logic changes use **partial reconfiguration** (no halting the live system — §71.4.2, ties Ch. 77).
- [ ] The fabric logic — **especially pre-trade risk / kill-switch** (Ch. 78) — is **rigorously verified** (simulation/formal/hardware-in-the-loop — the hardware Ch. 40) and fails safe (§71.5).
- [ ] The **investment** (specialized engineers, hours-long toolchain, hardware cost) is justified by the **latency/determinism edge** — fabric scope is bounded to what fits and stays stable (§71.5).

## 71.7 References

- The AMD/Xilinx (Vivado/Vitis HLS) and Intel/Altera (Quartus) FPGA toolchain documentation — RTL/HLS development, synthesis, place-and-route, partial reconfiguration (§71.2, §71.4.2).
- The Exablaze/Cisco (ExaNIC FPGA), AMD/Solarflare, and Napatech FPGA-NIC documentation — NIC-integrated inline FPGA for trading (§71.4.1).
- Verilog/VHDL and HLS texts (e.g. Pong Chu's FPGA books, the Vitis HLS guide) — describing hardware (§71.2).
- HFT FPGA case studies and talks (low-latency trading on FPGA, inline feed handling/order entry) — the patterns of §71.4.
- Ch. 68 references (PCIe) — the host-FPGA boundary; Ch. 54, 57 (decoding/NIC) — what the inline FPGA does.

## 71.8 Additional Reading

- Talks on "FPGA in HFT" and wire-to-wire trading pipelines — real-world inline fabric trading systems.
- The partial-reconfiguration and FPGA-verification literature — live updates and rigorous hardware verification (§71.4.2, §71.5).
- Ch. 68 (*PCIe*) — the round-trip inline placement avoids; Ch. 54 (*Decoding*) — feed handling, now in fabric; Ch. 57/64 (*NIC/Bypass*) — the software path FPGA undercuts; Ch. 72 (*SmartNICs/DPUs*) — programmable NICs (some FPGA-based); Ch. 78 (*Process Topology*) — the kill-switch/safety in hardware; Ch. 77 (*Hot Reload*) — live parameter/PR updates; Ch. 1 (*Latency/Determinism*) — the flat-tail edge; Ch. 84 (*Case Study*) — wire-to-wire.
- **Appendix E** — wire-to-wire latency numbers (FPGA vs software bypass); **Appendix F** — FPGA/RTL/HLS/PR glossary.

---

*Next: Ch. 72 — SmartNICs & DPUs, the programmable hop between wire and host that generalizes the FPGA-NIC idea: NIC-resident ARM cores or FPGA fabric (NVIDIA BlueField, Intel IPU, AMD Pensando) that offload packet processing, filtering, and even feed handling/order entry — and when a DPU earns its place on the tick-to-trade path vs adding a hop.*
