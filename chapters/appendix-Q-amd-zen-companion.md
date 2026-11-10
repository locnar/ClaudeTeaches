# Appendix Q — AMD Zen (EPYC & Ryzen) Companion

> **Relates to:** Ch. 8 and Ch. 16 (caches, TLBs — the hierarchy that changes most), Ch. 12–14 (pipeline, op cache, branch prediction), Ch. 2 (the PMU — every `perf` event name in the book's Measure-it sections is Intel's), Ch. 18 (CCDs, the I/O die, NPS — the topology chapter, not repeated here), Ch. 6, 7 and 55 (firmware knobs, clocks, `amd_pstate`), Ch. 34 (`pdep`/`pext`), Ch. 35 (AVX-512 without a frequency licence), Ch. 36–38 (memory model and spin loops — what does *not* change, and the one thing that does), Ch. 53 (L3 QoS), Ch. 69 (no DDIO). The main chapters are calibrated to an Intel Ice Lake-SP reference machine (Appendix L.3); this appendix names exactly what changes on AMD Zen 3/4/5, in the same spirit as Appendix A does for ARM. Appendix R has the per-generation numbers.

The one-line summary: **Zen is x86-64 with the same memory model, the same instruction set (AVX-512 included from Zen 4) and a different uncore** — a chiplet topology with a private L3 per CCD (Ch. 18), no DDIO, no PEBS (IBS instead), no AVX frequency licence, a continuous boost controller rather than a turbo table, and an entirely different set of `perf` event names and firmware knobs. Everything in Parts III–VI transfers unchanged; Parts I–II's *measurements* and Part VII's *knobs* need this appendix.

## Q.1 Topology: a socket is CCDs around an I/O die (Ch. 17–18)

Ch. 18 develops it; the facts to carry: a **CCD** is 8 cores (16 with SMT) sharing a 32 MiB L3 (Zen 4c/5c "dense" CCDs: 16 cores on a 16 MiB L3 in Bergamo/Turin-dense), the **IOD** carries 12 DDR5 channels (8 DDR4 on Zen 3) and 128 PCIe lanes in four quadrants, **NPS1/2/4** chooses how many NUMA nodes the memory controllers are reported as, and **"ACPI SRAT L3 Cache as NUMA Domain"** reports every CCD as a node. Core-to-core: ~19 ns inside a CCD, ~85–100 ns across CCDs through the IOD, ~130–200 ns across sockets (xGMI). The V-Cache ("X3D" / Genoa-X / Milan-X) parts stack 64 MiB more L3 on a CCD — 96 MiB per CCD — at a few hundred MHz lower clock on that CCD. Read the map (Ch. 18 §18.3) before pinning anything; `lstopo` draws the L3 boxes correctly, `numactl --hardware` does not unless NPS4 or L3-as-NUMA is set.

## Q.2 Cache hierarchy and TLBs (Ch. 8, 16)

```
                     Zen 3 (Milan, 5000)     Zen 4 (Genoa, 7000)        Zen 5 (Turin, 9000)         Ice Lake-SP (reference)
   L1d               32 KiB 8-way, 4 cyc     32 KiB 8-way, 4 cyc        48 KiB 12-way, 4 cyc        48 KiB 12-way, 5 cyc
   L1i               32 KiB 8-way            32 KiB 8-way               32 KiB 8-way                32 KiB 8-way
   L2 (private)      512 KiB 8-way, ~12 cyc  1 MiB 8-way, ~14 cyc       1 MiB 16-way, ~14 cyc       1.25 MiB 20-way, ~14 cyc
   L3                32 MiB / CCD, 16-way,   32 MiB / CCD, 16-way,      32 MiB / CCD, 16-way,       1.5 MiB/core slice, 12-way,
                     non-inclusive VICTIM,   ~50 cyc (~14 ns)            ~47 cyc (~12 ns);           non-inclusive, shared across
                     ~46 cyc (~13 ns)        (+64 MiB V-Cache on -X)     V-Cache variants            the socket, ~61 cyc (~21 ns)
   L1 DTLB           64 entries (all sizes)  72 entries                 96 entries                  64 (4K) + 32 (2M) + 8 (1G)
   L2 TLB (STLB)     2048, 4K/2M; 1G in L2   3072, 4K/2M/1G             4096, 4K/2M/1G              2048 shared 4K/2M, 1024 1G
   line size         64 B (adjacent-line     64 B                       64 B                        64 B + spatial (adjacent-line)
                     prefetcher: NO pairing)                                                          prefetcher pairs 128 B (Ch. 39)
   HW prefetchers    L1 stream, L1 stride,   + L1 region, L2 up/down    same                        L1 IP-stride, L1 next-line,
                     L2 stream                                                                       L2 stream, L2 AMP, spatial
   DRAM (unloaded)   ~105 ns NPS1 / ~95 NPS4 ~110 NPS1 / ~95 NPS4        ~100 NPS1 (DDR5-6400)       ~94 ns (DDR4-3200, 8 ch)
```

