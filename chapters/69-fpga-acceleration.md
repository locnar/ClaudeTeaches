# Part XI — Heterogeneous Computing & Hardware Acceleration

# Chapter 69 — FPGA Acceleration

> **Prerequisites:** Ch. 66 (PCIe — the round-trip FPGAs avoid by going inline on the NIC), Ch. 53 (feed decoding — what an inline FPGA does in hardware), Ch. 55/62 (NIC / bypass — the FPGA sits on/replaces the NIC path), Ch. 1 (latency/determinism), Ch. 67–68 (GPU/MPI — the *throughput* accelerators; FPGA is the *latency* one).
>
> **Leads into:** Ch. 70 (SmartNICs/DPUs — programmable NICs, some FPGA-based), Ch. 53 (the feed handler, now partly in fabric), Ch. 76 (the wire-to-wire case study). Part XI's *latency*-tier accelerator — the one that *can* go on the tick-to-trade path.

---

## 69.1 Why it matters: deterministic nanosecond pipelines inline on the wire

GPUs (Ch. 67) and MPI clusters (Ch. 68) are *throughput* accelerators, kept off the order path by the PCIe round-trip (Ch. 66) and network latency. The **FPGA (Field-Programmable Gate Array)** is the accelerator that breaks that rule — the one that *can* go on the tick-to-trade path — and it does so by a fundamentally different placement: instead of sitting *behind* PCIe as a coprocessor the CPU calls (paying the round-trip), the FPGA is placed **inline on the NIC, directly on the wire**, so packets flow *through* it as they arrive. An FPGA is **programmable hardware** — you configure its logic gates to implement *your* circuit — so you can build a **feed handler or order-entry pipeline as a hardware circuit** that processes a packet *in the FPGA, at the wire, in nanoseconds*, with **no software, no CPU, no PCIe round-trip, no operating system** in the path. This is the lowest-latency tier in all of computing for this kind of work: **tens to low-hundreds of nanoseconds wire-to-wire**, an order of magnitude below even kernel bypass (Ch. 62's ~1 µs).

The two properties that make FPGAs uniquely suited to HFT are **latency** and **determinism**. Latency: a hardware pipeline processes a packet in a fixed, tiny number of clock cycles — there's no instruction fetch, no cache miss (Ch. 7), no branch misprediction (Ch. 13), no OS jitter (Ch. 41, 42, 43, 45) — just signals propagating through gates. Determinism: a hardware pipeline takes **exactly the same number of cycles every time** — the latency isn't just *low*, it's *constant* (no tail — Ch. 1), because there's nothing to vary it (no cache, no scheduler, no contention). For a tick-to-trade path where the *tail* is the product (Ch. 1) and the open is a microburst (Ch. 53), a deterministic-nanosecond hardware pipeline is the ultimate answer. The canonical HFT FPGA use: a **NIC-integrated FPGA** that decodes the market-data feed (Ch. 53), runs a (simple) trading/risk decision, and emits an order — **all in fabric, wire-to-wire**, with the CPU only configuring it and handling the complex/rare cases.

The cost — and why FPGAs are a specialized, high-investment tier — is **development complexity**. You don't *program* an FPGA in the software sense; you *describe hardware* — in **RTL** (Verilog/VHDL — gate-level, hard, slow to develop, lowest latency) or **HLS** (High-Level Synthesis — C/C++ compiled to hardware, easier, higher-level, often higher latency). The toolchains are slow (synthesis/place-and-route takes *hours*), the debugging is hard (it's hardware), the engineers are specialized and scarce, and the logic you can fit is *limited* (an FPGA has finite gates — you can't run a huge complex strategy in fabric). So the discipline is **what belongs in fabric vs software** (§69.4.3): the *simplest, hottest, most latency-critical* part (decode, a fast pre-trade risk check, a simple signal → order) goes in the FPGA; the *complex, rare, evolving* logic stays in software (CPU). This chapter explains the HLS/RTL and host-FPGA-boundary mental model (§69.2), measures wire-to-wire hardware latency (§69.3), details NIC-integrated inline pipelines, partial reconfiguration, and the fabric-vs-software decision (§69.4), and warns about host-boundary stalls and toolchain complexity (§69.5). It's the latency accelerator — the hardware floor under the tick-to-trade path.

## 69.2 Mental model: HLS vs RTL; the host–FPGA boundary

**An FPGA is programmable hardware — a circuit you configure, not a processor you program.** It's a sea of configurable logic blocks (LUTs — lookup tables), flip-flops, DSP blocks, block RAM, and I/O, connected by configurable routing. You *describe a circuit* and the toolchain *synthesizes* it onto the fabric:

```
   CPU/software (Ch.6-13):  fetch instruction → decode → execute → ...  (sequential, cached, branchy, JITTERY)
   FPGA:                    your CIRCUIT — packet flows THROUGH the gates, pipelined, in N fixed cycles
                            (PARALLEL by construction, NO instruction fetch/cache/branch, DETERMINISTIC)

   inline placement (the HFT win):
     wire → [FPGA: parse → decide → emit order] → wire     ← nanoseconds, no CPU, no PCIe round-trip (Ch.54)
                    ↕ (PCIe, Ch.54)
                   host CPU (config, complex/rare cases, monitoring) — OFF the hot path
```

- **Why it's fast and deterministic.** A hardware pipeline does many things *in parallel* (every stage every cycle) and takes a *fixed* number of cycles — no instruction fetch (Ch. 12), no cache (Ch. 7), no branch prediction (Ch. 13), no OS (Ch. 41, 42, 43, 45). Latency = pipeline depth × clock period — *constant*. This is the latency *and* determinism (no tail — Ch. 1) FPGAs uniquely deliver.
- **HLS vs RTL — how you describe the circuit:**
  - **RTL (Register Transfer Level — Verilog/VHDL):** describe the hardware at the gate/register level — *full control*, *lowest latency*, but *hard* (you're designing a circuit cycle-by-cycle), slow to develop, specialized skill. The choice for the ultra-hot, latency-critical pipeline.
  - **HLS (High-Level Synthesis):** write C/C++ (with pragmas) and a tool compiles it to RTL — *easier*, *faster development*, accessible to software engineers, but typically *higher latency / less efficient* than hand-written RTL (the tool's circuit isn't as tight). Good for less-ultra-critical logic, prototyping, complex algorithms. The trade: HLS productivity vs RTL latency.
- **The host–FPGA boundary (Ch. 66).** The FPGA connects to the host CPU via **PCIe** (Ch. 66) — for *configuration* (loading the bitstream), *control* (parameters, the strategy's tunable state), and *exceptions* (the complex/rare cases the FPGA hands up to software). **Crossing this boundary costs the PCIe round-trip (Ch. 66)** — so the hot path *stays in fabric* (inline on the wire), and the host boundary is only for off-hot-path config/control/exceptions. Anything that crosses to the host per-packet defeats the purpose (§69.5).
- **Inline (on the NIC) vs lookaside (behind PCIe).** Two placements: **inline** — the FPGA is *on the NIC*, on the wire path (packets flow through it) → nanosecond wire-to-wire, the HFT model (§69.4.1); **lookaside** — the FPGA is a PCIe coprocessor the CPU sends data to and gets results back (paying the round-trip — Ch. 66) → for *throughput* offload (like a GPU), not the latency win. **Inline is the tick-to-trade FPGA; lookaside is a throughput coprocessor.**

The model: **an FPGA is a configurable circuit (described in RTL — lowest latency, hard, or HLS — easier, higher latency) — parallel, no instruction-fetch/cache/branch/OS, so it's nanosecond-fast and *deterministic* (no tail). Placed *inline on the NIC* (on the wire), it processes packets in fabric with no CPU/PCIe-round-trip — the lowest-latency, most deterministic tick-to-trade path. The host (over PCIe) handles config/control/exceptions off the hot path. The cost is hardware-development complexity, so only the simplest-hottest logic goes in fabric.**

## 69.3 Measure it: wire-to-wire latency of a hardware path

The FPGA's metric is **wire-to-wire latency** — from a packet arriving on the FPGA's input to the response leaving its output — measured with hardware timestamps (Ch. 55, 58). The headline: tens-to-hundreds of nanoseconds, *deterministic* (a flat distribution, the key property), an order of magnitude below software bypass (Ch. 62).

```
   # Measure with hardware timestamps (Ch.49-50) at the FPGA's wire I/O, or an external timing tap:
   #   t_out (order leaves) - t_in (feed packet arrives) = wire-to-wire latency
   # Compare: FPGA inline vs software kernel bypass (Ch.52) vs kernel stack (Ch.44).
   # The KEY metric is not just the median — it's the DISTRIBUTION (determinism / tail — Ch.1).
```

Representative results — NIC-integrated FPGA inline path vs software (illustrative; the *tier* and the *determinism* are the point):

```
   tick-to-trade path                  wire-to-wire latency (p50)     p99 / tail        determinism
   kernel stack (Ch.44)                ~5-20 µs                       much worse        jittery (OS, Ch.39-42)
   software kernel bypass (Ch.52)      ~1 µs                          ~2 µs             good (tuned)
   FPGA inline (HLS)                   ~hundreds of ns                ~tight            very deterministic
   FPGA inline (RTL, optimized)        ~tens to ~100 ns               p99 ≈ p50         ESSENTIALLY CONSTANT
                                                                       ↑ the tail is FLAT — no cache/branch/OS jitter

   the FPGA win: ~10x lower than software bypass AND deterministic (no tail) — the order-path floor.
```

Read it: the FPGA inline path is **~10× lower latency than software kernel bypass** (Ch. 62's ~1 µs → ~tens-to-hundreds of ns) — but the *more important* number is the **determinism**: the FPGA's **p99 ≈ p50** (the distribution is *flat*), because a hardware pipeline takes *exactly* the same cycles every time — no cache miss (Ch. 7), no branch mispredict (Ch. 13), no OS jitter (Ch. 41, 42, 43, 45), no contention (Ch. 31). For a tick-to-trade path where the *tail* is what you trade against (Ch. 1) and the open is a microburst (Ch. 53), a **deterministic** ~tens-of-nanoseconds path is the ultimate edge — not just fast on average, but *always* fast. **RTL** gives the lowest latency (tens of ns), **HLS** is higher but easier (§69.2). The measurement discipline: **measure wire-to-wire with hardware timestamps (Ch. 55, 58), and measure the *distribution* (the determinism is the FPGA's signature property), not just the median.** This is the order-path floor — and the reason HFT firms invest in FPGA: not just the ~10× over software, but the *flat tail*. (Beyond the FPGA, the only lower tier is... a tighter FPGA, or moving more into fabric — §69.4.)

## 69.4 Techniques

### 69.4.1 NIC-integrated FPGAs and inline feed handling/order entry

The canonical HFT FPGA — the trading pipeline *in fabric on the NIC*:

- **The FPGA *is* (part of) the NIC.** A NIC-integrated FPGA (or an FPGA SmartNIC — Ch. 70) puts the fabric *on the wire path* — packets arrive at the FPGA's MAC/PHY, flow through your circuit, and orders leave from the FPGA — *inline*, nanoseconds, no host (§69.2). Vendors: Exablaze/Cisco (ExaNIC + FPGA), AMD/Xilinx (Alveo, and the Solarflare X2/X3 with FPGA), Napatech, etc.
- **Feed handling in fabric (Ch. 53).** The market-data decode (Ch. 53 — parse the fixed-binary message, extract price/qty/symbol) is a *fixed-layout, deterministic* operation — ideal for hardware. The FPGA parses the feed at the wire (a hardware version of Ch. 53's overlay), maintains a (simple) book or top-of-book in block RAM, and detects the trigger conditions — all in nanoseconds.
- **Pre-trade risk and order entry in fabric.** A **fast pre-trade risk check** (price/size limits, position checks — Ch. 27's integer math in hardware) and **order generation** (build the order message — Ch. 53's encoding in reverse) in fabric → the FPGA can decide and *emit an order* wire-to-wire without the CPU. Risk checks in hardware are also a *safety* mechanism (a hardware kill-switch — Ch. 74).
- **The CPU handles the complex/rare (§69.4.3).** The FPGA does the *simple, hot* path; complex strategy logic, the full book, rare cases, and reconfiguration are in *software* on the host (over PCIe — Ch. 66, off the hot path). The FPGA and CPU split the work by latency-criticality (§69.4.3). A common pattern: the FPGA handles the simplest "obvious" trades at nanosecond latency, and passes everything else to the software strategy.
- **Hardware timestamping (Ch. 55, 58).** FPGA NICs provide extremely precise hardware timestamps (Ch. 55, 58) — the FPGA stamps packets at the wire with sub-nanosecond precision — for measurement (Ch. 76) and sequencing. Often the *best* timestamps available.

### 69.4.2 Partial reconfiguration

Updating the FPGA without taking it offline:

- **The problem.** Reconfiguring an FPGA (loading a new bitstream) normally *halts* it — unacceptable for a trading system that must stay live. And synthesis (compiling new logic) takes *hours* (§69.5) — you can't recompile per parameter change.
- **Partial reconfiguration (PR).** PR lets you reconfigure *part* of the FPGA (a defined region) while the *rest keeps running* — so you can swap out one module (a strategy, a parameter set) without halting the feed-handling/risk pipeline. Update the changing part live; keep the stable infrastructure running.
- **Parameters vs logic.** Most *runtime* changes are *parameters* (risk limits, thresholds, enabled symbols) — these are written to FPGA registers over PCIe (Ch. 66, off the hot path) *without* reconfiguration (just updating values the running circuit reads). Reserve PR (and full re-synthesis) for *logic* changes (a new strategy circuit, a protocol change). Design the circuit so the tunable behavior is *parameters* (fast, live) and only structural changes need PR/re-synthesis.
- **Ties to hot reload (Ch. 73).** Live parameter updates and PR are the FPGA version of hot reload (Ch. 73) — change behavior without dropping a tick. The host pushes new parameters (validated — Ch. 73) to FPGA registers; the running circuit picks them up.

### 69.4.3 Deciding what belongs in fabric vs software

The central FPGA engineering decision — partition by latency-criticality and complexity:

- **Fabric: the simplest, hottest, most latency-critical, most deterministic-benefiting.** Feed decode (Ch. 53 — fixed, hot, deterministic), fast pre-trade risk (Ch. 27 — simple integer checks, safety-critical), simple signal→order triggers (the "obvious" trades), hardware timestamping (Ch. 55, 58), and kill-switch/safety logic (Ch. 74). These are *hot every packet*, *simple enough to fit*, and *benefit hugely from determinism*.
- **Software (CPU): the complex, rare, evolving.** Full strategy logic, the complete order book (Ch. 25), complex/multi-leg decisions, gap recovery (Ch. 53), the slow/rare paths, reconfiguration, monitoring, and anything that *changes often* (software iterates in minutes; FPGA in hours — §69.5). Complex logic doesn't *fit* in fabric anyway (finite gates), and putting evolving logic in hardware is too slow to iterate.
- **The partition principle.** Put in fabric *only* what is (1) on the *hottest* path, (2) *simple* enough to implement and fit, and (3) *stable* enough not to need frequent change — and where the *determinism* matters. Everything else is software. The FPGA handles the *common, simple, fast* case; the CPU handles the *rare, complex* case (the hot/cold split — Ch. 1 — at the hardware/software boundary).
- **The FPGA-software handoff (§69.5).** When the FPGA hands a case to software (a complex decision), it crosses the PCIe boundary (Ch. 66) — so that handoff is *not* on the nanosecond path. Design so the *fabric* path is self-contained (decode→risk→order in fabric for the common case), and the software handoff is for the *rare* case (where the extra latency is acceptable). A per-packet FPGA→software round-trip defeats the FPGA (§69.5).
- **Evolve the partition.** As strategies stabilize, move more into fabric (lower latency); as they need flexibility, keep them in software. The partition is a living decision balancing latency (fabric) vs agility (software).

## 69.5 Pitfalls & anti-patterns: host-boundary stalls; toolchain complexity

- **Per-packet FPGA→host round-trips (the cardinal FPGA sin).** If the FPGA crosses the PCIe boundary (Ch. 66) to the host *per packet* (e.g. to consult software for every decision), you pay the PCIe round-trip on every packet — *defeating* the inline-nanosecond win (§69.2-3). The *fabric* path must be **self-contained** for the common case (decode→decide→order in fabric); the host boundary is for *config/control/rare exceptions only*, off the hot path (§69.4.3).
- **Toolchain complexity / slow iteration.** FPGA synthesis + place-and-route takes **hours** (vs software's seconds) — so the develop-test-iterate loop is *brutally slow* compared to software (Ch. 3's iterate loop, at hardware scale). Debugging is hardware-level (signal taps, simulation). This makes FPGA development *expensive* and *slow* — a reason to keep evolving logic in software (§69.4.3) and put only *stable* logic in fabric. Budget for the toolchain reality.
- **Putting too much (or evolving) logic in fabric.** An FPGA has *finite* gates — a complex strategy may not *fit*, and evolving logic iterates too slowly in hardware (§69.4.3). Don't try to put the whole system in fabric; put the *simple, hot, stable* core, and keep complexity/agility in software. Over-ambitious fabric scope is a common, costly mistake.
- **RTL vs HLS mismatch.** Using HLS (easier, higher latency) for the ultra-hot path where RTL's tighter latency is needed — or hand-writing RTL (hard, slow) for logic where HLS would suffice. Match the tool to the latency requirement: RTL for the critical pipeline, HLS for the rest/prototyping (§69.2).
- **Reconfiguration halting the system.** Reconfiguring the FPGA (new bitstream) halts it (§69.4.2) — doing this naively drops ticks. Use **partial reconfiguration** for live logic changes, and **register parameters** for live tuning (no reconfiguration) — design the changeable behavior as parameters (§69.4.2, ties Ch. 73).
- **Underestimating cost/specialization.** FPGA development needs specialized (scarce, expensive) hardware engineers, long timelines, and significant hardware cost. It's a *high-investment* tier — justified for the latency edge at the top, but not a casual optimization. Weigh the ROI (the latency/determinism win vs the cost).
- **Inadequate verification.** Hardware bugs are hard to fix (re-synthesis, possibly re-spin) and a bug in the fabric trading path is a financial risk (a wrong order at nanosecond speed). Verify rigorously (simulation, formal verification, hardware-in-the-loop testing — the hardware version of Ch. 40) — *especially* the risk/safety logic (a hardware kill-switch that fails is catastrophic — Ch. 74).
- **Lookaside FPGA expecting the inline win.** Using the FPGA as a *lookaside* PCIe coprocessor (CPU sends data, FPGA computes, returns — Ch. 66) and expecting nanosecond latency — that pays the PCIe round-trip (like a GPU — Ch. 67), so it's a *throughput* offload, not the latency win. The latency win requires *inline* placement (§69.2).
- **Not measuring the distribution.** The FPGA's value is *determinism* (flat tail — §69.3); measuring only the median misses the point. Measure the *distribution* (the constant-latency property is the edge — Ch. 1).

## 69.6 Exercises & checklist

**Exercises**

1. **Wire-to-wire + determinism.** (If you have an FPGA NIC) Measure wire-to-wire latency of an inline FPGA path (e.g. a simple feed-decode → echo) with hardware timestamps (Ch. 55, 58); compare the *distribution* to software kernel bypass (Ch. 62). Confirm the FPGA is ~10× lower *and* deterministic (p99 ≈ p50 — §69.3). The flat tail is the lesson.
2. **HLS vs RTL.** Implement a simple parser (Ch. 53) in HLS (C++) and (if able) RTL; compare latency, resource usage, and development effort. Quantify the HLS-productivity vs RTL-latency trade (§69.2).
3. **Fabric/software partition.** For a simple strategy, decide what goes in fabric (decode, fast risk, simple trigger) vs software (complex logic, full book, rare cases — §69.4.3); sketch the FPGA→software handoff and confirm the *fabric* path is self-contained for the common case (no per-packet host round-trip — §69.5).
4. **Parameters vs reconfiguration.** Design a circuit where risk limits are *register parameters* (live-updatable over PCIe, no reconfiguration) vs logic that needs re-synthesis. Demonstrate updating a parameter live (the FPGA hot-reload — §69.4.2, Ch. 73).
5. **Pre-trade risk in hardware.** Implement a pre-trade risk check (price/size limits — Ch. 27) as a hardware circuit; verify it (simulation) including edge cases. Treat it as a safety-critical kill-switch (Ch. 74) — what happens if it fails? (§69.5)

**Checklist — FPGA acceleration**

- [ ] The FPGA is placed **inline on the NIC / on the wire** (nanosecond wire-to-wire, no CPU/PCIe-round-trip) for the tick-to-trade path — **not lookaside** (which pays the PCIe round-trip — a throughput offload — §69.2, §69.5).
- [ ] The **fabric path is self-contained** for the common case (decode→risk→order — Ch. 53, 27) — **no per-packet FPGA→host round-trip** (§69.5); the host boundary is config/control/rare-exceptions only, off the hot path (§69.4.3).
- [ ] The **fabric/software partition** puts only the **simplest, hottest, most stable, determinism-benefiting** logic in fabric; complex/rare/evolving logic stays in software (§69.4.3).
- [ ] **RTL** is used for the ultra-hot critical pipeline (lowest latency); **HLS** for the rest/prototyping (the productivity-vs-latency trade — §69.2).
- [ ] Latency is measured **wire-to-wire with hardware timestamps** (Ch. 55, 58) and as a **distribution** — the **determinism (flat tail)** is the verified property (§69.3, Ch. 1).
- [ ] Live changes are **register parameters** (no reconfiguration) where possible; logic changes use **partial reconfiguration** (no halting the live system — §69.4.2, ties Ch. 73).
- [ ] The fabric logic — **especially pre-trade risk / kill-switch** (Ch. 74) — is **rigorously verified** (simulation/formal/hardware-in-the-loop — the hardware Ch. 40) and fails safe (§69.5).
- [ ] The **investment** (specialized engineers, hours-long toolchain, hardware cost) is justified by the **latency/determinism edge** — fabric scope is bounded to what fits and stays stable (§69.5).

## 69.7 References

- The AMD/Xilinx (Vivado/Vitis HLS) and Intel/Altera (Quartus) FPGA toolchain documentation — RTL/HLS development, synthesis, place-and-route, partial reconfiguration (§69.2, §69.4.2).
- The Exablaze/Cisco (ExaNIC FPGA), AMD/Solarflare, and Napatech FPGA-NIC documentation — NIC-integrated inline FPGA for trading (§69.4.1).
- Verilog/VHDL and HLS texts (e.g. Pong Chu's FPGA books, the Vitis HLS guide) — describing hardware (§69.2).
- HFT FPGA case studies and talks (low-latency trading on FPGA, inline feed handling/order entry) — the patterns of §69.4.
- Ch. 66 references (PCIe) — the host-FPGA boundary; Ch. 53, 55 (decoding/NIC) — what the inline FPGA does.

## 69.8 Additional Reading

- Talks on "FPGA in HFT" and wire-to-wire trading pipelines — real-world inline fabric trading systems.
- The partial-reconfiguration and FPGA-verification literature — live updates and rigorous hardware verification (§69.4.2, §69.5).
- Ch. 66 (*PCIe*) — the round-trip inline placement avoids; Ch. 53 (*Decoding*) — feed handling, now in fabric; Ch. 55/62 (*NIC/Bypass*) — the software path FPGA undercuts; Ch. 70 (*SmartNICs/DPUs*) — programmable NICs (some FPGA-based); Ch. 74 (*Process Topology*) — the kill-switch/safety in hardware; Ch. 73 (*Hot Reload*) — live parameter/PR updates; Ch. 1 (*Latency/Determinism*) — the flat-tail edge; Ch. 76 (*Case Study*) — wire-to-wire.
- **Appendix E** — wire-to-wire latency numbers (FPGA vs software bypass); **Appendix F** — FPGA/RTL/HLS/PR glossary.

---

*Next: Ch. 70 — SmartNICs & DPUs, the programmable hop between wire and host that generalizes the FPGA-NIC idea: NIC-resident ARM cores or FPGA fabric (NVIDIA BlueField, Intel IPU, AMD Pensando) that offload packet processing, filtering, and even feed handling/order entry — and when a DPU earns its place on the tick-to-trade path vs adding a hop.*