What changes in practice. **The L3 is a victim cache of its CCD's L2s**: a line is installed in L3 only when evicted from an L2, so a streaming read that misses L2 goes to DRAM and does *not* leave a copy in L3 the way Intel's non-inclusive-but-fill-on-miss L3 does — Ch. 8's sweep shows a cliff at 32 MiB per CCD (96 on V-Cache) and *nothing* shared with the other CCDs. **There is no adjacent-line prefetcher pairing**: Ch. 39's 128-byte padding advice is Intel-specific; 64 bytes separates two writers on Zen (keep 128 if the binary must run on both). The **L2 doubled** in Zen 4 — Ch. 31's "fit the book in L2" budget is 1 MiB, not 1.25 — and the **L2 TLB covers 1 GiB pages** from Zen 4, so Ch. 16's huge-page argument is weaker for the TLB and unchanged for the page-walk cost. Prefetcher control is a firmware menu (Q.7), not an MSR you can flip at runtime.

## Q.3 The front end: op cache, decode, branch prediction (Ch. 13–14)

Zen's **op cache** (Intel: DSB) is larger — 4K µops (Zen 3), 6.75K (Zen 4), 6K with a two-ahead fetch (Zen 5) vs Ice Lake's 2.25K — and delivers 8 µops/cycle (Zen 4) to a 6-wide dispatch (8-wide on Zen 5), so Ch. 13's "stay in the µop cache" budget is roughly 3× larger per core; the legacy decoders are 4-wide (2 × 4 on Zen 5, serving two threads or two basic blocks). The **branch predictor** is a TAGE-class predictor with a two-level BTB (L1 ~1K–1.5K entries at zero bubbles, L2 ~6.5K–7K at one bubble on Zen 3/4; Zen 5 predicts two taken branches per cycle) and a 32-entry return stack; mispredict cost is ~13–19 cycles — Ch. 14's numbers hold within a few cycles. `perf`'s front-end attribution uses `ic_fetch_stall.*`, `de_dis_dispatch_token_stalls*` and `op_cache_hit_miss.*` rather than Intel's `FRONTEND_RETIRED`/`IDQ` events (Q.5).

## Q.4 The pipeline, the vector units and the integer tricks (Ch. 12, 34, 35)

```
                        Zen 3          Zen 4          Zen 5                 Ice Lake-SP (Sunny Cove)
   dispatch width       6              6              8                     5 (allocation)
   ROB                  256            320            448                   352
   integer ALUs / AGUs  4 / 3          4 / 3          6 / 4                 4 / 2 load + 2 store AGUs
   loads / stores per cycle  3 / 2 (shared 3 mem ops)  3 / 2        4 / 2             2 / 2 (3 loads from Alder Lake-P)
   FP/vector pipes      4 × 256-bit    4 × 256-bit (AVX-512 double-pumped)  4 × 512-bit (Turin/9950X; 256-bit on dense/mobile)  2 × 512-bit FMA (on 2-FMA SKUs)
   AVX-512              no             yes (same frequency: no licence)      yes, no licence         yes, licence levels 1/2 (Ch. 35)
   imul r64 / add / load-to-use   3 / 1 / 4 cycles      3 / 1 / 4        3 / 1 / 4            3 / 1 / 5
   pdep / pext          3 cycles (fast since Zen 3; Zen 1/2 were ~18 cycles microcoded — Ch. 34)                1 cycle
   pause                ~65 cycles (Zen 2+; Zen 1: ~10)                                          ~140 cycles (Skylake+), ~40 (Ice Lake-SP)
```

Three things to re-check from Part II on a Zen box: Ch. 12's dependency-chain table (the `imul` chain used by Ch. 7's probe is 3 cycles on every Zen — the probe is valid; load-to-use is 4, not 5, so an L1-resident chase runs ~20 % faster per hop at equal clock); Ch. 35's **downclocking section does not apply** (Zen has no AVX frequency licence — wide vectors cost power through the boost controller's PPT/EDC limits, not through a licence table, so a neighbour's AVX-512 lowers the hot core's clock only if the *package* is at its power limit); and Ch. 34's `pdep`/`pext` trap is **Zen 1/2 only** — Zen 3+ executes them in 3 cycles, so the bitmap tricks are usable. Ch. 38's spin loops: `pause` is ~65 cycles on Zen 2+ — shorter than Skylake's ~140, longer than Ice Lake's ~40; re-tune backoff constants per part.

## Q.5 The PMU, `perf` and IBS (Ch. 2, 17, 37, 39)

This is the section a reader on AMD hardware needs most: **every `perf` event name in the book's Measure-it sections is Intel's**. The generic names (`cycles`, `instructions`, `cache-misses`, `branch-misses`, `L1-dcache-load-misses`, `dTLB-load-misses`) work, mapped by the kernel; everything specific maps as follows (names from `perf list` and `tools/perf/pmu-events/arch/x86/amdzen{3,4,5}/`):

```
   what the chapter counts             Intel event (book)                              Zen 3/4/5 event
   ─────────────────────────────────── ─────────────────────────────────────────────── ──────────────────────────────────────────────────────
   retired instructions / branches     inst_retired.any, br_inst_retired.*             ex_ret_instr, ex_ret_brn, ex_ret_brn_misp, ex_ret_near_ret_mispred
   L1d misses / fills by source        mem_load_retired.l1_miss, l2_hit, l3_hit        ls_l1_d_tlb_miss.*, ls_dmnd_fills_from_sys.{local_l2,local_ccx,
   (Ch. 8, 16 §16.3, 17 §17.3)         mem_load_l3_miss_retired.{local,remote}_dram        near_cache,far_cache,dram_io_near,dram_io_far}  (Zen 4+ names)
   L2 behaviour                        l2_rqsts.*                                      l2_cache_req_stat.*, l2_pf_hit_l2, l2_pf_miss_l2_hit_l3
   L3 (per CCX uncore PMU)             uncore_cha_*/LLC_LOOKUP                         amd_l3/l3_lookup_state.l3_miss/, amd_l3/l3_xi_sampled_latency/
   DRAM / fabric traffic (Ch. 17, 69)  uncore_imc_*/cas_count_read                     amd_df/dram_channel_data_controller_N/ (bytes × 64), umc_* on Zen 4+
   front-end stalls (Ch. 13)           idq_uops_not_delivered, frontend_retired.*      ic_fetch_stall.{ic_stall_any,ic_stall_back_pressure,ic_stall_dq_empty},
                                                                                          op_cache_hit_miss.*, de_dis_dispatch_token_stalls1.*
   top-down (Ch. 2 §2.2.2)             perf stat --topdown (TMA levels 1–2)            Zen 3: NOT available; Zen 4/5: perf stat -M PipelineL1 / PipelineL2
                                                                                          (de_no_dispatch_per_slot.*, ex_no_retire.*, de_src_op_disp.*)
   precise sampling (Ch. 2 §2.4.4)     PEBS: -e mem_load_retired.l3_miss:pp            IBS: -e ibs_op//p (one tagged op in N; data address, cache level,
                                                                                          DRAM latency, HITM bit) and -e ibs_fetch// for the front end
   perf mem / perf c2c (Ch. 37, 39)    PEBS data-source                                 ibs_op: perf mem record -e ibs_op// … / perf c2c record (kernel ≥ 5.x
                                                                                          maps IBS data-source; HITM shown as "remote"/"local HITM" from the
                                                                                          IBS cache-level bits) — sample rates are lower; run longer
   APERF/MPERF for Ch. 55              rdmsr 0xE8/0xE7 (IPI)                            rdpru (user-space, no MSR, Zen 2+): ecx=0 → MPERF, 1 → APERF
   cycles vs ref-cycles                cycles, ref-cycles                               cycles, ref-cycles (via IRPERF/APERF); ls_not_halted_cyc
   fixed counters                      3 fixed + 8 programmable per thread               6 programmable per thread (no Intel-style fixed set), 6 per L3, 4+ per DF
```

Method consequences. **Top-down as the book uses it (Ch. 2 §2.2.2) does not exist on Zen 3**; on Zen 4/5 the `PipelineL1`/`PipelineL2` metric groups give a comparable four-way split (frontend-bound / bad speculation / backend-bound / retiring) from the dispatch-slot events — use them where the book says `--topdown`. **IBS replaces PEBS** everywhere a chapter says `:pp`: it is one tagged µop per *N* with the full data-side record, so `perf mem` and `perf c2c` work (Ch. 37/39's false-sharing hunts transfer), but the sampling is sparser and `perf c2c`'s HITM classification comes from IBS cache-level bits rather than a dedicated event — run longer and read the "remote/local HITM" columns with that in mind. The **uncore PMUs are per CCX (`amd_l3`) and per socket (`amd_df`)**, so an L3 counter must be read for the CCX the hot thread is on (`perf stat -e amd_l3/…/ -C <cpu>` picks the CCX by CPU). And **`rdpru`** gives Ch. 55's flat-clock monitor an IPI-free, root-free APERF/MPERF read from the hot thread itself — the method the Intel side lacks. For the GUI, **AMD uProf** (`AMDuProfCLI collect --config tbp` for time-based, `--config ibs` for IBS, `--config memory` for the bandwidth/latency views) is the VTune counterpart Ch. 2 §2.4.3 names.

## Q.6 Memory model, atomics and spin loops (Ch. 36–38): what does not change

Nothing in Part VI changes: Zen is **x86-TSO**, `lock`-prefixed RMWs are full barriers, `mfence`/`lfence`/`sfence` have the same semantics, and every listing in Ch. 36–43 is correct as written. Two numbers move: the **contended-line transfer** is CCD-topology-dependent (Ch. 18 — 19 ns inside, 85+ across, vs the 45–65 ns band of the monolithic reference machine), so an SPSC pair that straddles CCDs is 4× slower and a false-sharing bug 4× more expensive; and **`pause`** is ~65 cycles (Zen 2+), which changes Ch. 38's backoff arithmetic and the Ch. 44 poll-loop duty cycle. `lock cmpxchg16b` is single-instruction and fast on all Zen (Ch. 38's DWCAS note); `clwb`/`clflushopt` are present; `movdir64b` (Ch. 79's doorbell write) is Zen 4+.

## Q.7 Firmware knobs, clocks and power (Ch. 6, 7, 55; Appendix C.1)

The AMD names for Appendix C.1's rows, as EPYC/Ryzen firmware labels them (exact menu paths vary by board vendor):

```
   Appendix C.1 item                 AMD firmware knob(s)                                         Linux-side verification
   ───────────────────────────────── ──────────────────────────────────────────────────────────── ────────────────────────────────────────────
   deep C-states off                 Global C-state Control = Disabled (C1 only); Power Supply      cpuidle: only C1 listed (Ch. 55 cstates);
                                     Idle Control = Typical Current Idle; DF C-states = Disabled   /sys/…/cpuidle/state1 absent or disabled
   fixed / pinned frequency          Core Performance Boost = Disabled (Ch. 6 posture) or Enabled   amd_pstate status (active/guided/passive);
                                     + Determinism Control = Manual → Performance; cTDP/PPT at      cpufreq scaling_{min,max}_freq a point;
                                     the SKU maximum; Ryzen: PBO limits / Curve Optimizer / fixed  rdpru APERF/MPERF ratio flat (Ch. 55 §55.3)
                                     all-core ratio (Ch. 7 §7.4.2)
   uncore pinned (Ch. 55 §55.2.5)    APBDIS = 1 + Fixed SOC P-state = P0 (pins the Infinity Fabric  amd_df bandwidth/latency steady after idle;
                                     and memory clock at their maximum); DF C-states = Disabled     the first-L3-miss-after-idle test (Ch. 55)
   SMT                               SMT Control = Disabled / Enabled (Ch. 52)                     lscpu "Thread(s) per core"
   NUMA / clustering (Ch. 18)        NPS = 1/2/4; ACPI SRAT L3 Cache as NUMA Domain = Enabled        numactl --hardware node count; c2c map blocks
   prefetchers (Ch. 8)               L1 Stream HW Prefetcher, L1 Stride, L1 Region, L2 Stream,       (no MSR interface for most; re-run Ch. 8's
                                     L2 Up/Down — each Enable/Disable                                 sweep after a change)
   I/O placement                     Preferred IO = the NIC's PCIe bus (prioritises its traffic in   /sys/bus/pci/devices/…/numa_node; IRQ counts
                                     the IOD); IOMMU = Disabled or passthrough (Ch. 75); x2APIC on
   SMIs / RAS                        Platform First Error Handling (PFEH) — firmware-first MCEs       no architectural SMI counter (MSR 0x34 and
                                     raise SMIs (Ch. 6 §6.4.4); disable for OS-first handling         turbostat's SMI column are Intel-only): detect
                                                                                                      firmware stalls with hwlat / rtla hwnoise (Ch. 56)
```

The **boost controller** (Precision Boost 2) is continuous: with "Core Performance Boost" on and nothing pinned, the clock is a function of package power (PPT), current (TDC/EDC) and temperature, *not* of a core-count table — Ch. 7 §7.2.3. The deterministic posture is either boost off (Ch. 6 — the base clock, 2.5–3.5 GHz on EPYC) or, on Ryzen/Threadripper, a fixed all-core ratio that disables the controller; EPYC firmware's "Determinism = Performance" mode makes all parts of a SKU run the *same* (lower) clock curve rather than each chip's own silicon-quality-dependent one — it is a fleet-uniformity knob, not a jitter knob. `amd_pstate=active` (the EPP driver, kernel ≥ 6.3) is the Linux side; `performance` EPP pins the CPPC request to the highest perf level (Ch. 55 §55.4.3). C-states: AMD exposes C1 and one deep core state (ACPI C2 = CC6, with the firmware-reported exit latency that Ch. 55 §55.2.1 warns is optimistic), plus package C6; "Global C-state Control = Disabled" leaves C1 only.

## Q.8 Cache QoS (Ch. 53) and the I/O path (Ch. 69, 75)

AMD **Platform QoS** appears in `resctrl` like Intel RDT: L3 CAT with a 16-bit capacity bitmask (16 ways per CCX), per-CCX domains (`info/L3/` lists one domain per L3 — eight to twelve on an EPYC, one per CCD, which is Ch. 18 §18.4.4's point), **MBA** as a coarse per-CCX bandwidth throttle (Zen 2+; the units are different from Intel's percentages — read `info/MB/`), no L2 CAT, no CDP; Zen 4 adds **SMBA** (slow-memory bandwidth allocation for CXL) and **BMEC** (bandwidth monitoring event configuration). The monitoring side (`llc_occupancy`, `mbm_total_bytes`) exists from Zen 2.

There is **no DDIO** on AMD: a NIC's DMA writes go to DRAM (through the IOD's quadrant that owns the slot), and the first touch of the RX buffer by the consumer is a DRAM miss (~95–110 ns) rather than an L3 hit — Ch. 69's measurements do not transfer, and Ch. 18 §18.4.3's "consumer in the device's quadrant, NPS4" is the mitigation (the miss is at least local). Zen 4/5 EPYC supports **PCIe 5.0 × 128 lanes** per socket and CXL 1.1/2.0 memory expanders (which appear as CPU-less NUMA nodes); "Preferred IO" gives one device priority in the IOD arbitration — set it for the market-data NIC.

## Q.9 Re-running the book on a Zen box: what to expect

- **Ch. 8's cache sweep** shows cliffs at 32 KiB (L1), 1 MiB (L2; 512 KiB on Zen 3), 32 MiB (the CCD's L3; 96 MiB on V-Cache) and *no* further plateau — the other CCDs' L3 is not yours. The L3 plateau sits at ~12–14 ns, well under the reference machine's ~21.
- **Ch. 12's chain latencies**: `add` 1, `imul` 3, load-to-use 4, `div r64` ~13–19 (Zen 4+; ~45 on Zen 3 — the integer divider was reworked), FMA 4.
- **Ch. 18's map**: tight 8-core blocks at ~19 ns, a flat ~85–100 ns everywhere else on the socket.
- **Ch. 35's AVX-512 measurement**: the throughput gain without the downclocking row; Zen 4 executes 512-bit ops as two 256-bit halves (so the speed-up over AVX2 is in instruction count, masking and the EVEX encodings, not in width), Zen 5 at full width on the desktop and the Zen 5 (not 5c) EPYC parts.
- **Ch. 55's cstates table**: `acpi_idle` with `POLL`, `C1`, `C2` (CC6) — and the first-wake measurement, not the table, decides.
- **Appendix L.3's statement** gains a line: "Zen 4 EPYC 9354, 32 cores in 4 CCDs, NPS4, `amd_pstate=active`, boost off at 3.25 GHz; L3 as NUMA on" — and every number in a chapter re-run there is labelled with it.

## Q.10 References

- AMD, *Software Optimization Guide for the AMD Zen 3 / Zen 4 / Zen 5 Microarchitecture* (publications 56665, 57647 and the Zen 5 guide) — cache and TLB geometry, op cache, dispatch widths, instruction latencies, prefetcher descriptions, the `pause` latency.
- AMD, *Processor Programming Reference (PPR) for AMD Family 19h Models 01h/11h (Milan/Genoa) and Family 1Ah (Turin)* — the PMC event list (`PMCx…`), the L3 and DF PMU, IBS registers, `rdpru`, CPPC MSRs.
- AMD, *AMD EPYC™ 9004 Series Processors — BIOS & Workload Tuning Guide* and the *9005 Series Tuning Guides*; *HPC Tuning Guide for AMD EPYC 9004* — the firmware knob names in Q.7 (NPS, L3 as NUMA, Determinism, APBDIS, DF C-states, Preferred IO, prefetchers).
- AMD, *AMD uProf User Guide* — `AMDuProfCLI`, time-based and IBS profiling, the memory/bandwidth analysis modes.
- Linux kernel: `tools/perf/pmu-events/arch/x86/amdzen3|amdzen4|amdzen5/*.json` (the event and metric definitions, including the `PipelineL1`/`PipelineL2` groups), `Documentation/arch/x86/resctrl.rst` (AMD QoS, SMBA, BMEC), `Documentation/admin-guide/pm/amd-pstate.rst`.
- Agner Fog, *The microarchitecture of Intel, AMD and VIA CPUs* and *Instruction tables* (Zen 3/4/5 sections) — independent latency/throughput measurements, including `pdep`/`pext` and `pause`.

## Q.11 Additional Reading

- Chips and Cheese, the Zen 3, Zen 4, Zen 4c, Zen 5 and Genoa/Turin articles — measured cache latencies, core-to-core maps, op-cache and predictor behaviour per part.
- Ch. 18 (*Chiplets, Tiles & NUMA Within the Socket*) — the AMD topology chapter this appendix defers to; Ch. 7 (*Choosing & Clocking the Hot-Path CPU*) — Ryzen/Threadripper vs EPYC F-series for the hot box; Appendix R — the per-generation numbers side by side with Intel's.
- Appendix A (*ARM / Graviton*) — the template this appendix follows, and the other non-reference target; Appendix D.2 — `-march=znver3|znver4|znver5`.

---

*Next: Appendix R — x86 Generation Cheat Sheet: the reference machine's Ice Lake-SP set beside Sapphire, Emerald and Granite Rapids and beside Zen 3, 4 and 5 — cores, caches, channels, lanes, clocks, C-state exits and the Appendix E latency rows restated per generation — plus the on-die accelerators (AMX, DSA, IAA, QAT, DLB, AVX10, APX) and why they sit beside, not on, the tick-to-trade path.*
